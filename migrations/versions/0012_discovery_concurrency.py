"""app settings: discovery page-worker count

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "discovery_concurrency",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("32"),
        ),
    )
    op.create_check_constraint(
        "app_settings_discovery_concurrency",
        "app_settings",
        "discovery_concurrency BETWEEN 1 AND 64",
    )


def downgrade() -> None:
    op.drop_constraint("app_settings_discovery_concurrency", "app_settings", type_="check")
    op.drop_column("app_settings", "discovery_concurrency")
