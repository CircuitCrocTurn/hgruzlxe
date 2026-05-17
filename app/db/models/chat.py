"""Chat — диалог юзера с работодателем на внешней площадке (hh.ru, habr и т.п.).

Один Chat = один диалог по конкретной вакансии. Содержит мета-инфу
(название/компания/иконка), ссылку на исходный диалог в самой
площадке и список сообщений (см. :class:`Message`).

Источники данных:
* ``hh.ru`` — ``chatik.hh.ru/chatik/api/chats`` (см.
  :mod:`app.services.job_sites.hh.chats_sync`).
* остальные сервисы — добавлять отдельной интеграцией.

Сохраняем в БД все чаты, пришедшие из API площадки (включая
«вы написали, работодатель ещё не ответил» и DISCARD-чаты).
Фильтр «только актуальные» (работодатель ответил И не отказ)
применяется при рендере — см. ``is_chat_relevant`` в ``chats_sync``
и тоггл на фронте.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.message import Message
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Сервисы — те же ключи, что и в ``platform_credentials.platform`` и
# ``vacancy_responses.service``. Расширяй по мере добавления площадок.
SERVICE_HH = "hh"
SERVICE_HABR = "habr"


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (
        # Один и тот же чат на площадке = одна строка в БД (на юзера).
        UniqueConstraint(
            "user_id",
            "service",
            "external_id",
            name="uq_chats_user_service_external",
        ),
    )

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

    # Площадка, откуда диалог: ``hh`` / ``habr`` / ...
    service: Mapped[str] = mapped_column(String(32), nullable=False)

    # ID диалога в самой площадке. Для hh.ru — числовой id из
    # ``chats.items[].id`` (например, 5302879447). Храним строкой, чтобы
    # не зависеть от формата чужих api.
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Заголовок диалога (обычно — название вакансии).
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    # Подзаголовок: название компании. Опционально.
    subtitle: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # URL логотипа компании на площадке (для фронта). Опционально.
    icon_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Ссылка на сам диалог в исходной площадке
    # (например, ``https://hh.ru/chats/5302879447``).
    link: Mapped[str] = mapped_column(String(512), nullable=False)

    # Сколько непрочитанных сообщений по данным площадки.
    unread_messages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # Короткое превью последнего сообщения (для списка чатов).
    last_message_preview: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )

    # Когда было последнее активное действие (last_activity_time у hh.ru).
    # Используется для сортировки списка чатов.
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Статус процесса найма на площадке.
    # Для hh.ru — ``workflowTransition.applicantState`` последнего
    # сообщения: ``RESPONSE`` / ``INVITATION`` / ``DISCARD`` / ...
    # Используется в фильтре «работодатель ответил, не отказ».
    applicant_state: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    # Кто написал последнее сообщение — мы (True) или работодатель (False).
    # Нужно для фильтра «только актуальные» без N+1 по ``messages``:
    # релевантным считаем чат, где ``last_message_outgoing=False``
    # И ``applicant_state ∉ REJECTED``.
    last_message_outgoing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # ID последнего просмотренного юзером сообщения на стороне hh.ru
    # (``lastViewedByCurrentUserMessageId``). Используется при POST на
    # ``chatik.hh.ru/chatik/api/mark_read`` — там это поле обязательное.
    # Может быть ``None`` для свежих чатов, где ни одного сообщения
    # ещё не прочитали.
    last_viewed_message_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # ID соискателя в этом диалоге на стороне площадки.
    # Для hh.ru — ``currentParticipantId`` в payload'е ``/chatik/api/chats``,
    # он же подставляется как ``applicantId`` в запросе
    # ``GET /chatik/api/chat_data?chatId=…&applicantId=…``, который тянет
    # всю историю сообщений диалога.
    applicant_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

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

    # ── relationships ────────────────────────────────────────────────────
    user: Mapped["User"] = relationship("User", back_populates="chats")
    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="Message.sent_at",
    )
