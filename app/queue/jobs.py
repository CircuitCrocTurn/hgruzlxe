"""Фоновые задачи (воркер-функции).

Парсинг резюме запускается inline через ``asyncio.create_task``
(см. ``app.queue.enqueue_resume_parse``). Авто-отклики — через
шедулер + ``enqueue_work_at_all_job``. Когда поднимем Redis +
arq — воркеры будут регистрироваться в ``WorkerSettings.functions`` и
вызываться ``arq``-ом.
"""
from __future__ import annotations

import asyncio
import logging
import re
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update

# ``func.lower`` — case-insensitive сравнение email/ФИО в Stage-2.
# Алиас, чтобы не путать с локальными переменными.
func_lower = func.lower

from app.config import TWOCAPTCHA_KEY
from app.db.models.job_run import (
    STATUS_FAILED as JOB_RUN_FAILED,
    STATUS_RUNNING as JOB_RUN_RUNNING,
    STATUS_SUCCESS as JOB_RUN_SUCCESS,
    JobRun,
)
from app.db.models.resume import Resume
from app.db.models.user import User
from app.db.models.vacancy_responses import VacancyResponse
from app.db.resumes import hydrate_user_prefs_from_resume, resume_to_db
from app.db.session import session_scope
from app.queue.context import WorkerContext, load_worker_context
from app.queue.locks import user_lock
from app.services.job_sites.dispatcher import get_client_class
from app.services.job_sites.hh import HHAuthError, HHClient

logger = logging.getLogger("offerday.workers.resume")
work_logger = logging.getLogger("offerday.workers.work_at_all")
identity_logger = logging.getLogger("offerday.workers.identity_check")


WORKER_NAME_WORK_AT_ALL = "work_at_all"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_RESUME_HASH_FROM_PATH_RE = re.compile(r"/resume/([^/?#]+)")
_RESUME_HASH_FROM_QUERY_RE = re.compile(r"[?&]hash=([^&]+)")


def _resume_id_from_url(url: str) -> str | None:
    """Достаёт hash резюме из ссылок вида:

        https://hh.ru/resume/<hash>
        https://hh.ru/resume_converter/resume.txt?hash=<hash>&type=txt

    Возвращает None, если URL не похож ни на один из них.
    """
    m = _RESUME_HASH_FROM_PATH_RE.search(url)
    if m:
        return m.group(1)
    m = _RESUME_HASH_FROM_QUERY_RE.search(url)
    if m:
        return m.group(1)
    return None


