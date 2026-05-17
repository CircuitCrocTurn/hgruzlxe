"""ResumeJob — фоновая задача парсинга резюме hh.ru.

После успешного логина мы НЕ дёргаем синхронно
``get_resume_id → get_resume → resume_to_db`` в эндпоинте
``/auth/verify-code``: эта цепочка занимает секунды (а с учётом
капчи/перелогина — минуты), и юзер всё это время сидит на спиннере.
Вместо этого ставим запись сюда — а воркер
(см. :func:`app.queue.jobs.run_resume_job`) разбирает её в фоне.

Состояния (`status`):
    queued    — в очереди, воркер ещё не взял
    running   — воркер взял задачу
    done      — резюме распарсено и сохранено в ``resumes``
    failed    — на каком-то шаге упало; смотри ``error``

Уникальный частичный индекс на ``(user_id) WHERE status IN
('queued','running')`` гарантирует, что на одного юзера не бывает
двух активных задач — это идемпотентность на уровне БД (повторный
``/verify-code`` не создаст дубль, переиспользует ту же).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# Канонические значения `status` — используются и в моделях, и в
# репозитории (`app.db.resume_jobs`), и в воркере.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

ACTIVE_STATUSES: tuple[str, ...] = (STATUS_QUEUED, STATUS_RUNNING)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResumeJob(Base):
    __tablename__ = "resume_jobs"

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

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_QUEUED, index=True,
    )

    # Источник резюме.
    #   resume_url — если фронт явно передал ссылку (например, у юзера
    #   несколько резюме и он выбрал нужное в UI). Воркер достанет hash
    #   из URL.
    #   resume_id  — заполняется уже воркером после ``get_resume_id``,
    #   чтобы статус-эндпоинт мог вернуть его на фронт.
    resume_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resume_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Историческое поле: путь к pickle-файлу с cookies hh.ru. После
    # миграции на ``platform_credentials.encrypted_session`` (см.
    # :mod:`app.db.platform_credentials`) воркер достаёт cookies из
    # БД по user_id и это поле больше не нужно. Оставлено
    # nullable=True ради обратной совместимости со старыми строками
    # ``resume_jobs``, которые могут лежать в БД от прежних деплоев.
    # На новых записях всегда пусто.
    hh_session_file: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Телефон в формате hh (8XXXXXXXXXX) — нужен ``HHClient``-у в
    # конструкторе. На случай, если cookies протухнут и потребуется
    # перелогин (теоретически: на практике без OTP воркер всё равно
    # не залогинится, просто упадёт в `failed`).
    hh_phone: Mapped[str] = mapped_column(String(32), nullable=False)

    # Краткая выжимка результата для фронта (`user_name`, `title`,
    # `full_name`). Полный распарсенный профиль — в таблице ``resumes``
    # (поле `parsed_data`), сюда дублировать незачем.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    __table_args__ = (
        # Не более одной активной задачи на юзера. Postgres
        # поддерживает частичные unique-индексы — пользуемся.
        Index(
            "uq_resume_jobs_user_id_active",
            "user_id",
            unique=True,
            postgresql_where=text("status in ('queued','running')"),
        ),
    )
