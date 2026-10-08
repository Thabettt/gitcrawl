"""app settings: raise the GraphQL batch-size cap to 50

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("app_settings_batch_size", "app_settings", type_="check")
    op.create_check_constraint(
        "app_settings_batch_size",
        "app_settings",
        "graphql_batch_size BETWEEN 1 AND 50",
    )


def downgrade() -> None:
    op.execute("UPDATE app_settings SET graphql_batch_size = 20 WHERE graphql_batch_size > 20")
    op.drop_constraint("app_settings_batch_size", "app_settings", type_="check")
    op.create_check_constraint(
        "app_settings_batch_size",
        "app_settings",
        "graphql_batch_size BETWEEN 1 AND 20",
    )
