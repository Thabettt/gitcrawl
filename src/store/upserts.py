from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb
from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from store.models import FullNameHistory, Owner, Repo

_REPO_FIELDS: tuple[str, ...] = (
    "id",
    "node_id",
    "full_name",
    "owner_id",
    "name",
    "description",
    "homepage",
    "language",
    "license_spdx",
    "topics",
    "visibility",
    "fork",
    "parent_full_name",
    "source_full_name",
    "archived",
    "disabled",
    "mirror_url",
    "is_template",
    "size_kb",
    "stargazers",
    "forks_count",
    "watchers",
    "open_issues",
    "default_branch",
    "has_wiki",
    "has_issues",
    "has_projects",
    "has_pages",
    "has_discussions",
    "has_pull_requests",
    "custom_properties",
    "created_at",
    "pushed_at",
    "updated_at",
)
_COPY_FIELDS: tuple[str, ...] = _REPO_FIELDS + ("indexed_at",)
_COPY_BATCH = 1000


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    conflicts: int = 0
    history_rows: int = 0


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _last_segment(full_name: str) -> str:
    return full_name.rsplit("/", 1)[-1]


def _coerce_count(value: object) -> int | None:
    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _valid_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_repo(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    repo_id = item.get("id")
    node_id = item.get("node_id")
    full_name = item.get("full_name")
    owner = item.get("owner")
    if not _valid_id(repo_id) or not isinstance(node_id, str) or not node_id:
        return None
    if not isinstance(full_name, str) or not full_name or not isinstance(owner, dict):
        return None
    owner_id = owner.get("id")
    owner_login = owner.get("login")
    if not _valid_id(owner_id) or not isinstance(owner_login, str) or not owner_login:
        return None
    topics_raw = item.get("topics")
    if topics_raw is None:
        topics: list[str] = []
    elif isinstance(topics_raw, list) and all(isinstance(topic, str) for topic in topics_raw):
        topics = list(topics_raw)
    else:
        return None
    stargazers = _coerce_count(item.get("stargazers_count"))
    forks_count = _coerce_count(item.get("forks_count"))
    watchers = _coerce_count(item.get("watchers_count"))
    open_issues = _coerce_count(item.get("open_issues_count"))
    if stargazers is None or forks_count is None or watchers is None or open_issues is None:
        return None
    license_obj = item.get("license")
    parent = item.get("parent")
    source = item.get("source")
    visibility = item.get("visibility") or ("private" if item.get("private") else "public")
    return {
        "id": repo_id,
        "node_id": node_id,
        "full_name": full_name,
        "owner_id": owner_id,
        "name": item.get("name") or _last_segment(full_name),
        "description": item.get("description"),
        "homepage": item.get("homepage"),
        "language": item.get("language"),
        "license_spdx": license_obj.get("spdx_id") if isinstance(license_obj, dict) else None,
        "topics": topics,
        "visibility": visibility,
        "fork": bool(item.get("fork")),
        "parent_full_name": parent.get("full_name") if isinstance(parent, dict) else None,
        "source_full_name": source.get("full_name") if isinstance(source, dict) else None,
        "archived": bool(item.get("archived")),
        "disabled": bool(item.get("disabled")),
        "mirror_url": item.get("mirror_url"),
        "is_template": bool(item.get("is_template")),
        "size_kb": item.get("size"),
        "stargazers": stargazers,
        "forks_count": forks_count,
        "watchers": watchers,
        "open_issues": open_issues,
        "default_branch": item.get("default_branch"),
        "has_wiki": item.get("has_wiki"),
        "has_issues": item.get("has_issues"),
        "has_projects": item.get("has_projects"),
        "has_pages": item.get("has_pages"),
        "has_discussions": item.get("has_discussions"),
        "has_pull_requests": item.get("has_pull_requests"),
        "custom_properties": dict(item.get("custom_properties") or {}),
        "created_at": _parse_timestamp(item.get("created_at")),
        "pushed_at": _parse_timestamp(item.get("pushed_at")),
        "updated_at": _parse_timestamp(item.get("updated_at")),
        "owner_login": owner_login,
        "owner_type": owner.get("type") or "User",
    }


def _chunks(items: Iterable[dict], size: int) -> Iterator[list[dict]]:
    chunk: list[dict] = []
    for item in items:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _upsert_owners(
    connection: Connection, owners: dict[int, tuple[str, str]], stats: UpsertStats
) -> None:
    if not owners:
        return
    desired = {login.casefold(): (owner_id, login) for owner_id, (login, _) in owners.items()}
    holders = connection.execute(
        select(Owner.id, Owner.login).where(
            Owner.login.in_([login for login, _ in owners.values()])
        )
    ).all()
    for holder_id, holder_login in holders:
        entry = desired.get(holder_login.casefold())
        if entry is not None and entry[0] != holder_id:
            connection.execute(
                update(Owner).where(Owner.id == holder_id).values(login=f"{entry[1]}~{holder_id}")
            )
            stats.conflicts += 1
    existing = {
        row["id"]: dict(row)
        for row in connection.execute(
            select(Owner.id, Owner.login, Owner.type).where(Owner.id.in_(owners))
        ).mappings()
    }
    rows_to_insert = []
    rows_to_update = []
    for owner_id, (login, owner_type) in owners.items():
        current = existing.get(owner_id)
        if current is None:
            rows_to_insert.append({"id": owner_id, "login": login, "type": owner_type})
        elif current["login"] != login or current["type"] != owner_type:
            rows_to_update.append({"id": owner_id, "login": login, "type": owner_type})
    if rows_to_insert:
        statement = pg_insert(Owner).values(rows_to_insert)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=["id"],
                set_={"login": statement.excluded.login, "type": statement.excluded.type},
            )
        )
    for row in rows_to_update:
        connection.execute(
            update(Owner).where(Owner.id == row["id"]).values(login=row["login"], type=row["type"])
        )