async def pull_and_save_resume_for_user(
    *,
    user_id: uuid.UUID,
    hh_phone: str,
    resume_url: str | None = None,
) -> dict[str, Any]:
    """Сходить на hh.ru, распарсить резюме, апсертнуть в БД.

    «Чистая» функция-«мясо» резюме-пайплайна. Поднимает ``HHClient``
    под per-user локом, парсит резюме (``client.get_resume``),
    сохраняет результат через :func:`resume_to_db` +
    :func:`hydrate_user_prefs_from_resume`, возвращает короткий summary.

    Используется в двух местах:

    * :func:`app.queue.enqueue_resume_parse` — inline-обёртка
      вокруг этого «мяса», добавляющая in-memory жизненный цикл.
    * :func:`app.queue.context.load_worker_context` — если на момент
      загрузки контекста у юзера ещё нет распарсенного резюме (поле
      ``Resume.full_txt`` пустое), синхронно дёргает эту функцию,
      чтобы наполнить БД. Это «дозалив» по требованию воркера.

    Возвращает dict:
        {
            "resume_id": "<hh-hash>",
            "user_name": "Ivan Petrov" | None,
            "title": "Senior Backend" | None,
            "full_name": "Иван Петров" | None,
            "full_txt": "плейн-текст резюме",
        }

    Возможные исключения:
        * :class:`HHAuthError` — cookies протухли, нужен новый OTP.
        * :class:`RuntimeError` — не удалось распарсить ``resume_url``
          или ``get_resume_id`` отдал ошибку.
        * сетевые / парсинговые ошибки — пробрасываются как есть.

    Вызывающий код сам решает, как реагировать на исключения. Для
    :func:`enqueue_resume_parse` это in-memory ``status='failed'``;
    для :func:`load_worker_context` — лог + пустая строка в
    ``resume_txt``.
    """
    #async with hh_user_lock(hh_phone):
    if True:
        async with HHClient(
            phone=hh_phone,
            user_id=user_id,
        ) as client:
            if not await client.is_authenticated():
                # Без свежего OTP ``HHClient`` сам не залогинится —
                # `login()` спросит код через CodeProvider, а у
                # фонового кода его взять негде. Падаем явно.
                raise HHAuthError("hh_session_expired")

            if resume_url:
                resume_id_str = _resume_id_from_url(resume_url)
                if not resume_id_str:
                    raise RuntimeError(
                        f"can't parse resume_id from url: {resume_url}"
                    )
                info: dict[str, Any] = {
                    "success": True,
                    "resume_id": resume_id_str,
                }
            else:
                info = await client.get_resume_id()
                if not info.get("success"):
                    raise RuntimeError(
                        f"get_resume_id failed: {info.get('error', 'unknown')}"
                    )

            resume_id_str = info["resume_id"]
            parsed = await client.get_resume(resume_id_str)

    # БД-операции — отдельный scope: коммитимся сразу после получения
    # данных, не держа hh-лок.
    async with session_scope() as db:
        row = await resume_to_db(
            db,
            user_id=user_id,
            hh_resume_id=resume_id_str,
            parsed=parsed,
        )
        await hydrate_user_prefs_from_resume(
            db, user_id=user_id, parsed=parsed,
        )
        # row.full_txt доступен здесь (внутри session_scope), снаружи
        # он отдетачится — поэтому вытаскиваем именно здесь.
        full_txt = row.full_txt or ""

    return {
        "resume_id": resume_id_str,
        "user_name": info.get("user_name"),
        "title": parsed.title,
        "full_name": parsed.full_name,
        "full_txt": full_txt,
    }


# ── Парсер загруженных файлов-резюме (PDF/DOCX/image/TXT) ─────────────
#
# Идёт через Anthropic Claude API (см. ``app.services.resume_parser``):
# юзер загружает файл через ``/api/settings/resume/upload``,
# мы спавним фоновую задачу, она парсит PDF/DOCX/TXT и
# записывает результат в ``Resume.parsed_data`` + ``parsed_at`` +
# ``full_txt``.
#
# Жизненный цикл задачи — без отдельной таблицы статусов:
# статус виден в самой строке ``resumes`` (``parsed_at`` set → done,
# NULL → либо в работе, либо упало; пользователь может
# попробовать заново через UI).


