from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from enrich.geo_resolver import KNOWN_ISO_CODES

GEO_CONFIDENCE_ORDER: tuple[str, ...] = (
    "exact-iso",
    "name",
    "gazetteer-city",
    "geocoder",
    "weak",
)

_TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
_INT_HINT = "must be a non-negative integer"
_BOOL_HINT = "must be true or false"
_TOPIC_HINT = "must match ^[a-z0-9][a-z0-9-]{0,49}$ (lowercase letters, digits, hyphens)"
_ISO2_HINT = "must be a known ISO 3166-1 alpha-2 country code, e.g. `DE`"
_ENUM_HINT = "must be one of: {allowed}"


@dataclass(frozen=True)
class VirtualRule:
    name: str
    kind: str
    to_query: Callable[[object], str] | None
    post: str
    default: object = None
    allowed: tuple[str, ...] = ()


class VirtualFilterError(ValueError):
    def __init__(self, name: str, hint: str) -> None:
        self.name = name
        self.hint = hint
        super().__init__(f"invalid virtual filter `{name}`: {hint}")


VIRTUAL_FILTERS: Mapping[str, VirtualRule] = {
    "min_stars": VirtualRule(
        name="min_stars",
        kind="int",
        to_query=lambda value: f"stars:>={value}",
        post="stargazers >= min_stars",
    ),
    "team_topic": VirtualRule(
        name="team_topic",
        kind="topic",
        to_query=lambda value: f"topic:{value}",
        post="team_topic in repo topics",
    ),
    "has_dockerfile": VirtualRule(
        name="has_dockerfile",
        kind="bool",
        to_query=None,
        post="dockerfile presence equals has_dockerfile",
    ),
    "owner_country": VirtualRule(
        name="owner_country",
        kind="iso2",
        to_query=None,
        post="owner country equals owner_country at or above the confidence threshold",
    ),
    "min_geo_confidence": VirtualRule(
        name="min_geo_confidence",
        kind="enum",
        to_query=None,
        post="geo confidence at or above min_geo_confidence",
        default="gazetteer-city",
        allowed=GEO_CONFIDENCE_ORDER,
    ),
    "min_commits": VirtualRule(
        name="min_commits",
        kind="int",
        to_query=None,
        post="commit count >= min_commits",
    ),
    "max_commits": VirtualRule(
        name="max_commits",
        kind="int",
        to_query=None,
        post="commit count <= max_commits",
    ),
    "min_language_bytes": VirtualRule(
        name="min_language_bytes",
        kind="int",
        to_query=None,
        post="primary-language bytes >= min_language_bytes",
    ),
    "max_language_bytes": VirtualRule(
        name="max_language_bytes",
        kind="int",
        to_query=None,
        post="primary-language bytes <= max_language_bytes",
    ),
    "min_loc": VirtualRule(
        name="min_loc",
        kind="int",
        to_query=None,
        post="lines of code >= min_loc",
    ),
    "max_loc": VirtualRule(
        name="max_loc",
        kind="int",
        to_query=None,
        post="lines of code <= max_loc",
    ),
}


def _unknown_hint(name: str) -> str:
    valid = ", ".join(VIRTUAL_FILTERS)
    close = difflib.get_close_matches(name, list(VIRTUAL_FILTERS), n=1)
    if close:
        return f"did you mean `{close[0]}`? valid virtuals: {valid}"
    return f"valid virtuals: {valid}"


def _validate_int(name: str, value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VirtualFilterError(name, _INT_HINT)
    return value


def _validate_bool(name: str, value: object) -> object:
    if not isinstance(value, bool):
        raise VirtualFilterError(name, _BOOL_HINT)
    return value


def _validate_topic(name: str, value: object) -> object:
    if not isinstance(value, str):
        raise VirtualFilterError(name, _TOPIC_HINT)
    topic = value.strip().lower()
    if _TOPIC_PATTERN.fullmatch(topic) is None:
        raise VirtualFilterError(name, _TOPIC_HINT)
    return topic


def _validate_iso2(name: str, value: object) -> object:
    if not isinstance(value, str):
        raise VirtualFilterError(name, _ISO2_HINT)
    code = value.strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha() or code not in KNOWN_ISO_CODES:
        raise VirtualFilterError(name, _ISO2_HINT)
    return code


def _validate_enum(name: str, value: object, allowed: tuple[str, ...]) -> object:
    if not isinstance(value, str) or value not in allowed:
        raise VirtualFilterError(name, _ENUM_HINT.format(allowed=", ".join(allowed)))
    return value


def validate_virtual(name: str, value: object) -> object:
    rule = VIRTUAL_FILTERS.get(name)
    if rule is None:
        raise VirtualFilterError(name, _unknown_hint(name))
    if value is None:
        return rule.default
    if rule.kind == "int":
        return _validate_int(name, value)
    if rule.kind == "bool":
        return _validate_bool(name, value)
    if rule.kind == "topic":
        return _validate_topic(name, value)
    if rule.kind == "iso2":
        return _validate_iso2(name, value)
    if rule.kind == "enum":
        return _validate_enum(name, value, rule.allowed)
    raise VirtualFilterError(name, f"unsupported virtual kind `{rule.kind}`")


def translate_virtuals(virtual: Mapping[str, object]) -> list[str]:
    fragments: list[str] = []
    for name, rule in VIRTUAL_FILTERS.items():
        if rule.to_query is None or name not in virtual:
            continue
        value = validate_virtual(name, virtual[name])
        if value is None:
            continue
        fragments.append(rule.to_query(value))
    return fragments
