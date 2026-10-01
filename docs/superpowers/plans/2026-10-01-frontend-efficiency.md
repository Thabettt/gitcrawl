# Frontend Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop clone-progress polls from growing without bound, pause polling in hidden tabs, cache static assets, and make `j`/`k` row navigation O(1)-per-keypress.

**Architecture:** Server caps and throttles progress documents (error cap from the enrich plan, atomic+throttled writes here); htmx triggers gain a visibility filter; static files get `Cache-Control` via a thin `StaticFiles` subclass; row navigation moves its pure index math into a tiny UMD module testable with `node --test`, while `app.js` caches the row list and invalidates on htmx swaps.

**Tech Stack:** htmx 2.0.4, vanilla ES5-style JS, Jinja2, CSS, Node's built-in test runner (guaranteed present in this environment; tests skip if `node` is unavailable), pytest.

**Spec:** `docs/superpowers/plans/2026-10-01-efficiency-audit-report.md` (findings S10, S14, S15, #36-41)

## Global Constraints

- No new runtime dependencies; no build step; ES5-compatible syntax in `app.js` (the file uses `var`/IIFE style — keep it).
- htmx stays pinned at 2.0.4.
- Server-side changes that this plan consumes must already exist: `CloneProgress.error_count` (enrich plan Task 4) and `read_clone_progress` parsing it.
- One commit per task, prefix `perf:`.

---

## File Map

| File | Change |
|---|---|
| `src/serve/templates/partials/clone_progress.html` | capped error list + `--progress` custom property |
| `src/serve/templates/partials/status.html` | visibility-gated polling |
| `src/serve/runs.py` | `_PersistedProgress` throttled atomic emit, `emit(force=True)` terminals, `error_count` read |
| `src/enrich/cloner.py` | `CloneProgress.emit(*, force=False)`; terminal emissions forced |
| `src/serve/app.py` | `CachedStaticFiles` mount |
| `src/serve/pages.py` | `Jinja2Templates(auto_reload=False)` |
| `src/serve/static/app.css` | transform-based progress bar, row containment, hover background |
| `src/serve/static/rownav.js` | **new** UMD pure helper |
| `src/serve/static/app.js` | cached rows, invalidate on swap, double-submit guard |
| `src/serve/templates/base.html` | include `rownav.js` |
| `tests/js/rownav.test.mjs` | **new** Node test |
| `tests/unit/test_rownav_js.py` | **new** pytest wrapper |
| `tests/contract/test_clone_endpoints.py`, `test_run_detail.py`, `test_console_pages.py` | attribute/header assertions |

---

### Task 1: Capped, throttled, atomic clone progress

**Files:**
- Modify: `src/enrich/cloner.py:50-61`, `src/serve/runs.py:178-250`, `src/serve/templates/partials/clone_progress.html`
- Test: `tests/unit/test_cloner.py`, `tests/contract/test_clone_endpoints.py`

**Interfaces:**
- `CloneProgress.emit(*, force: bool = False) -> None`.
- `_PersistedProgress(path, *, now: Callable[[], float] = time.monotonic, emit_interval: float = 0.25, **values)` writes via temp file + `os.replace` at most every `emit_interval` unless `force=True`.
- `read_clone_progress` populates `error_count` (defaulting to `len(errors)`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cloner.py
def test_persisted_progress_throttles_and_replaces_atomically(tmp_path):
    from serve.runs import _PersistedProgress

    now = {"value": 0.0}
    progress = _PersistedProgress(
        tmp_path / "progress.json",
        status="running", total=10, completed=0, failed=0,
        now=lambda: now["value"],
    )
    progress.completed = 1
    progress.emit()
    first = (tmp_path / "progress.json").read_text(encoding="utf-8")
    progress.completed = 2
    progress.emit()  # throttled
    assert (tmp_path / "progress.json").read_text(encoding="utf-8") == first
    progress.emit(force=True)
    assert '"completed": 2' in (tmp_path / "progress.json").read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))
```

```python
# tests/contract/test_clone_endpoints.py
def test_clone_progress_caps_rendered_errors(client):
    # seed a _PersistedProgress with 25 errors under the run's bundle dir
    ...
    response = client.get(f"/partials/runs/{run_id}/clone-progress", headers={"HX-Request": "true"})
    assert response.text.count("<li>") <= 20
    assert "5 more failure" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_cloner.py -k persisted_progress -v`
Expected: FAIL — `_PersistedProgress` takes no `now`; every emit rewrites.

- [ ] **Step 3: Implement**

`cloner.py` base class:

```python
    def emit(self, *, force: bool = False) -> None:
        return None
```

Force terminal emissions: in `clone_repos`, change the final `progress.emit()` (status `"done"`) to `progress.emit(force=True)`; in `serve/runs.py::_clone_worker` final `progress.emit()` to `progress.emit(force=True)`; in `start_clone` the zero-limit `progress.emit()` to `force=True`.

`serve/runs.py`:

```python
_EMIT_INTERVAL_SECONDS = 0.25


