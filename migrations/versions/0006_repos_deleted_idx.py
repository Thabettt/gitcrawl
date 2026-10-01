"""partial index for tombstone purges

Revision ID: 0006
Revises: 0005

Locking: `CREATE INDEX` (non-concurrent) takes a SHARE lock on `repos`, which blocks
writes for the duration of the build; the downgrade's `DROP INDEX` takes ACCESS
EXCLUSIVE. Run this revision in a maintenance window on large tables, preferably with
`SET lock_timeout = '50ms'` and `SET statement_timeout = '5s'`. For hot deployments,
create the index with `CREATE INDEX CONCURRENTLY` in an autocommit block instead.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "repos_deleted_idx",
        "repos",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("repos_deleted_idx", table_name="repos")
