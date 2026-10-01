from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_no_relative_path_literals_in_tests(repo_root: Path):
    offenders: list[str] = []
    for path in sorted((repo_root / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "Path":
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if not os.path.isabs(first.value):
                    offenders.append(f"{path.relative_to(repo_root).as_posix()}:{node.lineno}")
    assert offenders == []


def _run_from_another_cwd(repo_root: Path, tmp_path: Path, target: str, *extra: str):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(repo_root / target),
            *extra,
            "-q",
            "-p",
            "no:warnings",
        ],
        cwd=str(tmp_path),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )


def test_css_asset_test_passes_from_another_cwd(repo_root: Path, tmp_path: Path):
    result = _run_from_another_cwd(
        repo_root,
        tmp_path,
        "tests/contract/test_console_pages.py",
        "-k",
        "app_css_uses_compositor_progress",
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_wrapper_passes_from_another_cwd(repo_root: Path, tmp_path: Path):
    result = _run_from_another_cwd(repo_root, tmp_path, "tests/unit/test_rownav_js.py")
    assert result.returncode == 0, result.stdout + result.stderr
