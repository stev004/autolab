"""Re-run the external audit's surviving safety mutants against the culture tests.

Each mutant is a one-line change that weakens a safety check. A test suite that
still passes with the mutant applied does not protect that check. Run with
``uv run python scripts/check_safety_mutants.py``; it applies each mutant to a
temporary copy of ``src`` (the checkout is never modified), runs the culture
tests and prints KILLED or SURVIVED. Exit status is 1 if any mutant survives.

The mutant numbers follow the audit (M1, M2, M4, M6, M8, M10); M2 has two
variants because "treat a possibly-sent setpoint as never sent" can be written
two ways.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "src/autonomous_lab/culture/runner.py"
SERVICE = "src/autonomous_lab/campaigns/culture_service.py"
TESTS = ("tests/unit/culture", "tests/e2e/test_culture_campaign.py")


@dataclass(frozen=True)
class Mutant:
    name: str
    description: str
    path: str
    original: str
    mutated: str


MUTANTS = (
    Mutant(
        "M1",
        "resume ignores protection_tripped",
        RUNNER,
        "\n                    or sample.protection_tripped",
        "",
    ),
    Mutant(
        "M2a",
        "resume treats a journalled-but-unacknowledged setpoint as never sent",
        RUNNER,
        "if not any(item.sent for item in replicates):",
        "if not any(item.acknowledgement is not None for item in replicates):",
    ),
    Mutant(
        "M2b",
        "resume always treats an interrupted dispatch as never sent",
        RUNNER,
        "if not any(item.sent for item in replicates):",
        "if True:",
    ),
    Mutant(
        "M4",
        "receipt policy-hash staleness check removed",
        RUNNER,
        "if self.policy.sha256 != receipt.validation_policy_hash:",
        "if False:",
    ),
    Mutant(
        "M6",
        "sensor request_id mismatch check removed",
        RUNNER,
        "if measurement.request_id != request.request_id:",
        "if False:",
    ),
    Mutant(
        "M8",
        "journal replay forgets protection trips",
        SERVICE,
        'if event.reactor_id and event.code == "protection_tripped":',
        "if False:",
    ),
    Mutant(
        "M10",
        "readings whose window closed during an outage are not skipped",
        RUNNER,
        "if late and point.window(started_at)[1] < self.clock.now():",
        "if False:",
    ),
)


def tests_pass(workspace: Path) -> bool:
    env = {**os.environ, "PYTHONPATH": str(workspace / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *TESTS],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def run(mutant: Mutant, workspace: Path) -> bool:
    """Apply ``mutant`` in ``workspace`` and return True if the tests kill it."""

    target = workspace / mutant.path
    pristine = (ROOT / mutant.path).read_text(encoding="utf-8")
    if pristine.count(mutant.original) != 1:
        raise SystemExit(f"{mutant.name}: anchor not found exactly once in {mutant.path}")
    target.write_text(pristine.replace(mutant.original, mutant.mutated), encoding="utf-8")
    try:
        return not tests_pass(workspace)
    finally:
        target.write_text(pristine, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as scratch:
        workspace = Path(scratch)
        shutil.copytree(ROOT / "src", workspace / "src")
        # Source installs find their Alembic migrations next to src/.
        shutil.copytree(ROOT / "migrations", workspace / "migrations")
        # Control: a failure unrelated to any mutant would make every mutant look
        # killed, so the unmutated tests must pass first.
        if not tests_pass(workspace):
            print("the culture tests fail without any mutant; fix them first")
            return 2
        survivors = []
        for mutant in MUTANTS:
            killed = run(mutant, workspace)
            print(f"{mutant.name:4} {'KILLED  ' if killed else 'SURVIVED'} {mutant.description}")
            if not killed:
                survivors.append(mutant.name)
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
