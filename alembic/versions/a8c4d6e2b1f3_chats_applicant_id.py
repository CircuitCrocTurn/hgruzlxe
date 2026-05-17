"""chats: add applicant_id

Revision ID: a8c4d6e2b1f3
Revises: f7b2d9c0a8e4
Create Date: 2026-05-13 16:00:00.000000+00:00

Добавляет ``chats.applicant_id`` — ID соискателя в диалоге на стороне
площадки. Для hh.ru это ``currentParticipantId`` из payload'а
``/chatik/api/chats``; он же подставляется как ``applicantId`` в
``GET /chatik/api/chat_data?chatId=…&applicantId=…``, который тянет
всю историю сообщений диалога.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8c4d6e2b1f3"
down_revision: Union[str, None] = "f7b2d9c0a8e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chats",
        sa.Column("applicant_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chats", "applicant_id")
