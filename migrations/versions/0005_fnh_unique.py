"""unique full_name_history (repo_id, full_name)

Revision ID: 0005
Revises: 0004

Locking: `ADD CONSTRAINT ... UNIQUE` takes an ACCESS EXCLUSIVE lock on
`full_name_history` (blocking reads and writes) for the index build; the preceding
dedupe `DELETE` is a self-join whose cost scales with the table size. Run this revision
in a maintenance window on large tables, preferably with `SET lock_timeout = '50ms'`
and `SET statement_timeout = '5s'` so it fails fast instead of queueing behind readers.
For hot deployments, build the unique index with `CREATE UNIQUE INDEX CONCURRENTLY` in
an autocommit block instead.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM full_name_history a USING full_name_history b"
        " WHERE a.id > b.id AND a.repo_id = b.repo_id AND a.full_name = b.full_name"
    )
    op.create_unique_constraint("fnh_repo_name_key", "full_name_history", ["repo_id", "full_name"])


def downgrade() -> None:
    op.drop_constraint("fnh_repo_name_key", "full_name_history", type_="unique")
