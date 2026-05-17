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
from app.db.models.subscription import (
    PLAN_BASIC,
    PLAN_FREE,
    PLAN_PRO,
    Subscription,
)
from app.db.models.user import User
from app.db.session import session_scope
from app.queue.jobs import WORKER_NAME_WORK_AT_ALL


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


# ── Лайфсайкл — start/stop хуки для main.lifespan ────────────────────


_scheduler_task: asyncio.Task | None = None
_scheduler_stop: asyncio.Event | None = None


def start_scheduler() -> None:
    """Стартует шедулер в текущем event-loop'е uvicorn-процесса.

    Идемпотентен: если уже запущен — повторный вызов ничего не делает.
    """
    global _scheduler_task, _scheduler_stop
    if _scheduler_task is not None and not _scheduler_task.done():
        logger.info("scheduler already running, skip")
        return
    _scheduler_stop = asyncio.Event()
    _scheduler_task = asyncio.create_task(
        _scheduler_loop(_scheduler_stop),
        name="offerday-scheduler",
    )


async def stop_scheduler() -> None:
    """Аккуратно тушит шедулер: взводит ``stop_event``, ждёт цикл,
    проглатывает CancelledError."""
    global _scheduler_task, _scheduler_stop
    if _scheduler_task is None:
        return
    if _scheduler_stop is not None:
        _scheduler_stop.set()
    try:
        await asyncio.wait_for(_scheduler_task, timeout=5)
    except asyncio.TimeoutError:
        logger.warning("scheduler did not stop in 5s, cancelling")
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _scheduler_task = None
    _scheduler_stop = None


# ── Утилиты для тестов ───────────────────────────────────────────────


def is_running() -> bool:
    """True, если шедулер сейчас работает в этом процессе."""
    return _scheduler_task is not None and not _scheduler_task.done()


__all__: Iterable[str] = (
    "TICK_SECONDS",
    "PLAN_INTERVALS_MIN",
    "DEFAULT_PLAN",
    "start_scheduler",
    "stop_scheduler",
    "is_running",
)