def _rename_stale_full_names(
    connection: Connection, normalized: list[dict], stats: UpsertStats
) -> None:
    desired = {row["full_name"].casefold(): (row["id"], row["full_name"]) for row in normalized}
    holders = connection.execute(
        select(Repo.id, Repo.full_name).where(
            Repo.full_name.in_([row["full_name"] for row in normalized])
        )
    ).all()
    for holder_id, holder_name in holders:
        entry = desired.get(holder_name.casefold())
        if entry is not None and entry[0] != holder_id:
            connection.execute(
                update(Repo).where(Repo.id == holder_id).values(full_name=f"{entry[1]}~{holder_id}")
            )
            connection.execute(
                insert(FullNameHistory).values(repo_id=holder_id, full_name=holder_name)
            )
            stats.conflicts += 1
            stats.history_rows += 1


def _load_existing(connection: Connection, ids: list[int]) -> dict[int, dict]:
    columns = [getattr(Repo, field) for field in _REPO_FIELDS]
    rows = connection.execute(select(*columns).where(Repo.id.in_(ids))).mappings()
    return {row["id"]: dict(row) for row in rows}


def _changed(current: dict, row: dict) -> bool:
    return any(current[field] != row[field] for field in _REPO_FIELDS)


def _write_repos(connection: Connection, rows: list[dict]) -> None:
    if not rows:
        return
    values = [{field: row[field] for field in _REPO_FIELDS} for row in rows]
    statement = pg_insert(Repo).values(values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=["id"],
            set_={field: statement.excluded[field] for field in _REPO_FIELDS if field != "id"},
        )
    )


def upsert_repos(engine: Engine, items: Iterable[dict], *, batch_size: int = 500) -> UpsertStats:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    stats = UpsertStats()
    for chunk in _chunks(items, batch_size):
        normalized: list[dict] = []
        owners: dict[int, tuple[str, str]] = {}
        for item in chunk:
            row = normalize_repo(item)
            if row is None:
                stats.skipped += 1
                continue
            normalized.append(row)
            owners[row["owner_id"]] = (row["owner_login"], row["owner_type"])
        if not normalized:
            continue
        with engine.begin() as connection:
            _upsert_owners(connection, owners, stats)
            _rename_stale_full_names(connection, normalized, stats)
            existing = _load_existing(connection, [row["id"] for row in normalized])
            inserts = []
            updates = []
            for row in normalized:
                current = existing.get(row["id"])
                if current is None:
                    inserts.append(row)
                elif _changed(current, row):
                    updates.append(row)
                else:
                    stats.unchanged += 1
            _write_repos(connection, inserts + updates)
            history = [{"repo_id": row["id"], "full_name": row["full_name"]} for row in inserts]
            for row in updates:
                previous = existing[row["id"]]["full_name"]
                if previous != row["full_name"]:
                    history.append({"repo_id": row["id"], "full_name": previous})
            if history:
                connection.execute(insert(FullNameHistory), history)
                stats.history_rows += len(history)
            stats.inserted += len(inserts)
            stats.updated += len(updates)
    return stats


