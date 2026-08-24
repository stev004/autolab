from __future__ import annotations

import ast
from pathlib import Path


def test_optimization_package_cannot_import_hidden_simulation() -> None:
    package = Path(__file__).parents[3] / "src" / "autonomous_lab" / "optimization"
    forbidden = (
        "autonomous_lab.simulation",
        "autonomous_lab.hardware",
        "autonomous_lab.orchestration",
    )
    violations: list[str] = []
    for source_path in package.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = (node.module or "",)
            else:
                continue
            for name in imported:
                if name.startswith(forbidden):
                    violations.append(f"{source_path.name}:{node.lineno}:{name}")

    assert violations == []
