from __future__ import annotations

import hashlib
import json
from pathlib import Path

from serve.app import create_app

PIN_FILE = Path(__file__).resolve().parent / "snapshots" / "openapi.sha256"


def openapi_digest() -> str:
    schema = create_app().openapi()
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def test_openapi_schema_is_pinned():
    assert PIN_FILE.is_file(), f"missing OpenAPI pin at {PIN_FILE}"
    pinned = PIN_FILE.read_text(encoding="utf-8").strip()
    actual = openapi_digest()
    assert actual == pinned, (
        f"OpenAPI schema changed: pinned={pinned} actual={actual}; "
        f"update {PIN_FILE} only if the contract change is intended"
    )
