from __future__ import annotations

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.corpora import CorpusError, freeze_corpus, get_corpus, list_corpora
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run

FILTER = {"gitcrawl_filter": 1, "q": "language:rust"}
DETECT_COPY = (
    "Scans the frozen corpus through GitHub’s API on its own allowance "
    "and stores evidence per repo."
)


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[{"id": 1, "login": "octo", "type": "User"}],
        repos=[
            {
                "id": 101,
                "node_id": "R_101",
                "full_name": "octo/alpha",
                "owner_id": 1,
                "name": "alpha",
                "visibility": "public",
                "stargazers": 30,
            },
            {
                "id": 102,
                "node_id": "R_102",
                "full_name": "octo/beta",
                "owner_id": 1,
                "name": "beta",
                "visibility": "public",
                "stargazers": 20,
            },
        ],
    )


def payload_item(repo_id: int, full_name: str, stargazers: int) -> RunPayloadItem:
    return RunPayloadItem(
        repo_id=repo_id,
        full_name=full_name,
        stargazers=stargazers,
        pushed_at="2026-09-30T12:00:00Z",
        archived=False,
        language="Rust",
        license_spdx="MIT",
        country_iso="DE",
        geo_confidence="name",
        virtuals={},
    )


def seed_run(engine: Engine, tmp_path, *, incomplete: bool = False) -> tuple[int, str]:
    run_id = create_run(engine, FILTER, api_version=API_VERSION)
    payload = RunPayload(
        total_count=2,
        fetched=2,
        incomplete=incomplete,
        items=[payload_item(101, "octo/alpha", 30), payload_item(102, "octo/beta", 20)],
    )
    execute_run(
        engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path / "runs")
    )
    with engine.connect() as connection:
        filter_hash = connection.scalar(
            text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id}
        )
    return run_id, str(filter_hash)


def _forbidden_runner(_run_id: int, _spec: dict) -> RunPayload:
    raise AssertionError("freezing must not execute a run")


def make_client(engine: Engine, tmp_path, **overrides) -> TestClient:
    options = {
        "runs_root": str(tmp_path / "runs"),
        "clone_root": str(tmp_path / "clones"),
        "runner_factory": lambda _engine: _forbidden_runner,
    }
    options.update(overrides)
    application = create_app(engine=engine, **options)
    return TestClient(application, raise_server_exceptions=False)


def healthy_client(engine: Engine, tmp_path, monkeypatch, **overrides) -> TestClient:
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-token-value")
    monkeypatch.delenv("GITHUB_TOKENS", raising=False)
    return make_client(engine, tmp_path, **overrides)


def csrf_token(client: TestClient) -> str:
    response = client.get("/")
    marker = 'name="csrf-token" content="'
    start = response.text.index(marker) + len(marker)
    return response.text[start : response.text.index('"', start)]


def freeze(client: TestClient, run_id: int, **data: str):
    token = csrf_token(client)
    return client.post(
        f"/runs/{run_id}/corpus",
        data=data,
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )


def corpus_rows(engine: Engine) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT id, name, source_run_id, note, repo_count FROM corpora ORDER BY id")
            ).mappings()
        ]


