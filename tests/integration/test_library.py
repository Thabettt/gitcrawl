from __future__ import annotations

from datetime import datetime

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine

from serve.filter_spec import parse_filter_spec, spec_hash, spec_to_dict
from serve.library import (
    LibraryError,
    SavedFilterView,
    create_filter,
    delete_filter,
    get_filter,
    list_filters,
    rename_filter,
)

SPEC = {"gitcrawl_filter": 1, "q": "language:rust", "sort": "stars", "order": "desc"}
OTHER_SPEC = {"gitcrawl_filter": 1, "q": "topic:cli", "virtual": {"min_stars": 5}}


@pytest.fixture(scope="module", autouse=True)
def schema(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture()
def clean(alembic_engine: Engine) -> Engine:
    with alembic_engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE saved_filters RESTART IDENTITY CASCADE"))
    return alembic_engine


def test_create_filter_normalizes_spec_and_matches_library_hash(clean: Engine):
    view = create_filter(clean, "  rusty  ", {"gitcrawl_filter": 1, "q": "  language:rust   "})

    parsed = parse_filter_spec({"gitcrawl_filter": 1, "q": "language:rust"})
    assert isinstance(view, SavedFilterView)
    assert view.name == "rusty"
    assert view.filter_spec == spec_to_dict(parsed)
    assert view.filter_hash == spec_hash(parsed)
    assert isinstance(view.created_at, datetime)
    assert isinstance(view.updated_at, datetime)


def test_create_filter_rejects_empty_and_whitespace_names(clean: Engine):
    with pytest.raises(LibraryError) as empty:
        create_filter(clean, "", SPEC)
    with pytest.raises(LibraryError) as blank:
        create_filter(clean, "   ", SPEC)

    assert empty.value.code == "invalid_name"
    assert blank.value.code == "invalid_name"
    assert empty.value.hints
    assert blank.value.hints


def test_create_filter_rejects_too_long_names(clean: Engine):
    with pytest.raises(LibraryError) as excinfo:
        create_filter(clean, "a" * 81, SPEC)

    assert excinfo.value.code == "invalid_name"
    assert excinfo.value.hints


def test_create_filter_accepts_max_length_name(clean: Engine):
    view = create_filter(clean, "b" * 80, SPEC)

    assert view.name == "b" * 80


def test_create_filter_rejects_non_string_names(clean: Engine):
    with pytest.raises(LibraryError) as excinfo:
        create_filter(clean, 7, SPEC)

    assert excinfo.value.code == "invalid_name"


def test_create_filter_rejects_bad_spec_with_hints(clean: Engine):
    with pytest.raises(LibraryError) as excinfo:
        create_filter(clean, "bad", {"gitcrawl_filter": 1, "q": "x", "bogus": True})

    error = excinfo.value
    assert error.code == "invalid_spec"
    assert "bogus" in error.message
    assert error.hints


def test_create_filter_rejects_non_mapping_spec(clean: Engine):
    with pytest.raises(LibraryError) as excinfo:
        create_filter(clean, "nope", "not-a-spec")

    assert excinfo.value.code == "invalid_spec"
    assert excinfo.value.hints


def test_create_filter_duplicate_name_raises(clean: Engine):
    create_filter(clean, "dup", SPEC)

    with pytest.raises(LibraryError) as excinfo:
        create_filter(clean, "  dup  ", OTHER_SPEC)

    assert excinfo.value.code == "duplicate_name"


def test_list_filters_orders_by_name(clean: Engine):
    create_filter(clean, "zulu", SPEC)
    create_filter(clean, "alpha", OTHER_SPEC)
    create_filter(clean, "mike", SPEC)

    views = list_filters(clean)

    assert [view.name for view in views] == ["alpha", "mike", "zulu"]
    assert all(isinstance(view, SavedFilterView) for view in views)


def test_list_filters_is_empty_without_rows(clean: Engine):
    assert list_filters(clean) == []


def test_get_filter_round_trip(clean: Engine):
    created = create_filter(clean, "one", SPEC)

    fetched = get_filter(clean, created.id)

    assert fetched == created


def test_get_filter_unknown_raises_not_found(clean: Engine):
    with pytest.raises(LibraryError) as excinfo:
        get_filter(clean, 424242)

    assert excinfo.value.code == "not_found"


def test_rename_filter_persists_new_name(clean: Engine):
    created = create_filter(clean, "before", SPEC)

    renamed = rename_filter(clean, created.id, "  after  ")

    assert renamed.id == created.id
    assert renamed.name == "after"
    assert renamed.filter_hash == created.filter_hash
    assert renamed.filter_spec == created.filter_spec
    assert get_filter(clean, created.id).name == "after"


def test_rename_filter_to_same_name_is_allowed(clean: Engine):
    created = create_filter(clean, "same", SPEC)

    renamed = rename_filter(clean, created.id, " same ")

    assert renamed.name == "same"


def test_rename_filter_unknown_raises_not_found(clean: Engine):
    with pytest.raises(LibraryError) as excinfo:
        rename_filter(clean, 424242, "newname")

    assert excinfo.value.code == "not_found"


def test_rename_filter_rejects_invalid_names(clean: Engine):
    created = create_filter(clean, "valid", SPEC)

    with pytest.raises(LibraryError) as empty:
        rename_filter(clean, created.id, "  ")
    with pytest.raises(LibraryError) as too_long:
        rename_filter(clean, created.id, "c" * 81)

    assert empty.value.code == "invalid_name"
    assert too_long.value.code == "invalid_name"


def test_rename_filter_collision_raises_duplicate_name(clean: Engine):
    first = create_filter(clean, "first", SPEC)
    create_filter(clean, "second", OTHER_SPEC)

    with pytest.raises(LibraryError) as excinfo:
        rename_filter(clean, first.id, "second")

    assert excinfo.value.code == "duplicate_name"
    assert get_filter(clean, first.id).name == "first"


def test_delete_filter_removes_row_then_raises_not_found(clean: Engine):
    created = create_filter(clean, "gone", SPEC)

    delete_filter(clean, created.id)

    with pytest.raises(LibraryError):
        get_filter(clean, created.id)
    with pytest.raises(LibraryError) as excinfo:
        delete_filter(clean, created.id)
    assert excinfo.value.code == "not_found"
