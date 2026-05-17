"""Resume — резюме юзера.

На юзера — одно активное резюме (см. UNIQUE по ``user_id``).
Используется матчером для оценки вакансий и генератором
сопроводительных.

``parsed_data`` — результат прогона PDF через LLM-парсер
(см. ``services/resume_parser/``). Это «source of truth» для всех
данных резюме (уровень, опыт, языки, проекты, образование, email,
ФИО, дата рождения, ``skills``). Структура внутри — pydantic
``app.schemas.resume.Resume``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Resume(Base):
    __tablename__ = "resumes"

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
        index=True,
    )

    # Полный распарсенный JSON: уровень, опыт, языки, проекты,
    # образование, скиллы (внутри ``parsed_data["skills"]``).
    # Структура — ``app.schemas.resume.Resume`` (pydantic).
    parsed_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Когда резюме было распарсено (None = ещё не парсили).
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    full_txt: Mapped[str | None] = mapped_column(String(32768))

    # Путь к файлу. Локально — на диске сервиса; в проде стоит положить
    # в S3-совместимое хранилище и складывать сюда object key.
    file_path_pdf: Mapped[str] = mapped_column(String(512), nullable=False)

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

    user: Mapped["User"] = relationship("User", back_populates="resumes")
