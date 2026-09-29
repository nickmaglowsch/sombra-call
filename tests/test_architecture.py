"""Enforces the module boundary rule from docs/ARCHITECTURE.md.

Feature packages (``sombra.audio``, ``sombra.trigger``, ...) may import only
``sombra.contracts`` and their own package. Only the wiring layer may import
concrete implementations from several packages.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sombra"
WIRING = {"orchestrator", "cli", "config"}  # allowed to import any sombra package
SHARED = {"contracts"}  # importable by everyone


def _sombra_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] == "sombra" and len(parts) > 1:
                found.add(parts[1])
    return found


def test_feature_packages_only_import_contracts() -> None:
    violations = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        owner = rel.parts[0].removesuffix(".py")
        if owner in WIRING or owner == "__init__":
            continue
        for imported in _sombra_imports(path) - SHARED - {owner}:
            violations.append(f"{rel} imports sombra.{imported}")
    assert not violations, "cross-module imports (use contracts + orchestrator):\n" + "\n".join(
        violations
    )
