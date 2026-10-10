# Transport and pipeline efficiency measurements (measured 2026-10-10)

Verification of the transport/pipeline efficiency work on branch `perf/runtime-adaptive-control`
(HEAD `b721719` at measurement time). No source changes in this task: the focused deterministic
tests, the full suite with the coverage gate, and the lint/format/type gates below are the
evidence. Measured 2026-10-10 on Windows, Python 3.12.10, pytest 9.1.1, with `TEST_DATABASE_URL`
already set in the environment to the documented local cluster (`localhost:5433`, database
`gitcrawl_test`); the value was used untouched and is not reproduced here.

## 1. What changed

- **Task 1 (`09f352c`, pool pinning):** `src/lib/gh_client.py:51-64` — `CLIENT_POOL_LIMITS =
  httpx.Limits(max_connections=64, max_keepalive_connections=64, keepalive_expiry=60.0)` is
  passed to `httpx.Client(...)` together with `trust_env=False`; no `http2` argument is passed
  (HTTP/1.1 retained).
- **Task 2 (`039e956`, cached JSON):** `src/lib/graphql_batch.py:157-162` — `_post` reads
  `audit.cached_json(response)` first and calls `response.json()` only when no body was cached,
  so a batch response is parsed once when the audit hook cached it.
- **Task 3 (`d176e83` + `6ead5cb`, buffered discovery upserts):** `src/discover/pipeline.py:32-33`
  (`_UPSERT_EVERY_PAGES = 10`, `_UPSERT_EVERY_ITEMS = 1000`), `:225-226` and `:390-391` (threshold
  parameters on `_Worker`/`run_search_discovery`), `:274-288` (`flush_pending` and the best-effort
  wrapper), `:323-327` (threshold check after each page); `6ead5cb` switches the deadline path at
  `:305` to `flush_pending_best_effort()`.
- **Task 4 (`b721719`, audit writer thread):** `src/lib/audit.py:137` (`_WRITER_QUEUE_DEPTH = 4`),
  `:140-200` — `AuditBuffer.add` (`:166`) hands batches to a bounded daemon writer thread instead
  of inserting inline; `flush` (`:176`) is the barrier that drains, joins, and re-raises the first
  write failure.

## 2. Evidence (focused tests, Task 5 Step 1)

| # | Command | Expected observation | Recorded observation |
|---|---|---|---|
| 1 | `.venv\Scripts\python.exe -m pytest tests/unit/test_gh_client.py::test_create_client_pins_the_pool_and_ignores_environment_proxies -q` | `create_client` passes `httpx.Limits(max_connections=64, max_keepalive_connections=64, keepalive_expiry=60.0)` and `trust_env=False`, and never passes `http2` (connection reuse is asserted at configuration level; `httpx.MockTransport` bypasses pooling by design, and one shared client per run is already pinned by `tests/integration/test_runner.py:1118`) | `1 passed in 0.50s` (recorded from the verbose re-run) |
| 2 | `.venv\Scripts\python.exe -m pytest tests/unit/test_graphql_batch_core.py -q -k "cached_body or parses_once"` | `response.json()` is called exactly once per batch response when the hook cached the body (Task 2 test), and once without a hook | `2 passed, 23 deselected in 0.24s` — `test_fetch_batch_reads_the_hook_cached_body_without_reparsing`, `test_fetch_batch_parses_once_without_a_caching_hook` |
| 3 | `.venv\Scripts\python.exe -m pytest tests/integration/test_pipeline.py -q -k "batched_by_page_threshold or wait_for_the_upsert or item_threshold or repeated_repo"` | `events == ["page", "page", "page", "upsert-start", "upsert-end"]` — all three page fetches complete before the first Postgres upsert, and a 3-page shard issues 2 upserts instead of 3 | `4 passed, 29 deselected in 2.41s` — `test_multi_page_shard_upserts_are_batched_by_page_threshold`, `test_page_fetch_does_not_wait_for_the_upsert`, `test_item_threshold_flushes_before_the_page_threshold`, `test_repeated_repo_within_one_flush_window_is_deduped` |
| 4 | `.venv\Scripts\python.exe -m pytest tests/integration/test_audit_db.py -q -k "buffer"` | `add()` returns while the writer is blocked inside the INSERT; `flush()` blocks until it returns; a failed write surfaces exactly once from `flush()` | `7 passed, 10 deselected in 1.04s` — `test_audit_buffer_batches_inserts`, `test_audit_buffer_flush_persists_remaining_records_and_clears`, `test_audit_buffer_rejects_non_positive_batch_size`, `test_audit_buffer_add_returns_while_the_insert_is_blocked`, `test_audit_buffer_flush_waits_for_the_blocked_insert`, `test_audit_buffer_flush_raises_the_write_failure`, `test_audit_buffer_add_raises_a_stored_write_failure` |

All four commands were run exactly as written above (the brief's Step 1 commands); the brief's
explicit `-q` combines with `addopts = "-q"` (`pyproject.toml:8`) to `-qq`, which still prints
the dots and `[100%]` but suppresses the per-test identity and final `N passed` line, so each
entry above was re-run with `-v` to capture the counts and test ids shown.

## 3. Full suite and coverage (Task 5 Step 2)

- Command (exact brief form): `.venv\Scripts\python.exe -m pytest -q --cov=src --cov-report=term-missing`
  — all dots and `[100%]` with no failure markers, and the coverage gate line printed
  (`-qq` double-quiet suppresses only the trailing count line, not the coverage gate line).
- Same command with the single `-q` from `addopts`:
  `1495 passed, 1 warning in 314.14s (0:05:14)`.
- Coverage: `TOTAL 7119 371 95%`; `Required test coverage of 93.0% reached. Total coverage: 94.79%`
  (floor `fail_under = 93`, `pyproject.toml:43`). 15 files were skipped as fully covered.

## 4. Live discovery timing (Task 5 Step 4)

**Not run.** The step is optional, consumes GitHub API points, and run-to-run server latency
variance was measured at 2x on identical code (run 18 vs run 19); a single live run would not be
informative. The deterministic mocked tests in section 2 are the gate. The comparison point stays
the recorded 95.6 s discovery phase for the 39,128-repo run (spec §2); no new live number is
claimed here.

## 5. Residual risks

- **Async audit writes persist at `flush()`.** A process kill loses at most the four queued batches
  plus the partial in-memory batch — roughly 500 records at the default batch size of 100 — a
  small, bounded loss; nothing that `flush()` acknowledged is lost.
- **Window-level dedupe accounting.** Repeated ids within one flush window are deduped before the
  upsert, so per-batch upsert counters reflect unique ids rather than raw page items; the database
  end state is identical.
- **`trust_env=False` removes implicit env-proxy support.** No environment-proxy behavior is
  documented for this project; explicit proxies would need a code change.
- **HTTP/2 remains deliberately off.** `h2` is not installed; the client stays on HTTP/1.1
  keep-alive, as before.
