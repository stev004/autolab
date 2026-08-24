import ast
from pathlib import Path


def test_sensor_boundary_never_imports_assay_simulation() -> None:
    root = Path("src/autonomous_lab/hardware/sensors")
    forbidden: list[tuple[Path, str]] = []

    for source_file in root.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            imported: list[str]
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            forbidden.extend(
                (source_file, name)
                for name in imported
                if name == "autonomous_lab.simulation"
                or name.startswith("autonomous_lab.simulation.")
            )

    assert forbidden == []
