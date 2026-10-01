from __future__ import annotations

from urllib.parse import urlparse

import httpx
import pytest

from discover.search_shards import RequestFailed

TREE_RESPONSE = {
    "sha": "tree-sha",
    "truncated": True,
    "tree": [
        {"path": "src", "type": "tree", "sha": "t1"},
        {"path": "src/app.py", "type": "blob", "sha": "b1"},
        {"path": "Dockerfile", "type": "blob", "sha": "b2"},
        {"path": ".github/workflows/ci.yml", "type": "blob", "sha": "b3"},
        {"path": "README.md", "type": "blob", "sha": "b4"},
        {"path": None, "type": "blob"},
        {"path": "ghost", "type": "commit"},
    ],
}

TREE_BLOB_PATHS = frozenset({"src/app.py", "Dockerfile", ".github/workflows/ci.yml", "README.md"})

FIXTURE_PATHS = (
    "Dockerfile",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "docs/Dockerfile.md",
    "src/main.py",
    "README.md",
)

METAFILES_BASE_URL = "https://repos.ecosyste.ms/api/v1/hosts/GitHub/repositories"


def client_from(responses, recorder=None):
    iterator = iter(responses)

    def handler(request):
        if recorder is not None:
            recorder.append(request)
        return next(iterator)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_tree_resolves_default_branch_and_collects_blobs_only():
    from enrich.trees_first import fetch_tree

    captured = []
    client = client_from(
        [
            httpx.Response(200, json={"default_branch": "trunk"}),
            httpx.Response(200, json=TREE_RESPONSE),
        ],
        captured,
    )
    presence = fetch_tree(client, "octo/hello")
    assert presence.repo_full_name == "octo/hello"
    assert presence.paths == TREE_BLOB_PATHS
    assert presence.truncated is True
    assert presence.source == "tree"
    assert [urlparse(str(request.url)).path for request in captured] == [
        "/repos/octo/hello",
        "/repos/octo/hello/git/trees/trunk",
    ]
    assert urlparse(str(captured[1].url)).query == "recursive=1"


def test_fetch_tree_with_explicit_ref_makes_single_request():
    from enrich.trees_first import fetch_tree

    captured = []
    client = client_from([httpx.Response(200, json=TREE_RESPONSE)], captured)
    presence = fetch_tree(client, "octo/hello", ref="v1.0.0")
    assert presence.paths == TREE_BLOB_PATHS
    assert len(captured) == 1
    assert urlparse(str(captured[0].url)).path == "/repos/octo/hello/git/trees/v1.0.0"


def test_fetch_tree_truncated_defaults_to_false_when_absent():
    from enrich.trees_first import fetch_tree

    payload = {"tree": [{"path": "only.txt", "type": "blob"}]}
    client = client_from([httpx.Response(200, json=payload)])
    presence = fetch_tree(client, "octo/hello", ref="main")
    assert presence.paths == frozenset({"only.txt"})
    assert presence.truncated is False


def test_fetch_tree_empty_tree_list_yields_empty_paths():
    from enrich.trees_first import fetch_tree

    client = client_from([httpx.Response(200, json={"tree": [], "truncated": False})])
    presence = fetch_tree(client, "octo/hello", ref="main")
    assert presence.paths == frozenset()


def test_fetch_tree_repo_lookup_404_raises_request_failed():
    from enrich.trees_first import fetch_tree

    captured = []
    client = client_from([httpx.Response(404, json={"message": "Not Found"})], captured)
    with pytest.raises(RequestFailed) as excinfo:
        fetch_tree(client, "octo/missing")
    assert excinfo.value.status == 404
    assert excinfo.value.message == "Not Found"
    assert len(captured) == 1


def test_fetch_tree_missing_default_branch_raises_request_failed():
    from enrich.trees_first import fetch_tree

    captured = []
    client = client_from([httpx.Response(200, json={"id": 1})], captured)
    with pytest.raises(RequestFailed) as excinfo:
        fetch_tree(client, "octo/hello")
    assert "default_branch" in excinfo.value.message
    assert len(captured) == 1


def test_fetch_tree_tree_lookup_404_raises_request_failed():
    from enrich.trees_first import fetch_tree

    client = client_from(
        [
            httpx.Response(200, json={"default_branch": "main"}),
            httpx.Response(404, json={"message": "Not Found"}),
        ]
    )
    with pytest.raises(RequestFailed) as excinfo:
        fetch_tree(client, "octo/hello")
    assert excinfo.value.status == 404


