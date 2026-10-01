from __future__ import annotations

import difflib
import json


def test_golden_endpoints_match_snapshots(golden_observations, snapshot_dir):
    problems: list[str] = []
    for observation in golden_observations:
        snapshot = snapshot_dir / f"{observation.case}.json"
        if not snapshot.is_file():
            problems.append(
                f"{observation.case}: missing {snapshot}; "
                "regenerate with UPDATE_GOLDEN=1 pytest tests/golden -q"
            )
            continue
        expected = json.loads(snapshot.read_text(encoding="utf-8"))
        actual = observation.to_document()
        if expected == actual:
            continue
        problems.append(
            "\n".join(
                difflib.unified_diff(
                    json.dumps(expected, indent=2, sort_keys=True).splitlines(),
                    json.dumps(actual, indent=2, sort_keys=True).splitlines(),
                    fromfile=str(snapshot),
                    tofile=f"observed {observation.path}",
                    lineterm="",
                )
            )
        )
    assert not problems, "\n\n".join(problems)


def test_snapshot_set_matches_route_set(golden_observations, snapshot_dir):
    expected = {observation.case for observation in golden_observations}
    existing = {path.stem for path in snapshot_dir.glob("*.json")}
    assert existing - expected == set(), (
        f"stale golden snapshots: {sorted(existing - expected)}; "
        "delete them or add the route case"
    )
