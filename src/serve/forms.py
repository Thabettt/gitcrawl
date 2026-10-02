from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from serve.filter_spec import FILTER_SPEC_VERSION

DEFAULT_PER_PAGE = 20
DEFAULT_MAX_PAGES = 3
IN_SCOPES = ("name", "description", "topics", "readme")
COUNT_FIELDS = ("stars", "forks", "size", "followers", "topics")
_OPERATORS = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "=": "", "eq": ""}
_FORK_VALUES = {"include": "fork:true", "only": "fork:only"}


@dataclass(frozen=True)
class FormState:
    fields: Mapping[str, str]
    errors: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    props_enabled: bool = False


def _clean(form: Mapping[str, str], name: str) -> str:
    value = form.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _int_or_raw(value: str) -> int | str:
    return int(value) if value.isascii() and value.isdigit() else value


def _scope_fragment(form: Mapping[str, str]) -> str:
    chosen = {token.strip() for token in _clean(form, "in_scope").split(",")}
    selected = [scope for scope in IN_SCOPES if scope in chosen]
    return f"in:{','.join(selected)}" if selected else ""


def _count_fragment(form: Mapping[str, str], name: str) -> str:
    comparator = _clean(form, f"cmp_{name}") or "eq"
    if comparator == "range":
        low = _clean(form, f"range_{name}_min")
        high = _clean(form, f"range_{name}_max")
        if not low and not high:
            return ""
        return f"{name}:{low or '*'}..{high or '*'}"
    if comparator not in _OPERATORS:
        return ""
    value = _clean(form, f"val_{name}")
    if not value:
        return ""
    return f"{name}:{_OPERATORS[comparator]}{value}"


def _date_fragment(form: Mapping[str, str], name: str) -> str:
    start = _clean(form, f"{name}_from")
    end = _clean(form, f"{name}_to")
    if start and end:
        return f"{name}:{start}..{end}"
    if start:
        return f"{name}:>={start}"
    if end:
        return f"{name}:<={end}"
    return ""


def _flag_fragments(form: Mapping[str, str]) -> list[str]:
    fragments: list[str] = []
    fork = _clean(form, "fork")
    if fork in _FORK_VALUES:
        fragments.append(_FORK_VALUES[fork])
    for name in ("archived", "mirror", "template"):
        value = _clean(form, name).lower()
        if value in ("true", "false"):
            fragments.append(f"{name}:{value}")
    visibility = _clean(form, "visibility")
    if visibility in ("public", "private", "internal"):
        fragments.append(f"is:{visibility}")
    sponsorable = _clean(form, "sponsorable").lower()
    if sponsorable in ("true", "false"):
        prefix = "" if sponsorable == "true" else "-"
        fragments.append(f"{prefix}is:sponsorable")
    funding = _clean(form, "funding_file").lower()
    if funding in ("true", "false"):
        prefix = "" if funding == "true" else "-"
        fragments.append(f"{prefix}has:funding-file")
    for name, qualifier in (
        ("good_first_min", "good-first-issues"),
        ("help_wanted_min", "help-wanted-issues"),
    ):
        value = _clean(form, name)
        if value:
            fragments.append(f"{qualifier}:>={value}")
    return fragments


def _prop_fragment(form: Mapping[str, str]) -> str:
    name = _clean(form, "props_name")
    value = _clean(form, "props_value")
    if name and value:
        return f"props.{name}:{value}"
    return ""


def build_query_from_form(form: Mapping[str, str]) -> str:
    parts: list[str] = []
    keywords = _clean(form, "keywords")
    if keywords:
        parts.append(keywords)
    scope = _scope_fragment(form)
    if scope:
        parts.append(scope)
    for name in ("user", "org", "repo"):
        value = _clean(form, name)
        if value:
            parts.append(f"{name}:{value}")
    for name in COUNT_FIELDS:
        fragment = _count_fragment(form, name)
        if fragment:
            parts.append(fragment)
    for name in ("created", "pushed"):
        fragment = _date_fragment(form, name)
        if fragment:
            parts.append(fragment)
    for name in ("language", "topic", "license"):
        value = _clean(form, name)
        if value:
            parts.append(f"{name}:{value}")
    parts.extend(_flag_fragments(form))
    prop = _prop_fragment(form)
    if prop:
        parts.append(prop)
    return " ".join(parts)


def _virtual_from_form(form: Mapping[str, str]) -> dict[str, object]:
    virtual: dict[str, object] = {}
    for name in ("min_stars", "min_commits", "max_commits", "min_loc", "max_loc"):
        value = _clean(form, name)
        if value:
            virtual[name] = _int_or_raw(value)
    team_topic = _clean(form, "team_topic")
    if team_topic:
        virtual["team_topic"] = team_topic
    docker = _clean(form, "has_dockerfile").lower()
    if docker in ("true", "false"):
        virtual["has_dockerfile"] = docker == "true"
    elif docker and docker != "any":
        virtual["has_dockerfile"] = docker
    country = _clean(form, "owner_country")
    if country:
        virtual["owner_country"] = country
    confidence = _clean(form, "min_geo_confidence")
    if confidence:
        virtual["min_geo_confidence"] = confidence
    return virtual


def _page_value(form: Mapping[str, str], name: str, default: int) -> int | str:
    value = _clean(form, name)
    if not value:
        return default
    return _int_or_raw(value)


def build_spec_from_form(form: Mapping[str, str]) -> dict:
    document: dict[str, object] = {
        "gitcrawl_filter": FILTER_SPEC_VERSION,
        "q": build_query_from_form(form),
    }
    sort = _clean(form, "sort")
    if sort:
        document["sort"] = sort
    order = _clean(form, "order")
    if order:
        document["order"] = order
    virtual = _virtual_from_form(form)
    if virtual:
        document["virtual"] = virtual
    document["page"] = {
        "per_page": _page_value(form, "per_page", DEFAULT_PER_PAGE),
        "max_pages": _page_value(form, "max_pages", DEFAULT_MAX_PAGES),
    }
    return document


def spec_to_form_values(spec: Mapping[str, object]) -> dict[str, str]:
    values: dict[str, str] = {}
    q = spec.get("q")
    if isinstance(q, str) and q:
        values["keywords"] = q
    for name in ("sort", "order"):
        value = spec.get(name)
        if isinstance(value, str) and value:
            values[name] = value
    virtual = spec.get("virtual")
    if isinstance(virtual, Mapping):
        for name, value in virtual.items():
            if isinstance(value, bool):
                values[str(name)] = "true" if value else "false"
            elif value is not None:
                values[str(name)] = str(value)
    page = spec.get("page")
    if isinstance(page, Mapping):
        for name in ("per_page", "max_pages"):
            value = page.get(name)
            if value is not None:
                values[name] = str(value)
    return values


def form_state(form: Mapping[str, str]) -> FormState:
    fields = {key: value for key, value in form.items() if isinstance(value, str)}
    org = fields.get("org", "").strip()
    return FormState(fields=fields, props_enabled=len(org.split()) == 1)
