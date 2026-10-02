from __future__ import annotations

import httpx

from hydrate.graphql_repo import RepoDetails, RepoDetailsAdapter
from lib.graphql_batch import fetch_batch
from store.upserts import normalize_repo

NODE = {
    "databaseId": 911,
    "id": "R_911",
    "name": "alpha",
    "nameWithOwner": "octo/alpha",
    "stargazerCount": 300,
    "forkCount": 30,
    "watchers": {"totalCount": 31},
    "issues": {"totalCount": 3},
    "diskUsage": 100,
    "isArchived": False,
    "isDisabled": False,
    "isFork": False,
    "isTemplate": False,
    "visibility": "PUBLIC",
    "description": "Fast crawler",
    "homepageUrl": "https://example.test",
    "pushedAt": "2025-12-30T12:00:00Z",
    "updatedAt": "2026-01-01T00:00:00Z",
    "createdAt": "2024-01-01T00:00:00Z",
    "defaultBranchRef": {"name": "main"},
    "primaryLanguage": {"name": "Rust"},
    "licenseInfo": {"spdxId": "MIT"},
    "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}, {"topic": {"name": "crawler"}}]},
    "hasIssuesEnabled": True,
    "hasWikiEnabled": False,
    "hasProjectsEnabled": True,
    "hasDiscussionsEnabled": True,
    "hasPullRequestsEnabled": True,
    "parent": None,
    "owner": {"databaseId": 901, "login": "octo", "__typename": "User"},
}


def test_build_query_uses_owner_name_aliases():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    query = adapter.build_query({"n0": "911"})
    assert 'n0: repository(owner: "octo", name: "alpha")' in query
    assert "databaseId" in query
    assert "stargazerCount" in query
    assert "first: 100" in query


def test_parse_maps_graphql_node_to_rest_shaped_payload():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    payload = {"data": {"n0": NODE}}
    parsed = adapter.parse(payload, {"n0": "911"})
    details = parsed.values["911"]
    assert isinstance(details, RepoDetails)
    assert details.repo_id == 911
    assert details.node_id == "R_911"
    assert details.full_name == "octo/alpha"
    row = normalize_repo(details.payload)
    assert row is not None
    assert row["id"] == 911
    assert row["owner_id"] == 901
    assert row["owner_login"] == "octo"
    assert row["stargazers"] == 300
    assert row["forks_count"] == 30
    assert row["watchers"] == 31
    assert row["open_issues"] == 3
    assert row["size_kb"] == 100
    assert row["language"] == "Rust"
    assert row["license_spdx"] == "MIT"
    assert row["topics"] == ["cli", "crawler"]
    assert row["visibility"] == "public"
    assert row["default_branch"] == "main"


def test_parse_organizations_and_missing_optional_fields():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    node = dict(NODE)
    node["owner"] = {"databaseId": 950, "login": "org", "__typename": "Organization"}
    node["licenseInfo"] = None
    node["primaryLanguage"] = None
    node["repositoryTopics"] = {"nodes": []}
    parsed = adapter.parse({"data": {"n0": node}}, {"n0": "911"})
    row = normalize_repo(parsed.values["911"].payload)
    assert row is not None
    assert row["owner_type"] == "Organization"
    assert row["license_spdx"] is None
    assert row["language"] is None
    assert row["topics"] == []


def test_parse_rejects_a_node_without_a_database_id():
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    node = dict(NODE)
    node["databaseId"] = None
    parsed = adapter.parse({"data": {"n0": node}}, {"n0": "911"})
    assert parsed.values == {}
    assert "911" in parsed.failures


def test_adapter_end_to_end_with_batch_core():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"n0": NODE}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = RepoDetailsAdapter({"911": "octo/alpha"})
    outcome = fetch_batch(adapter, ["911"], client=client)
    assert outcome.values["911"].payload["id"] == 911
