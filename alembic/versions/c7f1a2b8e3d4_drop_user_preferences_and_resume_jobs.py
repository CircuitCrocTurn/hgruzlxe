"""drop user_preferences and resume_jobs, move three flags to users

Revision ID: c7f1a2b8e3d4
Revises: b91d5f0e4c27
Create Date: 2026-05-14 06:30:00.000000+00:00

Перетягивает три флага из ``user_preferences`` прямо в ``users``
и сносит таблицы ``user_preferences`` и ``resume_jobs`` вместе со
связанными индексами / ограничениями.

После этой миграции:

* ``users`` имеет столбцы ``auto_apply_running``, ``notif_email``,
  ``notif_telegram`` (перенесены) и новый ``auto_up_resume``;
* остальные поля старой ``user_preferences`` живут как «вью»
  поверх ``resumes.parsed_data`` (см. ``app.db.users.PrefsView``);
* парсинг резюме идёт inline через ``asyncio.create_task``;
  отдельная таблица-«заявка» больше не нужна.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7f1a2b8e3d4"
down_revision: Union[str, None] = "b91d5f0e4c27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Добавляем недостающие столбцы в ``users``. server_default
    #    указан явно, чтобы существующие строки получили дефолт
    #    (NOT NULL без default'а не накатится).
    op.add_column(
        "users",
        sa.Column(
            "auto_apply_running",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "notif_email",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "notif_telegram",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "auto_up_resume",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # 2. Если таблица ``user_preferences`` существует — пере-
    #    таскиваем три флага по user_id и сносим её. Делаем через
    #    bind.execute, чтобы миграция была идемпотентной на свежих
    #    инсталляциях, где таблицы могло вообще не быть.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "user_preferences" in table_names:
        bind.execute(
            sa.text(
                """
                UPDATE users
                SET auto_apply_running = COALESCE(up.auto_apply_running, false),
                    notif_email        = COALESCE(up.notif_email,        false),
                    notif_telegram     = COALESCE(up.notif_telegram,     false)
                FROM user_preferences AS up
                WHERE users.id = up.user_id
                """
            )
        )
        op.drop_table("user_preferences")

    # 3. Сносим ``resume_jobs`` если есть.
    if "resume_jobs" in table_names:
        op.drop_table("resume_jobs")


def downgrade() -> None:
    # Откат пересоздаёт обе таблицы в их минимальной форме (без
    # старых индексов / уникальных ограничений), чтобы код мог
    # подняться. Если нужен полный откат — катить старые
    # миграции по очереди.
    op.create_table(
        "resume_jobs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("hh_phone", sa.String(length=32), nullable=False),
        sa.Column("resume_url", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "user_preferences",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("auto_apply_running", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notif_email", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("notif_telegram", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.drop_column("users", "auto_up_resume")
    op.drop_column("users", "notif_telegram")
    op.drop_column("users", "notif_email")
    op.drop_column("users", "auto_apply_running")