class _PersistedProgress(CloneProgress):
    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.monotonic,
        emit_interval: float = _EMIT_INTERVAL_SECONDS,
        **values: object,
    ) -> None:
        super().__init__(**values)
        self._path = path
        self._now = now
        self._emit_interval = emit_interval
        self._last_emit = 0.0

    def emit(self, *, force: bool = False) -> None:
        moment = self._now()
        if not force and moment - self._last_emit < self._emit_interval:
            return
        self._last_emit = moment
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self._path)
```

`read_clone_progress` parsed branch:

```python
            return CloneProgress(
                status=str(data.get("status", "done")),
                total=int(data.get("total", 0)),
                completed=int(data.get("completed", 0)),
                failed=int(data.get("failed", 0)),
                current=data.get("current"),
                errors=[str(item) for item in errors] if isinstance(errors, list) else [],
                error_count=int(data.get("error_count", len(errors) if isinstance(errors, list) else 0)),
            )
```

`clone_progress.html` list block:

```html
  {% if progress.error_count %}
  <ul id="clone-progress-errors" class="warnings">
    {% for error in progress.errors %}
    <li>{{ error }}</li>
    {% endfor %}
    {% if progress.error_count > progress.errors|length %}
    <li id="clone-progress-errors-more" class="muted">and {{ progress.error_count - progress.errors|length }} more failure(s)</li>
    {% endif %}
  </ul>
  {% endif %}
```

Add the `--progress` property to the bar (CSS arrives in Task 3, the attribute can land now): `style="--progress: {{ ... }}"` replacing `width:`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_cloner.py tests/contract/test_clone_endpoints.py -q`
Expected: PASS. If existing tests assert `width: …%`, update them to the `--progress` value.

- [ ] **Step 5: Commit**

```bash
git add src/enrich/cloner.py src/serve/runs.py src/serve/templates/partials/clone_progress.html tests/unit/test_cloner.py tests/contract/test_clone_endpoints.py
git commit -m "perf: cap and throttle clone progress persistence and rendering"
```

---

### Task 2: Pause polling in hidden tabs

**Files:**
- Modify: `src/serve/templates/partials/status.html:1`, `src/serve/templates/partials/clone_progress.html:2`
- Test: `tests/contract/test_run_detail.py`, `tests/contract/test_clone_endpoints.py`

**Interfaces:** triggers become `every 2s [document.visibilityState === 'visible']` and `every 1s [document.visibilityState === 'visible']`.

- [ ] **Step 1: Write the failing assertions**

In `tests/contract/test_run_detail.py` find the polling assertion (currently matching `every 2s`) and change it to:

```python
    assert "every 2s [document.visibilityState === 'visible']" in response.text
```

In `tests/contract/test_clone_endpoints.py`:

```python
    assert "every 1s [document.visibilityState === 'visible']" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/contract/test_run_detail.py tests/contract/test_clone_endpoints.py -q`
Expected: FAIL — plain `every 2s` / `every 1s`.

- [ ] **Step 3: Implement**

Edit the two templates' `hx-trigger` attributes to include the filter. No other markup changes.

- [ ] **Step 4: Run tests**

Run: `pytest tests/contract/test_run_detail.py tests/contract/test_clone_endpoints.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve/templates/partials/status.html src/serve/templates/partials/clone_progress.html tests/contract/test_run_detail.py tests/contract/test_clone_endpoints.py
git commit -m "perf: pause htmx polling while the tab is hidden"
```

---

### Task 3: Static cache headers, no template auto-reload, compositor-only progress bar

**Files:**
- Modify: `src/serve/app.py` (static mount), `src/serve/pages.py:56`, `src/serve/static/app.css:325-394`
- Test: `tests/contract/test_pages.py`, `tests/contract/test_console_pages.py`

**Interfaces:** `/static/*` responses carry `Cache-Control: public, max-age=3600`; templates are compiled once (`auto_reload=False`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/contract/test_pages.py
def test_static_assets_are_cacheable(client):
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
```

```python
# tests/contract/test_console_pages.py
def test_app_css_uses_compositor_progress_and_row_containment():
    from pathlib import Path

    css = Path("src/serve/static/app.css").read_text(encoding="utf-8")
    assert "transform: scaleX(var(--progress, 0))" in css
    assert "content-visibility: auto" in css
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/contract/test_pages.py -k cacheable tests/contract/test_console_pages.py -k compositor -v`
Expected: FAIL — no `Cache-Control`; CSS uses `width`.

- [ ] **Step 3: Implement**

`app.py` (where `StaticFiles` is mounted; find with `rg -n "StaticFiles" src/serve/app.py`):

```python
from starlette.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        return response
```

Mount with `CachedStaticFiles(directory=...)`.

`pages.py:56`: `_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR), auto_reload=False)`.

`app.css`:

```css
.progress-bar {
  height: 100%;
  background: var(--gc-accent);
  width: 100%;
  transform-origin: left center;
  transform: scaleX(var(--progress, 0));
  transition: transform 0.2s ease;
}

.diff-section .data-table tbody tr,
#run-table tbody tr {
  content-visibility: auto;
  contain-intrinsic-size: 1.9rem;
}

