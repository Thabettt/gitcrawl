from __future__ import annotations

import pytest
from sqlalchemy import text

from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.quality import run_quality

FILTER = {"q": "language:rust"}


def seed_run(clean_db, tmp_path, *, item_count=3, inserted=None, status="done"):
    engine = clean_db(
        owners=[
            {"id": 1, "login": "octo", "type": "User"},
            {"id": 2, "login": "octo2", "type": "User"},
            {"id": 3, "login": "octo3", "type": "User"},
        ],
        repos=[
            {
                "id": i,
                "node_id": f"n{i}",
                "full_name": f"octo/repo{i}",
                "owner_id": i,
                "name": f"repo{i}",
                "visibility": "public",
            }
            for i in (1, 2, 3)
        ],
    )
    run_id = create_run(engine, FILTER, api_version="2022-11-28")
    payload = RunPayload(
        total_count=item_count,
        fetched=item_count,
        items=[
            RunPayloadItem(
                repo_id=i + 1,
                full_name=f"octo/repo{i + 1}",
                stargazers=10 + i,
                language="Rust" if i < item_count - 1 else None,
                license_spdx="MIT" if i < item_count - 1 else None,
                country_iso="DE" if i == 0 else None,
                virtuals={},
            )
            for i in range(item_count)
        ],
    )
    execute_run(engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE runs SET status = :status, inserted = :inserted WHERE id = :id"),
            {
                "status": status,
                "inserted": inserted if inserted is not None else item_count,
                "id": run_id,
            },
        )
    return engine, run_id


def test_clean_run_is_ok(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path)
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    assert report.status == "ok"
    assert {check.name for check in report.checks} >= {
        "count_parity",
        "duplicate_full_names",
        "missing_language",
        "missing_license_spdx",
        "missing_country_iso",
        "bundle",
    }


def test_count_mismatch_warns(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path, inserted=99)
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    check = next(c for c in report.checks if c.name == "count_parity")
    assert check.status == "warn"
    assert report.status in {"warn", "fail"}


def test_duplicate_full_name_fails(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path, item_count=2)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE run_items SET full_name = 'octo/repo1' WHERE run_id = :id"),
            {"id": run_id},
        )
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    check = next(c for c in report.checks if c.name == "duplicate_full_names")
    assert check.status == "fail"
    assert report.status == "fail"


def test_missing_bundle_warns(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path)
    for path in tmp_path.rglob("bundle.json"):
        path.unlink()
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    check = next(c for c in report.checks if c.name == "bundle")
    assert check.status == "warn"


def test_partial_status_is_flagged(clean_db, tmp_path):
    engine, run_id = seed_run(clean_db, tmp_path, status="partial")
    report = run_quality(engine, run_id, runs_root=str(tmp_path))
    check = next(c for c in report.checks if c.name == "incomplete")
    assert check.status == "warn"


def test_unknown_run_raises_keyerror(clean_db, tmp_path):
    engine = clean_db()
    with pytest.raises(KeyError):
        run_quality(engine, 4242, runs_root=str(tmp_path))
