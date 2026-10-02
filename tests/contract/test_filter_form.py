from __future__ import annotations

import json
import re
import threading
import time

import fakeredis
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from lib.gh_client import API_VERSION
from serve.app import create_app
from serve.executor import RunPayload, RunPayloadItem, create_run, execute_run
from serve.filter_spec import FilterSpecError, parse_filter_spec, spec_hash, spec_to_dict
from serve.forms import FormState, build_query_from_form, build_spec_from_form, form_state
from serve.pages import CSRF_COOKIE

SECTION_IDS = (
    'id="filter-keywords"',
    'id="filter-owner"',
    'id="filter-counts"',
    'id="filter-dates"',
    'id="filter-meta"',
    'id="filter-flags"',
    'id="filter-props"',
    'id="filter-virtuals"',
    'id="filter-result"',
    'id="filter-controls"',
)

FIELD_NAMES = (
    "keywords",
    "in_scope",
    "user",
    "org",
    "repo",
    "cmp_stars",
    "val_stars",
    "range_stars_min",
    "range_stars_max",
    "cmp_forks",
    "val_forks",
    "range_forks_min",
    "range_forks_max",
    "cmp_size",
    "val_size",
    "range_size_min",
    "range_size_max",
    "cmp_followers",
    "val_followers",
    "range_followers_min",
    "range_followers_max",
    "cmp_topics",
    "val_topics",
    "range_topics_min",
    "range_topics_max",
    "created_from",
    "created_to",
    "pushed_from",
    "pushed_to",
    "language",
    "topic",
    "license",
    "fork",
    "archived",
    "mirror",
    "template",
    "visibility",
    "sponsorable",
    "funding_file",
    "good_first_min",
    "help_wanted_min",
    "props_name",
    "props_value",
    "min_stars",
    "team_topic",
    "has_dockerfile",
    "owner_country",
    "min_geo_confidence",
    "min_commits",
    "max_commits",
    "min_loc",
    "max_loc",
    "sort",
    "order",
    "per_page",
    "max_pages",
    "action",
    "spec_file",
)

RICH_FORM: dict[str, str] = {
    "keywords": "tetris assembly",
    "in_scope": "description,name",
    "user": "defunkt",
    "org": "github",
    "repo": "octocat/hello-world",
    "cmp_stars": ">=",
    "val_stars": "500",
    "cmp_forks": "range",
    "range_forks_min": "10",
    "range_forks_max": "20",
    "cmp_size": ">",
    "val_size": "30000",
    "cmp_followers": "<=",
    "val_followers": "100",
    "cmp_topics": "eq",
    "val_topics": "3",
    "created_from": "2011-01-01",
    "created_to": "2011-02-01",
    "pushed_from": "2013-02-01",
    "language": "javascript",
    "topic": "jekyll",
    "license": "apache-2.0",
    "fork": "only",
    "archived": "false",
    "mirror": "true",
    "template": "true",
    "visibility": "public",
    "sponsorable": "true",
    "funding_file": "true",
    "good_first_min": "2",
    "help_wanted_min": "5",
    "props_name": "environment",
    "props_value": "production",
    "min_stars": "50",
    "team_topic": "rust",
    "has_dockerfile": "true",
    "owner_country": "DE",
    "min_geo_confidence": "name",
    "min_commits": "100",
    "min_loc": "5000",
    "sort": "stars",
    "order": "desc",
    "per_page": "100",
    "max_pages": "10",
}

RICH_Q = (
    "tetris assembly in:name,description user:defunkt org:github repo:octocat/hello-world "
    "stars:>=500 forks:10..20 size:>30000 followers:<=100 topics:3 "
    "created:2011-01-01..2011-02-01 pushed:>=2013-02-01 "
    "language:javascript topic:jekyll license:apache-2.0 fork:only archived:false "
    "mirror:true template:true is:public is:sponsorable has:funding-file "
    "good-first-issues:>=2 help-wanted-issues:>=5 props.environment:production"
)


