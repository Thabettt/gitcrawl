"""app settings: single-row operator-tunable run limits

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_DEFAULTS = {
    "max_shards": "10",
    "max_candidates": "500",
    "max_hydrate": "200",
    "max_enrich": "100",
    "request_deadline_seconds": "3600",
    "graphql_batch_size": "20",
    "limiter_max_concurrent": "10",
}


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False, nullable=False),
        *(
            sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text(value))
            for name, value in _DEFAULTS.items()
        ),
        sa.Column(
            "graphql_batch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="app_settings_single_row"),
        sa.CheckConstraint("graphql_batch_size BETWEEN 1 AND 20", name="app_settings_batch_size"),
        sa.CheckConstraint(
            "limiter_max_concurrent BETWEEN 1 AND 100", name="app_settings_concurrency"
        ),
    )
    op.execute("INSERT INTO app_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def downgrade() -> None:
    op.drop_table("app_settings")
