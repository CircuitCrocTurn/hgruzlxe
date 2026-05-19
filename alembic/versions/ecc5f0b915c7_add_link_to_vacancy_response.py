"""add link to vacancy_response

Revision ID: ecc5f0b915c7
Revises: a30e0c14b104
Create Date: 2026-05-12 10:45:54.420436+00:00

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ecc5f0b915c7'
down_revision: Union[str, None] = 'a30e0c14b104'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # 1. Добавить колонку как nullable
    op.add_column('vacancy_responses', sa.Column('link', sa.String(128), nullable=True))
    
    # 2. Заполнить существующие строки (например, пустой строкой или дефолтным URL)
    op.execute("UPDATE vacancy_responses SET link = '' WHERE link IS NULL")
    
    # 3. Сделать NOT NULL
    op.alter_column('vacancy_responses', 'link', nullable=False)

def downgrade():
    op.drop_column('vacancy_responses', 'link')
