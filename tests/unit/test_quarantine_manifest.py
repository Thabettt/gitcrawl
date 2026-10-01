from __future__ import annotations

import ast
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]
ROOT = TESTS_ROOT.parent
SRC = ROOT / "src"
MANIFEST = TESTS_ROOT / "quarantine_manifest.txt"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _src_modules() -> set[str]:
    return {_module_name(path) for path in SRC.rglob("*.py")}


def _test_imports() -> set[str]:
    names: set[str] = set()
    for path in TESTS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
    return names


def _manifest_entries() -> list[str]:
    return [
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _imported_by_tests(module: str, imports: set[str]) -> bool:
    return any(name == module or name.startswith(module + ".") for name in imports)


def _entry_covers(entry: str, module: str) -> bool:
    return entry == module or entry.startswith(module + ".")


def test_every_src_module_is_tested_or_quarantined():
    imports = _test_imports()
    entries = _manifest_entries()
    missing = [
        module
        for module in sorted(_src_modules())
        if not _imported_by_tests(module, imports)
        and not any(_entry_covers(entry, module) for entry in entries)
    ]
    assert missing == []


def test_every_quarantine_entry_has_a_test():
    imports = _test_imports()
    modules = _src_modules()
    bodies = [path.read_text(encoding="utf-8") for path in TESTS_ROOT.rglob("*.py")]
    unresolved: list[str] = []
    for entry in _manifest_entries():
        if entry in modules:
            covered = _imported_by_tests(entry, imports)
        else:
            _, _, name = entry.rpartition(".")
            covered = any(name in body for body in bodies)
        if not covered:
            unresolved.append(entry)
    assert unresolved == []


def test_manifest_entries_exist_in_src():
    for entry in _manifest_entries():
        if (SRC / (entry.replace(".", "/") + ".py")).is_file():
            continue
        module, _, name = entry.rpartition(".")
        source = SRC / (module.replace(".", "/") + ".py")
        assert source.is_file(), f"manifest module is missing: {module}"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        assert name in names, f"{entry} is not defined in {module}"
