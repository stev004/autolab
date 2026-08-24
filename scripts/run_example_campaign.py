"""Run the documented V0 CLI workflow from an installed development environment."""

from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    database = root / "data" / "autolab.sqlite3"
    config = root / "configs" / "assay-reagent-reduction.yaml"
    policy = root / "configs" / "validation-policy.yaml"
    subprocess.run(
        ["autolab", "init", "--database", str(database)],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["autolab", "campaign", "validate", str(config), "--policy", str(policy)],
        cwd=root,
        check=True,
    )
    created = subprocess.run(
        [
            "autolab",
            "campaign",
            "create",
            str(config),
            "--database",
            str(database),
            "--id-only",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    campaign_id = created.stdout.strip()
    subprocess.run(
        [
            "autolab",
            "campaign",
            "run",
            campaign_id,
            "--database",
            str(database),
        ],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            "autolab",
            "campaign",
            "report",
            campaign_id,
            "--database",
            str(database),
        ],
        cwd=root,
        check=True,
    )


if __name__ == "__main__":
    main()
