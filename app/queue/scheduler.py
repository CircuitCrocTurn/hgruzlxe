"""Периодический шедулер для воркера ``work_at_all``.

Тикает раз в ``TICK_SECONDS`` секунд (по умолчанию — 60). На каждом тике:

1. Берёт всех ``is_active=True`` юзеров вместе с их планом подписки
   (``free`` если подписки нет) и временем последнего запуска
   ``work_at_all`` (по ``job_runs``).
2. Для каждого юзера считает интервал по тарифу
   (см. :data:`PLAN_INTERVALS_MIN`). Если ``now - last_started_at >= interval``
   ИЛИ воркер ещё ни разу не запускался — кидает в очередь
   :func:`app.queue.enqueue_work_at_all_job`.
3. Если у юзера уже есть запись ``job_runs(worker='work_at_all',
   status='running')`` — пропускает (идемпотентность). Это защита от
   ситуации, когда тяжёлый ``work_at_all`` ещё не успел отработать,
   а уже наступил следующий тик.

Когда переедем на ``arq + Redis``, эту функцию можно заменить на
``cron_jobs`` в ``WorkerSettings`` или на полноценный планировщик
(arq не имеет per-user cron из коробки, но idempotent enqueue
из тика — рабочая схема и для arq).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.job_run import JobRun, STATUS_RUNNING as JOB_RUN_RUNNING
from app.db.models.platform_credential import (
    PlatformCredential,
    STATUS_ACTIVE as PC_STATUS_ACTIVE,
)
from app.db.models.subscription import (
    PLAN_BASIC,
    PLAN_FREE,
    PLAN_PRO,
    Subscription,
)
from app.db.models.user import User
from app.db.session import session_scope
from app.queue.jobs import WORKER_NAME_WORK_AT_ALL
from app.services.job_sites.registry import PLATFORM_HABR, PLATFORM_HH
from app.services.realtime import active_user_ids


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

logger = logging.getLogger("offerday.scheduler")


# ── Тюнинг ───────────────────────────────────────────────────────────
#
# Как часто шедулер просыпается. Юзер в задаче формулировал «каждую
# минуту», поэтому 60. Если интервал самой частой группы (pro = 5 мин)
# в разы больше тика — это нормально: тик просто чаще сверяется,
# enqueue не дублируется (см. has_running и idempotency в очереди).
TICK_SECONDS: int = 60

# Интервалы между запусками ``work_at_all`` для каждого тарифа.
# ``None`` означает «никогда не запускать автоматически» — на случай,
# если ``free`` решим выключить совсем.
PLAN_INTERVALS_MIN: dict[str, int | None] = {
    PLAN_FREE: 60,
    PLAN_BASIC: 15,
    PLAN_PRO: 1,
}

# Если юзер по какой-то причине без подписки — считаем как FREE.
# Обычно подписку создаёт регистрация (или биллинг), но строкой меньше
# в коде не помешает.
DEFAULT_PLAN: str = PLAN_FREE


# ── Тюнинг chats-sync ────────────────────────────────────────────────
#
# Как часто ходим на hh.ru / habr.com за свежими чатами для юзеров,
# у которых сейчас открыт WebSocket ``/ws/chats`` (т.е. реально
# смотрят на список). Не путать с ``TICK_SECONDS`` выше — тот про
# отклики, этот про входящие сообщения.
#
# 30с — компромисс: при типовом idle-таймауте чатовой страницы
# полминуты задержки для «появилось новое сообщение» незаметны,
# а нагрузка на чужие API остаётся посильной. Для юзеров не на
# ``/chats`` мы не ходим вообще — данные подтянутся при следующем
# заходе на страницу через ``POST /api/chats/sync`` на load.
CHATS_SYNC_TICK_SECONDS: int = 30

# Параллелизм обхода юзеров за один тик. Защита от «много кто открыл
# /chats, тик ушёл гонять 100 параллельных HHClient'ов и съел весь
# пул коннектов». Per-tick gather всё равно тушим целиком до
# следующего тика, так что эта семафора — только про пиковую
# одновременную нагрузку.
CHATS_SYNC_CONCURRENCY: int = 5


# ── Запрос на «кому пора» ────────────────────────────────────────────


async def _due_users(db: AsyncSession) -> list[tuple[uuid.UUID, str]]:
    """Возвращает список ``(user_id, plan)`` для юзеров, у которых
    наступило время следующего ``work_at_all``.

    Решение «пора/не пора» делается в Python (а не в SQL) — так проще
    править интервалы, не трогая запрос.
    """
    # Скоррелированные подзапросы — на каждого юзера достаём:
    #   * MAX(started_at) последнего запуска (любой статус) — это «когда
    #     мы в последний раз пытались»;
    #   * EXISTS — есть ли висящая 'running' запись (тогда не enqueue'им).
    last_started_at_subq = (
        select(func.max(JobRun.started_at))
        .where(
            JobRun.user_id == User.id,
            JobRun.worker_name == WORKER_NAME_WORK_AT_ALL,
        )
        .correlate(User)
        .scalar_subquery()
    )
    has_running_subq = (
        select(literal(1))
        .where(
            JobRun.user_id == User.id,
            JobRun.worker_name == WORKER_NAME_WORK_AT_ALL,
            JobRun.status == JOB_RUN_RUNNING,
        )
        .correlate(User)
        .exists()
    )

    # Авто-отклики — opt-in: юзер должен нажать «Начать»
    # на /responses, чтобы ``users.auto_apply_running`` стал ``True``.
    # По дефолту выключено (server_default='false'). Кнопка
    # «Приостановить» сбрасывает флаг обратно (см.
    # ``app/api/dashboard.py``: ``/api/jobs/start`` и ``/api/jobs/pause``).
    stmt = (
        select(
            User.id,
            func.coalesce(Subscription.plan, DEFAULT_PLAN).label("plan"),
            last_started_at_subq.label("last_started_at"),
            has_running_subq.label("has_running"),
        )
        .join(Subscription, Subscription.user_id == User.id, isouter=True)
        .where(
            User.is_active.is_(True),
            User.auto_apply_running.is_(True),
        )
    )
    rows = (await db.execute(stmt)).all()

    now = _utcnow()
    due: list[tuple[uuid.UUID, str]] = []
    for user_has_runningid, plan, last_started_at, has_running in rows:
        print(f"ROOOW: {user_has_runningid, plan, last_started_at, has_running}")
        if has_running:
            # Идемпотентность: предыдущий запуск ещё не закрылся.
            continue
        interval_min = PLAN_INTERVALS_MIN.get(plan)
        print(f"interval_min: {interval_min}")
        if interval_min is None:
            continue
        print(f"last_started_at: {last_started_at}")
        if last_started_at is None:
            # Никогда не запускались — пора прямо сейчас.
            due.append((user_has_runningid, plan))
            continue
        # last_started_at у нас timezone-aware (DateTime(timezone=True)),
        # поэтому сравниваем с timezone-aware now.
        if now - last_started_at >= timedelta(minutes=interval_min):
            due.append((user_has_runningid, plan))
    return due


# ── Сам цикл ─────────────────────────────────────────────────────────


async def _scheduler_tick() -> int:
    """Один проход шедулера. Возвращает количество отправленных в
    очередь задач — удобно для тестов и метрик."""
    # Импорт внутри функции — чтобы не было циклического импорта
    # (``app.queue.__init__`` импортирует scheduler, а scheduler — его).
    from app.queue import enqueue_work_at_all_job

    async with session_scope() as db:
        due = await _due_users(db)
        print("DUE USERS", due)
    enqueued = 0
    for user_id, plan in due:
        try:
            await enqueue_work_at_all_job(user_id=user_id)
            enqueued += 1
            logger.debug(
                "scheduler: enqueued work_at_all for user=%s plan=%s",
                user_id, plan,
            )
        except Exception:
            logger.exception(
                "scheduler: failed to enqueue work_at_all for user=%s",
                user_id,
            )
    if enqueued:
        logger.info("scheduler tick: enqueued %d work_at_all job(s)", enqueued)
    return enqueued


async def _scheduler_loop(stop_event: asyncio.Event) -> None:
    """Бесконечный цикл «тик → сон → тик». Останавливается, когда
    взведён ``stop_event`` (graceful shutdown)."""
    logger.info(
        "scheduler started: tick=%ds, intervals_min=%s",
        TICK_SECONDS, PLAN_INTERVALS_MIN,
    )
    try:
        while not stop_event.is_set():
            try:
                await _scheduler_tick()
            except Exception:
                # Никогда не даём исключению уронить цикл — иначе
                # шедулер «молча умрёт», и юзер ничего не заметит,
                # пока кто-нибудь не пожалуется, что отклики не идут.
                logger.exception("scheduler tick crashed")

            # Спим до следующего тика, но просыпаемся сразу,
            # как только взведён stop_event — чтобы shutdown был
            # моментальным, а не висел до конца минуты.
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=TICK_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("scheduler stopped")


# ── Периодический chats-sync для онлайн-юзеров ──────────────────────


async def _resolve_chats_sync_targets(
    db: AsyncSession, user_ids: list[str],
) -> list[dict]:
    """Достать для каждого WS-подписчика список активных площадок и
    нужные креды (``hh_phone`` / ``habr_email``).

    Возвращает список словарей вида:
        {"user_id": uuid.UUID, "platforms": set[str],
         "hh_phone": str | None, "habr_email": str | None}

    Юзеры без активных площадок отфильтровываются — для них всё равно
    нечего синкать.
    """
    if not user_ids:
        return []
    # Парсим id из str; невалидные просто пропускаем (теоретически
    # туда не должно прилетать — ChatHub кладёт уже ``str(uuid)``).
    parsed_ids: list[uuid.UUID] = []
    for raw in user_ids:
        try:
            parsed_ids.append(uuid.UUID(raw))
        except (ValueError, TypeError):
            continue
    if not parsed_ids:
        return []

    # Один запрос на всех — джойним юзеров с их активными credentials.
    rows = (
        await db.execute(
            select(
                User.id,
                User.phone_number,
                User.email,
                User.temp_email,
                PlatformCredential.platform,
            )
            .join(
                PlatformCredential,
                PlatformCredential.user_id == User.id,
            )
            .where(
                User.id.in_(parsed_ids),
                User.is_active.is_(True),
                PlatformCredential.status == PC_STATUS_ACTIVE,
            )
        )
    ).all()

    by_user: dict[uuid.UUID, dict] = {}
    for uid, phone, email, temp_email, platform in rows:
        info = by_user.setdefault(
            uid,
            {
                "user_id": uid,
                "platforms": set(),
                "hh_phone": phone,
                # _habr_email_for: основной email или temp_email.
                "habr_email": email or temp_email,
            },
        )
        info["platforms"].add(platform)
    return list(by_user.values())


async def _run_chats_sync_for_user(info: dict) -> None:
    """Дёрнуть chats_sync по всем активным площадкам одного юзера.

    Каждый sync уже сам делает ``notify_chat_change`` при изменениях —
    мы тут просто запускаем «движок данных». Ошибки логируем и едем
    дальше: один упавший юзер не должен валить тик.
    """
    # Локальный импорт — chats_sync тянет тяжёлые транзитивные
    # зависимости (httpx, площадочные клиенты). Не хотим, чтобы они
    # импортились на старте процесса вообще ради одного шедулера.
    from app.services.job_sites.habr.chats_sync import (
        sync_habr_chats_for_user,
    )
    from app.services.job_sites.hh.chats_sync import (
        sync_hh_chats_for_user,
    )

    user_id = info["user_id"]
    platforms = info["platforms"]

    tasks: list = []
    if PLATFORM_HH in platforms and info.get("hh_phone"):
        tasks.append(
            sync_hh_chats_for_user(
                user_id=user_id, hh_phone=info["hh_phone"],
            ),
        )
    if PLATFORM_HABR in platforms:
        tasks.append(
            sync_habr_chats_for_user(
                user_id=user_id, habr_email=info.get("habr_email") or "",
            ),
        )
    if not tasks:
        return

    # ``return_exceptions`` — habr-auth-error не должен валить hh-sync
    # и наоборот. ``HHAuthError`` логируем мягко, без стека: это
    # ожидаемый исход «сессия протухла», ``ensure_logged_in`` внутри
    # клиента уже пометил ``platform_credentials.status='expired'``.
    # Прочие исключения — настоящие сетевые/парсинг-ошибки.
    from app.services.job_sites.hh import HHAuthError

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, HHAuthError):
            logger.info(
                "chats_sync tick: user=%s hh-сессия expired, "
                "пропущено", user_id,
            )
        elif isinstance(res, Exception):
            logger.warning(
                "chats_sync tick: user=%s sync failed: %s: %s",
                user_id, type(res).__name__, res,
            )


async def _chats_sync_tick() -> int:
    """Один проход chats-sync шедулера.

    1. Берём из ``ChatHub`` список юзеров с живым ``/ws/chats``.
    2. Резолвим у них активные площадки и креды (один SQL-запрос).
    3. Параллельно (но с лимитом ``CHATS_SYNC_CONCURRENCY``) гоняем
       chats_sync по каждой площадке каждого юзера.

    Возвращает число обработанных юзеров — для тестов / метрик.
    """
    user_ids = active_user_ids()
    if not user_ids:
        # Никто не онлайн — ходить за свежими данными бессмысленно,
        # снапшот всё равно некому пушить. Просто пропускаем тик.
        return 0

    async with session_scope() as db:
        targets = await _resolve_chats_sync_targets(db, user_ids)
    if not targets:
        return 0

    sem = asyncio.Semaphore(CHATS_SYNC_CONCURRENCY)

    async def _bounded(info: dict) -> None:
        async with sem:
            try:
                await _run_chats_sync_for_user(info)
            except Exception:
                logger.exception(
                    "chats_sync tick crashed for user=%s",
                    info.get("user_id"),
                )

    await asyncio.gather(*(_bounded(info) for info in targets))
    logger.debug(
        "chats_sync tick: processed %d user(s) (online=%d)",
        len(targets), len(user_ids),
    )
    return len(targets)


async def _chats_sync_loop(stop_event: asyncio.Event) -> None:
    """Бесконечный цикл chats-sync шедулера.

    Семантика та же, что у :func:`_scheduler_loop`: ловим исключения,
    спим до следующего тика, но мгновенно просыпаемся на ``stop_event``.
    """
    logger.info(
        "chats_sync scheduler started: tick=%ds, concurrency=%d",
        CHATS_SYNC_TICK_SECONDS, CHATS_SYNC_CONCURRENCY,
    )
    try:
        while not stop_event.is_set():
            try:
                await _chats_sync_tick()
            except Exception:
                logger.exception("chats_sync tick crashed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=CHATS_SYNC_TICK_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("chats_sync scheduler stopped")


# ── Лайфсайкл — start/stop хуки для main.lifespan ────────────────────


_scheduler_task: asyncio.Task | None = None
_chats_sync_task: asyncio.Task | None = None
_scheduler_stop: asyncio.Event | None = None


def start_scheduler() -> None:
    """Стартует оба шедулера (work_at_all + chats_sync) в текущем
    event-loop'е uvicorn-процесса.

    Идемпотентен: если оба уже запущены — повторный вызов ничего не
    делает. Если запущен только один (теоретически — после частичного
    краша), второй стартуется.
    """
    global _scheduler_task, _chats_sync_task, _scheduler_stop

    work_running = _scheduler_task is not None and not _scheduler_task.done()
    chats_running = (
        _chats_sync_task is not None and not _chats_sync_task.done()
    )
    if work_running and chats_running:
        logger.info("scheduler already running, skip")
        return

    # Один общий stop_event на оба цикла — на shutdown будятся вместе.
    if _scheduler_stop is None or _scheduler_stop.is_set():
        _scheduler_stop = asyncio.Event()

    if not work_running:
        _scheduler_task = asyncio.create_task(
            _scheduler_loop(_scheduler_stop),
            name="offerday-scheduler",
        )
    if not chats_running:
        _chats_sync_task = asyncio.create_task(
            _chats_sync_loop(_scheduler_stop),
            name="offerday-chats-sync",
        )


async def stop_scheduler() -> None:
    """Аккуратно тушит оба шедулера: взводит общий ``stop_event``,
    ждёт оба цикла, проглатывает CancelledError."""
    global _scheduler_task, _chats_sync_task, _scheduler_stop

    if _scheduler_stop is not None:
        _scheduler_stop.set()

    tasks = [t for t in (_scheduler_task, _chats_sync_task) if t is not None]
    for task in tasks:
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning(
                "scheduler task %s did not stop in 5s, cancelling",
                task.get_name(),
            )
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    _scheduler_task = None
    _chats_sync_task = None
    _scheduler_stop = None


# ── Утилиты для тестов ───────────────────────────────────────────────


def is_running() -> bool:
    """True, если **оба** шедулера сейчас работают в этом процессе.
    На half-up состояние (один из двух тасков упал) тоже вернёт False
    — это маркер «надо вызвать start_scheduler() ещё раз».
    """
    work_alive = _scheduler_task is not None and not _scheduler_task.done()
    chats_alive = (
        _chats_sync_task is not None and not _chats_sync_task.done()
    )
    return work_alive and chats_alive


__all__: Iterable[str] = (
    "CHATS_SYNC_CONCURRENCY",
    "CHATS_SYNC_TICK_SECONDS",
    "DEFAULT_PLAN",
    "PLAN_INTERVALS_MIN",
    "TICK_SECONDS",
    "is_running",
    "start_scheduler",
    "stop_scheduler",
)
