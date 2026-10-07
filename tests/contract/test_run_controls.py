from __future__ import annotations

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import create_run
from serve.pages import CSRF_COOKIE

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db()


def make_client(engine, tmp_path) -> TestClient:
    application = create_app(
        engine=engine,
        runs_root=str(tmp_path),
        redis_ping=lambda: True,
        token_present=lambda: True,
    )
    return TestClient(application, raise_server_exceptions=False)


def csrf_token(client) -> str:
    client.get("/")
    return client.cookies.get(CSRF_COOKIE)


def test_cancel_queued_run_marks_it_stopped(clean, tmp_path):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    client = make_client(clean, tmp_path)
    token = csrf_token(client)

    response = client.post(
        f"/runs/{run_id}/cancel", headers={"x-csrf-token": token}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/runs/{run_id}"
    with clean.connect() as connection:
        row = connection.execute(
            text("SELECT status, finished_at FROM runs WHERE id = :id"), {"id": run_id}
        ).one()
    assert row[0] == "cancelled"
    assert row[1] is not None


def test_cancel_terminal_run_is_400(clean, tmp_path):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    with clean.begin() as connection:
        connection.execute(text("UPDATE runs SET status='done' WHERE id = :id"), {"id": run_id})
    client = make_client(clean, tmp_path)
    token = csrf_token(client)

    response = client.post(f"/runs/{run_id}/cancel", headers={"x-csrf-token": token})

    assert response.status_code == 400


def test_cancel_requires_csrf(clean, tmp_path):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    client = make_client(clean, tmp_path)

    assert client.post(f"/runs/{run_id}/cancel").status_code == 403


def test_save_filter_stores_and_reports_duplicates(clean, tmp_path):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    client = make_client(clean, tmp_path)
    token = csrf_token(client)

    first = client.post(
        f"/runs/{run_id}/save-filter",
        data={"name": "rust picks"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert first.headers["location"] == f"/runs/{run_id}?saved=1"
    with clean.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM saved_filters WHERE name = 'rust picks'")
        ).scalar()
    assert count == 1

    duplicate = client.post(
        f"/runs/{run_id}/save-filter",
        data={"name": "rust picks"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert duplicate.status_code == 303
    assert duplicate.headers["location"] == f"/runs/{run_id}?save_error=duplicate_name"


def test_running_status_partial_shows_progress_clock_eta_and_stop(clean, tmp_path):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    with clean.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET status='running', started_at = now() - interval '1 minute', "
                "total_count=1000, fetched=300, "
                "progress_phase='discovering', progress_done=3, progress_total=10 "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )
    client = make_client(clean, tmp_path)

    html = client.get(f"/partials/runs/{run_id}/status").text
    assert 'id="abort-run"' in html
    assert "Stop search" in html
    assert "3 / 10" in html
    assert "est." in html
    assert "elapsed" in html
    assert "truncated" not in html
    assert "Found <strong>1000</strong>" in html
    assert "Found <strong>300</strong>" in html  # raw fetched stays in technical details

    detail = client.get(f"/runs/{run_id}").text
    assert 'id="save-filter-open"' in detail
    assert 'id="save-filter-modal"' in detail

    with clean.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET status='cancelled', finished_at=now(), progress_phase=NULL "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )
    stopped = client.get(f"/partials/runs/{run_id}/status").text
    assert "Stopped" in stopped
    assert 'id="abort-run"' not in stopped
    assert "resume" in stopped.lower()
    assert "truncated: found 300 of ~1000" in stopped


def test_progress_eta_is_phase_relative_and_hidden_early(clean, tmp_path):
    run_id = create_run(clean, FILTER, api_version=API_VERSION)
    with clean.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET status='running', started_at = now() - interval '30 minutes', "
                "progress_phase='discovering', progress_done=1, progress_total=190, "
                "progress_started_at = now() - interval '30 minutes' WHERE id = :id"
            ),
            {"id": run_id},
        )
    client = make_client(clean, tmp_path)

    early = client.get(f"/partials/runs/{run_id}/status").text
    assert "estimating" in early
    assert "est." not in early

    with clean.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET progress_done=10, "
                "progress_started_at = now() - interval '60 seconds' WHERE id = :id"
            ),
            {"id": run_id},
        )
    later = client.get(f"/partials/runs/{run_id}/status").text
    assert "est. 18m 0s left" in later
