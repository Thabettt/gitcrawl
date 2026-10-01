"""run_items index matching NULLS LAST ordering

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("run_items_stars_idx", table_name="run_items")
    op.create_index(
        "run_items_stars_idx",
        "run_items",
        ["run_id", sa.text("stargazers DESC NULLS LAST"), "repo_id"],
    )


def downgrade() -> None:
    op.drop_index("run_items_stars_idx", table_name="run_items")
    op.create_index("run_items_stars_idx", "run_items", ["run_id", sa.text("stargazers DESC")])
