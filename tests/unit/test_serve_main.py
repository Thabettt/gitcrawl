from __future__ import annotations


def test_serve_main_is_importable():
    import serve.__main__ as serve_main

    assert callable(serve_main.main)
