"""add temp_email and platform_password to users

Revision ID: a4f6b8c0d2e1
Revises: d8a3f1e7c920
Create Date: 2026-05-17 12:00:00.000000+00:00

Добавляем два столбца на ``users``:

* ``temp_email``     — временная почта из сервиса temp.coda.ink,
                       присваивается в /auth/verify-code при создании юзера.
                       Уникальная, nullable=True (для старых строк лениво
                       заполнится при первом заходе на /settings).
* ``platform_password`` — детерминированный пароль, сгенерированный из
                       ``user.id`` + ``PLATFORM_PASSWORD_SALT`` через
                       SHA-256 (см. ``app.services.platform_password``).
                       Хранится «как есть» специально: его нужно показывать
                       пользователю в /settings → «Доступы для ручного
                       входа», и им же мы регистрируем аккаунты юзера на
                       job-площадках (habr и т.д.).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a4f6b8c0d2e1"
down_revision: Union[str, None] = "d8a3f1e7c920"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("temp_email", sa.String(length=255), nullable=True),
    )
    op.create_index(
        op.f("ix_users_temp_email"),
        "users",
        ["temp_email"],
        unique=True,
    )
    op.add_column(
        "users",
        sa.Column("platform_password", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "platform_password")
    op.drop_index(op.f("ix_users_temp_email"), table_name="users")
    op.drop_column("users", "temp_email")
