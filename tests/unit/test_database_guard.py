from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = "tests/contract/test_pages.py::test_dashboard_renders_empty_state_and_key_elements"


def test_missing_test_database_url_fails_when_required():
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TEST_DATABASE_URL", "DATABASE_URL"}
    }
    env["GITCRAWL_REQUIRE_TEST_DB"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", TARGET],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "TEST_DATABASE_URL" in result.stdout
    assert "skipped" not in result.stdout.lower()