def rich_spec() -> dict:
    return {
        "gitcrawl_filter": 1,
        "q": RICH_Q,
        "sort": "stars",
        "order": "desc",
        "virtual": {
            "min_stars": 50,
            "team_topic": "rust",
            "has_dockerfile": True,
            "owner_country": "DE",
            "min_geo_confidence": "name",
            "min_commits": 100,
            "min_loc": 5000,
        },
        "page": {"per_page": 100, "max_pages": 10},
    }


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(clean_db):
    return clean_db(
        owners=[
            {
                "id": 1,
                "login": "octo",
                "type": "User",
                "location_raw": "Berlin, Germany",
                "country_iso": "DE",
                "geo_confidence": "name",
            }
        ],
        repos=[
            {
                "id": 1296269,
                "node_id": "R_1296269",
                "full_name": "octo/hello",
                "owner_id": 1,
                "name": "hello",
                "visibility": "public",
                "description": "My first repo",
                "language": "Ruby",
                "license_spdx": "MIT",
                "topics": ["octocat"],
                "stargazers": 80,
                "forks_count": 9,
                "open_issues": 0,
                "pushed_at": "2011-01-26T19:06:43Z",
            }
        ],
    )


def payload_item(**overrides) -> RunPayloadItem:
    values: dict[str, object] = {
        "repo_id": 1296269,
        "full_name": "octo/hello",
        "stargazers": 80,
        "pushed_at": "2011-01-26T19:06:43Z",
        "archived": False,
        "language": "Ruby",
        "license_spdx": "MIT",
        "country_iso": "DE",
        "geo_confidence": "name",
        "virtuals": {"has_dockerfile": True},
    }
    values.update(overrides)
    return RunPayloadItem(**values)


def make_client(engine: Engine, tmp_path, *, calls: list | None = None, runner=None) -> TestClient:
    captured = calls if calls is not None else []
    if runner is None:

        def runner(run_id: int, filter_spec: dict) -> RunPayload:
            captured.append(filter_spec)
            return RunPayload(total_count=1, fetched=1, items=[payload_item()])

    application = create_app(
        engine=engine,
        runner_factory=lambda _engine: runner,
        runs_root=str(tmp_path),
        redis_ping=fakeredis.FakeRedis().ping,
        token_present=lambda: True,
    )
    return TestClient(application, raise_server_exceptions=False, follow_redirects=False)


def wait_for_run(engine: Engine, run_id: int, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            status = connection.scalar(
                text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}
            )
        if status in ("done", "partial", "failed"):
            return str(status)
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach a terminal status in {timeout}s")


def csrf_token(client: TestClient) -> str:
    client.get("/find")
    token = client.cookies.get(CSRF_COOKIE)
    assert token
    return token


def seed_run(engine: Engine, tmp_path) -> tuple[int, str]:
    filter_spec = {"gitcrawl_filter": 1, "q": "language:rust"}
    run_id = create_run(engine, filter_spec, api_version=API_VERSION)
    payload = RunPayload(total_count=1, fetched=1, items=[payload_item()])
    execute_run(engine, run_id, runner=lambda _rid, _spec: payload, runs_root=str(tmp_path))
    with engine.connect() as connection:
        filter_hash = connection.scalar(
            text("SELECT filter_hash FROM runs WHERE id = :id"), {"id": run_id}
        )
    return run_id, str(filter_hash)


def test_empty_form_builds_a_minimal_spec():
    assert build_query_from_form({}) == ""
    assert build_spec_from_form({}) == {
        "gitcrawl_filter": 1,
        "q": "",
        "page": {"per_page": 20, "max_pages": 3},
    }


def test_rich_form_matches_the_equivalent_literal_spec():
    assert build_query_from_form(RICH_FORM) == RICH_Q
    assert build_spec_from_form(RICH_FORM) == rich_spec()