.button.primary:hover {
  background: color-mix(in srgb, var(--gc-accent) 92%, white);
}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/contract/test_pages.py tests/contract/test_console_pages.py tests/contract/test_run_detail.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/serve/app.py src/serve/pages.py src/serve/static/app.css tests/contract/test_pages.py tests/contract/test_console_pages.py
git commit -m "perf: cache static assets and move progress animation off layout"
```

---

### Task 4: O(1) row navigation + clone double-submit guard

**Files:**
- Create: `src/serve/static/rownav.js`, `tests/js/rownav.test.mjs`, `tests/unit/test_rownav_js.py`
- Modify: `src/serve/static/app.js:89-120,294-320`, `src/serve/templates/base.html:22-23`
- Test: Node test via pytest wrapper

**Interfaces:**
- `gitcrawlRowNav.nextIndex(current: number, delta: number, length: number) -> number` (returns `-1` when `length <= 0`).
- `app.js` caches `selectableRows()` until an htmx swap, removes `row-selected` from only the previously selected row, and disables the clone start button while its POST is in flight.

- [ ] **Step 1: Write the failing tests**

```javascript
// tests/js/rownav.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import rownav from "../../src/serve/static/rownav.js";

test("nextIndex starts at either end depending on direction", () => {
  assert.equal(rownav.nextIndex(-1, 1, 5), 0);
  assert.equal(rownav.nextIndex(-1, -1, 5), 4);
});

test("nextIndex clamps at both ends", () => {
  assert.equal(rownav.nextIndex(0, -1, 5), 0);
  assert.equal(rownav.nextIndex(4, 1, 5), 4);
});

test("nextIndex returns -1 for an empty list", () => {
  assert.equal(rownav.nextIndex(-1, 1, 0), -1);
});
```

```python
# tests/unit/test_rownav_js.py
from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_rownav_module():
    result = subprocess.run(
        ["node", "--test", "tests/js/rownav.test.mjs"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_rownav_js.py -v`
Expected: FAIL — `rownav.js` missing (or Node reports module not found).

- [ ] **Step 3: Implement**

```javascript
// src/serve/static/rownav.js
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.gitcrawlRowNav = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function nextIndex(current, delta, length) {
    if (length <= 0) {
      return -1;
    }
    if (current < 0) {
      return delta > 0 ? 0 : length - 1;
    }
    return Math.min(Math.max(current + delta, 0), length - 1);
  }

  return { nextIndex: nextIndex };
});
```

`base.html`: add `<script src="/static/rownav.js" defer></script>` before `app.js`.

`app.js`:

```javascript
  var rowCache = null;

  function invalidateRowCache() {
    rowCache = null;
  }

  function selectableRows() {
    if (rowCache) {
      return rowCache;
    }
    rowCache = Array.prototype.slice.call(document.querySelectorAll("tbody tr")).filter(function (row) {
      return !row.hidden && row.querySelector("a[href]");
    });
    return rowCache;
  }

  function selectRow(delta) {
    var rows = selectableRows();
    if (!rows.length) {
      return;
    }
    var current = -1;
    for (var index = 0; index < rows.length; index += 1) {
      if (rows[index].classList.contains("row-selected")) {
        current = index;
        break;
      }
    }
    var next = window.gitcrawlRowNav.nextIndex(current, delta, rows.length);
    var previous = document.querySelector("tr.row-selected");
    if (previous) {
      previous.classList.remove("row-selected");
    }
    rows[next].classList.add("row-selected");
    if (rows[next].scrollIntoView) {
      rows[next].scrollIntoView({ block: "nearest" });
    }
  }
```

In the `DOMContentLoaded` handler, add `document.body.addEventListener("htmx:afterSwap", invalidateRowCache);`.

`startClone(button)`: first lines become

```javascript
  function startClone(button) {
    if (button.disabled) {
      return;
    }
    button.disabled = true;
    ...
      .finally(function () {
        button.disabled = false;
      });
```

(attach `.finally` to the existing `fetch(...).then(...).catch(...)` chain).

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_rownav_js.py -q`
Expected: PASS (or SKIP when Node is absent). Manually verify `j`/`k` on a run table in the browser (step is not CI-covered).

- [ ] **Step 5: Commit**

```bash
git add src/serve/static/rownav.js src/serve/static/app.js src/serve/templates/base.html tests/js/rownav.test.mjs tests/unit/test_rownav_js.py
git commit -m "perf: cache row navigation and guard duplicate clone starts"
```

---

## Plan self-review

- **Spec coverage:** S10/#40 → T1 + T3; S14/#39 → T2; #36/#37/#49 → T3; #38/#41 → T4.
- **Placeholders:** none; the one manual browser check is explicitly labeled non-CI.
- **Type consistency:** `emit(force=)` is keyword-only across base and subclass; `error_count` is an int on `CloneProgress` and defaulted when reading old progress files; `rownav.nextIndex` handles `current=-1` and `length=0`.
- **Dependency note:** T1 consumes `error_count` from the enrich plan Task 4 and the capped `errors` list; if the enrich plan has not run, T1 still works, with `error_count` defaulting to the file's existing list length.
