from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_rownav_module():
    result = subprocess.run(
        ["node", "--test", "tests/js/rownav.test.mjs"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
