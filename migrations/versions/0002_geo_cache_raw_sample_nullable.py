"""geo_cache raw_sample nullable

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("geo_cache", "raw_sample", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE geo_cache SET raw_sample = '' WHERE raw_sample IS NULL")
    op.alter_column("geo_cache", "raw_sample", existing_type=sa.Text(), nullable=False)
