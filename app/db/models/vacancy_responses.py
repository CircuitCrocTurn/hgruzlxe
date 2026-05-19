"""VacancyResponse — отклик юзера на конкретную вакансию.

Каждая строка — единичный auto-apply: матчер по описанию
вакансии решил, что подходит, и отправил отклик. Хранит снапшот
важных полей (название вакансии, компания, ссылка) на момент
отправки, чтобы UI «История откликов» оставался валидным даже
после удаления вакансии с площадки.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID, BOOLEAN
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VacancyResponse(Base):
    __tablename__ = "vacancy_responses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=False,
        nullable=False,
        index=True,
    )

    # Юзерское название резюме ("Backend senior", "Data engineer").
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    # Имя компании из вакансии (например, "Яндекс"). Опционально —
    # на ранних версиях воркера колонки не было, поэтому ``nullable``.
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Зарплата в том виде, в каком её отдала площадка
    # ("250 000 — 350 000 ₽", "от 200 000 ₽ за месяц", ...).
    salary: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ия сервиса - hh.ru, habr, ...
    service: Mapped[str] = mapped_column(String(32), nullable=False)

    link: Mapped[str] = mapped_column(String(128), nullable=False)

    # Расширение скачанного логотипа компании в ``app/static/logotips/``.
    # Файл лежит по пути ``app/static/logotips/<id>.<logo_ext>`` (id —
    # UUID этой строки). NULL значит «логотипа не нашли / не скачали» —
    # карточка тогда рендерит инициалы.
    logo_ext: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # Полный распарсенный JSON: уровень, опыт, языки, проекты, образование.
    # Структура — Profile-схема из services/resume_parser/.
    is_sent: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, default=False)

    error: Mapped[str] = mapped_column(String(2048), nullable=True)

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

    user: Mapped["User"] = relationship("User", back_populates="vacancy_responses")
