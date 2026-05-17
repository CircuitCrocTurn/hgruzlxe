"""add temp_email_token to users

Revision ID: b5e7d1c92a08
Revises: a4f6b8c0d2e1
Create Date: 2026-05-17 08:10:00.000000+00:00

Добавляем столбец ``users.temp_email_token`` — Bearer-токен ящика на
``temp.coda.ink``, который возвращается при ``POST /v1/address`` и
является ЕДИНСТВЕННЫМ способом читать inbox этого ящика. Без него мы
не сможем достать письмо с кодом подтверждения от habr (см.
``app/services/temp_mail.py`` и ``app/services/job_sites/habr/auth_flow.py``).

Колонка nullable=True специально: на момент миграции у существующих
юзеров уже есть ``temp_email`` (его выдала прежняя ЗАГЛУШКА, локальный
рандом), но НЕТ соответствующего ящика на temp.coda.ink — токена для
такого ящика не существует. Эти юзеры просто потеряют возможность
получать письма на старый «временный» адрес; новый ящик с настоящим
токеном выпишется лениво при первом заходе на /settings (см.
``app.db.users.ensure_platform_credentials``).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b5e7d1c92a08"
down_revision: Union[str, None] = "a4f6b8c0d2e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("temp_email_token", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "temp_email_token")
