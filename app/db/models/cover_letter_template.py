"""CoverLetterTemplate — шаблоны сопроводительных.

Юзер может завести несколько шаблонов (например, "стандарт", "для
стартапов", "для энтерпрайза", "EN") и пометить один как дефолтный.
Дефолтный используется, если в момент отклика не выбран другой.

В Pro-тарифе шаблон превращается в "промпт" — генератор сопроводительных
кладёт текст шаблона в LLM и просит адаптировать под конкретную
вакансию. В Basic — берёт буквальный текст с подстановкой переменных
типа `{vacancy_title}`, `{company}`, `{user_name}`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CoverLetterTemplate(Base):
    __tablename__ = "cover_letter_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Юзерское название шаблона.
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Сам текст. Может содержать плейсхолдеры: {vacancy_title}, {company},
    # {user_name}, {top_skills} — список разруливается в services/cover_letter/.
    template_text: Mapped[str] = mapped_column(Text, nullable=False)

    # ISO-639 код: "ru", "en". Нужен, чтобы матчер не подсовывал
    # русский шаблон на англоязычную вакансию.
    language: Mapped[str] = mapped_column(String(8), default="ru", nullable=False)

    # Дефолтный шаблон. Инвариант "ровно один is_default=True у юзера"
    # держим на уровне приложения, не БД (на стажёрской миграции делать
    # partial unique index — лишний оверкилл).
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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

    user: Mapped["User"] = relationship("User", back_populates="cover_letter_templates")
