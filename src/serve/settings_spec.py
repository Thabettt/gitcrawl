from __future__ import annotations

from collections.abc import Mapping

_BOUNDS: dict[str, tuple[int, int]] = {
    "max_shards": (1, 10_000),
    "max_candidates": (1, 1_000_000),
    "max_hydrate": (1, 1_000_000),
    "max_enrich": (1, 1_000_000),
    "request_deadline_seconds": (60, 86_400),
    "graphql_batch_size": (1, 50),
    "limiter_max_concurrent": (1, 100),
    "discovery_concurrency": (1, 64),
}

_CORPUS_VALUES: dict[str, object] = {
    "max_shards": 1_000,
    "max_candidates": 100_000,
    "max_hydrate": 100_000,
    "max_enrich": 100_000,
    "request_deadline_seconds": 86_400,
    "graphql_batch_size": 29,
    "limiter_max_concurrent": 32,
    "discovery_concurrency": 32,
    "graphql_batch": True,
}


def bounds_text() -> dict[str, str]:
    return {name: f"{low:,}–{high:,}" for name, (low, high) in _BOUNDS.items()}


class SettingsError(ValueError):
    def __init__(self, errors, hints=()):
        self.errors = tuple(errors)
        self.hints = tuple(hints)
        super().__init__("; ".join(self.errors))


def parse_settings_form(
    form: Mapping[str, str], *, pinned: frozenset[str] = frozenset()
) -> dict[str, object]:
    if form.get("reset") == "1":
        return {}
    if form.get("preset") == "corpus":
        return {name: value for name, value in _CORPUS_VALUES.items() if name not in pinned}
    errors: list[str] = []
    values: dict[str, object] = {}
    for field, (low, high) in _BOUNDS.items():
        if field in pinned:
            continue
        raw = str(form.get(field, "")).strip()
        try:
            parsed = int(raw)
        except ValueError:
            errors.append(f"`{field}` must be an integer between {low} and {high}")
            continue
        if not low <= parsed <= high:
            errors.append(f"`{field}` must be between {low} and {high}")
            continue
        values[field] = parsed
    if "graphql_batch" not in pinned:
        values["graphql_batch"] = form.get("graphql_batch") in {"on", "1", "true"}
    hydrate = values.get("max_hydrate")
    candidates = values.get("max_candidates")
    if isinstance(hydrate, int) and isinstance(candidates, int) and hydrate > candidates:
        errors.append("`max_hydrate` must not exceed `max_candidates`")
    if errors:
        raise SettingsError(errors, ("raise `max_candidates` or lower `max_hydrate`",))
    return values
