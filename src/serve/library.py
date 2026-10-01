from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from serve.filter_spec import FilterSpecError, parse_filter_spec, spec_hash, spec_to_dict
from store.models import SavedFilter

NAME_MAX = 80

_VIEW_COLUMNS = (
    SavedFilter.id,
    SavedFilter.name,
    SavedFilter.filter_spec,
    SavedFilter.created_at,
    SavedFilter.updated_at,
)


@dataclass(frozen=True)
class SavedFilterView:
    id: int
    name: str
    filter_spec: dict
    filter_hash: str
    created_at: datetime
    updated_at: datetime


class LibraryError(ValueError):
    def __init__(self, code: str, message: str, hints: tuple[str, ...] = ()) -> None:
        self.code = code
        self.message = message
        self.hints = tuple(hints)
        super().__init__(message)


def _view(row) -> SavedFilterView:
    spec = parse_filter_spec(row["filter_spec"])
    return SavedFilterView(
        id=row["id"],
        name=row["name"],
        filter_spec=dict(spec_to_dict(spec)),
        filter_hash=spec_hash(spec),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _valid_name(name: object) -> str:
    if not isinstance(name, str):
        raise LibraryError(
            "invalid_name",
            "name must be a string",
            ("name must be 1-80 characters",),
        )
    trimmed = name.strip()
    if not trimmed:
        raise LibraryError(
            "invalid_name",
            "name must not be empty",
            ("give the filter a name of 1-80 characters",),
        )
    if len(trimmed) > NAME_MAX:
        raise LibraryError(
            "invalid_name",
            f"name must be at most {NAME_MAX} characters",
            (f"shorten the name to {NAME_MAX} characters or fewer",),
        )
    return trimmed


def _normalized_spec(spec_doc: object) -> dict:
    if not isinstance(spec_doc, Mapping):
        raise LibraryError(
            "invalid_spec",
            "filter spec must be a JSON object",
            ("pass a filter-spec v1 JSON object",),
        )
    try:
        spec = parse_filter_spec(spec_doc)
    except FilterSpecError as exc:
        raise LibraryError("invalid_spec", "; ".join(exc.errors), exc.hints) from exc
    return spec_to_dict(spec)


def _duplicate(name: str) -> LibraryError:
    return LibraryError(
        "duplicate_name",
        f"a filter named `{name}` already exists",
        ("rename the existing filter or pick a different name",),
    )


def list_filters(engine: Engine) -> list[SavedFilterView]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(*_VIEW_COLUMNS).order_by(SavedFilter.name.asc(), SavedFilter.id.asc())
            )
            .mappings()
            .all()
        )
    return [_view(row) for row in rows]


def get_filter(engine: Engine, filter_id: int) -> SavedFilterView:
    with engine.connect() as connection:
        row = (
            connection.execute(select(*_VIEW_COLUMNS).where(SavedFilter.id == filter_id))
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise LibraryError("not_found", f"no saved filter with id {filter_id}")
    return _view(row)


def create_filter(engine: Engine, name: str, spec_doc: Mapping) -> SavedFilterView:
    clean_name = _valid_name(name)
    normalized = _normalized_spec(spec_doc)
    with engine.begin() as connection:
        existing = connection.execute(
            select(SavedFilter.id).where(SavedFilter.name == clean_name)
        ).scalar_one_or_none()
        if existing is not None:
            raise _duplicate(clean_name)
        try:
            row = (
                connection.execute(
                    insert(SavedFilter)
                    .values(name=clean_name, filter_spec=normalized)
                    .returning(*_VIEW_COLUMNS)
                )
                .mappings()
                .one()
            )
        except IntegrityError as exc:
            raise _duplicate(clean_name) from exc
    return _view(row)


def rename_filter(engine: Engine, filter_id: int, name: str) -> SavedFilterView:
    clean_name = _valid_name(name)
    with engine.begin() as connection:
        row = (
            connection.execute(select(*_VIEW_COLUMNS).where(SavedFilter.id == filter_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LibraryError("not_found", f"no saved filter with id {filter_id}")
        conflict = connection.execute(
            select(SavedFilter.id).where(
                SavedFilter.name == clean_name, SavedFilter.id != filter_id
            )
        ).scalar_one_or_none()
        if conflict is not None:
            raise _duplicate(clean_name)
        try:
            updated = (
                connection.execute(
                    update(SavedFilter)
                    .where(SavedFilter.id == filter_id)
                    .values(name=clean_name, updated_at=func.now())
                    .returning(*_VIEW_COLUMNS)
                )
                .mappings()
                .one()
            )
        except IntegrityError as exc:
            raise _duplicate(clean_name) from exc
    return _view(updated)


def delete_filter(engine: Engine, filter_id: int) -> None:
    with engine.begin() as connection:
        result = connection.execute(delete(SavedFilter).where(SavedFilter.id == filter_id))
        if result.rowcount == 0:
            raise LibraryError("not_found", f"no saved filter with id {filter_id}")
