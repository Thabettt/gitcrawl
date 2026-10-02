from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from lib.qualify import tokenize
from lib.qualify import validate as validate_query
from serve.virtual_params import (
    VIRTUAL_FILTERS,
    VirtualFilterError,
    translate_virtuals,
    validate_virtual,
)

FILTER_SPEC_VERSION = 1

SORT_VALUES = frozenset({"stars", "forks", "help-wanted-issues", "updated"})
ORDER_VALUES = frozenset({"desc", "asc"})

ALLOWED_TOP_LEVEL = {"gitcrawl_filter", "q", "sort", "order", "virtual", "page", "frame", "as_of"}

_TOKEN_KEYS = frozenset({"token", "authorization", "github_token"})
_PAGE_KEYS = frozenset({"per_page", "max_pages"})
_DEFAULT_PER_PAGE = 20
_DEFAULT_MAX_PAGES = 3
_PER_PAGE_RANGE = (1, 100)
_MAX_PAGES_RANGE = (1, 10)


class FilterSpecError(ValueError):
    def __init__(self, errors: Iterable[str], hints: Iterable[str] = ()) -> None:
        self.errors = tuple(errors)
        self.hints = tuple(hints)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class FilterSpec:
    q: str
    sort: str | None
    order: str | None
    virtual: Mapping[str, object]
    per_page: int
    max_pages: int
    frame: Mapping[str, object] | None
    as_of: str | None
    raw: Mapping[str, object]


def _collapse_whitespace(query: str) -> str:
    return " ".join(tokenize(query))


def _bounded_int(
    value: object,
    *,
    name: str,
    bounds: tuple[int, int],
    default: int,
    errors: list[str],
    hints: list[str],
) -> int:
    low, high = bounds
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        errors.append(f"`{name}` must be an integer between {low} and {high}")
        hints.append(f"use an integer between {low} and {high} for `{name}`")
        return default
    return value


def _parse_page(page: object, errors: list[str], hints: list[str]) -> tuple[int, int]:
    if page is None:
        return _DEFAULT_PER_PAGE, _DEFAULT_MAX_PAGES
    if not isinstance(page, Mapping):
        errors.append("`page` must be an object with `per_page` and/or `max_pages`")
        hints.append("pass page as {per_page: 1-100, max_pages: 1-10}")
        return _DEFAULT_PER_PAGE, _DEFAULT_MAX_PAGES
    for key in page:
        if key not in _PAGE_KEYS:
            errors.append(f"unknown page parameter `{key}`")
    hints.append("page accepts only `per_page` and `max_pages`")
    per_page = _bounded_int(
        page.get("per_page"),
        name="per_page",
        bounds=_PER_PAGE_RANGE,
        default=_DEFAULT_PER_PAGE,
        errors=errors,
        hints=hints,
    )
    max_pages = _bounded_int(
        page.get("max_pages"),
        name="max_pages",
        bounds=_MAX_PAGES_RANGE,
        default=_DEFAULT_MAX_PAGES,
        errors=errors,
        hints=hints,
    )
    return per_page, max_pages