def test_fetch_tree_malformed_json_raises_request_failed():
    from enrich.trees_first import fetch_tree

    client = client_from([httpx.Response(200, text="<html>not json</html>")])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_tree(client, "octo/hello", ref="main")
    assert excinfo.value.status == 200


def test_fetch_tree_non_dict_payload_raises_request_failed():
    from enrich.trees_first import fetch_tree

    client = client_from([httpx.Response(200, json=["not", "a", "dict"])])
    with pytest.raises(RequestFailed):
        fetch_tree(client, "octo/hello", ref="main")


def test_fetch_tree_missing_tree_list_raises_request_failed():
    from enrich.trees_first import fetch_tree

    client = client_from([httpx.Response(200, json={"truncated": False})])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_tree(client, "octo/hello", ref="main")
    assert "tree" in excinfo.value.message


def test_file_presence_has_is_exact_path_membership():
    from enrich.trees_first import FilePresence

    presence = FilePresence(
        repo_full_name="octo/hello",
        paths=frozenset({"Dockerfile", "docs/Dockerfile.md"}),
        truncated=False,
        source="tree",
    )
    assert presence.has("Dockerfile") is True
    assert presence.has("docs/Dockerfile.md") is True
    assert presence.has("Dockerfile.md") is False
    assert presence.has("dockerfile") is False


def test_file_presence_match_exact_glob_and_miss():
    from enrich.trees_first import FilePresence

    presence = FilePresence(
        repo_full_name="octo/hello",
        paths=frozenset(FIXTURE_PATHS),
        truncated=False,
        source="tree",
    )
    assert presence.match(["Dockerfile", "docs/*.md", "*.rs"]) == {
        "Dockerfile": True,
        "docs/*.md": True,
        "*.rs": False,
    }


def test_file_presence_match_preserves_pattern_order():
    from enrich.trees_first import FilePresence

    presence = FilePresence(
        repo_full_name="octo/hello",
        paths=frozenset(FIXTURE_PATHS),
        truncated=False,
        source="tree",
    )
    assert list(presence.match(["*.rs", "Dockerfile"])) == ["*.rs", "Dockerfile"]


def prefix_scan(paths, pattern):
    if pattern.endswith("/*"):
        return any(path.startswith(pattern[:-1]) for path in paths)
    return pattern in paths


def test_match_equals_independent_prefix_scan():
    from enrich.trees_first import FilePresence

    presence = FilePresence(
        repo_full_name="octo/hello",
        paths=frozenset(FIXTURE_PATHS),
        truncated=False,
        source="tree",
    )
    patterns = ("Dockerfile", ".github/workflows/*")
    expected = {pattern: prefix_scan(FIXTURE_PATHS, pattern) for pattern in patterns}
    assert presence.match(patterns) == expected
    assert expected == {"Dockerfile": True, ".github/workflows/*": True}


def test_match_handles_literals_without_fnmatch(monkeypatch):
    from enrich import trees_first

    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("fnmatchcase called for a literal pattern")

    monkeypatch.setattr(trees_first, "fnmatchcase", boom, raising=False)
    presence = trees_first.FilePresence(
        repo_full_name="octo/mono",
        paths=frozenset({"Dockerfile", "README.md", "src/main.py"}),
        truncated=False,
        source="tree",
    )
    assert presence.match(["Dockerfile", "LICENSE"]) == {"Dockerfile": True, "LICENSE": False}


def test_match_still_supports_globs():
    from enrich.trees_first import FilePresence

    presence = FilePresence(
        repo_full_name="octo/mono",
        paths=frozenset({"Dockerfile", "src/main.py", "docs/guide.md"}),
        truncated=False,
        source="tree",
    )
    assert presence.match(["**/main.py", "*.md", "Dockerfile"]) == {
        "**/main.py": True,
        "*.md": True,
        "Dockerfile": True,
    }


def test_fetch_metafiles_parses_list_of_dicts_and_filters_requested_names():
    from enrich.trees_first import fetch_metafiles

    captured = []
    payload = {
        "manifests": [
            {"filename": "Dockerfile"},
            {"path": ".github/workflows/ci.yml"},
            {"filename": "package.json"},
            "README.md",
        ]
    }
    client = client_from([httpx.Response(200, json=payload)], captured)
    presence = fetch_metafiles(
        client,
        "octo/hello",
        names=("Dockerfile", ".github/workflows/ci.yml", "README.md"),
    )
    assert presence.paths == frozenset({"Dockerfile", ".github/workflows/ci.yml", "README.md"})
    assert presence.repo_full_name == "octo/hello"
    assert presence.truncated is False
    assert presence.source == "metafiles"
    assert str(captured[0].url) == f"{METAFILES_BASE_URL}/octo/hello"


