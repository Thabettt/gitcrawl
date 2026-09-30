"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "owners",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("login", postgresql.CITEXT(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("location_raw", sa.Text(), nullable=True),
        sa.Column("country_iso", sa.CHAR(length=2), nullable=True),
        sa.Column("geo_confidence", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("blog", sa.Text(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column(
            "synced_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("login"),
    )

    op.create_table(
        "repos",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("full_name", postgresql.CITEXT(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("homepage", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("license_spdx", sa.Text(), nullable=True),
        sa.Column(
            "topics",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("fork", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("parent_full_name", sa.Text(), nullable=True),
        sa.Column("source_full_name", sa.Text(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("mirror_url", sa.Text(), nullable=True),
        sa.Column("is_template", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("size_kb", sa.Integer(), nullable=True),
        sa.Column("stargazers", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("forks_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("watchers", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("open_issues", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("has_wiki", sa.Boolean(), nullable=True),
        sa.Column("has_issues", sa.Boolean(), nullable=True),
        sa.Column("has_projects", sa.Boolean(), nullable=True),
        sa.Column("has_pages", sa.Boolean(), nullable=True),
        sa.Column("has_discussions", sa.Boolean(), nullable=True),
        sa.Column("has_pull_requests", sa.Boolean(), nullable=True),
        sa.Column(
            "custom_properties",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("pushed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "indexed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["owners.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("full_name"),
    )

    op.create_table(
        "full_name_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name", postgresql.CITEXT(), nullable=False),
        sa.Column(
            "seen_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "geo_cache",
        sa.Column("normalized", sa.Text(), nullable=False),
        sa.Column("country_iso", sa.CHAR(length=2), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=False),
        sa.Column("raw_sample", sa.Text(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("normalized"),
    )

    op.create_table(
        "shards",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("range_start", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("range_end", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("since_id", sa.BigInteger(), nullable=True),
        sa.Column("since_max", sa.BigInteger(), nullable=True),
        sa.Column("org", sa.Text(), nullable=True),
        sa.Column("tier", sa.Text(), nullable=False, server_default=sa.text("'cold'")),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("watermark", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("fetched", sa.Integer(), nullable=True, server_default=sa.text("0")),
        sa.Column("incomplete", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "ts",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("query_hash", sa.Text(), nullable=True),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("etag_sent", sa.Text(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("rl_limit", sa.Integer(), nullable=True),
        sa.Column("rl_remaining", sa.Integer(), nullable=True),
        sa.Column("rl_reset", sa.BigInteger(), nullable=True),
        sa.Column("rl_resource", sa.Text(), nullable=True),
        sa.Column("retry_after", sa.Integer(), nullable=True),
        sa.Column("link_next", sa.Boolean(), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("incomplete_results", sa.Boolean(), nullable=True),
        sa.Column("token_fp", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "repos_pushed_idx",
        "repos",
        ["pushed_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "repos_updated_idx",
        "repos",
        ["updated_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "repos_stars_idx",
        "repos",
        [sa.text("stargazers DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "repos_owner_idx",
        "repos",
        ["owner_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("fnh_repo_idx", "full_name_history", ["repo_id", "seen_at"])
    op.create_index("shards_state_idx", "shards", ["state", "tier"])
    op.create_index("audit_ts_idx", "audit_log", ["ts"])

    op.execute(
        "ALTER TABLE repos SET (fillfactor = 80, autovacuum_vacuum_scale_factor = 0.02, "
        "autovacuum_vacuum_insert_scale_factor = 0.02)"
    )
    op.execute(
        "ALTER TABLE audit_log SET (autovacuum_vacuum_scale_factor = 0.02, "
        "autovacuum_vacuum_insert_scale_factor = 0.02)"
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("shards")
    op.drop_table("geo_cache")
    op.drop_table("full_name_history")
    op.drop_table("repos")
    op.drop_table("owners")
    op.execute("DROP EXTENSION IF EXISTS citext")
