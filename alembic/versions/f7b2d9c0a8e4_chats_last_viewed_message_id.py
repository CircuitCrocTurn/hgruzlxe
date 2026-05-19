"""chats: add last_viewed_message_id

Revision ID: f7b2d9c0a8e4
Revises: e4f9a2c8b631
Create Date: 2026-05-13 13:30:00.000000+00:00

Добавляет ``chats.last_viewed_message_id`` — id последнего просмотренного
сообщения в чате на стороне hh.ru (``lastViewedByCurrentUserMessageId``).
Нужно, чтобы при «прочитать всё» отправлять корректный ``messageId`` в
``chatik.hh.ru/chatik/api/mark_read``.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7b2d9c0a8e4"
down_revision: Union[str, None] = "e4f9a2c8b631"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chats",
        sa.Column("last_viewed_message_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chats", "last_viewed_message_id")
