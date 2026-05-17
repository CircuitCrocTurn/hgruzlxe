"""resumes: GENERATED columns parsed_email/parsed_full_name/parsed_birthday

Revision ID: b91d5f0e4c27
Revises: a8c4d6e2b1f3
Create Date: 2026-05-13 17:30:00.000000+00:00

Добавляет к ``resumes`` три PostgreSQL ``GENERATED ALWAYS AS … STORED``
колонки, которые автоматически синхронизируются с ``parsed_data`` JSONB:

* ``parsed_email``     ← ``parsed_data->>'email'``
* ``parsed_full_name`` ← ``parsed_data->>'full_name'``
* ``parsed_birthday``  ← ``parsed_data->>'birthday'``

Зачем — см. обсуждение в чате: вместо плоского рефакторинга модели
под полный набор полей из ``app.schemas.resume.Resume``, продвигаем
точечно те поля, которые реально нужны для запросов (например,
Stage-2 fraud-check, поиск дубликатов по email/ФИО). Остальное —
живёт в ``parsed_data`` JSONB и читается через pydantic-схему.

``GENERATED ALWAYS … STORED`` Postgres сам пересчитывает при каждом
``UPDATE`` JSONB-поля → никаких ручных триггеров / Python-апдейтов.
Индекс на ``parsed_email`` — для быстрого поиска дубликатов.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "b91d5f0e4c27"
down_revision: Union[str, None] = "a8c4d6e2b1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # GENERATED-колонки в Postgres нельзя завести через SQLAlchemy
    # ``op.add_column`` без явного ``GENERATED ALWAYS AS … STORED``
    # выражения, поэтому льём чистым SQL. ``IF NOT EXISTS`` — на
    # случай повторного запуска миграции в dev (Postgres его
    # поддерживает на ``ALTER TABLE … ADD COLUMN``).
    op.execute(
        "ALTER TABLE resumes "
        "ADD COLUMN IF NOT EXISTS parsed_email TEXT "
        "GENERATED ALWAYS AS (parsed_data->>'email') STORED"
    )
    op.execute(
        "ALTER TABLE resumes "
        "ADD COLUMN IF NOT EXISTS parsed_full_name TEXT "
        "GENERATED ALWAYS AS (parsed_data->>'full_name') STORED"
    )
    op.execute(
        "ALTER TABLE resumes "
        "ADD COLUMN IF NOT EXISTS parsed_birthday TEXT "
        "GENERATED ALWAYS AS (parsed_data->>'birthday') STORED"
    )
    # Индекс на parsed_email для Stage-2 fraud-check и поиска
    # «один email — несколько юзеров».
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_resumes_parsed_email "
        "ON resumes (parsed_email)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_resumes_parsed_email")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS parsed_birthday")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS parsed_full_name")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS parsed_email")
