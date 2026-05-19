"""CRUD-обёртки над ``resume_jobs``.

Эндпоинты и воркер ходят сюда, а не в ORM напрямую — чтобы все
переходы по статусам (``queued → running → done|failed``) и
идемпотентный ``enqueue`` жили в одном месте.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.resume_job import (
    ACTIVE_STATUSES,
    ResumeJob,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
)


async def find_active_for_user(
    db: AsyncSession, user_id: uuid.UUID,
) -> ResumeJob | None:
    """Активная задача (queued|running) на юзера, если есть."""
    res = await db.execute(
        select(ResumeJob).where(
            ResumeJob.user_id == user_id,
            ResumeJob.status.in_(ACTIVE_STATUSES),
        )
    )
    return res.scalar_one_or_none()


async def get_latest_for_user(
    db: AsyncSession, user_id: uuid.UUID,
) -> ResumeJob | None:
    """Последняя задача юзера (любого статуса) — для эндпоинта статуса.

    Если задач нет вовсе — None: фронт по этому показывает «не
    запускали».
    """
    res = await db.execute(
        select(ResumeJob)
        .where(ResumeJob.user_id == user_id)
        .order_by(ResumeJob.created_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def get_by_id(db: AsyncSession, job_id: uuid.UUID) -> ResumeJob | None:
    return await db.get(ResumeJob, job_id)


async def enqueue(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    hh_phone: str,
    resume_url: str | None = None,
) -> ResumeJob:
    """Идемпотентно поставить задачу в очередь.

    Если на юзера уже есть активная задача (`queued` или `running`)
    — возвращаем её, обновив только phone/url (cookies теперь живут
    в ``platform_credentials.encrypted_session`` и воркер достаёт их
    по user_id сам).

    Уникальный частичный индекс на (user_id) WHERE status in
    ('queued','running') гарантирует, что параллельные вызовы не
    создадут дубль даже если эта проверка race-condition'ит.
    """
    existing = await find_active_for_user(db, user_id)
    if existing is not None:
        existing.hh_phone = hh_phone
        if resume_url:
            existing.resume_url = resume_url
        await db.flush()
        return existing

    job = ResumeJob(
        user_id=user_id,
        status=STATUS_QUEUED,
        hh_phone=hh_phone,
        resume_url=resume_url,
    )
    db.add(job)
    await db.flush()
    return job


async def mark_running(db: AsyncSession, job_id: uuid.UUID) -> ResumeJob | None:
    job = await get_by_id(db, job_id)
    if job is None:
        return None
    job.status = STATUS_RUNNING
    job.attempts = (job.attempts or 0) + 1
    job.error = None
    await db.flush()
    return job


async def mark_done(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    resume_id: str | None,
    result: dict[str, Any] | None,
) -> ResumeJob | None:
    job = await get_by_id(db, job_id)
    if job is None:
        return None
    job.status = STATUS_DONE
    job.resume_id = resume_id
    job.result = result
    job.error = None
    await db.flush()
    return job


async def mark_failed(
    db: AsyncSession, job_id: uuid.UUID, error: str,
) -> ResumeJob | None:
    job = await get_by_id(db, job_id)
    if job is None:
        return None
    job.status = STATUS_FAILED
    # обрезаем — TEXT-колонка вроде безразмерная, но мусор в логи мы
    # уж точно тащить не хотим.
    job.error = (error or "")[:4096]
    await db.flush()
    return job