async def run_resume_doc_parse_job(
    *,
    resume_id: uuid.UUID,
    user_id: uuid.UUID,
    file_path_rel: str,
    mime_type: str | None = None,
    filename: str | None = None,
) -> None:
    """Фоновая задача: распарсить ``resumes.file_path_pdf`` через
    Claude API и записать результат в ``Resume.parsed_data``
    (+ ``parsed_at``, ``full_txt``).

    Если что-то пойдёт не так, ``parsed_at`` остаётся ``None`` — это
    UI-сигнал «парсинг не выполнен» (юзер увидит «парсится…» и
    сможет загрузить файл заново). Все детали ошибки — в логах.

    ``file_path_rel`` — путь относительно ``PROJECT_ROOT`` (так же
    мы храним его в ``Resume.file_path_pdf``).
    """
    # Локальный импорт, чтобы не тащить httpx в module-level импорты
    # очереди (модуль ``app.queue.jobs`` грузится на старте процесса
    # — а парсер с его tool-схемой нужен только при upload'е резюме).
    from app.config import PROJECT_ROOT
    from app.services.resume_parser import (
        ResumeParseError,
        parse_resume_file,
    )

    abs_path = PROJECT_ROOT / file_path_rel
    logger.info(
        "resume doc parse: started resume=%s user=%s file=%s mime=%s",
        resume_id, user_id, abs_path, mime_type,
    )

    try:
        parsed = await parse_resume_file(
            abs_path,
            mime_type=mime_type,
        )
    except ResumeParseError:
        logger.exception(
            "resume doc parse failed (parser): resume=%s user=%s",
            resume_id, user_id,
        )
        return
    except Exception:
        logger.exception(
            "resume doc parse failed (unexpected): resume=%s user=%s",
            resume_id, user_id,
        )
        return

    # Запись результата. Раньше здесь же был ``hydrate_user_prefs``
    # (дозаполнял имя / желаемую должность в ``user_preferences``).
    # Теперь всё читается прямо из ``resumes.parsed_data`` —
    # хелпер оставлен как no-op shim для обратной совместимости.
    try:
        async with session_scope() as db:
            row = (
                await db.execute(
                    select(Resume).where(Resume.id == resume_id)
                )
            ).scalar_one_or_none()
            if row is None:
                logger.warning(
                    "resume doc parse: resume row gone resume=%s",
                    resume_id,
                )
                return

            parsed_data = parsed.model_dump()
            row.parsed_data = parsed_data
            row.parsed_at = _utcnow()

            # Подсунем плоский full-text для cover-letter генератора и
            # WorkerContext — даже для PDF/DOCX, у которых исходного
            # plain-текста у нас нет. Собираем «обзорный дамп» из
            # ключевых полей.
            row.full_txt = _flatten_parsed_resume_text(parsed)

            await hydrate_user_prefs_from_resume(
                db, user_id=user_id, parsed=parsed,
            )
        logger.info(
            "resume doc parse: done resume=%s user=%s",
            resume_id, user_id,
        )
    except Exception:
        logger.exception(
            "resume doc parse: write failed resume=%s user=%s",
            resume_id, user_id,
        )


def _flatten_parsed_resume_text(parsed: Any) -> str:
    """Собрать компактный plain-text дамп распарсенного резюме.

    Используется как ``Resume.full_txt`` для cover-letter генератора
    и :class:`app.queue.context.WorkerContext` — те ожидают единый
    текстовый блок, а не структурированный JSON.
    """
    parts: list[str] = []
    if parsed.full_name:
        parts.append(parsed.full_name)
    if parsed.title:
        parts.append(parsed.title)
    if parsed.summary:
        parts.append(f"О себе: {parsed.summary}")
    if parsed.skills:
        parts.append("Навыки: " + ", ".join(parsed.skills))
    if parsed.total_experience:
        parts.append(f"Опыт: {parsed.total_experience}")
    for exp in (parsed.experience or [])[:10]:
        chunk = " — ".join(
            x for x in (exp.company, exp.position, exp.start) if x
        )
        if chunk:
            parts.append(chunk)
        if exp.description:
            parts.append(exp.description)
    for edu in (parsed.education or [])[:5]:
        chunk = " — ".join(
            x for x in (edu.institution, edu.faculty, edu.year) if x
        )
        if chunk:
            parts.append(chunk)
    return "\n".join(parts).strip()[:32_000]


# ── Периодический воркер: work_at_all ─────────────────────────────────
#
# Заглушка под будущий «полный обход»: проверка резюме, скрейпинг
# вакансий, отклики, AI cover letter (на pro). Вызывается шедулером
# (``app.queue.scheduler``), который каждую минуту смотрит, у кого
# из юзеров истёк интервал по тарифу.
#
# Сигнатура у `work_at_all` намеренно простая (только ``user_id``),
# чтобы её было легко зарегистрировать в arq в будущем:
#
#     class WorkerSettings:
#         functions = [run_work_at_all_job]
#
# Вся логика логирования запусков (``JobRun``) и обработки ошибок —
# в обёртке ``run_work_at_all_job``. Реальная функция-«мясо»
# (``work_at_all``) ничего о ``JobRun`` не знает и может работать
# отдельно (например, в тестах).

