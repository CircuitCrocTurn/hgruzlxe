"""chats: add last_message_outgoing

Revision ID: e4f9a2c8b631
Revises: d3c8a1b75f12
Create Date: 2026-05-13 10:00:00.000000+00:00

Добавляет колонку ``last_message_outgoing`` в таблицу ``chats``.
Нужно, чтобы фильтр «только актуальные» (работодатель ответил И
не отказ) можно было применить на уровне ``Chat`` без N+1-запросов
к ``messages``. См. ``app/services/job_sites/hh/chats_sync.py``
(``is_chat_relevant``).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e4f9a2c8b631"
down_revision: Union[str, None] = "d3c8a1b75f12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chats",
        sa.Column(
            "last_message_outgoing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("chats", "last_message_outgoing")