def _parse_virtual(virtual: object, errors: list[str], hints: list[str]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    if virtual is None:
        return normalized
    if not isinstance(virtual, Mapping):
        errors.append("`virtual` must be an object of virtual-filter names to values")
        hints.append("virtual accepts keys: " + ", ".join(VIRTUAL_FILTERS))
        return normalized
    valid = ", ".join(VIRTUAL_FILTERS)
    for name in virtual:
        if name not in VIRTUAL_FILTERS:
            errors.append(f"unknown virtual filter `{name}`")
            hints.append(f"valid virtuals: {valid}")
    for name in VIRTUAL_FILTERS:
        if name not in virtual:
            continue
        try:
            normalized[name] = validate_virtual(name, virtual[name])
        except VirtualFilterError as exc:
            errors.append(f"invalid virtual filter `{exc.name}`")
            hints.append(exc.hint)
    if normalized.get("owner_country") is not None and "min_geo_confidence" not in normalized:
        normalized["min_geo_confidence"] = VIRTUAL_FILTERS["min_geo_confidence"].default
    return normalized


def _parse_frame(frame: object, errors: list[str], hints: list[str]) -> Mapping[str, object] | None:
    if frame is None:
        return None
    if not isinstance(frame, Mapping):
        errors.append("`frame` must be an object")
        hints.append("pass corpus-frame config as a JSON object; it is recorded verbatim")
        return None
    return dict(frame)


def _is_iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_as_of(as_of: object, errors: list[str], hints: list[str]) -> str | None:
    if as_of is None:
        return None
    if not isinstance(as_of, str) or not _is_iso(as_of.strip()):
        errors.append(f"invalid `as_of` value `{as_of}`")
        hints.append("as_of must be an ISO-8601 date or datetime string, e.g. `2025-01-01`")
        return None
    return as_of.strip()


def _validate_top_level(doc: Mapping[str, object], errors: list[str], hints: list[str]) -> None:
    for key in doc:
        if key in ALLOWED_TOP_LEVEL:
            continue
        lowered = key.lower()
        if key in _TOKEN_KEYS or "token" in lowered or "authorization" in lowered:
            errors.append(
                f"`{key}` is not allowed: filter-spec files hold filters only, "
                "never tokens or local state"
            )
            hints.append("remove token/state fields; tokens stay in the server environment")
        else:
            errors.append(f"unknown top-level parameter `{key}`")
            hints.append("allowed top-level keys: " + ", ".join(sorted(ALLOWED_TOP_LEVEL)))


def _validate_version(doc: Mapping[str, object], errors: list[str], hints: list[str]) -> None:
    if "gitcrawl_filter" not in doc:
        errors.append("missing required `gitcrawl_filter` version")
        hints.append(f"add `gitcrawl_filter: {FILTER_SPEC_VERSION}` at the top level")
        return
    version = doc["gitcrawl_filter"]
    if type(version) is not int or version != FILTER_SPEC_VERSION:
        errors.append(f"unsupported `gitcrawl_filter` version `{version}`")
        hints.append(f"only filter-spec version {FILTER_SPEC_VERSION} is supported")


def _parse_q(q: object, errors: list[str], hints: list[str]) -> str:
    if not isinstance(q, str):
        errors.append("`q` must be a string")
        hints.append("provide the GitHub search query as a string")
        return ""
    query = _collapse_whitespace(q)
    result = validate_query(query)
    errors.extend(result.errors)
    hints.extend(result.hints)
    return query


def _parse_sort(sort: object, errors: list[str], hints: list[str]) -> str | None:
    if sort is None:
        return None
    if not isinstance(sort, str) or sort not in SORT_VALUES:
        errors.append(f"invalid `sort` value `{sort}`")
        hints.append("sort must be one of: stars, forks, help-wanted-issues, updated")
        return None
    return sort


def _parse_order(order: object, errors: list[str], hints: list[str]) -> str | None:
    if order is None:
        return None
    if not isinstance(order, str) or order not in ORDER_VALUES:
        errors.append(f"invalid `order` value `{order}`")
        hints.append("order must be one of: desc, asc (recorded but ignored without `sort`)")
        return None
    return order


def parse_filter_spec(doc: Mapping[str, object]) -> FilterSpec:
    if not isinstance(doc, Mapping):
        raise FilterSpecError(
            ("filter-spec must be a JSON object",),
            ("pass a JSON object with `gitcrawl_filter: 1`",),
        )
    errors: list[str] = []
    hints: list[str] = []
    _validate_top_level(doc, errors, hints)
    _validate_version(doc, errors, hints)
    q = _parse_q(doc.get("q", ""), errors, hints)
    sort = _parse_sort(doc.get("sort"), errors, hints)
    order = _parse_order(doc.get("order"), errors, hints)
    virtual = _parse_virtual(doc.get("virtual"), errors, hints)
    per_page, max_pages = _parse_page(doc.get("page"), errors, hints)
    frame = _parse_frame(doc.get("frame"), errors, hints)
    as_of = _parse_as_of(doc.get("as_of"), errors, hints)
    if errors:
        raise FilterSpecError(errors, hints)
    return FilterSpec(
        q=q,
        sort=sort,
        order=order,
        virtual=virtual,
        per_page=per_page,
        max_pages=max_pages,
        frame=frame,
        as_of=as_of,
        raw=dict(doc),
    )


def spec_to_query(spec: FilterSpec) -> str:
    parts = [_collapse_whitespace(spec.q), *translate_virtuals(spec.virtual)]
    return " ".join(part for part in parts if part)


_COUNT_LABELS: Mapping[str, str] = {
    "stars": "stars",
    "forks": "forks",
    "size": "KB",
    "followers": "followers",
    "topics": "topics",
    "good-first-issues": "good first issues",
    "help-wanted-issues": "help wanted issues",
}
_VIRTUAL_COUNT_LABELS: Mapping[str, str] = {
    "min_commits": "commits",
    "max_commits": "commits",
    "min_loc": "lines of code",
    "max_loc": "lines of code",
}


def _describe_count(label: str, raw: str) -> str:
    if ".." in raw:
        low, _, high = raw.partition("..")
        low = low or "*"
        high = high or "*"
        if low == "*":
            return f"up to {high} {label}"
        if high == "*":
            return f"{low}+ {label}"
        return f"{low}–{high} {label}"
    for prefix, template in (
        (">=", "{value}+ {label}"),
        (">", "over {value} {label}"),
        ("<=", "up to {value} {label}"),
        ("<", "under {value} {label}"),
    ):
        if raw.startswith(prefix):
            return template.format(value=raw[len(prefix) :], label=label)
    return f"{raw} {label}"


def _describe_date(label: str, raw: str) -> str:
    if ".." in raw:
        low, _, high = raw.partition("..")
        return f"{label} {low or '*'} to {high or '*'}"
    if raw.startswith(">="):
        return f"{label} since {raw[2:]}"
    if raw.startswith(">"):
        return f"{label} after {raw[1:]}"
    if raw.startswith("<=") or raw.startswith("<"):
        return f"{label} before {raw.lstrip('<=')}"
    return f"{label} {raw}"


def _describe_token(token: str) -> str:
    excluded = token.startswith("-")
    stripped = token[1:] if excluded else token
    if stripped in ("AND", "OR", "NOT") or ":" not in stripped:
        return token
    key, _, value = stripped.partition(":")
    lowered = key.lower()
    if lowered.startswith("props."):
        described = f"property {key[len('props.') :]} = {value}"
    elif lowered == "language":
        described = value.replace("-", " ").title()
    elif lowered in _COUNT_LABELS:
        described = _describe_count(_COUNT_LABELS[lowered], value)
    elif lowered in ("created", "pushed"):
        described = _describe_date("created" if lowered == "created" else "updated", value)
    elif lowered == "in":
        described = "in " + value.replace(",", ", ")
    elif lowered in ("user", "org", "repo"):
        described = f"{lowered} {value}"
    elif lowered in ("topic", "license"):
        described = f"{lowered} {value}"
    elif lowered in ("archived", "mirror", "template"):
        has_flag = (value == "true") != excluded
        described = lowered if has_flag else f"no {lowered}"
    elif lowered == "fork":
        described = {"true": "include forks", "only": "forks only"}.get(value, "no forks")
    elif lowered == "is":
        described = value.replace("-", " ")
    elif lowered == "has":
        return f"{'no' if excluded else 'has'} {value.replace('-', ' ')}"
    else:
        described = f"{lowered} {value}"
    return f"not {described}" if excluded else described


def _describe_virtual(name: str, value: object) -> str | None:
    if name in _VIRTUAL_COUNT_LABELS:
        qualifier = "at least" if name.startswith("min_") else "at most"
        return f"{qualifier} {value} {_VIRTUAL_COUNT_LABELS[name]}"
    if name == "min_stars":
        return _describe_count("stars", f">={value}")
    if name == "team_topic":
        return f"topic {value}"
    if name == "has_dockerfile":
        return "has Dockerfile" if value else "no Dockerfile"
    if name == "owner_country":
        return f"owner country {str(value).upper()}"
    return None


def describe_spec(spec: FilterSpec) -> str:
    parts = [_describe_token(token) for token in tokenize(spec.q)]
    for name, value in spec.virtual.items():
        described = _describe_virtual(name, value)
        if described:
            parts.append(described)
    return " · ".join(part for part in parts if part) or "Everything"


def spec_to_dict(spec: FilterSpec) -> dict:
    normalized: dict[str, object] = {
        "gitcrawl_filter": FILTER_SPEC_VERSION,
        "q": spec.q,
    }
    if spec.sort is not None:
        normalized["sort"] = spec.sort
    if spec.order is not None:
        normalized["order"] = spec.order
    if spec.virtual:
        normalized["virtual"] = dict(spec.virtual)
    normalized["page"] = {"per_page": spec.per_page, "max_pages": spec.max_pages}
    if spec.frame is not None:
        normalized["frame"] = dict(spec.frame)
    if spec.as_of is not None:
        normalized["as_of"] = spec.as_of
    return normalized


def spec_hash(spec: FilterSpec) -> str:
    canonical = json.dumps(spec_to_dict(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
