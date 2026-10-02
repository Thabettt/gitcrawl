"""run_items repo_id nullable with ON DELETE SET NULL

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_items", sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False))
    op.drop_constraint("run_items_pkey", "run_items", type_="primary")
    op.create_primary_key("run_items_pkey", "run_items", ["id"])
    op.drop_constraint("run_items_repo_id_fkey", "run_items", type_="foreignkey")
    op.alter_column("run_items", "repo_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_foreign_key(
        "run_items_repo_id_fkey",
        "run_items",
        "repos",
        ["repo_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint("run_items_run_repo_key", "run_items", ["run_id", "repo_id"])


def downgrade() -> None:
    """Downgrade restores NOT NULL and therefore drops rows whose repo_id is NULL.

    Lossy by design; export those rows before downgrading (see ruling R55).
    """
    op.drop_constraint("run_items_run_repo_key", "run_items", type_="unique")
    op.drop_constraint("run_items_repo_id_fkey", "run_items", type_="foreignkey")
    op.execute("DELETE FROM run_items WHERE repo_id IS NULL")
    op.alter_column("run_items", "repo_id", existing_type=sa.BigInteger(), nullable=False)
    op.create_foreign_key("run_items_repo_id_fkey", "run_items", "repos", ["repo_id"], ["id"])
    op.drop_constraint("run_items_pkey", "run_items", type_="primary")
    op.create_primary_key("run_items_pkey", "run_items", ["run_id", "repo_id"])
    op.drop_column("run_items", "id")
