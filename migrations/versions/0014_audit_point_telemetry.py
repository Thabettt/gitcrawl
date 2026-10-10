"""audit_log: point telemetry columns, run attribution; batch default 29

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("rl_used", sa.Integer(), nullable=True))
    op.add_column("audit_log", sa.Column("run_id", sa.BigInteger(), nullable=True))
    op.add_column("audit_log", sa.Column("phase", sa.Text(), nullable=True))
    op.create_index("audit_run_idx", "audit_log", ["run_id"])
    op.alter_column("app_settings", "graphql_batch_size", server_default=sa.text("29"))
    op.execute(
        "UPDATE app_settings SET graphql_batch_size = 29 WHERE graphql_batch_size = 20"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE app_settings SET graphql_batch_size = 20 WHERE graphql_batch_size = 29"
    )
    op.alter_column("app_settings", "graphql_batch_size", server_default=sa.text("20"))
    op.drop_index("audit_run_idx", table_name="audit_log")
    op.drop_column("audit_log", "phase")
    op.drop_column("audit_log", "run_id")
    op.drop_column("audit_log", "rl_used")
