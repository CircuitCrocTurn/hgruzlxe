"""PlatformCredential — учётка юзера на habr / hh / linkedin / ...

Ключевое архитектурное решение: `encrypted_creds` и `encrypted_session`
шифруются РАЗНЫМИ ключами:

* `encrypted_creds` (логин/пароль/токены) шифруется ключом, который ты
  можешь восстановить — он нужен для авто-логина при ротации сессии.
* `encrypted_session` (cookies, csrf-токены, прочая жвачка площадки)
  шифруется одноразовым ключом и регулярно ротируется. Если БД
  утечёт — украденная сессия мертва, потому что ключ к ней не
  лежит рядом.

Уникальность `(user_id, platform)` — у юзера одна учётка на каждую
площадку. Если юзеру понадобятся несколько аккаунтов на одной площадке
(редкий сценарий) — снимаем unique и добавляем поле `label`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Статусы учётки. Воркер при ошибке логина выставляет `error` или
# `captcha_required` — UI показывает юзеру badge "переавторизуйся".
STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"            # сессия протухла, нужен повторный логин
STATUS_CAPTCHA_REQUIRED = "captcha_required"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"          # юзер сам отключил


class PlatformCredential(Base):
    __tablename__ = "platform_credentials"

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

    # Идентификатор площадки: "habr", "hh", "linkedin", "freelance_habr", ...
    # Совпадает с ключом в services/job_sites/registry.py.
    platform: Mapped[str] = mapped_column(String(64), nullable=False)

    # Креды (логин/пароль/api-токены) — шифруются ключом, который сервер
    # умеет восстанавливать. Нужны для авто-логина при ротации сессии.
    encrypted_creds: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Сессия (cookies, csrf, прочее) — шифруется одноразовым ключом
    # и регулярно ротируется. Может быть None, если ещё не логинились.
    encrypted_session: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=STATUS_ACTIVE, nullable=False)
    
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

    user: Mapped["User"] = relationship("User", back_populates="credentials")

    __table_args__ = (
        UniqueConstraint("user_id", "platform", name="uq_platform_credentials_user_platform"),
    )
