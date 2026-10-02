from __future__ import annotations

from serve.forms import _int_or_raw, build_spec_from_form


def test_int_or_raw_converts_ascii_digits():
    assert _int_or_raw("123") == 123


def test_int_or_raw_rejects_non_ascii_digits():
    assert _int_or_raw("١٢٣") == "١٢٣"
    assert _int_or_raw("１２３") == "１２３"
    assert _int_or_raw("²") == "²"


def test_form_virtuals_keep_non_ascii_digits_as_text():
    spec = build_spec_from_form({"min_stars": "١٢٣"})

    assert spec["virtual"]["min_stars"] == "١٢٣"
