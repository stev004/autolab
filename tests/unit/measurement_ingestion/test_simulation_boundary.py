from __future__ import annotations

import ast
from pathlib import Path


def test_external_ingestion_never_imports_simulation() -> None:
    package = Path("src/autonomous_lab/measurement_ingestion")
    violations: list[str] = []
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.startswith("autonomous_lab.simulation") for name in names):
                violations.append(f"{path}:{node.lineno}")
    assert not violations, f"external ingestion imports hidden simulation: {violations}"
