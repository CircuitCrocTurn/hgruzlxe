"""Репозиторий для таблицы ``identity_checks``.

Stage-2-проверка хитрецов (см. ``app.queue.jobs.run_identity_check_job``)
дёргает эти функции, чтобы записать результат в БД. Сюда же ходит
будущий админский UI, когда ему понадобится «история проверок этого
юзера».
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.identity_check import (
    IdentityCheck,
    POLICY_STRICT_EMAIL_AND_NAME,
    STATUS_FAILED,
    STATUS_MATCHED,
    STATUS_NO_MATCH,
    STATUS_QUEUED,
    STATUS_RUNNING,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    policy: str = POLICY_STRICT_EMAIL_AND_NAME,
) -> IdentityCheck:
    """Заводит строку проверки в статусе ``queued``."""
    row = IdentityCheck(
        user_id=user_id,
        status=STATUS_QUEUED,
        policy=policy,
    )
    db.add(row)
    await db.flush()
    return row


async def mark_running(
    db: AsyncSession, check_id: uuid.UUID,
) -> None:
    row = (
        await db.execute(
            select(IdentityCheck).where(IdentityCheck.id == check_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.status = STATUS_RUNNING
    await db.flush()


async def mark_matched(
    db: AsyncSession,
    check_id: uuid.UUID,
    *,
    candidate_user_ids: list[str],
    details: dict | None = None,
) -> None:
    row = (
        await db.execute(
            select(IdentityCheck).where(IdentityCheck.id == check_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.status = STATUS_MATCHED
    row.candidate_user_ids = candidate_user_ids
    row.score = len(candidate_user_ids)
    row.details = details or {}
    row.decided_at = _utcnow()
    await db.flush()


async def mark_no_match(
    db: AsyncSession,
    check_id: uuid.UUID,
    *,
    details: dict | None = None,
) -> None:
    row = (
        await db.execute(
            select(IdentityCheck).where(IdentityCheck.id == check_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.status = STATUS_NO_MATCH
    row.candidate_user_ids = []
    row.score = 0
    row.details = details or {}
    row.decided_at = _utcnow()
    await db.flush()


async def mark_failed(
    db: AsyncSession,
    check_id: uuid.UUID,
    *,
    error: str,
) -> None:
    row = (
        await db.execute(
            select(IdentityCheck).where(IdentityCheck.id == check_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.status = STATUS_FAILED
    row.error = error
    row.decided_at = _utcnow()
    await db.flush()