def test_query_order_is_deterministic_regardless_of_field_order():
    reversed_form = dict(reversed(list(RICH_FORM.items())))

    assert build_query_from_form(reversed_form) == RICH_Q


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"cmp_stars": ">", "val_stars": "10"}, "stars:>10"),
        ({"cmp_stars": ">=", "val_stars": "10"}, "stars:>=10"),
        ({"cmp_stars": "<", "val_stars": "10"}, "stars:<10"),
        ({"cmp_stars": "<=", "val_stars": "10"}, "stars:<=10"),
        ({"cmp_stars": "=", "val_stars": "10"}, "stars:10"),
        ({"cmp_stars": "eq", "val_stars": "10"}, "stars:10"),
        (
            {"cmp_stars": "range", "range_stars_min": "10", "range_stars_max": "20"},
            "stars:10..20",
        ),
        ({"cmp_stars": "range", "range_stars_min": "10"}, "stars:10..*"),
        ({"cmp_stars": "range", "range_stars_max": "20"}, "stars:*..20"),
        ({"cmp_stars": "range"}, ""),
        ({"cmp_stars": ">", "val_stars": ""}, ""),
        ({}, ""),
    ],
)
def test_count_comparators_and_ranges(fields, fragment):
    assert build_query_from_form(fields) == fragment


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"created_from": "2011-01-01"}, "created:>=2011-01-01"),
        ({"created_to": "2011-02-01"}, "created:<=2011-02-01"),
        (
            {"created_from": "2011-01-01", "created_to": "2011-02-01"},
            "created:2011-01-01..2011-02-01",
        ),
        ({"pushed_from": "2013-02-01"}, "pushed:>=2013-02-01"),
        ({"pushed_to": "2013-03-01"}, "pushed:<=2013-03-01"),
        ({"pushed_from": "2013-02-01", "pushed_to": "2013-03-01"}, "pushed:2013-02-01..2013-03-01"),
    ],
)
def test_date_bounds(fields, fragment):
    assert build_query_from_form(fields) == fragment


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"fork": "include"}, "fork:true"),
        ({"fork": "only"}, "fork:only"),
        ({"fork": "omit"}, ""),
        ({"archived": "true"}, "archived:true"),
        ({"archived": "false"}, "archived:false"),
        ({"archived": "any"}, ""),
        ({"mirror": "true"}, "mirror:true"),
        ({"template": "false"}, "template:false"),
        ({"visibility": "public"}, "is:public"),
        ({"visibility": "private"}, "is:private"),
        ({"visibility": "internal"}, "is:internal"),
        ({"sponsorable": "true"}, "is:sponsorable"),
        ({"sponsorable": "false"}, "-is:sponsorable"),
        ({"funding_file": "true"}, "has:funding-file"),
        ({"funding_file": "false"}, "-has:funding-file"),
        ({"good_first_min": "2"}, "good-first-issues:>=2"),
        ({"help_wanted_min": "5"}, "help-wanted-issues:>=5"),
        ({"good_first_min": ""}, ""),
    ],
)
def test_flag_fields(fields, fragment):
    assert build_query_from_form(fields) == fragment


def test_virtuals_are_typed_and_empty_values_are_omitted():
    spec = build_spec_from_form(
        {
            "min_stars": "50",
            "min_commits": "100",
            "min_loc": "5000",
            "team_topic": "Rust",
            "has_dockerfile": "false",
            "owner_country": "de",
            "min_geo_confidence": "name",
        }
    )

    assert spec["virtual"] == {
        "min_stars": 50,
        "min_commits": 100,
        "min_loc": 5000,
        "team_topic": "Rust",
        "has_dockerfile": False,
        "owner_country": "de",
        "min_geo_confidence": "name",
    }
    assert "virtual" not in build_spec_from_form({"has_dockerfile": "any", "min_stars": ""})


def test_virtuals_normalize_through_parse_filter_spec():
    spec = parse_filter_spec(build_spec_from_form({"owner_country": "de"}))

    assert spec.virtual["owner_country"] == "DE"
    assert spec.virtual["min_geo_confidence"] == "gazetteer-city"


def test_props_require_a_single_org_scope():
    with pytest.raises(FilterSpecError) as excinfo:
        parse_filter_spec(
            build_spec_from_form({"props_name": "environment", "props_value": "prod"})
        )

    assert any("org" in hint for hint in excinfo.value.hints)
    parsed = parse_filter_spec(
        build_spec_from_form(
            {"org": "github", "props_name": "environment", "props_value": "production"}
        )
    )
    assert parsed.q == "org:github props.environment:production"


def test_form_state_echoes_fields_and_gates_props():
    assert form_state({}) == FormState(fields={}, props_enabled=False)
    assert form_state({"org": "github"}).props_enabled is True
    assert form_state({"org": ""}).props_enabled is False
    assert form_state({"org": "github gitlab"}).props_enabled is False
    assert form_state({"keywords": "rust"}).fields["keywords"] == "rust"


