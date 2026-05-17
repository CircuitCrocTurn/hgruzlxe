"""ActionLog — структурированные бизнес-логи в БД.

ВАЖНО: это НЕ замена обычному логированию. Технические логи
(DEBUG/INFO/ERROR/трейсбэки) идут в stdout/файл через
core/logger.py — их смотришь через `docker logs` или Loki.

Сюда падают только бизнес-события для дешборда и истории:
* "application_sent"      — отклик отправлен
* "application_failed"    — отклик не отправлен
* "captcha_solved"        — капча решена
* "vacancy_filtered"      — вакансия отброшена фильтром
* "credentials_expired"   — сессия протухла
* "telegram_match_found"  — найдена tg-вакансия
* и т.п.

Поля `entity_type` + `entity_id` — полиморфная ссылка, без FK
(потому что entity может быть из разных таблиц). Это ОК для логов.

Индекс на `(user_id, created_at desc)` — основной запрос дешборда:
"последние N событий юзера X".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Уровни — для фильтрации в UI ("показать только ошибки").
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"

# Статусы исхода действия. Не путать с уровнем (level).
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


class ActionLog(Base):
    __tablename__ = "action_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Юзер, к которому относится событие. Nullable — потому что бывают
    # системные события (например, обход telegram-каналов воркером
    # для всех юзеров сразу).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Идентификатор действия — короткий snake_case код.
    # См. константы и список в docstring модуля.
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # info / warning / error — для фильтрации в UI.
    level: Mapped[str] = mapped_column(String(16), default=LEVEL_INFO, nullable=False)

    # ok / error / skipped — исход самого действия.
    status: Mapped[str] = mapped_column(String(16), default=STATUS_OK, nullable=False)

    # Полиморфная ссылка на сущность, к которой относится событие.
    # Без FK — типы разные: "vacancy", "application", "telegram_match", ...
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Человекочитаемое сообщение для UI ("Отклик отправлен на 'Backend
    # в Avito'"). Не для машинного парсинга — для глаз.
    message: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Структурированные детали: input/output, причины фильтрации,
    # время выполнения, response code и т.п.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
        index=True,
    )

    user: Mapped["User | None"] = relationship("User", back_populates="action_logs")

    __table_args__ = (
        # Главный индекс дешборда: последние события юзера.
        Index(
            "ix_action_logs_user_created",
            "user_id",
            "created_at",
        ),
        # Поиск всех логов по конкретной сущности (например, история
        # одного отклика).
        Index(
            "ix_action_logs_entity",
            "entity_type",
            "entity_id",
        ),
    )