async def work_at_all_one_platform(ctx: WorkerContext, slug: str):
    cls = get_client_class(slug)
    if cls is None:
        work_logger.warning( "user=%s platform=%s: no client class registered", ctx.user_id, slug,)
    else:
        async with cls.open_for(ctx) as client:
            try:
                work_logger.info(f"ctx.user_id: {ctx.user_id}")
                result = await client.respond_to_vacancies(ctx)        
                work_logger.info("user=%s platform=%s: would use %s",
                ctx.user_id, slug, cls.__qualname__,
            )
            except NotImplementedError:
                pass


async def work_at_all(ctx: WorkerContext) -> dict[str, Any]:
    """Заглушка периодического обхода под пользователя.

    Принимает ``WorkerContext`` — снимок всего состояния юзера на
    момент старта задачи (тариф, настройки поиска, активные площадки,
    дневной лимит откликов). См. :mod:`app.queue.context`.

    В будущем здесь будет:
        1. Проверить актуальность резюме (если давно не обновлялось —
           перезапустить ``enqueue_resume_parse`` для этого юзера).
        2. Сходить за свежими вакансиями на ``ctx.active_platforms``,
           отфильтровать по ``ctx.prefs.position``/``stack``/
           ``stop_companies``/``stop_words``, поднять матчи через
           ``matcher``.
        3. Сделать отклики в рамках ``ctx.applications_per_day``
           (``application_sender``).
        4. Если ``ctx.prefs.ai_cover_letter_enabled`` и ``ctx.is_paid`` —
           сгенерить AI cover letter под каждую вакансию.

    Сейчас просто пишет в лог и возвращает короткую сводку — её
    положит ``run_work_at_all_job`` в ``JobRun.stats``.
    """
    work_logger.info(
        "work_at_all stub: user=%s plan=%s platforms=%s daily_limit=%s",
        ctx.user_id,
        ctx.subscription.plan,
        ctx.active_platforms,
        ctx.applications_per_day,
    )

    # ── Проход по площадкам через dispatcher ────────────────────────
    #
    # Сейчас — только лог + статистика «реализовано/нет реализации».
    # Когда у клиентов появится унифицированный метод поиска
    # вакансий, тут будет:
    #
    #     for slug in ctx.active_platforms:
    #         cls = get_client_class(slug)
    #         if cls is None:
    #             continue
    #         async with cls.from_context(ctx) as client:
    #             vacancies = await client.search(ctx.prefs)
    #             ...
    #
    # А пока — фиксируем по каждому slug'у, есть ли клиент.
    scraped_vacancies = 0
    cover_letters_generated = 0
        
    r = await asyncio.gather(*(work_at_all_one_platform(ctx, slug) for slug in ctx.active_platforms))
    print(r, '\n========')
    for i in r:
        print('\n\n!!!\n\n ', i)
                    

    # Имитируем небольшую работу, чтобы шедулер видел реалистичный
    # finished_at-started_at в JobRun.
    await asyncio.sleep(0)
    return {
        "stub": True,
        "user_id": str(ctx.user_id),
        "plan": ctx.subscription.plan,
        "active_platforms": ctx.active_platforms,
        "applications_per_day": ctx.applications_per_day,
        "scraped_vacancies": scraped_vacancies,
        "cover_letters_generated": cover_letters_generated,
    }


