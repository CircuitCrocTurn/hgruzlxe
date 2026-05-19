"""Subscription — заглушка под тарифы (free/basic/pro).

На MVP-этапе достаточно одной активной подписки на юзера, поэтому
`user_id` сделан `unique=True`. Если потом понадобится история
подписок/платежей — снимаем unique и заводим отдельную таблицу
`payments`.

Биллинг (YooKassa и т.п.) подключается отдельно — здесь только
состояние подписки. Поле `external_payment_id` оставлено для связи
с ID платежа в платёжке, когда дойдут руки.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Допустимые значения. Не enum'ом, чтобы было проще миграции делать —
# при необходимости поменять список достаточно правок в коде, без ALTER TYPE.
PLAN_FREE = "free"
PLAN_BASIC = "basic"
PLAN_PRO = "pro"

STATUS_TRIAL = "trial"
STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
STATUS_PAUSED = "paused"  # как обсуждали — пауза вместо отмены


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # free / basic / pro — см. константы выше.
    plan: Mapped[str] = mapped_column(String(32), default=PLAN_FREE, nullable=False)
    # trial / active / expired / cancelled / paused
    status: Mapped[str] = mapped_column(String(32), default=STATUS_TRIAL, nullable=False)

    # Когда подписка началась (для отчётов и расчёта продлений).
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    # Когда истекает. Null = бессрочная (free).
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # По умолчанию false — как обсуждали, чтобы не было неприятных
    # автосписаний после того как юзер нашёл работу.
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ID последнего платежа в платёжке (YooKassa и т.п.).
    # Полная история платежей — отдельная таблица в будущем.
    external_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )

    user: Mapped["User"] = relationship("User", back_populates="subscription")
