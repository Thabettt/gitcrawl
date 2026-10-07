from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    login: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    location_raw: Mapped[str | None] = mapped_column(Text)
    country_iso: Mapped[str | None] = mapped_column(CHAR(2))
    geo_confidence: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    blog: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (
        Index("repos_pushed_idx", "pushed_at", postgresql_where=text("deleted_at IS NULL")),
        Index("repos_updated_idx", "updated_at", postgresql_where=text("deleted_at IS NULL")),
        Index(
            "repos_stars_idx",
            text("stargazers DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("repos_owner_idx", "owner_id", postgresql_where=text("deleted_at IS NULL")),
        Index(
            "repos_deleted_idx",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("owners.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    homepage: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    license_spdx: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    visibility: Mapped[str] = mapped_column(Text, nullable=False)
    fork: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    parent_full_name: Mapped[str | None] = mapped_column(Text)
    source_full_name: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    mirror_url: Mapped[str | None] = mapped_column(Text)
    is_template: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    size_kb: Mapped[int | None] = mapped_column(Integer)
    stargazers: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    forks_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    watchers: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    open_issues: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    default_branch: Mapped[str | None] = mapped_column(Text)
    has_wiki: Mapped[bool | None] = mapped_column(Boolean)
    has_issues: Mapped[bool | None] = mapped_column(Boolean)
    has_projects: Mapped[bool | None] = mapped_column(Boolean)
    has_pages: Mapped[bool | None] = mapped_column(Boolean)
    has_discussions: Mapped[bool | None] = mapped_column(Boolean)
    has_pull_requests: Mapped[bool | None] = mapped_column(Boolean)
    custom_properties: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    pushed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    etag: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    indexed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class FullNameHistory(Base):
    __tablename__ = "full_name_history"
    __table_args__ = (
        Index("fnh_repo_idx", "repo_id", "seen_at"),
        UniqueConstraint("repo_id", "full_name", name="fnh_repo_name_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(CITEXT, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class GeoCache(Base):
    __tablename__ = "geo_cache"

    normalized: Mapped[str] = mapped_column(Text, primary_key=True)
    country_iso: Mapped[str | None] = mapped_column(CHAR(2))
    confidence: Mapped[str] = mapped_column(Text, nullable=False)
    raw_sample: Mapped[str | None] = mapped_column(Text)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class Shard(Base):
    __tablename__ = "shards"
    __table_args__ = (Index("shards_state_idx", "state", "tier"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str | None] = mapped_column(Text)
    range_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    range_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    since_id: Mapped[int | None] = mapped_column(BigInteger)
    since_max: Mapped[int | None] = mapped_column(BigInteger)
    org: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'cold'"))
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    watermark: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    total_count: Mapped[int | None] = mapped_column(Integer)
    fetched: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    incomplete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class Runs(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("runs_created_idx", text("created_at DESC")),
        Index("runs_filter_hash_idx", "filter_hash", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    filter_hash: Mapped[str] = mapped_column(Text, nullable=False)
    filter_spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    api_version: Mapped[str] = mapped_column(Text, nullable=False)
    total_count: Mapped[int | None] = mapped_column(Integer)
    fetched: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    incomplete_shards: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text)
    bundle_dir: Mapped[str | None] = mapped_column(Text)
    progress_phase: Mapped[str | None] = mapped_column(Text)
    progress_done: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    progress_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    progress_started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class RunItem(Base):
    __tablename__ = "run_items"
    __table_args__ = (
        Index("run_items_repo_idx", "repo_id"),
        Index(
            "run_items_stars_idx",
            "run_id",
            text("stargazers DESC NULLS LAST"),
            "repo_id",
        ),
        UniqueConstraint("run_id", "repo_id", name="run_items_run_repo_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    repo_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("repos.id", ondelete="SET NULL")
    )
    full_name: Mapped[str] = mapped_column(CITEXT, nullable=False)
    stargazers: Mapped[int | None] = mapped_column(Integer)
    pushed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    archived: Mapped[bool | None] = mapped_column(Boolean)
    language: Mapped[str | None] = mapped_column(Text)
    license_spdx: Mapped[str | None] = mapped_column(Text)
    country_iso: Mapped[str | None] = mapped_column(CHAR(2))
    geo_confidence: Mapped[str | None] = mapped_column(Text)
    virtuals: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))


class Corpus(Base):
    __tablename__ = "corpora"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("runs.id"), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    repo_count: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class SavedFilter(Base):
    __tablename__ = "saved_filters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    filter_spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class AppSettings(Base):
    __tablename__ = "app_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="app_settings_single_row"),
        CheckConstraint("graphql_batch_size BETWEEN 1 AND 20", name="app_settings_batch_size"),
        CheckConstraint(
            "limiter_max_concurrent BETWEEN 1 AND 100", name="app_settings_concurrency"
        ),
        CheckConstraint(
            "discovery_concurrency BETWEEN 1 AND 64",
            name="app_settings_discovery_concurrency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    max_shards: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    max_candidates: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("500"))
    max_hydrate: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("200"))
    max_enrich: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    request_deadline_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600")
    )
    graphql_batch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    graphql_batch_size: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("20")
    )
    limiter_max_concurrent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10")
    )
    discovery_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("32")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("audit_ts_idx", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    query_hash: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    etag_sent: Mapped[str | None] = mapped_column(Text)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    rl_limit: Mapped[int | None] = mapped_column(Integer)
    rl_remaining: Mapped[int | None] = mapped_column(Integer)
    rl_reset: Mapped[int | None] = mapped_column(BigInteger)
    rl_resource: Mapped[str | None] = mapped_column(Text)
    retry_after: Mapped[int | None] = mapped_column(Integer)
    link_next: Mapped[bool | None] = mapped_column(Boolean)
    total_count: Mapped[int | None] = mapped_column(Integer)
    incomplete_results: Mapped[bool | None] = mapped_column(Boolean)
    token_fp: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