def _copy_sql(table_name: str) -> str:
    columns = ", ".join(_COPY_FIELDS)
    return (
        f"COPY {table_name} ({columns}) FROM STDIN "
        "WITH (FORMAT TEXT, ON_ERROR ignore, REJECT_LIMIT 100)"
    )


def _copy_row(row: dict, indexed_at: datetime) -> tuple:
    return tuple(
        Jsonb(row[field]) if field == "custom_properties" else row[field] for field in _REPO_FIELDS
    ) + (indexed_at,)


def _merge_staging(connection: Connection, table_name: str) -> tuple[int, int]:
    columns = ", ".join(_REPO_FIELDS)
    updates = ", ".join(f"{field} = EXCLUDED.{field}" for field in _REPO_FIELDS if field != "id")
    inserted = int(
        connection.scalar(
            text(
                f"SELECT count(*) FROM {table_name} s"
                f" WHERE NOT EXISTS (SELECT 1 FROM repos r WHERE r.id = s.id)"
            )
        )
        or 0
    )
    renamed = int(
        connection.scalar(
            text(
                f"SELECT count(*) FROM {table_name} s JOIN repos r ON r.id = s.id"
                f" WHERE r.full_name IS DISTINCT FROM s.full_name"
            )
        )
        or 0
    )
    statement = text(
        f"WITH existing AS ("
        f" SELECT r.id, r.full_name FROM repos r JOIN {table_name} s ON s.id = r.id"
        f" WHERE r.full_name IS DISTINCT FROM s.full_name"
        f"), new_repos AS ("
        f" SELECT s.id, s.full_name FROM {table_name} s"
        f" WHERE NOT EXISTS (SELECT 1 FROM repos r WHERE r.id = s.id)"
        f"), merged AS ("
        f" INSERT INTO repos ({columns}) SELECT {columns} FROM {table_name}"
        f" ON CONFLICT (id) DO UPDATE SET {updates}"
        f") INSERT INTO full_name_history (repo_id, full_name)"
        f" SELECT id, full_name FROM existing"
        f" UNION ALL SELECT id, full_name FROM new_repos"
    )
    connection.execute(statement)
    return inserted, renamed


def _bootstrap_chunk(
    engine: Engine,
    normalized: list[dict],
    owners: dict[int, tuple[str, str]],
    stats: UpsertStats,
) -> int:
    table_name = f"repos_staging_{uuid4().hex}"
    indexed_at = datetime.now(UTC)
    with engine.begin() as connection:
        try:
            with connection.begin_nested():
                _upsert_owners(connection, owners, stats)
                _rename_stale_full_names(connection, normalized, stats)
                connection.execute(
                    text(f"CREATE UNLOGGED TABLE IF NOT EXISTS {table_name} (LIKE repos)")
                )
                driver = connection.connection.driver_connection
                with driver.cursor() as cursor:
                    with cursor.copy(_copy_sql(table_name)) as copy:
                        for row in normalized:
                            copy.write_row(_copy_row(row, indexed_at))
                staged = int(connection.scalar(text(f"SELECT count(*) FROM {table_name}")) or 0)
                inserted, renamed = _merge_staging(connection, table_name)
                stats.inserted += inserted
                stats.history_rows += inserted + renamed
        finally:
            connection.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    return staged


def bootstrap_copy(engine: Engine, items: Iterable[dict]) -> UpsertStats:
    stats = UpsertStats()
    total = 0
    staged_total = 0
    for chunk in _chunks(items, _COPY_BATCH):
        total += len(chunk)
        normalized: list[dict] = []
        owners: dict[int, tuple[str, str]] = {}
        for item in chunk:
            row = normalize_repo(item)
            if row is None:
                continue
            normalized.append(row)
            owners[row["owner_id"]] = (row["owner_login"], row["owner_type"])
        if not normalized:
            continue
        staged_total += _bootstrap_chunk(engine, normalized, owners, stats)
    stats.skipped = total - staged_total
    return stats
