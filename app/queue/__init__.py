"""Абстракция очереди фоновых задач.

Парсинг резюме запускается inline через ``asyncio.create_task``.
Когда поднимем Redis + arq, тело :func:`enqueue_resume_parse`
поменяется на ``arq_pool.enqueue_job(...)`` — публичный интерфейс
останется тем же.

Почему НЕ ``BackgroundTasks`` от FastAPI:
    в версиях FastAPI 0.106-0.120 yield-cleanup зависимости
    (``get_db``) выполняется ПОСЛЕ background tasks. То есть к
    моменту старта воркера транзакция эндпоинта ещё не закоммичена,
    и воркер не увидит свежевставленный row. Чтобы не зависеть от
    внутренностей FastAPI, сами явно коммитим транзакцию и потом
    стартуем задачу через ``asyncio.create_task``.

Особенности текущей (in-process) реализации:

* Один воркер uvicorn'а — никакого кросс-процессного выполнения. Если
  ``--workers 4``, разные запросы юзера улетают в разные процессы;
  задача всё равно выполнится в том процессе, где пришёл
  ``/api/settings/resume/url``. Это нормально для текущего масштаба.
* Если процесс рестартанулся, незавершённая задача парсинга
  потеряется. Поллер ``/api/me/resume-job`` увидит ``null`` и не
  заблочит UI. Юзер сможет руками нажать «Сохранить изменения»
  ещё раз — спавн идемпотентен по ``user_id``.
* Для периодического воркера ``work_at_all`` подвисшие
  ``job_runs`` со статусом ``running`` чистим в
  :func:`reset_stuck_jobs` на старте процесса.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.job_run import (
    JobRun,
    STATUS_FAILED as JOB_RUN_FAILED,
    STATUS_RUNNING as JOB_RUN_RUNNING,
)
from app.db.session import session_scope
from app.queue.jobs import (
    WORKER_NAME_WORK_AT_ALL,
    pull_and_save_resume_for_user,
    run_identity_check_job,
    run_resume_doc_parse_job,
    run_work_at_all_job,
)

logger = logging.getLogger("offerday.queue")


# Держим strong-references на запущенные таски, чтобы их не съел GC.
# Без этого ``asyncio.create_task`` иногда теряет таск (см. python issue
# 88831 / docs.python: "Save a reference to the result of this function").
_TASKS: set[asyncio.Task] = set()


# ── Inline-резюме без очереди ────────────────────────────────────────
#
# Парсинг резюме (URL → resume_converter/resume.txt → ``resumes``)
# идёт прямо в текущем event-loop'е uvicorn'а через
# ``asyncio.create_task``. Отдельной таблицы-«заявки» больше нет —
# фронт всё равно поллит статус (``/api/me/resume-job``), для UI
# достаточно in-memory dict'а.

# Активный (ещё не завершённый) ``asyncio.Task`` парсинга на юзера.
# Нужен фронтовому поллеру (``/api/me/resume-job``), чтобы понимать,
# крутится ли что-то прямо сейчас.
_INFLIGHT_RESUME_TASKS: dict[uuid.UUID, asyncio.Task] = {}

# Последний ЗАВЕРШЁННЫЙ результат парсинга на юзера — для того же
# поллера: даже когда таск уже снят с inflight, можно ответить
# ``status="done"`` (или ``"failed"`` + error) пока юзер
# дополлит и обновит страницу. На рестарте процесса просто
# теряется — фронт получит ``null`` и трактует это как «всё ок».
_LAST_RESUME_RESULT: dict[uuid.UUID, dict] = {}


async def enqueue_resume_parse(
    db: AsyncSession | None = None,
    *,
    user_id: uuid.UUID,
    hh_phone: str,
    resume_url: str | None = None,
) -> asyncio.Task:
    """Запустить парсинг резюме **сразу**, через ``asyncio.create_task``.

    Идемпотентность: если для этого ``user_id`` сейчас уже крутится
    inline-парсинг, возвращаем существующий ``Task`` без повторного
    спавна.

    ``db`` (опц.): если передан, перед спавном таска вызываем
    ``await db.commit()``. Это нужно вызывающему коду, который только
    что вставил/обновил строку ``User`` / ``platform_credentials``
    в той же сессии: без явного коммита воркер в своём
    ``session_scope()`` мог бы не увидеть свежий row (особенно в
    ``/auth/verify-code``, где параллельно идёт миграция cookies).
    Обходимся одной строкой вместо отдельного bookkeeping'а в каждом
    вызывающем месте.
    """
    if db is not None:
        await db.commit()

    prev = _INFLIGHT_RESUME_TASKS.get(user_id)
    if prev is not None and not prev.done():
        logger.info(
            "enqueue_resume_parse: inline task for user=%s already running, "
            "returning existing task",
            user_id,
        )
        return prev

    async def _runner() -> dict:
        try:
            result = await pull_and_save_resume_for_user(
                user_id=user_id,
                hh_phone=hh_phone,
                resume_url=resume_url,
            )
            _LAST_RESUME_RESULT[user_id] = {
                "status": "done",
                "result": {
                    "user_name": result.get("user_name"),
                    "title": result.get("title"),
                    "full_name": result.get("full_name"),
                },
            }
            # Stage-2 fraud-check: fire-and-forget; пишет только
            # в логи (таблицы ``identity_checks`` больше нет).
            try:
                stage2 = asyncio.create_task(
                    run_identity_check_job(user_id=user_id),
                    name=f"identity-check-{user_id}",
                )
                _TASKS.add(stage2)
                stage2.add_done_callback(_TASKS.discard)
            except Exception:
                logger.exception(
                    "could not schedule identity check after inline parse "
                    "(user=%s)",
                    user_id,
                )
            logger.info(
                "enqueue_resume_parse: inline parse done (user=%s)",
                user_id,
            )
            return result
        except Exception as e:
            _LAST_RESUME_RESULT[user_id] = {
                "status": "failed",
                "error": repr(e),
            }
            logger.exception(
                "enqueue_resume_parse: inline parse failed (user=%s)",
                user_id,
            )
            raise

    task = asyncio.create_task(_runner(), name=f"resume-inline-{user_id}")
    _INFLIGHT_RESUME_TASKS[user_id] = task

    def _on_done(t: asyncio.Task) -> None:
        # Снимаем strong-reference только если это всё ещё наш таск
        # (а не свежий повторный спавн — теоретически возможно при
        # очень быстром повторном клике юзера).
        if _INFLIGHT_RESUME_TASKS.get(user_id) is t:
            _INFLIGHT_RESUME_TASKS.pop(user_id, None)

    task.add_done_callback(_on_done)
    return task


def get_resume_parse_status(user_id: uuid.UUID) -> dict | None:
    """Текущий статус inline-парсинга резюме для юзера.

    Возвращает:
        ``{"status": "running"}`` — таск ещё крутится;
        ``{"status": "done", "result": {...}}`` — успешно завершён,
            ``result`` содержит ``user_name``, ``title``, ``full_name``;
        ``{"status": "failed", "error": "<repr>"}`` — упал, см. error;
        ``None`` — для этого юзера в этом процессе ничего не
            запускалось (или процесс рестартанулся).
    """
    inflight = _INFLIGHT_RESUME_TASKS.get(user_id)
    if inflight is not None and not inflight.done():
        return {"status": "running"}
    return _LAST_RESUME_RESULT.get(user_id)


async def enqueue_work_at_all_job(*, user_id: uuid.UUID) -> None:
    """Кидает в очередь периодический ``work_at_all`` для пользователя.

    Используется шедулером (см. :mod:`app.queue.scheduler`). Эта
    функция НЕ создаёт строку-«заявку» в отдельной таблице: запись в
    ``job_runs`` создаёт сама воркер-обёртка ``run_work_at_all_job``
    сразу при старте. Идемпотентность обеспечивается шедулером (он
    смотрит ``has_running`` перед enqueue) и тем, что воркер дешёвый
    (см. заглушку).

    Когда переедем на arq:
        await arq_pool.enqueue_job("run_work_at_all_job", str(user_id))
    Сигнатура самой функции (``user_id``) уже совместима.
    """
    task = asyncio.create_task(
        run_work_at_all_job(user_id=user_id),
        name=f"work-at-all-{user_id}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def enqueue_resume_doc_parse_job(
    *,
    resume_id: uuid.UUID,
    user_id: uuid.UUID,
    file_path_rel: str,
    mime_type: str | None = None,
    filename: str | None = None,
) -> None:
    """Поставить парсинг загруженного резюме (PDF / image / DOCX / TXT)
    через Anthropic Claude — fire-and-forget.

    Статус парсинга смотрим прямо по полям самого ``resumes`` —
    ``parsed_at`` (timestamp) и ``parsed_data`` (JSONB): если
    ``parsed_at`` пуст, значит фоновая задача либо в работе,
    либо упала.

    Сигнатура воркера (``run_resume_doc_parse_job``) уже совместима
    с будущим arq — переезд на Redis-очередь не сломает интерфейс.
    """
    task = asyncio.create_task(
        run_resume_doc_parse_job(
            resume_id=resume_id,
            user_id=user_id,
            file_path_rel=file_path_rel,
            mime_type=mime_type,
            filename=filename,
        ),
        name=f"resume-doc-parse-{resume_id}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def reset_stuck_jobs() -> None:
    """Помечает все ``running`` ``job_runs`` как ``failed``.

    Вызывать на старте процесса (см. ``app.main.lifespan``). Текущий
    runner — in-process, поэтому пережить рестарт активная задача
    физически не может: помечаем потерянные.

    Чистит таблицу ``job_runs`` (периодический воркер ``work_at_all``).
    Парсинг резюме теперь inline — он своей таблицы больше не имеет.

    Когда переедем на ``arq`` (Redis), эта функция станет no-op
    (или вообще удалится) — там воркер сам подхватит задачу обратно
    из очереди.
    """
    async with session_scope() as db:
        result = await db.execute(
            update(JobRun)
            .where(JobRun.status == JOB_RUN_RUNNING)
            .values(
                status=JOB_RUN_FAILED,
                error_message="lost_on_restart",
            )
        )
        if result.rowcount:
            logger.warning(
                "reset_stuck_jobs: marked %s job_run(s) as failed (lost_on_restart)",
                result.rowcount,
            )
