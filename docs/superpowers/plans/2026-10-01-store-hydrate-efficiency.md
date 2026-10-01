# Store, Hydrate & HTTP Client Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse hydration from three transactions per repo to one repo write, make history recording a single idempotent statement, index and batch tombstone purges, stop paying CREATE/DROP DDL per bootstrap chunk, and retry transport errors in the HTTP client.

**Architecture:** Thread the ETag through the existing upsert path instead of a second UPDATE, add one unique constraint plus one partial index via Alembic revisions 0005/0006, reuse a single UNLOGGED staging table per bootstrap run, and add a `classify_transport` branch to the existing retry classifier.

**Tech Stack:** Python 3.12, SQLAlchemy 2.1 + psycopg3 (Postgres 18), Alembic, httpx, pytest.

**Spec:** `docs/superpowers/plans/2026-10-01-efficiency-audit-report.md` (findings S7, #24, #25, #26, #32, #33, #34, #45, #51)

## Global Constraints

- Python 3.12, ruff line length 100, `from __future__ import annotations`.
- Migrations: revision files `migrations/versions/0005_*.py` and `0006_*.py` with sequential `down_revision`; never edit 0001-0004. `tests/integration/test_models_migrations.py` pins model/migration parity — update it as part of the task if it enumerates indexes/constraints.
- Purge migrations must be safe on populated tables: dedupe data before adding a unique constraint; add indexes with plain `CREATE INDEX` only if run during a maintenance window, otherwise use the commented `CONCURRENTLY` variant and note the `lock_timeout=50ms` convention from `design/research.md:68-71`.
- Integration tests need `TEST_DATABASE_URL` ending `_test`.
- One commit per task, prefix `perf:`.

---

## File Map

| File | Change |
|---|---|
| `src/store/upserts.py` | `etags` parameter, `_write_repos(include_etag=)`, `_load_existing` selects `etag`, bootstrap staging reuse, owner-login collision tokenize, `copy_batch` param |
| `src/store/lifecycle.py` | etag through upsert; `_record_history` one statement; batched `purge_tombstones` |
| `src/store/models.py` | `FullNameHistory` unique constraint; `repos_deleted_idx` partial index |
| `migrations/versions/0005_fnh_unique.py` | **new** — dedupe + unique `(repo_id, full_name)` |
| `migrations/versions/0006_repos_deleted_idx.py` | **new** — partial index on `deleted_at` |
| `src/lib/gh_client.py` | transport-error retry; body slice decode |
| `src/limiter/classifier.py` | `classify_transport` |
| `tests/unit/test_gh_client.py`, `tests/unit/test_classifier.py` | new retry tests |
| `tests/integration/test_lifecycle.py` | single-write etag test; batched purge test |
| `tests/integration/test_upserts.py` | staging-reuse test; owner collision test |

---

### Task 1: ETag in one repo write + idempotent history

**Files:**
- Modify: `src/store/upserts.py:15-50,278-342`, `src/store/lifecycle.py:31-95`, `src/store/models.py:99-110`
- Create: `migrations/versions/0005_fnh_unique.py`
- Test: `tests/integration/test_lifecycle.py`, `tests/integration/test_models_migrations.py`, `tests/integration/test_upserts.py`

**Interfaces:**
- `upsert_repos(engine, items, *, batch_size=500, etags: Mapping[int, str | None] | None = None) -> UpsertStats` — when `etags` is given, ETags are written in the same `ON CONFLICT` statement.
- `_write_repos(connection, rows, *, include_etag: bool = False)`.
- `apply_hydration` performs exactly one `repos` write for a 200 response.

- [ ] **Step 1: Write the failing tests**

Add to `tests/integration/test_lifecycle.py` (reuse `seed`, `payload`, `repo_row`, `history_names`):

```python
def test_apply_hydration_writes_etag_in_single_repo_write(clean: Engine):
    from sqlalchemy import event

    from hydrate.repo_client import HydratedRepo
    from store.lifecycle import apply_hydration

    seed(clean, "octo/one", repo_id=1)
    statements: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(clean, "before_cursor_execute", listener)
    try:
        hydrated = HydratedRepo(
            id=1,
            node_id="R_1",
            full_name="octo/one",
            payload=payload(1, full_name="octo/one", description="changed"),
            etag='W/"e1"',
            not_modified=False,
        )
        apply_hydration(clean, "octo/one", hydrated)
    finally:
        event.remove(clean, "before_cursor_execute", listener)
    assert repo_row(clean, 1)["etag"] == 'W/"e1"'
    writes = [s for s in statements if "INTO repos" in s.upper() or s.upper().startswith("UPDATE REPOS")]
    assert len(writes) == 1


def test_repeated_rename_does_not_duplicate_history(clean: Engine):
    from hydrate.repo_client import HydratedRepo
    from store.lifecycle import apply_hydration

    seed(clean, "octo/old", repo_id=1)
    for _ in range(2):
        hydrated = HydratedRepo(
            id=1,
            node_id="R_1",
            full_name="octo/new",
            payload=payload(1, full_name="octo/new"),
            etag=None,
            not_modified=False,
        )
        apply_hydration(clean, "octo/old", hydrated)
        seed(clean, "octo/old", repo_id=1)  # force the rename again
    with clean.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM full_name_history WHERE repo_id = 1 AND full_name = 'octo/old'"
            )
        )
    assert count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_lifecycle.py -k "single_repo_write or duplicate_history" -v`
Expected: FAIL — two `repos` writes (upsert + etag UPDATE); history has duplicate rows.

- [ ] **Step 3: Implement `upserts.py`**

```python
def _load_existing(connection: Connection, ids: list[int]) -> dict[int, dict]:
    columns = [getattr(Repo, field) for field in _REPO_FIELDS] + [Repo.etag]
    rows = connection.execute(select(*columns).where(Repo.id.in_(ids))).mappings()
    return {row["id"]: dict(row) for row in rows}


def _changed(current: dict, row: dict) -> bool:
    if "etag" in row and current.get("etag") != row["etag"]:
        return True
    return any(current[field] != row[field] for field in _REPO_FIELDS)


def _write_repos(connection: Connection, rows: list[dict], *, include_etag: bool = False) -> None:
    if not rows:
        return
    fields = _REPO_FIELDS + (("etag",) if include_etag else ())
    values = [{field: row.get(field) for field in fields} for row in rows]
    statement = pg_insert(Repo).values(values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=["id"],
            set_={field: statement.excluded[field] for field in fields if field != "id"},
        )
    )


def upsert_repos(
    engine: Engine,
    items: Iterable[dict],
    *,
    batch_size: int = 500,
    etags: Mapping[int, str | None] | None = None,
) -> UpsertStats:
    ...
            except Exception if etags:
                etag = etags.get(row["id"])
                if etag is not None:
                    row = dict(row, etag=etag)
            normalized.append(row)
    ...
            rows_to_write = inserts + updates
            include_etag = bool(rows_to_write) and all("etag" in row for row in rows_to_write)
            _write_repos(connection, rows_to_write, include_etag=include_etag)
```

Add `Mapping` to the `collections.abc` import. The owner/history logic is unchanged.

- [ ] **Step 4: Implement `lifecycle.py` and the migration**

`apply_hydration` (replace `:79-89`):

```python
    etags = {payload["id"]: hydrated.etag} if hydrated.etag is not None else None
    upsert_repos(engine, [payload], etags=etags)
    repo_id = payload.get("id")
```

Delete the `if hydrated.etag is not None: with engine.begin() ... update(...)` block.

`_record_history` (replace `:38-47`):

```python
def _record_history(engine: Engine, repo_id: int, full_name: str) -> None:
    statement = (
        pg_insert(FullNameHistory)
        .values(repo_id=repo_id, full_name=full_name)
        .on_conflict_do_nothing(index_elements=["repo_id", "full_name"])
    )
    with engine.begin() as connection:
        connection.execute(statement)
```

Add `from sqlalchemy.dialects.postgresql import insert as pg_insert` to `lifecycle.py`.

`models.py` `FullNameHistory`:

```python
class FullNameHistory(Base):
    __tablename__ = "full_name_history"
    __table_args__ = (
        Index("fnh_repo_idx", "repo_id", "seen_at"),
        UniqueConstraint("repo_id", "full_name", name="fnh_repo_name_key"),
    )
```

`migrations/versions/0005_fnh_unique.py`:

```python
"""unique full_name_history (repo_id, full_name)

Revision ID: 0005
Revises: 0004
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
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/integration/test_lifecycle.py tests/integration/test_upserts.py tests/integration/test_models_migrations.py -q`
Expected: PASS. Existing rename tests use `set(history_names(...))`, so the dedupe does not break them. If `test_models_migrations.py` enumerates constraints, add `fnh_repo_name_key` to its expected set.

- [ ] **Step 6: Commit**

```bash
git add src/store/upserts.py src/store/lifecycle.py src/store/models.py migrations/versions/0005_fnh_unique.py tests/integration/test_lifecycle.py tests/integration/test_models_migrations.py
git commit -m "perf: single-write hydration with etag and idempotent history"
```

---

### Task 2: Indexed, batched tombstone purge

**Files:**
- Modify: `src/store/models.py:43-52`, `src/store/lifecycle.py:115-124`
- Create: `migrations/versions/0006_repos_deleted_idx.py`
- Test: `tests/integration/test_lifecycle.py`, `tests/integration/test_models_migrations.py`

**Interfaces:** `purge_tombstones(engine, *, retention_days=30, now=None, batch_size=1000) -> int` (same return meaning).

- [ ] **Step 1: Write the failing test**

```python
def test_purge_tombstones_batches(clean: Engine):
    from store.lifecycle import purge_tombstones, tombstone

    seed(clean, "octo/a", repo_id=1)
    seed(clean, "octo/b", repo_id=2)
    seed(clean, "octo/c", repo_id=3)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    for repo_id, name in ((1, "octo/a"), (2, "octo/b"), (3, "octo/c")):
        tombstone(clean, name, now=when)
    removed = purge_tombstones(clean, retention_days=0, now=when + timedelta(days=1), batch_size=1)
    assert removed == 3
```

(`datetime`, `timedelta` are already imported in the file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_lifecycle.py::test_purge_tombstones_batches -v`
Expected: FAIL — unexpected keyword `batch_size`.

- [ ] **Step 3: Implement**

`models.py` `Repo.__table_args__` add:

```python
        Index(
            "repos_deleted_idx",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL"),
        ),
```

`lifecycle.py`:

```python
def purge_tombstones(
    engine: Engine,
    *,
    retention_days: int = 30,
    now: datetime | Callable[[], datetime] | None = None,
    batch_size: int = 1000,
) -> int:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    cutoff = _resolve_now(now) - timedelta(days=retention_days)
    total = 0
    while True:
        with engine.begin() as connection:
            ids = select(Repo.id).where(Repo.deleted_at < cutoff).limit(batch_size)
            result = connection.execute(delete(Repo).where(Repo.id.in_(ids)))
            removed = int(result.rowcount or 0)
        total += removed
        if removed < batch_size:
            return total
```

`migrations/versions/0006_repos_deleted_idx.py`:

```python
"""partial index for tombstone purges

Revision ID: 0006
Revises: 0005
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_lifecycle.py tests/integration/test_models_migrations.py -q`
Expected: PASS; update the migration-parity test's expected `repos` index set if it enumerates indexes.

- [ ] **Step 5: Commit**

```bash
git add src/store/models.py src/store/lifecycle.py migrations/versions/0006_repos_deleted_idx.py tests/integration/test_lifecycle.py tests/integration/test_models_migrations.py
git commit -m "perf: index and batch tombstone purges"
```

---

### Task 3: Reuse one bootstrap staging table + tokenize login collisions

**Files:**
- Modify: `src/store/upserts.py:210-253` (`_upsert_owners`), `:398-446` (`_bootstrap_chunk`, `bootstrap_copy`)
- Test: `tests/integration/test_upserts.py`

**Interfaces:** `bootstrap_copy(engine, items, *, copy_batch: int = _COPY_BATCH)`; staging table created once per call.

- [ ] **Step 1: Write the failing tests**

```python
def test_bootstrap_creates_staging_table_once(clean: Engine):
    from sqlalchemy import event

    from store.upserts import bootstrap_copy

    creates: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if "CREATE UNLOGGED TABLE" in statement:
            creates.append(statement)

    event.listen(clean, "before_cursor_execute", listener)
    try:
        bootstrap_copy(clean, [payload(repo_id) for repo_id in range(1, 6)], copy_batch=2)
    finally:
        event.remove(clean, "before_cursor_execute", listener)
    assert len(creates) == 1


def test_same_batch_owner_login_collision_is_tokenized(clean: Engine):
    from store.upserts import upsert_repos

    first = payload(1)
    first["owner"] = {"id": 101, "login": "dup", "type": "User"}
    second = payload(2)
    second["owner"] = {"id": 102, "login": "DUP", "type": "Organization"}
    upsert_repos(clean, [first, second], batch_size=10)
    with clean.connect() as connection:
        logins = [
            str(row[0])
            for row in connection.execute(
                text("SELECT login FROM owners ORDER BY id")
            )
        ]
    assert len(logins) == 2
    assert logins[0].casefold() == "dup"
    assert logins[1].casefold().startswith("dup~")
```

(`payload` is the file's local helper; reuse it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_upserts.py -k "staging_table_once or login_collision" -v`
Expected: FAIL — one CREATE per chunk; unique violation on `owners.login`.

- [ ] **Step 3: Implement**

`bootstrap_copy`:

```python
def bootstrap_copy(engine: Engine, items: Iterable[dict], *, copy_batch: int = _COPY_BATCH) -> UpsertStats:
    stats = UpsertStats()
    total = 0
    staged_total = 0
    table_name = f"repos_staging_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f"CREATE UNLOGGED TABLE IF NOT EXISTS {table_name} (LIKE repos)"))
    try:
        for chunk in _chunks(items, copy_batch):
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
            staged_total += _bootstrap_chunk(engine, normalized, owners, stats, table_name)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    stats.skipped = total - staged_total
    return stats
```

`_bootstrap_chunk(engine, normalized, owners, stats, table_name)`: delete the CREATE and the `finally: DROP`; insert `connection.execute(text(f"TRUNCATE {table_name}"))` immediately before the COPY. The rest (nested savepoint, COPY, `staged`, `_merge_staging`, stats) is unchanged.

`_upsert_owners` inserts: replace the `for owner_id, (login, owner_type) in owners.items():` classification block with:

```python
    taken = {str(login).casefold() for _, login in holders}
    rows_to_insert = []
    rows_to_update = []
    for owner_id in sorted(owners):
        login, owner_type = owners[owner_id]
        current = existing.get(owner_id)
        if current is None:
            candidate = login
            if candidate.casefold() in taken:
                candidate = f"{login}~{owner_id}"
            taken.add(candidate.casefold())
            rows_to_insert.append({"id": owner_id, "login": candidate, "type": owner_type})
        else:
            taken.add(str(current["login"]).casefold())
            if current["login"] != login or current["type"] != owner_type:
                rows_to_update.append({"id": owner_id, "login": login, "type": owner_type})
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_upserts.py -q`
Expected: PASS; existing bootstrap stats/skips-dirty-row tests still hold because merge and counts are unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/store/upserts.py tests/integration/test_upserts.py
git commit -m "perf: reuse one bootstrap staging table and tokenize login collisions"
```

---

### Task 4: Retry transport errors; stop decoding whole bodies for error messages

**Files:**
- Modify: `src/limiter/classifier.py:43-45,91`, `src/lib/gh_client.py:84-103,130-177`
- Test: `tests/unit/test_classifier.py`, `tests/unit/test_gh_client.py`

**Interfaces:** `classifier.classify_transport(attempt: int, *, jitter=...) -> Decision`; `request_with_retry` retries `httpx.TransportError` under the same `max_attempts` budget.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_classifier.py
def test_classify_transport_backs_off_then_fails_loud():
    from limiter.classifier import Action, classify_transport

    first = classify_transport(0, jitter=lambda: 0.0)
    assert first.action is Action.BACKOFF
    assert first.sleep_seconds == 60.0
    assert classify_transport(5, jitter=lambda: 0.0).action is Action.FAIL_LOUD
```

```python
# tests/unit/test_gh_client.py
def test_transport_errors_are_retried():
    attempts: list[int] = []
    sleeps: list[float] = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = request_with_retry(
        client, "GET", "https://api.github.com/x", sleep=sleeps.append, now=lambda: 0.0, jitter=lambda: 0.0
    )
    assert response.status_code == 200
    assert len(attempts) == 3
    assert len(sleeps) == 2


def test_transport_errors_fail_loud_after_max_attempts():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.TransportError):
        request_with_retry(
            client, "GET", "https://api.github.com/x", sleep=lambda _: None, now=lambda: 0.0, jitter=lambda: 0.0
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_classifier.py tests/unit/test_gh_client.py -k transport -v`
Expected: FAIL — `classify_transport` missing; `ConnectError` propagates on first attempt.

- [ ] **Step 3: Implement**

`classifier.py`:

```python
def classify_transport(
    attempt: int, *, jitter: Callable[[], float] = lambda: random.random()
) -> Decision:
    if attempt >= 5:
        return Decision(Action.FAIL_LOUD, None, None, "retries exhausted")
    return Decision(Action.BACKOFF, _backoff_seconds(attempt, jitter), None, "transport error")
```

`gh_client.py`: import `classify_transport`. Restructure the request section of `request_with_retry`:

```python
        try:
            started = now()
            try:
                if auth:
                    response = client.request(method, url, **request_kwargs)
                else:
                    request = client.build_request(method, url, **request_kwargs)
                    request.headers.pop("Authorization", None)
                    response = client.send(request)
            finally:
                if limiter is not None:
                    limiter.release(resource, token_id)
        except httpx.TransportError:
            attempt += 1
            if attempt >= max_attempts:
                raise
            extra = {} if jitter is None else {"jitter": jitter}
            decision = classify_transport(attempt - 1, **extra)
            sleep(decision.sleep_seconds or 0.0)
            continue
        latency_ms = (now() - started) * 1000.0
        if on_response is not None:
            on_response(response, latency_ms)
        if limiter is not None:
            limiter.update_from_headers(resource, token_id, response.headers, now=now())
        if _sso_partial_results(response.headers):
            raise PartialResultsError(response)
        if response.status_code not in _TRIAGE_STATUSES:
            return response
        error_code, message = _error_fields(response)
        extra = {} if jitter is None else {"jitter": jitter}
        decision = classify(
            response.status_code,
            response.headers,
            attempt=attempt,
            error_code=error_code,
            message=message,
            now=now(),
            **extra,
        )
        if decision.action in _RETRYABLE_ACTIONS:
            attempt += 1
            if attempt >= max_attempts:
                return response
            sleep(decision.sleep_seconds)
            continue
        return response
```

Also change `_error_fields`'s fallback (`:102`) to:

```python
        message = response.content[:_BODY_FALLBACK_CHARS].decode("utf-8", errors="replace")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_gh_client.py tests/unit/test_classifier.py tests/contract/test_github_pagination.py tests/integration/test_pipeline.py -q`
Expected: PASS — ensure slot-release tests still see exactly one release per attempt (the inner `finally` guarantees it) and audit-hook exceptions still propagate (they now propagate after release, before retry classification).

- [ ] **Step 5: Commit**

```bash
git add src/lib/gh_client.py src/limiter/classifier.py tests/unit/test_gh_client.py tests/unit/test_classifier.py
git commit -m "perf: retry transport errors and avoid decoding full error bodies"
```

---

## Plan self-review

- **Spec coverage:** S7 → T1; #34 → T1; #25 → T2; #24 → T3; #32/#33 → T3; #26/#45/#51 → T4.
- **Placeholders:** none. Tests reference existing file-local helpers (`payload`, `seed`, `repo_row`, `clean`) by name; those exist in the named files.
- **Type consistency:** `etags` is `Mapping[int, str | None]`; `_write_repos(include_etag=)` is keyword-only; migration revisions chain 0004 → 0005 → 0006.
- **Safety note:** 0005 dedupes history before the unique constraint; 0006 creates a partial index whose predicate matches the purge query exactly.
