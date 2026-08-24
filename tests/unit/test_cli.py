import json
from pathlib import Path
from uuid import UUID

import yaml
from typer.testing import CliRunner

from autonomous_lab.campaigns.loader import load_campaign
from autonomous_lab.cli import app
from autonomous_lab.storage import Storage

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = ROOT / "configs" / "assay-reagent-reduction.yaml"
POLICY = ROOT / "configs" / "validation-policy.yaml"
runner = CliRunner()


def test_top_level_help_exposes_command_tree() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "campaign" in result.stdout
    assert "experiment" in result.stdout
    assert "init" in result.stdout


def test_init_creates_database_schema(tmp_path: Path) -> None:
    database = tmp_path / "autolab.sqlite3"

    result = runner.invoke(app, ["init", "--database", str(database)])

    assert result.exit_code == 0
    assert database.exists()
    assert (tmp_path / "raw").is_dir()
    assert (tmp_path / "processed").is_dir()
    assert (tmp_path / "optimizer").is_dir()
    assert "initialized AutoLab database" in result.stdout


def test_campaign_validate_compiles_midpoint_without_execution() -> None:
    result = runner.invoke(
        app,
        [
            "campaign",
            "validate",
            str(CAMPAIGN),
            "--policy",
            str(POLICY),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["report"]["status"] == "passed"
    assert payload["vessel_count"] == 27
    assert payload["step_count"] == 83
    assert len(payload["protocol_hash"]) == 64


def test_campaign_validate_returns_nonzero_and_issues_for_rejection(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    raw["wavelength_range"]["maximum"]["value"] = 500
    restrictive_policy = tmp_path / "policy.yaml"
    restrictive_policy.write_text(yaml.safe_dump(raw), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "campaign",
            "validate",
            str(CAMPAIGN),
            "--policy",
            str(restrictive_policy),
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["report"]["status"] == "rejected"
    assert "measurement.wavelength_outside_policy" in {
        issue["code"] for issue in payload["report"]["issues"]
    }


def test_status_and_experiment_show_read_persisted_evidence(tmp_path: Path) -> None:
    database = tmp_path / "autolab.sqlite3"
    storage = Storage.open(str(database))
    storage.initialize()
    campaign_id = UUID("00000000-0000-0000-0000-000000000501")
    experiment_id = UUID("00000000-0000-0000-0000-000000000502")
    try:
        with storage.unit_of_work() as uow:
            uow.campaigns.add(
                campaign_id=str(campaign_id),
                name="cli-test",
                state="ready",
                schema_version="1",
                config_source="schema_version: '1'",
                config={"schema_version": "1"},
                objective={"metrics": []},
                parameter_space={"parameters": {}},
            )
            uow.experiments.add(
                experiment_id=str(experiment_id),
                campaign_id=str(campaign_id),
                ordinal=0,
                state="proposed",
            )
            uow.records.add_metric(
                experiment_id=str(experiment_id),
                name="signal_to_noise",
                value=12.5,
                unit="dimensionless",
                algorithm_name="test-analysis",
                algorithm_version="1",
            )
            uow.commit()
    finally:
        storage.close()

    status = runner.invoke(
        app,
        ["campaign", "status", str(campaign_id), "--database", str(database), "--json"],
    )
    shown = runner.invoke(
        app,
        ["experiment", "show", str(experiment_id), "--database", str(database), "--json"],
    )

    assert status.exit_code == 0
    assert json.loads(status.stdout)["experiment_count"] == 1
    assert shown.exit_code == 0
    experiment = json.loads(shown.stdout)
    assert experiment["state"] == "proposed"
    assert experiment["measurement_count"] == 0
    assert experiment["metrics"] == {"signal_to_noise": 12.5}


def test_create_run_and_report_one_experiment_campaign(tmp_path: Path) -> None:
    raw = yaml.safe_load(CAMPAIGN.read_text(encoding="utf-8"))
    raw["campaign"]["max_experiments"] = 1
    raw["optimization"]["strategy"] = "random"
    raw["optimization"]["startup_experiments"] = 1
    config = tmp_path / "one-experiment.yaml"
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    database = tmp_path / "autolab.sqlite3"
    optimizer_root = tmp_path / "optimizer"

    initialized = runner.invoke(app, ["init", "--database", str(database)])
    created = runner.invoke(
        app,
        [
            "campaign",
            "create",
            str(config),
            "--database",
            str(database),
            "--policy",
            str(POLICY),
            "--optimizer-root",
            str(optimizer_root),
            "--id-only",
        ],
    )

    assert initialized.exit_code == 0
    assert created.exit_code == 0, created.stderr
    campaign_id = created.stdout.strip()
    assert UUID(campaign_id)

    run = runner.invoke(
        app,
        [
            "campaign",
            "run",
            campaign_id,
            "--database",
            str(database),
            "--policy",
            str(POLICY),
            "--optimizer-root",
            str(optimizer_root),
        ],
    )
    report = runner.invoke(
        app,
        [
            "campaign",
            "report",
            campaign_id,
            "--database",
            str(database),
            "--policy",
            str(POLICY),
        ],
    )

    assert run.exit_code == 0, run.stderr
    assert "state: completed" in run.stdout
    assert report.exit_code == 0, report.stderr
    payload = json.loads(report.stdout)
    assert payload["campaign_id"] == campaign_id
    assert payload["experiment_count"] == 1
    assert all(
        "experiment_id" in solution and "parameters" in solution
        for solution in payload["best_feasible_by_objective"].values()
    )
    loaded = load_campaign(config)
    assert payload["reproducibility"]["campaign_config_sha256"] == loaded.canonical_sha256
    assert payload["reproducibility"]["campaign_source_sha256"] == loaded.source_sha256
