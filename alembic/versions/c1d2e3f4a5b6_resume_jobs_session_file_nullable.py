"""resume_jobs.hh_session_file → nullable (миграция cookies в БД)

Revision ID: c1d2e3f4a5b6
Revises: 828ab3bd1f9d
Create Date: 2026-05-11 16:00:00.000000+00:00

Историческое поле ``hh_session_file`` хранило путь к pickle-файлу с
cookies hh.ru. После переноса cookies в столбец
``platform_credentials.encrypted_session`` (см.
:mod:`app.db.platform_credentials`) поле перестало заполняться. На
старых строках значение остаётся, на новых — NULL.

Удалять колонку сейчас намеренно не удаляем: при rollback'е кода
старые строки ``resume_jobs`` всё ещё могли бы прочитать pickle с
диска. После того, как уверенно убедимся, что pickle-файлов нет ни в
одном деплое — отдельной миграцией дропнем колонку.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "828ab3bd1f9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "resume_jobs",
        "hh_session_file",
        existing_type=sa.String(length=512),
        nullable=True,
    )


def downgrade() -> None:
    # Сначала проставляем пустую строку строкам, у которых NULL — иначе
    # NOT NULL constraint не приклеится. После реального перехода на БД
    # такие строки будут составлять большинство.
    op.execute(
        "UPDATE resume_jobs SET hh_session_file = '' WHERE hh_session_file IS NULL"
    )
    op.alter_column(
        "resume_jobs",
        "hh_session_file",
        existing_type=sa.String(length=512),
        nullable=False,
    )
