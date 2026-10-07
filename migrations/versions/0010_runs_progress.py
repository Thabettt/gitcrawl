"""runs: live progress fields

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("progress_phase", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("progress_done", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("progress_total", sa.Integer(), nullable=True))
    op.add_column(
        "runs",
        sa.Column(
            "progress_updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "progress_updated_at")
    op.drop_column("runs", "progress_total")
    op.drop_column("runs", "progress_done")
    op.drop_column("runs", "progress_phase")
