"""add company, salary, logo_ext to vacancy_responses

Revision ID: f1a2b3c4d5e6
Revises: ecc5f0b915c7
Create Date: 2026-05-12 13:30:00.000000+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "ecc5f0b915c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vacancy_responses",
        sa.Column("company", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "vacancy_responses",
        sa.Column("salary", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "vacancy_responses",
        sa.Column("logo_ext", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vacancy_responses", "logo_ext")
    op.drop_column("vacancy_responses", "salary")
    op.drop_column("vacancy_responses", "company")
