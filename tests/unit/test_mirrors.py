from __future__ import annotations

from urllib.parse import urlparse

import httpx
import pytest

from discover.search_shards import RequestFailed
from enrich.mirrors import (
    MirrorRepo,
    fetch_depsdev_project,
    fetch_ecosystems_repo,
    fetch_scorecard,
)

ECOSYSTEMS_PAYLOAD = {
    "full_name": "octo/hello",
    "stargazers_count": 1234,
    "forks_count": 56,
    "language": "Python",
    "license": "MIT",
    "topics": ["ai", "agents"],
    "pushed_at": "2026-01-02T03:04:05.000Z",
    "metadata": {
        "funding": {"github": ["octo"]},
        "readme": "docs",
        "codeowners": ["* @octo"],
        "security": {"policy": True},
    },
    "extra": "ignored",
}

DEPSDEV_PAYLOAD = {
    "projectKey": {"id": "github.com/octo/hello"},
    "scorecard": {"overallScore": 7.5},
    "versions": [{"versionKey": {"name": "v1.0.0"}}],
}

SCORECARD_PAYLOAD = {"score": 8.1, "checks": [{"name": "Maintained", "score": 10}]}


def client_from(responses, recorder=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_ecosystems_repo_parses_payload():
    client = client_from([httpx.Response(200, json=ECOSYSTEMS_PAYLOAD)])
    repo = fetch_ecosystems_repo(client, "octo/hello")
    assert repo == MirrorRepo(
        full_name="octo/hello",
        stars=1234,
        forks=56,
        language="Python",
        license="MIT",
        topics=("ai", "agents"),
        pushed_at="2026-01-02T03:04:05.000Z",
        metadata={
            "funding": {"github": ["octo"]},
            "readme": "docs",
            "codeowners": ["* @octo"],
            "security": {"policy": True},
        },
    )


def test_fetch_ecosystems_repo_url_encoding_and_mailto():
    captured = []
    client = client_from([httpx.Response(200, json=ECOSYSTEMS_PAYLOAD)], captured)
    fetch_ecosystems_repo(client, "octo/hello", mailto="dev@example.com")
    parsed = urlparse(str(captured[0].url))
    assert parsed.path == "/api/v1/hosts/GitHub/repositories/octo%2Fhello"
    assert parsed.query == "mailto=dev%40example.com"
    assert captured[0].method == "GET"


def test_fetch_ecosystems_repo_omits_mailto_when_absent():
    captured = []
    client = client_from([httpx.Response(200, json=ECOSYSTEMS_PAYLOAD)], captured)
    fetch_ecosystems_repo(client, "octo/hello")
    assert urlparse(str(captured[0].url)).query == ""


def test_fetch_ecosystems_repo_uses_custom_base_url():
    captured = []
    client = client_from([httpx.Response(200, json=ECOSYSTEMS_PAYLOAD)], captured)
    fetch_ecosystems_repo(client, "octo/hello", base_url="https://mirror.test/repos")
    assert str(captured[0].url) == "https://mirror.test/repos/octo%2Fhello"


@pytest.mark.parametrize("status", [404, 410])
def test_fetch_ecosystems_repo_not_found_returns_none(status):
    client = client_from([httpx.Response(status, json={"message": "Not Found"})])
    assert fetch_ecosystems_repo(client, "octo/missing") is None


def test_fetch_ecosystems_repo_malformed_json_raises_request_failed():
    client = client_from([httpx.Response(200, text="<html>not json</html>")])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_ecosystems_repo(client, "octo/hello")
    assert excinfo.value.status == 200


def test_fetch_ecosystems_repo_non_dict_json_raises_request_failed():
    client = client_from([httpx.Response(200, json=["not", "a", "dict"])])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_ecosystems_repo(client, "octo/hello")
    assert excinfo.value.status == 200


def test_fetch_ecosystems_repo_non_200_raises_request_failed():
    client = client_from([httpx.Response(400, json={"message": "bad request"})])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_ecosystems_repo(client, "octo/hello")
    assert excinfo.value.status == 400


def test_fetch_ecosystems_repo_missing_fields_are_defensive():
    client = client_from([httpx.Response(200, json={})])
    repo = fetch_ecosystems_repo(client, "octo/hello")
    assert repo == MirrorRepo(
        full_name="octo/hello",
        stars=None,
        forks=None,
        language=None,
        license=None,
        topics=(),
        pushed_at=None,
        metadata={},
    )


def test_fetch_ecosystems_repo_ignores_wrong_types():
    payload = {
        "full_name": 7,
        "stargazers_count": True,
        "forks_count": "9",
        "language": 3,
        "license": {"spdx_id": "Apache-2.0"},
        "topics": "ai",
        "pushed_at": 5,
        "metadata": ["nope"],
    }
    client = client_from([httpx.Response(200, json=payload)])
    repo = fetch_ecosystems_repo(client, "octo/hello")
    assert repo is not None
    assert repo.full_name == "octo/hello"
    assert repo.stars is None
    assert repo.forks is None
    assert repo.language is None
    assert repo.license == "Apache-2.0"
    assert repo.topics == ()
    assert repo.pushed_at is None
    assert repo.metadata == {}


def test_fetch_ecosystems_repo_promotes_known_top_level_metadata():
    payload = {"funding": ["https://github.com/sponsors/octo"], "readme": "hello"}
    client = client_from([httpx.Response(200, json=payload)])
    repo = fetch_ecosystems_repo(client, "octo/hello")
    assert repo is not None
    assert repo.metadata == {"funding": ["https://github.com/sponsors/octo"], "readme": "hello"}


def test_fetch_depsdev_project_returns_parsed_dict():
    client = client_from([httpx.Response(200, json=DEPSDEV_PAYLOAD)])
    assert fetch_depsdev_project(client, "octo/hello") == DEPSDEV_PAYLOAD


def test_fetch_depsdev_project_encodes_project_path():
    captured = []
    client = client_from([httpx.Response(200, json=DEPSDEV_PAYLOAD)], captured)
    fetch_depsdev_project(client, "octo/hello")
    parsed = urlparse(str(captured[0].url))
    assert parsed.path == "/v3/projects/github.com%2Focto%2Fhello"
    assert parsed.query == ""


def test_fetch_depsdev_project_404_returns_none():
    client = client_from([httpx.Response(404, json={"message": "Not Found"})])
    assert fetch_depsdev_project(client, "octo/missing") is None


def test_fetch_depsdev_project_malformed_json_raises_request_failed():
    client = client_from([httpx.Response(200, text="nope")])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_depsdev_project(client, "octo/hello")
    assert excinfo.value.status == 200


def test_fetch_depsdev_project_non_200_raises_request_failed():
    client = client_from([httpx.Response(400, json={"message": "bad"})])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_depsdev_project(client, "octo/hello")
    assert excinfo.value.status == 400


def test_fetch_scorecard_returns_parsed_dict():
    client = client_from([httpx.Response(200, json=SCORECARD_PAYLOAD)])
    assert fetch_scorecard(client, "octo/hello") == SCORECARD_PAYLOAD


def test_fetch_scorecard_project_path():
    captured = []
    client = client_from([httpx.Response(200, json=SCORECARD_PAYLOAD)], captured)
    fetch_scorecard(client, "octo/hello")
    parsed = urlparse(str(captured[0].url))
    assert parsed.path == "/projects/github.com/octo/hello"
    assert parsed.query == ""


def test_fetch_scorecard_404_returns_none():
    client = client_from([httpx.Response(404, json={"message": "Not Found"})])
    assert fetch_scorecard(client, "octo/missing") is None


def test_fetch_scorecard_malformed_json_raises_request_failed():
    client = client_from([httpx.Response(200, text="nope")])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_scorecard(client, "octo/hello")
    assert excinfo.value.status == 200


def test_fetch_scorecard_non_200_raises_request_failed():
    client = client_from([httpx.Response(400, json={"message": "bad"})])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_scorecard(client, "octo/hello")
    assert excinfo.value.status == 400


def test_fetchers_invoke_on_response_callback():
    seen = []

    def on_response(response, latency_ms):
        seen.append((response.status_code, latency_ms))

    client = client_from([httpx.Response(200, json=SCORECARD_PAYLOAD)])
    fetch_scorecard(client, "octo/hello", on_response=on_response)
    assert len(seen) == 1
    assert seen[0][0] == 200
    assert seen[0][1] >= 0.0
