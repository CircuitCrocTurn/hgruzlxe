"""add chats and messages

Revision ID: d3c8a1b75f12
Revises: f1a2b3c4d5e6
Create Date: 2026-05-13 09:00:00.000000+00:00

Создаёт таблицы ``chats`` и ``messages`` (см.
``app/db/models/chat.py`` и ``app/db/models/message.py``).

* Chat — диалог юзера с работодателем на внешней площадке
  (hh.ru/habr/...). Хранит только те диалоги, в которых работодатель
  ответил И ответ не является отказом (фильтр выполняется на стороне
  синхронизатора, см. ``app/services/job_sites/hh/chats_sync.py``).
* Message — отдельное сообщение в чате. На текущий момент сохраняется
  ``lastMessage`` от площадки; подгрузку всей истории добавим позднее.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d3c8a1b75f12"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("service", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("subtitle", sa.String(length=255), nullable=True),
        sa.Column("icon_url", sa.String(length=512), nullable=True),
        sa.Column("link", sa.String(length=512), nullable=False),
        sa.Column(
            "unread_messages",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_message_preview", sa.String(length=1024), nullable=True
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("applicant_state", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_chats_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chats")),
        sa.UniqueConstraint(
            "user_id",
            "service",
            "external_id",
            name="uq_chats_user_service_external",
        ),
    )
    op.create_index(
        op.f("ix_chats_user_id"), "chats", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_chats_last_activity_at"),
        "chats",
        ["last_activity_at"],
        unique=False,
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("chat_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=8), nullable=False),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column(
            "is_read",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["chats.id"],
            name=op.f("fk_messages_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "chat_id",
            "external_id",
            name="uq_messages_chat_external",
        ),
    )
    op.create_index(
        op.f("ix_messages_chat_id"),
        "messages",
        ["chat_id"],
        unique=False,
    )
    op.create_index(
        "ix_messages_chat_sent_at",
        "messages",
        ["chat_id", "sent_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_messages_chat_sent_at", table_name="messages")
    op.drop_index(op.f("ix_messages_chat_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_chats_last_activity_at"), table_name="chats")
    op.drop_index(op.f("ix_chats_user_id"), table_name="chats")
    op.drop_table("chats")
