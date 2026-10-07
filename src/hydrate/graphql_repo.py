from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from lib.graphql_batch import MAX_BATCH_SIZE, ParsedBatch

_FIELDS = """    databaseId
    id
    name
    nameWithOwner
    stargazerCount
    forkCount
    watchers { totalCount }
    issues(states: [OPEN]) { totalCount }
    diskUsage
    isArchived
    isDisabled
    isFork
    isTemplate
    visibility
    description
    homepageUrl
    pushedAt
    updatedAt
    createdAt
    defaultBranchRef {
      name
      target { ... on Commit { history(first: 1) { totalCount } } }
    }
    primaryLanguage { name }
    languages(first: 10) { edges { size node { name } } }
    licenseInfo { spdxId }
    repositoryTopics(first: 100) { nodes { topic { name } } }
    hasIssuesEnabled
    hasWikiEnabled
    hasProjectsEnabled
    hasDiscussionsEnabled
    hasPullRequestsEnabled
    parent { nameWithOwner }
    owner {
      login
      __typename
      ... on User { databaseId }
      ... on Organization { databaseId }
    }"""


@dataclass(frozen=True)
class RepoDetails:
    repo_id: int
    node_id: str
    full_name: str
    payload: dict
    commit_count: int | None = None
    language_bytes: dict[str, int] = field(default_factory=dict)


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _total_count(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    return _as_int(value.get("totalCount"))


def _owner(node: dict) -> dict | None:
    raw = node.get("owner")
    if not isinstance(raw, dict):
        return None
    owner_id = _as_int(raw.get("databaseId"))
    login = raw.get("login")
    if owner_id is None or not isinstance(login, str) or not login:
        return None
    kind = raw.get("__typename")
    return {
        "id": owner_id,
        "login": login,
        "type": "Organization" if kind == "Organization" else "User",
    }


def _topics(node: dict) -> list[str]:
    raw = node.get("repositoryTopics")
    if not isinstance(raw, dict):
        return []
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return []
    topics: list[str] = []
    for entry in nodes:
        if not isinstance(entry, dict):
            continue
        topic = entry.get("topic")
        if isinstance(topic, dict) and isinstance(topic.get("name"), str):
            topics.append(topic["name"])
    return topics


def _license(node: dict) -> dict | None:
    raw = node.get("licenseInfo")
    if not isinstance(raw, dict):
        return None
    spdx = raw.get("spdxId")
    return {"spdx_id": spdx} if isinstance(spdx, str) else None


def _language(node: dict) -> str | None:
    raw = node.get("primaryLanguage")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    return name if isinstance(name, str) else None


def _language_bytes(node: dict) -> dict[str, int]:
    raw = node.get("languages")
    if not isinstance(raw, dict):
        return {}
    edges = raw.get("edges")
    if not isinstance(edges, list):
        return {}
    sizes: dict[str, int] = {}
    for entry in edges:
        if not isinstance(entry, dict):
            continue
        size = _as_int(entry.get("size"))
        language = entry.get("node")
        name = language.get("name") if isinstance(language, dict) else None
        if size is None or not isinstance(name, str) or not name:
            continue
        sizes[name] = sizes.get(name, 0) + size
    return sizes


def _branch(node: dict) -> str | None:
    raw = node.get("defaultBranchRef")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    return name if isinstance(name, str) else None


def _commit_count(node: dict) -> int | None:
    branch = node.get("defaultBranchRef")
    if branch is None:
        return 0
    if not isinstance(branch, dict):
        return None
    target = branch.get("target")
    history = target.get("history") if isinstance(target, dict) else None
    total = history.get("totalCount") if isinstance(history, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def _parent(node: dict) -> str | None:
    raw = node.get("parent")
    if not isinstance(raw, dict):
        return None
    full_name = raw.get("nameWithOwner")
    return full_name if isinstance(full_name, str) else None


def _visibility(node: dict) -> str:
    raw = node.get("visibility")
    if isinstance(raw, str) and raw:
        return raw.lower()
    return "public"


def _payload(repo_id: int, node: dict) -> dict:
    owner = _owner(node)
    node_id = node.get("id")
    full_name = node.get("nameWithOwner")
    if owner is None or not isinstance(node_id, str) or not isinstance(full_name, str):
        raise ValueError("graphql repo node is missing identity fields")
    return {
        "id": repo_id,
        "node_id": node_id,
        "full_name": full_name,
        "owner": owner,
        "name": node.get("name"),
        "description": node.get("description"),
        "homepage": node.get("homepageUrl"),
        "language": _language(node),
        "license": _license(node),
        "topics": _topics(node),
        "visibility": _visibility(node),
        "fork": bool(node.get("isFork")),
        "parent": {"full_name": _parent(node)} if _parent(node) else None,
        "archived": bool(node.get("isArchived")),
        "disabled": bool(node.get("isDisabled")),
        "is_template": bool(node.get("isTemplate")),
        "size": _as_int(node.get("diskUsage")),
        "stargazers_count": _as_int(node.get("stargazerCount")),
        "forks_count": _as_int(node.get("forkCount")),
        "watchers_count": _total_count(node.get("watchers")),
        "open_issues_count": _total_count(node.get("issues")),
        "default_branch": _branch(node),
        "has_wiki": node.get("hasWikiEnabled"),
        "has_issues": node.get("hasIssuesEnabled"),
        "has_projects": node.get("hasProjectsEnabled"),
        "has_discussions": node.get("hasDiscussionsEnabled"),
        "has_pull_requests": node.get("hasPullRequestsEnabled"),
        "custom_properties": {},
        "created_at": node.get("createdAt"),
        "pushed_at": node.get("pushedAt"),
        "updated_at": node.get("updatedAt"),
    }


class RepoDetailsAdapter:
    name = "repo_details"

    def __init__(self, full_names: Mapping[str, str], *, batch_size: int = MAX_BATCH_SIZE) -> None:
        self._full_names = dict(full_names)
        self.batch_size = batch_size

    def _parts(self, key: str) -> tuple[str, str]:
        owner, _, name = self._full_names[key].partition("/")
        return owner, name

    def build_query(self, aliases: Mapping[str, str]) -> str:
        lines = ["query {"]
        for alias, key in aliases.items():
            owner, name = self._parts(key)
            lines.append(
                f"  {alias}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{"
            )
            lines.append(_FIELDS)
            lines.append("  }")
        lines.append("}")
        return "\n".join(lines)

    def parse(self, payload: Mapping[str, object], aliases: Mapping[str, str]) -> ParsedBatch:
        data = payload.get("data")
        values: dict[str, RepoDetails] = {}
        failures: dict[str, str] = {}
        if not isinstance(data, dict):
            return ParsedBatch(values=values, failures=failures)
        for alias, key in aliases.items():
            node = data.get(alias)
            if not isinstance(node, dict):
                continue
            repo_id = _as_int(node.get("databaseId"))
            if repo_id is None:
                failures[key] = "graphql node has no databaseId"
                continue
            try:
                values[key] = RepoDetails(
                    repo_id=repo_id,
                    node_id=str(node.get("id") or ""),
                    full_name=str(node.get("nameWithOwner") or ""),
                    payload=_payload(repo_id, node),
                    commit_count=_commit_count(node),
                    language_bytes=_language_bytes(node),
                )
            except ValueError as exc:
                failures[key] = str(exc)
        return ParsedBatch(values=values, failures=failures)
