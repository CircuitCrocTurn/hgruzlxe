"""cleanup: trim resumes columns, drop identity_checks and cover_letter_templates

Revision ID: d8a3f1e7c920
Revises: c7f1a2b8e3d4
Create Date: 2026-05-15 06:25:00.000000+00:00

После выпила resume_job (миграция ``c7f1a2b8e3d4``) причёсываем
схему до минимума, нужного приложению:

* ``resumes`` — убираем 6 «лишних» колонок:
    - ``title``                  (не используется UI)
    - ``original_filename``      (имя файла не нужно после парсинга)
    - ``content_hash``           (кэш по хэшу выпилен в API upload)
    - ``parsed_email``           (GENERATED, выпилен — данные есть в JSONB)
    - ``parsed_full_name``       (GENERATED, выпилен — данные есть в JSONB)
    - ``parsed_birthday``        (GENERATED, выпилен — данные есть в JSONB)
    - ``parsed_skills``          (JSON-массив, дублирующий ``parsed_data['skills']``)
  Все эти данные продолжают жить в ``parsed_data`` JSONB
  (``email``, ``full_name``, ``birthday``, ``skills``).

* ``identity_checks`` — таблица результатов Stage-2 fraud-check.
  Результаты теперь пишем только в логи (``offerday.workers
  .identity_check``); табличной истории не держим.

* ``cover_letter_templates`` — сопроводительные генерирует LLM
  на лету по резюме и описанию вакансии (см.
  ``app.services.cover_letter.generate_cover_letter``), хранить
  заранее заведённые шаблоны не требуется.

Миграция идемпотентна: все DROP'ы используют ``IF EXISTS``.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8a3f1e7c920"
down_revision: Union[str, None] = "c7f1a2b8e3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── resumes: drop computed/legacy columns + their indexes ──────────
    # Индекс на parsed_email — из миграции b91d5f0e4c27. На остальные
    # GENERATED-колонки индексов не было, но content_hash в init был
    # под индексом ``ix_resumes_content_hash``.
    op.execute("DROP INDEX IF EXISTS ix_resumes_parsed_email")
    op.execute("DROP INDEX IF EXISTS ix_resumes_content_hash")

    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS parsed_email")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS parsed_full_name")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS parsed_birthday")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS parsed_skills")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS title")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS original_filename")
    op.execute("ALTER TABLE resumes DROP COLUMN IF EXISTS content_hash")

    # ── drop identity_checks completely ────────────────────────────────
    op.execute("DROP TABLE IF EXISTS identity_checks CASCADE")

    # ── drop cover_letter_templates completely ─────────────────────────
    op.execute("DROP TABLE IF EXISTS cover_letter_templates CASCADE")


def downgrade() -> None:
    # Downgrade пересоздаёт таблицы и колонки в их «минимальной»
    # форме, чтобы Alembic смог откатиться. Полный набор индексов
    # и ограничений предыдущих миграций здесь сознательно не
    # восстанавливаем — данные при откате всё равно теряются.

    # ── resumes: re-add dropped columns ────────────────────────────────
    op.add_column(
        "resumes",
        sa.Column("title", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "resumes",
        sa.Column("original_filename", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "resumes",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "resumes",
        sa.Column(
            "parsed_skills",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
    )
    op.execute(
        "ALTER TABLE resumes "
        "ADD COLUMN parsed_email TEXT "
        "GENERATED ALWAYS AS (parsed_data->>'email') STORED"
    )
    op.execute(
        "ALTER TABLE resumes "
        "ADD COLUMN parsed_full_name TEXT "
        "GENERATED ALWAYS AS (parsed_data->>'full_name') STORED"
    )
    op.execute(
        "ALTER TABLE resumes "
        "ADD COLUMN parsed_birthday TEXT "
        "GENERATED ALWAYS AS (parsed_data->>'birthday') STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_resumes_parsed_email "
        "ON resumes (parsed_email)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_resumes_content_hash "
        "ON resumes (content_hash)"
    )

    # ── re-create identity_checks (минимальная форма) ──────────────────
    op.create_table(
        "identity_checks",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("details", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("candidate_user_ids", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── re-create cover_letter_templates (минимальная форма) ───────────
    op.create_table(
        "cover_letter_templates",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
