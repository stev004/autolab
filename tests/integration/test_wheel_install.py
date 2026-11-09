"""Release smoke test against the built wheel, outside the source checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_built_wheel_initializes_without_checkout_resources(tmp_path: Path) -> None:
    wheel_directory = os.environ.get("AUTOLAB_WHEEL_DIR")
    if wheel_directory is None:
        pytest.skip("Set AUTOLAB_WHEEL_DIR after uv build --wheel to test the release artifact")
    uv = shutil.which("uv")
    assert uv is not None, "uv is required for the offline wheel installation smoke test"
    wheels = list(Path(wheel_directory).resolve().glob("*.whl"))
    assert len(wheels) == 1, f"Expected one release wheel, found {wheels}"
    target = tmp_path / "installed"
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(target),
            "--cache-dir",
            str(tmp_path / "uv-cache"),
            str(wheels[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Reuse already installed dependency versions, but import AutoLab solely
    # from the installed wheel. Isolated mode ignores source PYTHONPATH values.
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import autonomous_lab
from autonomous_lab.cli import app
from autonomous_lab.storage import Storage
from sqlalchemy import inspect
assert Path(autonomous_lab.__file__).is_relative_to(Path(sys.argv[1]))
try:
    app(['init', '--database', sys.argv[2]])
except SystemExit as exc:
    assert exc.code in (None, 0), exc.code
storage = Storage.open(sys.argv[2])
try:
    assert 'external_measurement_revisions' in inspect(storage.database.engine).get_table_names()
finally:
    storage.close()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(target), str(tmp_path / "wheel.sqlite3")],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "initialized AutoLab database" in result.stdout