def test_get_find_renders_every_section_and_field(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    page = client.get("/find")
    alias = client.get("/vsearch/")

    assert page.status_code == alias.status_code == 200
    for response in (page, alias):
        html = response.text
        for element_id in SECTION_IDS:
            assert element_id in html
        for name in FIELD_NAMES:
            assert f'name="{name}"' in html
        assert 'data-props-enabled="false"' in html
        assert re.search(r'<fieldset[^>]*id="filter-props"[^>]* disabled', html)


def test_get_find_enables_props_with_a_single_org(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    without = client.get("/find")
    with_org = client.get("/find", params={"org": "github"})
    with_many = client.get("/find", params={"org": "github gitlab"})

    assert 'data-props-enabled="false"' in without.text
    assert 'data-props-enabled="true"' in with_org.text
    assert not re.search(r'<fieldset[^>]*id="filter-props"[^>]* disabled', with_org.text)
    assert 'data-props-enabled="false"' in with_many.text


def test_find_page_renders_a_no_js_form(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    html = client.get("/find").text

    assert 'id="find-form"' in html
    assert 'action="/find"' in html
    assert 'method="post"' in html
    assert 'name="csrf"' in html
    assert 'type="file"' in html
    assert f'name="csrf-token" content="{client.cookies.get(CSRF_COOKIE)}"' in html


def test_download_returns_the_attachment_spec_with_the_upload_door_hash(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(clean, tmp_path, calls=calls)
    token = csrf_token(client)

    download = client.post("/find", data={**RICH_FORM, "action": "download", "csrf": token})
    downloads_ran_nothing = list(calls)
    uploaded = client.post("/vsearch/run", json=rich_spec())

    assert download.status_code == 200
    assert download.headers["content-type"] == "application/json"
    assert "attachment" in download.headers["content-disposition"]
    assert download.json() == rich_spec()
    assert downloads_ran_nothing == []
    assert uploaded.status_code == 200
    assert uploaded.json()["filter_hash"] == spec_hash(parse_filter_spec(download.json()))


def test_find_action_creates_a_run_and_redirects_to_it(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(clean, tmp_path, calls=calls)
    token = csrf_token(client)

    response = client.post(
        "/find", data={"csrf": token, "action": "find", "keywords": "language:rust"}
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert re.fullmatch(r"/runs/\d+", location)
    run_id = int(location.rsplit("/", 1)[1])
    assert wait_for_run(clean, run_id) == "done"
    assert client.get(location).status_code == 200
    with clean.connect() as connection:
        row = connection.execute(
            text("SELECT filter_hash, status FROM runs WHERE id = :id"), {"id": run_id}
        ).one()
    expected = spec_hash(parse_filter_spec(build_spec_from_form({"keywords": "language:rust"})))
    assert row == (expected, "done")
    assert calls == [
        spec_to_dict(
            parse_filter_spec(
                {
                    "gitcrawl_filter": 1,
                    "q": "language:rust",
                    "page": {"per_page": 20, "max_pages": 3},
                }
            )
        )
    ]


def test_find_redirects_before_the_run_finishes(clean: Engine, tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocking(_run_id: int, _spec: dict) -> RunPayload:
        started.set()
        assert release.wait(10)
        return RunPayload(total_count=1, fetched=1, items=[payload_item()])

    client = make_client(clean, tmp_path, runner=blocking)
    token = csrf_token(client)

    response = client.post(
        "/find", data={"csrf": token, "action": "find", "keywords": "language:rust"}
    )

    assert response.status_code == 303
    assert release.is_set() is False
    assert started.wait(10)
    run_id = int(response.headers["location"].rsplit("/", 1)[1])
    with clean.connect() as connection:
        status = connection.scalar(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id})
    assert status in ("queued", "running")
    release.set()
    assert wait_for_run(clean, run_id) == "done"


def test_upload_action_runs_the_uploaded_spec(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)
    token = csrf_token(client)

    response = client.post(
        "/find",
        data={"csrf": token, "action": "upload"},
        files={
            "spec_file": (
                "spec.json",
                json.dumps({"gitcrawl_filter": 1, "q": "language:rust"}).encode(),
                "application/json",
            )
        },
    )

    assert response.status_code == 303
    assert re.fullmatch(r"/runs/\d+", response.headers["location"])
    run_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert wait_for_run(clean, run_id) == "done"


def test_upload_action_rejects_a_broken_file_and_missing_file(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)
    token = csrf_token(client)

    broken = client.post(
        "/find",
        data={"csrf": token, "action": "upload"},
        files={"spec_file": ("spec.json", b"{not json", "application/json")},
    )
    missing = client.post("/find", data={"csrf": token, "action": "upload"})

    assert broken.status_code == 400
    assert 'id="filter-errors"' in broken.text
    assert missing.status_code == 400
    assert 'id="filter-errors"' in missing.text


def test_validation_failure_rerenders_the_form_with_hints(clean: Engine, tmp_path):
    calls: list = []
    client = make_client(clean, tmp_path, calls=calls)
    token = csrf_token(client)

    response = client.post(
        "/find", data={"csrf": token, "action": "find", "keywords": "updated:>2024"}
    )

    assert response.status_code == 400
    assert 'id="filter-errors"' in response.text
    assert "pushed" in response.text
    assert 'value="updated:' in response.text
    assert calls == []
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_props_without_an_org_are_rejected(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)
    token = csrf_token(client)

    response = client.post(
        "/find",
        data={
            "csrf": token,
            "action": "find",
            "props_name": "environment",
            "props_value": "production",
        },
    )

    assert response.status_code == 400
    assert "org" in response.text


def test_posts_without_csrf_are_rejected(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)
    client.get("/find")

    response = client.post("/find", data={"action": "find", "keywords": "language:rust"})

    assert response.status_code == 403


def test_run_page_renders_banner_rows_and_links(clean: Engine, tmp_path):
    run_id, filter_hash = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)

    response = client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    html = response.text
    assert 'id="run-header"' in html
    assert 'id="run-status-pill"' in html
    assert "done" in html
    assert 'id="run-items"' in html
    assert "octo/hello" in html
    assert f"/runs/{run_id}/export?format=json" in html
    assert f"/runs/{run_id}/export?format=csv" in html
    assert f'action="/runs/{run_id}/replay"' in html
    assert re.search(rf'<span id="run-hash"[^>]*>{filter_hash[:8]}</span>', html)


def test_run_page_unknown_is_404(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)

    response = client.get("/runs/999999")

    assert response.status_code == 404


def test_replay_creates_a_new_run_and_redirects_back(clean: Engine, tmp_path):
    calls: list = []
    run_id, _filter_hash = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path, calls=calls)
    token = csrf_token(client)

    response = client.post(f"/runs/{run_id}/replay", data={"csrf": token})

    assert response.status_code == 303
    location = response.headers["location"]
    assert re.fullmatch(r"/runs/\d+", location)
    new_id = int(location.rsplit("/", 1)[1])
    assert new_id != run_id
    assert wait_for_run(clean, new_id) == "done"
    assert client.get(location).status_code == 200
    assert len(calls) == 1
    with clean.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 2


def test_replay_requires_csrf_and_rejects_unknown_runs(clean: Engine, tmp_path):
    run_id, _filter_hash = seed_run(clean, tmp_path)
    client = make_client(clean, tmp_path)
    client.get(f"/runs/{run_id}")

    without = client.post(f"/runs/{run_id}/replay")
    unknown = client.post("/runs/999999/replay", data={"csrf": client.cookies.get(CSRF_COOKIE)})

    assert without.status_code == 403
    assert unknown.status_code == 404


def test_recent_filters_quick_pick_is_opt_in_and_empty_by_default(clean: Engine, tmp_path):
    client = make_client(clean, tmp_path)
    html = client.get("/find").text

    assert 'id="recent-filters"' in html
    assert re.search(r'<div id="recent-filters"[^>]* hidden', html)
    assert 'id="recent-filters-list"' in html
    assert "gc-recent-filters" not in html

    script = client.get("/static/app.js").text
    assert "gc-recent-filters" in script
    assert "renderRecentFilters" in script
    helper = client.get("/static/recentfilters.js")
    assert helper.status_code == 200
