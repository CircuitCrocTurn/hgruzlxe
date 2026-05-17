"""Message — одно сообщение в чате (см. :class:`Chat`).

``author`` — кто написал сообщение:
    * ``"me"``     — текущий юзер (мы);
    * ``"them"``   — собеседник (рекрутёр / бот компании);
    * ``"system"`` — служебное сообщение площадки (без живого
      отправителя — например, «отклик отправлен», «вакансия снята»).

``sent_at`` — время создания сообщения по данным площадки (``creationTime``
в hh.ru API). ``created_at`` — когда мы записали строку у себя.

``external_id`` — id сообщения в самой площадке. Нужен, чтобы при
повторной синхронизации не плодить дубли (UNIQUE per chat + external_id).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.chat import Chat


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Допустимые значения ``Message.author``.
AUTHOR_ME = "me"
AUTHOR_THEM = "them"
AUTHOR_SYSTEM = "system"


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Один и тот же external_id (id сообщения на площадке) — одна
        # строка в БД. NULL'ам это правило не мешает: при ручном вставлении
        # без external_id оно остаётся пустым и UNIQUE не срабатывает.
        UniqueConstraint(
            "chat_id",
            "external_id",
            name="uq_messages_chat_external",
        ),
        Index("ix_messages_chat_sent_at", "chat_id", "sent_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chats.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ID сообщения на самой площадке (например, ``lastMessage.id`` у hh.ru).
    # Опционально (NULL) — позволяет вручную добавлять локально созданные
    # сообщения (черновики и т.п.) без коллизий.
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Содержание сообщения. Text, потому что письма работодателей бывают
    # длинные (тестовые задания, регламенты собеседования и т.п.).
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Кто написал — ``me`` / ``them`` / ``system``. См. константы выше.
    author: Mapped[str] = mapped_column(String(8), nullable=False)

    # Имя отправителя как мы хотим показать на UI ("Екатерина",
    # "HR Иван", "Бот компании"). Может быть NULL для system-сообщений.
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Прочитано ли сообщение текущим юзером (соответствует
    # ``unread_messages`` на стороне ``Chat``).
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Когда сообщение появилось в площадке (``creationTime`` у hh.ru).
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Когда мы записали строку у себя.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )

    # ── relationships ────────────────────────────────────────────────────
    chat: Mapped["Chat"] = relationship("Chat", back_populates="messages")
