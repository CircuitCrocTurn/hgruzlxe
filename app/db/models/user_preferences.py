"""UserPreferences — настройки и состояние юзера, которые видны на
``/settings`` и ``/responses``.

Эти данные не относятся к "ядру" модели юзера (id / email / password),
поэтому живут в отдельной таблице с ``user_id`` как PK. Так удобнее
расширять без миграции на ``users``.

Если фичу позже захочется вынести в отдельный сервис (например,
preference store), таблица легко выделяется.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # ── Авто-отклики ────────────────────────────────────────────────────
    # Флажок "сейчас бот рассылает отклики" — управляется кнопкой
    # «Начать / Приостановить» на /responses.
    auto_apply_running: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # ── Профиль кандидата ───────────────────────────────────────────────
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Желаемая роль / фильтры поиска ──────────────────────────────────
    position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    salary: Mapped[str | None] = mapped_column(String(128), nullable=True)
    work_formats: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # Было ``nullable=False`` без server_default — каждое новое создание
    # ``UserPreferences`` без явного ``about_me=""`` падало
    # ``NotNullViolation``. Тип ``Mapped[str | None]`` уже подразумевает
    # «опционально», переключаем колонку, чтобы было консистентно.
    about_me: Mapped[str | None] = mapped_column(String(1023), nullable=True)
    stop_companies: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_words: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ссылка на внешний профиль (hh.ru / LinkedIn) — добавляется
    # пользователем в блоке "Резюме и профиль кандидата".
    profile_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # ── Язык / регион ───────────────────────────────────────────────────
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    timezone_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #TODO ── Безопасность ──────────────────────────────────────────────────
    two_factor_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    #TODO ── Уведомления ─────────────────────────────────────────────────────
    notif_email: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notif_push: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notif_messages: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notif_weekly: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Telegram — то же самое, просто параллельный канал. Когда юзер
    # привязал бот (telegram_chat_id != null), уведомления шлются
    # туда тоже.
    notif_telegram: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #TODO ── Сопроводительные ────────────────────────────────────────────────
    ai_cover_letter_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    #TODO ── Telegram link token ─────────────────────────────────────────────
    # Одноразовая строка для deep-link `t.me/yourbot?start=offerday_<token>`.
    # Регенерируется при «Скопировать» если её ещё нет.
    telegram_link_token: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #TODO ── OTP-флоу для подключения площадок ───────────────────────────────
    # Пока флоу запросом-кода подключения площадки полностью
    # эмулируется на бэке: на запрос «Подключить» мы складываем сюда
    # ожидаемый код, на «Подтвердить код» — сравниваем и удаляем.
    pending_platform_otps: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    user: Mapped["User"] = relationship("User", back_populates="preferences")
