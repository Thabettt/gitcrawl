"""console persistence

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("filter_hash", sa.Text(), nullable=False),
        sa.Column("filter_spec", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("api_version", sa.Text(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("fetched", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("unchanged", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("incomplete_shards", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("bundle_dir", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("runs_created_idx", "runs", [sa.text("created_at DESC")])
    op.create_index("runs_filter_hash_idx", "runs", ["filter_hash", sa.text("created_at DESC")])

    op.create_table(
        "saved_filters",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("filter_spec", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "run_items",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name", postgresql.CITEXT(), nullable=False),
        sa.Column("stargazers", sa.Integer(), nullable=True),
        sa.Column("pushed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("license_spdx", sa.Text(), nullable=True),
        sa.Column("country_iso", sa.CHAR(length=2), nullable=True),
        sa.Column("geo_confidence", sa.Text(), nullable=True),
        sa.Column(
            "virtuals",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"]),
        sa.PrimaryKeyConstraint("run_id", "repo_id"),
    )
    op.create_index("run_items_repo_idx", "run_items", ["repo_id"])
    op.create_index("run_items_stars_idx", "run_items", ["run_id", sa.text("stargazers DESC")])


def downgrade() -> None:
    op.drop_index("run_items_stars_idx", table_name="run_items")
    op.drop_index("run_items_repo_idx", table_name="run_items")
    op.drop_table("run_items")
    op.drop_index("runs_filter_hash_idx", table_name="runs")
    op.drop_index("runs_created_idx", table_name="runs")
    op.drop_table("runs")
    op.drop_table("saved_filters")
