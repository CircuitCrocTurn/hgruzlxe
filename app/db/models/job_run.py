"""JobRun — запуски фоновых задач (воркеров).

Простая таблица "запуск воркера N для юзера X начался в T1, закончился
в T2 со статистикой Y". Очень помогает дебажить продакшен и показывать
в дешборде "последний обход hh был час назад, нашёл 12 вакансий".

`user_id` nullable — потому что некоторые воркеры работают
для одного юзера (например, scrape_sites под кредами конкретного
юзера), а некоторые — системно для всех (telegram-scraper мониторит
общий пул каналов; matcher разгребает все новые вакансии оптом).

`stats` — произвольный JSONB. Каждый воркер кладёт туда своё:
* job_sites_scrape:    {found: 47, new: 12, duplicates: 35, errors: 0, by_platform: {hh: 20, habr: 27}}
* tg_scraper:          {channels_polled: 230, matches_total: 45, by_user: {...}}
* matcher:             {processed: 100, matched: 23, threshold: 0.4}
* application_sender:  {attempted: 8, sent: 7, failed: 1}
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Статусы запуска. running выставляется на старте, success/failed — на финише.
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
# Прервано извне (graceful shutdown / killed). Полезно для пост-мортема.
STATUS_CANCELLED = "cancelled"


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Имя воркера: "scrape_sites", "tg_scraper", "matcher",
    # "application_sender", "sync_chats", "send_notifications".
    # Совпадает с именем таска/модуля в app/workers/.
    worker_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Юзер, для которого запускался воркер. Null = системный прогон.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(String(16), default=STATUS_RUNNING, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Произвольная статистика — см. примеры в docstring модуля.
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Сообщение об ошибке (если status=failed). Текст, потому что
    # стек-трейсы и длинные ответы API могут быть большими.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User | None"] = relationship("User", back_populates="job_runs")

    __table_args__ = (
        # "Последний запуск воркера X" — частый запрос дешборда.
        Index(
            "ix_job_runs_worker_started",
            "worker_name",
            "started_at",
        ),
        # "История запусков воркера для юзера X" — тоже встречается.
        Index(
            "ix_job_runs_user_worker_started",
            "user_id",
            "worker_name",
            "started_at",
        ),
    )
