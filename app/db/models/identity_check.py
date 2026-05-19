"""IdentityCheck — лог Stage-2-проверок «не хитрец ли это».

Stage-2 запускается после того, как воркер :func:`run_resume_job`
успешно распарсил резюме НОВОГО юзера (т.е. зарегистрировавшегося
под новым телефоном). Также запускается, когда юзер вручную
обновляет резюме через эндпоинт настроек.

Что ищем: существующих юзеров, у которых совпадает email **И** ФИО
(жёсткий матч). Если совпадение найдено — это попытка повторной
регистрации с новым номером, нового юзера блокируем
(``is_active=False``).

Зачем нужна таблица — у Stage-2 нет UI; результат проверки нужен
только для аудита и дебага. Сюда пишем:
    * на кого проверяли (``user_id``),
    * каких кандидатов нашли (``candidate_user_ids``),
    * по какой политике (``policy``: на будущее — может быть
      несколько вариантов матчинга),
    * статус и время решения.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Статусы — расширяемый набор; держим как строку, чтобы не мучиться
# с ALTER TYPE при появлении новых.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_MATCHED = "matched"          # найден дубль → блокируем
STATUS_NO_MATCH = "no_match"        # дублей нет → всё ок
STATUS_FAILED = "failed"            # упало с исключением

# Какой алгоритм матчинга использовался. Сейчас один —
# strict_email_and_name (email AND имя+фамилия). Если потом добавим
# мягкие/скоринговые варианты, поле различает их в логах.
POLICY_STRICT_EMAIL_AND_NAME = "strict_email_and_name"


class IdentityCheck(Base):
    __tablename__ = "identity_checks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Юзер, который проходит проверку. ondelete=CASCADE — если юзера
    # удалят, лог его проверок удалять не жалко: это диагностические
    # данные, а не аудит-trail в смысле compliance.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(16), default=STATUS_QUEUED, nullable=False, index=True,
    )

    # Кандидаты, которых матчер счёл «тем же человеком». Список UUID
    # как строки. JSONB, потому что обычно 0–1 элемент, но
    # раздельную таблицу делать смысла нет — это диагностика.
    candidate_user_ids: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True,
    )

    # Скоринговый балл (если когда-нибудь добавим мягкий матч).
    # Сейчас strict-policy кладёт сюда количество кандидатов.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    policy: Mapped[str] = mapped_column(
        String(32),
        default=POLICY_STRICT_EMAIL_AND_NAME,
        nullable=False,
    )

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Произвольная дополнительная инфа (исходные значения email/ФИО,
    # которые сравнивали; кто именно из кандидатов матчился).
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user: Mapped["User"] = relationship("User", back_populates=None)

    __table_args__ = (
        Index("ix_identity_checks_user_id", "user_id"),
        Index("ix_identity_checks_status", "status"),
    )
