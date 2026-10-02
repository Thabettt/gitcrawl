from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_PACKAGES = ("discover", "enrich", "hydrate", "scheduler", "limiter", "store", "lib")


def _is_type_checking(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    )


def _runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parent = parents.get(node)
            in_type_checking = False
            while parent is not None:
                if isinstance(parent, ast.If) and _is_type_checking(parent.test):
                    in_type_checking = True
                    break
                parent = parents.get(parent)
            if not in_type_checking:
                names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _offenders(package: str, prefix: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "src" / package).rglob("*.py")):
        hits = sorted(
            name
            for name in _runtime_imports(path)
            if name == prefix or name.startswith(prefix + ".")
        )
        if hits:
            found[path.relative_to(ROOT).as_posix()] = hits
    return found


def test_core_never_imports_serve():
    offenders: dict[str, list[str]] = {}
    for package in CORE_PACKAGES:
        offenders.update(_offenders(package, "serve"))
    assert offenders == {}


def test_store_does_not_import_hydrate_at_runtime():
    assert _offenders("store", "hydrate") == {}
