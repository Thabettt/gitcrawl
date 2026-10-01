"""run_items index matching NULLS LAST ordering

Revision ID: 0007
Revises: 0006

Locking: the upgrade drops and recreates `run_items_stars_idx`; `DROP INDEX` takes
ACCESS EXCLUSIVE and `CREATE INDEX` takes SHARE on `run_items`, so writes (and reads
during the drop) block for the duration. Run this revision in a maintenance window on
large tables, preferably with `SET lock_timeout = '50ms'` and
`SET statement_timeout = '5s'`. For hot deployments, use `DROP INDEX CONCURRENTLY` /
`CREATE INDEX CONCURRENTLY` in an autocommit block instead.
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