def test_fetch_metafiles_parses_mapping_dict_shape():
    from enrich.trees_first import fetch_metafiles

    payload = {"metafiles": {"Dockerfile": {"size": 10}, "package.json": {}}}
    client = client_from([httpx.Response(200, json=payload)])
    presence = fetch_metafiles(
        client, "octo/hello", names=("Dockerfile", "package.json", "missing")
    )
    assert presence.paths == frozenset({"Dockerfile", "package.json"})


def test_fetch_metafiles_parses_single_entry_dict_shape():
    from enrich.trees_first import fetch_metafiles

    payload = {"metafiles": {"path": "Dockerfile", "size": 12}}
    client = client_from([httpx.Response(200, json=payload)])
    presence = fetch_metafiles(client, "octo/hello", names=("Dockerfile",))
    assert presence.paths == frozenset({"Dockerfile"})


def test_fetch_metafiles_reads_both_keys_and_nested_lists():
    from enrich.trees_first import fetch_metafiles

    payload = {
        "manifests": [{"filename": "Dockerfile"}],
        "metafiles": {"files": [{"path": "FUNDING.yml"}]},
    }
    client = client_from([httpx.Response(200, json=payload)])
    presence = fetch_metafiles(client, "octo/hello", names=("Dockerfile", "FUNDING.yml"))
    assert presence.paths == frozenset({"Dockerfile", "FUNDING.yml"})


def test_fetch_metafiles_produces_no_paths_for_empty_payload():
    from enrich.trees_first import fetch_metafiles

    client = client_from([httpx.Response(200, json={"full_name": "octo/hello"})])
    presence = fetch_metafiles(client, "octo/hello", names=("Dockerfile",))
    assert presence.paths == frozenset()
    assert presence.source == "metafiles"


def test_fetch_metafiles_honors_custom_base_url():
    from enrich.trees_first import fetch_metafiles

    captured = []
    client = client_from([httpx.Response(200, json={"manifests": []})], captured)
    fetch_metafiles(
        client,
        "octo/hello",
        names=("Dockerfile",),
        base_url="https://mirror.test/repos",
    )
    assert str(captured[0].url) == "https://mirror.test/repos/octo/hello"


def test_fetch_metafiles_404_raises_request_failed():
    from enrich.trees_first import fetch_metafiles

    client = client_from([httpx.Response(404, json={"message": "Not Found"})])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_metafiles(client, "octo/missing", names=("Dockerfile",))
    assert excinfo.value.status == 404
    assert excinfo.value.message == "Not Found"


def test_fetch_metafiles_malformed_body_raises_request_failed():
    from enrich.trees_first import fetch_metafiles

    client = client_from([httpx.Response(200, text="<html>not json</html>")])
    with pytest.raises(RequestFailed) as excinfo:
        fetch_metafiles(client, "octo/hello", names=("Dockerfile",))
    assert excinfo.value.status == 200


def test_fetch_tree_reuses_the_audit_cached_body():
    from enrich.trees_first import fetch_tree
    from serve.audit import record_from_response

    parses: list[int] = []
    response = httpx.Response(200, json=TREE_RESPONSE)
    original = response.json

    def counting_json():
        parses.append(1)
        return original()

    response.json = counting_json
    client = client_from([response])

    def hook(resp, latency_ms):
        record_from_response({}, resp, token_fp="fp", latency_ms=latency_ms)

    presence = fetch_tree(client, "octo/hello", ref="v1", on_response=hook)

    assert presence.paths == TREE_BLOB_PATHS
    assert parses == [1]


def test_fetch_metafiles_never_forwards_the_client_authorization_header():
    from enrich.trees_first import fetch_metafiles

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"manifests": [{"filename": "Dockerfile"}]})

    client = httpx.Client(
        headers={"Authorization": "Bearer super-secret"},
        transport=httpx.MockTransport(handler),
    )

    presence = fetch_metafiles(client, "octo/hello", names=("Dockerfile",))

    assert presence.paths == frozenset({"Dockerfile"})
    assert len(captured) == 1
    assert "authorization" not in captured[0].headers