async def run_work_at_all_job(user_id: uuid.UUID) -> None:
    """Воркер-обёртка над :func:`work_at_all`.

    1. Загружает ``WorkerContext`` (один SQL — User + Subscription +
       Resume + active platforms). Если юзер удалён или
       ``is_active=False`` — выходим, JobRun не создаём.
    2. Создаёт строку ``job_runs(worker='work_at_all', status='running')``.
    3. Вызывает ``work_at_all(ctx)``.
    4. По результату обновляет строку в ``success`` / ``failed``.

    Эту функцию зовёт шедулер через ``asyncio.create_task``. Снаружи
    ей передаётся **только** ``user_id`` — это принципиально (чтобы
    при переезде на arq/Redis сериализация работала, и чтобы воркер
    видел всегда свежие данные, а не снимок на момент enqueue).
    Контекст загружается уже внутри.
    """
    work_logger.info("work_at_all job started (user=%s)", user_id)

    # 0. Подгружаем актуальный контекст. Делаем это ДО создания
    # JobRun — если юзер уже неактивен, лишних строк в job_runs не
    # будет. ``load_worker_context`` сам залогирует причину (not
    # found / inactive).
    ctx = await load_worker_context(user_id)
    if ctx is None:
        return

    # 1. Регистрируем запуск ДО работы. Если процесс упадёт — строка
    # останется со статусом 'running'; на следующем старте
    # ``reset_stuck_jobs`` пометит её ``failed``.
    async with session_scope() as db:
        run = JobRun(
            worker_name=WORKER_NAME_WORK_AT_ALL,
            user_id=user_id,
            status=JOB_RUN_RUNNING,
        )
        db.add(run)
        await db.flush()
        run_id = run.id

    try:
        stats = await work_at_all(ctx)
    except Exception as e:
        work_logger.exception("work_at_all job failed (user=%s)", user_id)
        try:
            async with session_scope() as db:
                await db.execute(
                    update(JobRun)
                    .where(JobRun.id == run_id)
                    .values(
                        status=JOB_RUN_FAILED,
                        finished_at=_utcnow(),
                        error_message=repr(e),
                    )
                )
        except Exception:
            work_logger.exception(
                "could not mark work_at_all run %s failed", run_id,
            )
        return

    # 3. Успех — пишем stats и finished_at.
    try:
        async with session_scope() as db:
            await db.execute(
                update(JobRun)
                .where(JobRun.id == run_id)
                .values(
                    status=JOB_RUN_SUCCESS,
                    finished_at=_utcnow(),
                    stats=stats,
                )
            )
    except Exception:
        work_logger.exception(
            "could not mark work_at_all run %s success", run_id,
        )
    work_logger.info("work_at_all job done (user=%s)", user_id)


# ── Stage-2: identity_check (детектор «хитрецов») ─────────────────────
#
# Запускается в конце ``enqueue_resume_parse._runner`` (после
# первичного парсинга резюме).
#
# Логика:
#   * Жёсткий матч: email AND (имя+фамилия) совпадают.
#   * При найденном кандидате: новый ``User.is_active = False``.
#   * Результаты проверки в БД не храним — только логи
#     (логгер ``offerday.workers.identity_check``).
#
# email читаем из ``Resume.parsed_data['email']`` (кладёт
# ``resume_parser``). Если real-email ещё нет (синтетика @hh.local
# или None), Stage-2 завершается no-op и оставляет юзера активным.


def _is_synthetic_email(email: str | None) -> bool:
    if not email:
        return True
    return email.lower().endswith("@hh.local")


def _norm_name(name: str | None) -> str:
    """Простая нормализация имени/фамилии для сравнения.

    Lower + strip + схлопывание пробелов. Намеренно НЕ делаем
    транслит/Левенштейн: матч строгий, иначе ложные блокировки.
    """
    if not name:
        return ""
    return " ".join(name.lower().split())


