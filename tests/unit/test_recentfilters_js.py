from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_recent_filters_module(repo_root: Path):
    result = subprocess.run(
        ["node", "--test", str(repo_root / "tests/js/recentfilters.test.mjs")],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
