from __future__ import annotations

import ast
from pathlib import Path


def test_research_agent_package_cannot_import_execution_layers() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "autonomous_lab" / "agents"
    forbidden = {
        "autonomous_lab.hardware",
        "autonomous_lab.orchestration",
        "autonomous_lab.protocols",
        "autonomous_lab.validation",
    }

    for source_path in root.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not any(
                    node.module == name or node.module.startswith(f"{name}.")
                    for name in forbidden
                )
            if isinstance(node, ast.Import):
                assert not any(
                    alias.name == name or alias.name.startswith(f"{name}.")
                    for alias in node.names
                    for name in forbidden
                )
