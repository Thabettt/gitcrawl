from __future__ import annotations

import re
from dataclasses import dataclass

ALLOWED_QUALIFIERS: frozenset[str] = frozenset(
    {
        "in",
        "repo",
        "user",
        "org",
        "size",
        "followers",
        "forks",
        "stars",
        "created",
        "pushed",
        "language",
        "topic",
        "topics",
        "license",
        "is",
        "mirror",
        "template",
        "archived",
        "good-first-issues",
        "help-wanted-issues",
        "has",
        "props",
        "fork",
        "deployable",
        "deployed",
    }
)

TYPO_HINTS: dict[str, str] = {
    "updated": "use `pushed:` + sort=updated",
    "push": "use `pushed:`",
    "is:archive": "use `archived:true`",
    "is:fork": "use `fork:true`",
    "is:forks": "use `fork:true`",
    "is:sponsor": "use `is:sponsorable`",
    "has:funding": "use `has:funding-file`",
    "is:mirror": "use `mirror:`",
    "is:template": "use `template:`",
}

KEYWORD_LIMIT = 256
OPERATOR_LIMIT = 5
OPERATORS = frozenset({"AND", "OR", "NOT"})
PROPS_HINT = "add a single org: scope"

_TOKEN_RE = re.compile(r'"[^"]*"|\S+')


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...]
    hints: tuple[str, ...]


def tokenize(query: str) -> list[str]:
    return _TOKEN_RE.findall(query)


def delta_narrows(base_total: int, candidate_total: int) -> bool:
    return candidate_total < base_total


def _hint_for(token: str) -> str | None:
    lowered = token.lower()
    if lowered in TYPO_HINTS:
        return TYPO_HINTS[lowered]
    parts = lowered.split(":")
    if len(parts) >= 2:
        pair = ":".join(parts[:2])
        if pair in TYPO_HINTS:
            return TYPO_HINTS[pair]
    if parts and parts[0] in TYPO_HINTS:
        return TYPO_HINTS[parts[0]]
    return None


def validate(query: str) -> ValidationResult:
    errors: list[str] = []
    hints: list[str] = []
    keywords: list[str] = []
    org_values: set[str] = set()
    operator_count = 0
    prop_tokens: list[str] = []

    for token in tokenize(query):
        if token.startswith('"'):
            keywords.append(token.strip('"'))
            continue
        if token in OPERATORS:
            operator_count += 1
            continue
        excluded = token.startswith("-")
        stripped = token[1:] if excluded else token
        if ":" not in stripped:
            keywords.append(stripped)
            continue
        name_part, value = stripped.split(":", 1)
        name = name_part.lower()
        hint = _hint_for(stripped)
        if hint is not None:
            errors.append(f"unknown qualifier `{stripped}`")
            hints.append(hint)
            continue
        if name.startswith("props."):
            if not name_part[len("props.") :]:
                errors.append(f"invalid qualifier `{stripped}`: missing custom property name")
                continue
            if not value:
                errors.append(f"qualifier `{stripped}` has an empty value")
                continue
            prop_tokens.append(stripped)
            continue
        if name == "props":
            errors.append(
                f"invalid qualifier `{stripped}`: custom properties require `props.<NAME>:<VALUE>`"
            )
            continue
        if name not in ALLOWED_QUALIFIERS:
            errors.append(f"unknown qualifier `{stripped}`")
            continue
        if not value:
            errors.append(f"qualifier `{stripped}` has an empty value")
            continue
        if name == "org" and not excluded:
            org_values.add(value.lower())

    if prop_tokens and len(org_values) != 1:
        for prop in prop_tokens:
            errors.append(f"`{prop}` requires exactly one `org:` scope in the query")
        hints.append(PROPS_HINT)

    keyword_characters = sum(len(keyword) for keyword in keywords)
    if keyword_characters > KEYWORD_LIMIT:
        errors.append(
            f"keyword text is {keyword_characters} characters; the limit is {KEYWORD_LIMIT}"
        )

    if operator_count > OPERATOR_LIMIT:
        errors.append(
            f"query uses {operator_count} explicit AND/OR/NOT operators; "
            f"the limit is {OPERATOR_LIMIT}"
        )

    return ValidationResult(not errors, tuple(errors), tuple(hints))