async def run_identity_check_job(*, user_id: uuid.UUID) -> None:
    """Stage-2 fraud-detection для конкретного юзера.

    Шаги:
        1. Загрузить ``User`` + ``Resume.parsed_data`` юзера.
        2. Достать email/ФИО из ``parsed_data``.
        3. Если real-email есть → найти других active-юзеров с
           таким же email, у которых в ``Resume.parsed_data.full_name``
           совпадает имя+фамилия (case-insensitive).
        4. Если кандидаты найдены — деактивировать текущего
           (``User.is_active = False``) и залогировать match.
        5. Иначе — залогировать no_match и (если ``User.email``
           пока синтетика) подменить её на реальный из резюме.
    """
    identity_logger.info("identity_check started (user=%s)", user_id)

    try:
        async with session_scope() as db:
            current_user = (
                await db.execute(select(User).where(User.id == user_id))
            ).scalar_one_or_none()
            if current_user is None:
                raise RuntimeError("user disappeared")

            resume_row = (
                await db.execute(
                    select(Resume).where(Resume.user_id == user_id)
                )
            ).scalar_one_or_none()

            parsed = (resume_row.parsed_data if resume_row else None) or {}
            parsed_email: str | None = parsed.get("email")
            parsed_full_name: str | None = parsed.get("full_name")

            our_first = ""
            our_last = ""
            if parsed_full_name:
                parts = parsed_full_name.split()
                if parts:
                    our_first = _norm_name(parts[0])
                if len(parts) > 1:
                    our_last = _norm_name(" ".join(parts[1:]))

            # Резолвим «реальный» email для матча. Приоритет:
            # email из резюме → user.email (уже промоутили в прошлом
            # прогоне). Если оба синтетика/пусто — оставляем no_match.
            user_email = current_user.email
            match_email: str | None = None
            for candidate in (parsed_email, user_email):
                if candidate and not _is_synthetic_email(candidate):
                    match_email = candidate
                    break

            if match_email is None:
                identity_logger.info(
                    "identity_check: no real email (parsed=%s, user=%s) — "
                    "no_match (user=%s)",
                    parsed_email, user_email, user_id,
                )
                return

            email_lc = match_email.lower()
            stmt = (
                select(User, Resume)
                .join(Resume, Resume.user_id == User.id, isouter=True)
                .where(
                    User.id != user_id,
                    User.is_active.is_(True),
                    func_lower(User.email) == email_lc,
                )
            )
            rows = (await db.execute(stmt)).all()

            matches: list[str] = []
            for cand_user, cand_resume in rows:
                cand_full = (
                    (cand_resume.parsed_data or {}).get("full_name")
                    if cand_resume is not None
                    else None
                )
                cand_first = ""
                cand_last = ""
                if cand_full:
                    cparts = cand_full.split()
                    if cparts:
                        cand_first = _norm_name(cparts[0])
                    if len(cparts) > 1:
                        cand_last = _norm_name(" ".join(cparts[1:]))
                if cand_first == our_first and cand_last == our_last:
                    matches.append(str(cand_user.id))

            if matches:
                identity_logger.warning(
                    "identity_check: MATCH user=%s candidates=%s "
                    "(email=%s, name=%s %s) -> blocking new user",
                    user_id, matches, match_email, our_first, our_last,
                )
                current_user.is_active = False
                await db.flush()
                return

            # Дублей нет → no_match. Если у текущего юзера email
            # ещё синтетика — заменим её на реальный из резюме.
            if _is_synthetic_email(current_user.email):
                conflict = (
                    await db.execute(
                        select(User.id).where(
                            User.id != user_id,
                            func_lower(User.email) == email_lc,
                        )
                    )
                ).first()
                if conflict is None:
                    identity_logger.info(
                        "identity_check: promoting User.email synthetic -> %s (user=%s)",
                        match_email, user_id,
                    )
                    current_user.email = match_email
                    await db.flush()
                else:
                    identity_logger.info(
                        "identity_check: email_promotion_skipped (conflict) (user=%s)",
                        user_id,
                    )

            identity_logger.info("identity_check: no_match (user=%s)", user_id)
    except Exception:
        identity_logger.exception(
            "identity_check failed (user=%s)", user_id,
        )
