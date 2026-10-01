from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_TRUNCATE_FILES = {
    "tests/conftest.py",
    "tests/golden/conftest.py",
    "tests/integration/test_golden_org.py",
    "tests/unit/test_since_scan.py",
    "tests/unit/test_state_machine.py",
    "tests/unit/test_fixture_centralization.py",
}


def _truncate_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED_TRUNCATE_FILES:
            continue
        if "TRUNCATE TABLE" in path.read_text(encoding="utf-8"):
            offenders.append(relative)
    return offenders


def test_truncate_sql_lives_only_in_the_shared_factory():
    assert _truncate_offenders() == []
