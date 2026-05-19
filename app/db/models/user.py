"""User — пользователь дешборда.

Multi-user SaaS, каждый юзер имеет свои настройки, учётки на площадках,
резюме, подписку и логи действий.

`telegram_chat_id` — это chat_id юзера с НАШИМ ботом (а не tg user id).
Получаем его, когда юзер делает /start с deep-link
`t.me/yourbot?start=<binding_token>`. До этого момента уведомления
показываются только в дешборде.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.action_log import ActionLog
    from app.db.models.chat import Chat
    from app.db.models.job_run import JobRun
    from app.db.models.platform_credential import PlatformCredential
    from app.db.models.resume import Resume
    from app.db.models.subscription import Subscription
    from app.db.models.vacancy_responses import VacancyResponse 
    


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Основной идентификатор юзера в нашей системе. По нему verify-code
    # определяет «новый или вернувшийся юзер»: есть в БД → returning,
    # нет → создаём + спавним парсинг резюме. Формат — E.164 (+7XXXXXXXXXX).
    #
    # nullable=True умышленно: так миграция накатывается на любую
    # существующую базу (пустую или нет), а в коде мы заполняем поле
    # сразу при создании User. Можно при следующем релизе ALTER в
    # NOT NULL, когда убедились, что у всех живых юзеров оно
    # заполнено (и, при желании, добавить CHECK).
    phone_number: Mapped[str | None] = mapped_column(
        String(32),
        unique=True,
        nullable=True,
        index=True,
    )
    email: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
    )
    # Временный email из сервиса temp.coda.ink, выдаётся при
    # первом успешном /auth/verify-code (см. ``create_user_for_phone``
    # → ``app.services.temp_mail.create_temp_email``). Используется как
    # «технический» адрес для регистрации аккаунтов на job-площадках
    # (habr и т.д.), пока юзер ещё не подтвердил основной email или
    # пока площадка не позволила сменить email на основной. В UI
    # /settings отображается в блоке «Доступы для ручного входа».
    #
    # nullable=True специально, чтобы миграция спокойно накатывалась
    # на существующую базу (для старых юзеров заполнится лениво при
    # первом заходе на /settings или при подключении площадки).
    temp_email: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
    )
    # Per-address Bearer-токен от temp.coda.ink (формат ``tm_at_…``).
    # Возвращается при создании ящика ``POST /v1/address`` и единственный
    # способ читать inbox этого ящика — даже владелец общего API-ключа
    # не может прочесть чужой ящик без его персонального токена
    # (проверено эмпирически: API возвращает 403 «Address token required»).
    # Поэтому токен нужно ОБЯЗАТЕЛЬНО сохранить рядом с ``temp_email`` —
    # иначе мы потеряем доступ к ящику навсегда (он не «восстанавливается»).
    # Используется в ``app.services.temp_mail.fetch_inbox`` /
    # ``extract_code_from_inbox`` для опроса писем во время habr-флоу.
    temp_email_token: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    # «Платформенный» пароль — детерминированно сгенерирован из
    # ``users.id`` + ``PLATFORM_PASSWORD_SALT`` через SHA-256
    # (см. ``app.services.platform_password.generate_platform_password``).
    # Заполняется ОДИН РАЗ при создании юзера в ``create_user_for_phone``
    # и больше не меняется — соль глобальная и неподвижная (как
    # договорились: один раз выбрана и не ротируется). Хранится как
    # plain-текст специально: значение нужно показывать пользователю в
    # /settings → «Доступы для ручного входа», и тем же паролем мы
    # регистрируем его аккаунты на job-площадках (habr и др.).
    #
    # nullable=True ради бесшовной миграции на существующую БД; для
    # старых юзеров поле дозаполнится лениво при первом заходе на
    # /settings (см. ``app.db.users.ensure_platform_credentials``).
    platform_password: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=True)

    # chat_id юзера в нашем @BotFather-боте (не tg user id).
    # Появляется после /start с deep-link.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── «plain»-флаги, переехавшие из таблицы user_preferences ──
    # 1) auto_apply_running — opt-in автоотклики. Шедулер берёт
    #    этот флаг в WHERE-селекте (см. ``app.queue.scheduler``).
    # 2) notif_email / notif_telegram — тогглы в /settings на выдачу
    #    уведомлений по соответствующим каналам. По дефолту оба
    #    выключены.
    # 3) auto_up_resume — будущая фича «авто-поднятия» резюме.
    #    Никто пока не читает, но колонка зарезервирована.
    auto_apply_running: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    notif_email: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    notif_telegram: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
    )
    auto_up_resume: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False,
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

    # ── relationships (для уже созданных моделей) ────────────────────────
    subscription: Mapped["Subscription | None"] = relationship(
        "Subscription",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    credentials: Mapped[list["PlatformCredential"]] = relationship(
        "PlatformCredential",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    resumes: Mapped["Resume"] = relationship(
        "Resume",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    action_logs: Mapped[list["ActionLog"]] = relationship(
        "ActionLog",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    job_runs: Mapped[list["JobRun"]] = relationship(
        "JobRun",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    vacancy_responses: Mapped[list["VacancyResponse"]] = relationship(
        "VacancyResponse",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    chats: Mapped[list["Chat"]] = relationship(
        "Chat",
        back_populates="user",
        cascade="all, delete-orphan",
    )


    # ── relationships (добавишь, когда появятся модели) ──────────────────
    # vacancy_matches:    relationship("VacancyMatch",    back_populates="user", cascade="all, delete-orphan")
    # applications:       relationship("Application",     back_populates="user", cascade="all, delete-orphan")
    # keyword_filters:    relationship("KeywordFilter",   back_populates="user", cascade="all, delete-orphan")
    # telegram_sources:   relationship("TelegramSource",  back_populates="user", cascade="all, delete-orphan")
    # telegram_matches:   relationship("TelegramMatch",   back_populates="user", cascade="all, delete-orphan")