def test_freeze_creates_corpus_and_redirects(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    token = csrf_token(client)
    response = client.post(
        f"/runs/{run_id}/corpus",
        data={"name": "rust-2026-10", "note": "thesis frame"},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/corpora/")
    corpus_id = int(response.headers["location"].rsplit("/", 1)[1])
    rows = corpus_rows(clean)
    assert rows == [
        {
            "id": corpus_id,
            "name": "rust-2026-10",
            "source_run_id": run_id,
            "note": "thesis frame",
            "repo_count": 2,
        }
    ]


def test_freeze_requires_a_finished_search(clean, tmp_path, monkeypatch):
    run_id = create_run(
        clean, {"gitcrawl_filter": 1, "q": "language:rust"}, api_version=API_VERSION
    )
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.post(
        f"/runs/{run_id}/corpus",
        data={"name": "x"},
        headers={"x-csrf-token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "finished" in response.text.lower()
    assert corpus_rows(clean) == []


def test_freeze_from_a_partial_search_is_allowed(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path, incomplete=True)
    response = freeze(healthy_client(clean, tmp_path, monkeypatch), run_id, name="partial-ok")
    assert response.status_code == 303
    assert [row["repo_count"] for row in corpus_rows(clean)] == [2]


def test_freeze_without_a_name_is_a_local_400(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    response = freeze(healthy_client(clean, tmp_path, monkeypatch), run_id, name="   ")
    assert response.status_code == 400
    assert "name" in response.text.lower()
    assert corpus_rows(clean) == []


def test_freeze_duplicate_name_is_a_local_400_with_a_hint(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    assert freeze(client, run_id, name="same-name").status_code == 303
    response = freeze(client, run_id, name="same-name")
    assert response.status_code == 400
    assert "already exists" in response.text
    assert "different name" in response.text
    assert len(corpus_rows(clean)) == 1


def test_freeze_unknown_run_is_404(clean, tmp_path, monkeypatch):
    response = freeze(healthy_client(clean, tmp_path, monkeypatch), 424242, name="nope")
    assert response.status_code == 404
    assert "does not exist" in response.text.lower()


def test_freeze_missing_csrf_is_403(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.post(f"/runs/{run_id}/corpus", data={"name": "x"}, follow_redirects=False)
    assert response.status_code == 403
    assert corpus_rows(clean) == []


def test_corpora_page_explains_what_a_corpus_is(clean, tmp_path, monkeypatch):
    body = healthy_client(clean, tmp_path, monkeypatch).get("/corpora").text
    assert "frozen snapshot of a finished search" in body


def test_corpora_page_lists_name_repos_frozen_source_and_greyed_detect(
    clean, tmp_path, monkeypatch
):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    assert freeze(client, run_id, name="rust frame").status_code == 303

    response = client.get("/corpora")

    assert response.status_code == 200
    body = response.text
    assert 'id="corpora-table"' in body
    assert "rust frame" in body
    assert f'href="/runs/{run_id}"' in body
    assert "Not yet" in body
    assert 'id="corpora-detect' in body or "Detect" in body
    assert "disabled" in body
    assert DETECT_COPY in body


def test_corpora_page_empty_state_offers_a_next_step(clean, tmp_path, monkeypatch):
    body = healthy_client(clean, tmp_path, monkeypatch).get("/corpora").text
    assert 'id="corpora-empty"' in body
    assert "No corpora yet" in body
    assert 'href="/runs"' in body


def test_corpus_detail_explains_frozen_semantics_and_actions(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    location = freeze(client, run_id, name="thesis frame", note="keep this").headers["location"]

    response = client.get(location)

    assert response.status_code == 200
    body = response.text
    assert "thesis frame" in body
    assert "keep this" in body
    assert "later changes on GitHub do not alter this corpus" in body
    assert f'href="/runs/{run_id}/results"' in body
    assert f'href="/runs/{run_id}/export?format=json"' in body
    assert f'href="/runs/{run_id}/export?format=csv"' in body
    assert DETECT_COPY in body
    assert "disabled" in body
    assert "never affects its source search" in body
    assert "octo/alpha" in body


def test_corpus_detail_hides_raw_internal_names(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    location = freeze(client, run_id, name="clean names").headers["location"]

    body = client.get(location).text

    for internal in ("source_run_id", "repo_count", "frozen_at", "filter_hash", "run_items"):
        assert internal not in body


def test_corpus_detail_unknown_is_404(clean, tmp_path, monkeypatch):
    response = healthy_client(clean, tmp_path, monkeypatch).get("/corpora/424242")
    assert response.status_code == 404
    assert "Corpus not found" in response.text


def test_store_helpers_expose_corpus_views(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    location = freeze(client, run_id, name="view helper").headers["location"]
    corpus_id = int(location.rsplit("/", 1)[1])

    views = list_corpora(clean)

    assert [view.id for view in views] == [corpus_id]
    assert views[0].repo_count == 2
    assert views[0].source_run_id == run_id
    assert "Rust" in views[0].sentence
    assert get_corpus(clean, corpus_id) == views[0]
    assert get_corpus(clean, 424242) is None


def test_freeze_corpus_helper_rejects_an_unknown_run(clean):
    with pytest.raises(CorpusError) as excinfo:
        freeze_corpus(clean, 424242, "nope", None)
    assert excinfo.value.code == "not_found"


def test_corpora_page_degrades_when_the_database_is_unavailable():
    broken = create_engine("postgresql+psycopg://nobody@127.0.0.1:1/nope")
    client = TestClient(create_app(engine=broken), raise_server_exceptions=False)

    response = client.get("/corpora")

    assert response.status_code == 200
    assert "unavailable" in response.text
    assert "database did not answer" in response.text


def test_delete_corpus_removes_it_and_leaves_the_source_search(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    location = freeze(client, run_id, name="delete me").headers["location"]
    corpus_id = int(location.rsplit("/", 1)[1])
    token = csrf_token(client)

    response = client.post(
        f"/corpora/{corpus_id}/delete",
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/corpora"
    assert corpus_rows(clean) == []
    with clean.connect() as connection:
        remaining = connection.scalar(
            text("SELECT count(*) FROM runs WHERE id = :id"), {"id": run_id}
        )
        items = connection.scalar(
            text("SELECT count(*) FROM run_items WHERE run_id = :id"), {"id": run_id}
        )
    assert (remaining, items) == (1, 2)


def test_delete_corpus_missing_csrf_is_403(clean, tmp_path, monkeypatch):
    run_id, _ = seed_run(clean, tmp_path)
    client = healthy_client(clean, tmp_path, monkeypatch)
    location = freeze(client, run_id, name="keep me").headers["location"]
    corpus_id = int(location.rsplit("/", 1)[1])

    response = client.post(f"/corpora/{corpus_id}/delete", follow_redirects=False)

    assert response.status_code == 403
    assert len(corpus_rows(clean)) == 1


def test_delete_unknown_corpus_is_404(clean, tmp_path, monkeypatch):
    client = healthy_client(clean, tmp_path, monkeypatch)
    response = client.post(
        "/corpora/424242/delete",
        headers={"x-csrf-token": csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert "Corpus not found" in response.text


def test_run_detail_offers_freeze_only_when_finished(clean, tmp_path, monkeypatch):
    done_run, _ = seed_run(clean, tmp_path)
    partial_run, _ = seed_run(clean, tmp_path, incomplete=True)
    queued_run = create_run(
        clean, {"gitcrawl_filter": 1, "q": "language:go"}, api_version=API_VERSION
    )
    client = healthy_client(clean, tmp_path, monkeypatch)

    done = client.get(f"/runs/{done_run}").text
    partial = client.get(f"/runs/{partial_run}").text
    queued = client.get(f"/runs/{queued_run}").text

    for body, run_id in ((done, done_run), (partial, partial_run)):
        assert "Freeze as corpus…" in body
        assert 'id="freeze-modal"' in body
        assert (
            "Records the exact repos and details as of now. No GitHub calls; does not copy code."
            in body
        )
        assert f'action="/runs/{run_id}/corpus"' in body
    assert "Freeze as corpus" not in queued
    assert 'id="freeze-modal"' not in queued
