"""Culture campaigns end to end: ask -> culture -> evidence in SQLite -> tell -> resume.

The rig is scripted (see tests/support/culture_fakes.py). Its OD response is a
made-up function of the applied setpoint so the optimiser has something to
learn; it is not a growth model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import pytest
import yaml
from culture_fakes import (
    START,
    TEMP,
    ScriptedController,
    ScriptedODSensor,
    capabilities,
    policy,
    variables,
)
from sqlalchemy import func, select
from typer.testing import CliRunner

from autonomous_lab.campaigns import culture_service as culture_service_module
from autonomous_lab.campaigns.culture_service import CultureCampaignService, CultureRig
from autonomous_lab.campaigns.lifecycle import CampaignExecutionError
from autonomous_lab.campaigns.loader import (
    ConfigurationLoadError,
    load_campaign,
    load_culture_campaign,
)
from autonomous_lab.campaigns.service import CampaignService
from autonomous_lab.cli import app
from autonomous_lab.culture import CultureClock, SteppedClock
from autonomous_lab.domain.enums import CampaignState, ExperimentState
from autonomous_lab.storage import Database, Storage
from autonomous_lab.storage.orm import (
    ActionEventRecord,
    ArtifactRecord,
    DerivedMetricRecord,
    ExecutionRunRecord,
    ExperimentRecord,
    MeasurementRecord,
    OptimizerObservationRecord,
    QCRunRecord,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "1",
        "experiment_type": "culture",
        "campaign": {"name": "scripted_culture", "seed": 3, "max_experiments": 4},
        "objective": {
            "metrics": [
                {"name": "final_od750", "direction": "maximize", "unit": "dimensionless"},
                {"name": "energy_kwh", "direction": "minimize", "unit": "kilowatt_hour"},
            ]
        },
        "parameter_space": {
            "temperature": {
                "kind": "continuous",
                "unit": "degC",
                "minimum": 20,
                "maximum": 34,
                "resolution": 0.1,
            },
            "light_intensity": {
                "kind": "continuous",
                "unit": "micromole / meter ** 2 / second",
                "minimum": 30,
                "maximum": 400,
                "resolution": 1,
            },
        },
        "culture_design": {
            "reactor_ids": ["reactor-1"],
            "duration": {"value": 24, "unit": "hour"},
            "sampling_interval": {"value": 6, "unit": "hour"},
            "sample_tolerance": {"value": 10, "unit": "minute"},
            "telemetry_interval": {"value": 1, "unit": "hour"},
        },
        "execution": {
            "controller": {"adapter": "scripted", "controller_id": "ctrl-1"},
            "clock": "simulated",
        },
        "optimization": {"strategy": "random", "startup_experiments": 2},
    }
    document.update(overrides)
    return document


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "culture.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


@dataclass
class ScriptedRig:
    """Builds controller + sensor pairs sharing the campaign clock.

    With ``reuse_controller`` the same controller object survives between
    ``run`` calls, like a real device that keeps holding while AutoLab restarts.
    """

    controller_kwargs: dict[str, object] = field(default_factory=dict)
    sensor_fault: BaseException | None = None
    fault_after_reads: int = 0
    reuse_controller: bool = False
    controllers: list[ScriptedController] = field(default_factory=list)

    async def __call__(self, config: object, clock: CultureClock) -> CultureRig:
        assert isinstance(clock, SteppedClock)
        if self.reuse_controller and self.controllers:
            controller = self.controllers[-1]
            controller.clock = clock
        else:
            controller = ScriptedController(clock=clock, **self.controller_kwargs)  # type: ignore[arg-type]
            self.controllers.append(controller)
        sensor = _SetpointResponsiveSensor(clock=clock, controller=controller)
        sensor.fault = self.sensor_fault
        sensor.fault_after_reads = self.fault_after_reads
        return CultureRig(controller=controller, sensor=sensor)


class _SetpointResponsiveSensor(ScriptedODSensor):
    """Scripted OD that rises fastest near 28 degC. Illustrative only."""

    def __init__(self, *, clock: SteppedClock, controller: ScriptedController) -> None:
        super().__init__(clock=clock, od=self._od)
        self.controller = controller
        self.fault: BaseException | None = None
        self.fault_after_reads = 0

    def _od(self, _hours_since_start: Decimal, reactor_id: str) -> Decimal:
        if self.fault is not None and len(self.reads) > self.fault_after_reads:
            raise self.fault
        applied = self.controller.applied[reactor_id]
        started = self.controller.started[reactor_id]
        hours = Decimal(str((self.clock.now() - started).total_seconds())) / 3600
        setpoint = applied.setpoints["temperature"].to(TEMP).value
        rate = Decimal("0.06") - Decimal("0.004") * abs(setpoint - 28)
        return (Decimal("0.1") + max(rate, Decimal("0.001")) * hours).quantize(Decimal("0.0001"))


def _service(
    tmp_path: Path, rig: ScriptedRig, *, start: Any = START, clock: SteppedClock | None = None
) -> tuple[Storage, CultureCampaignService]:
    storage = Storage(Database(tmp_path / "culture.sqlite3"))
    storage.initialize()
    return storage, CultureCampaignService(
        storage,
        policy=policy(),
        optimizer_root=tmp_path / "optimizer",
        artifact_root=tmp_path / "artifacts",
        prefer_gp=False,
        rig_factory=rig,
        clock_factory=lambda _config: clock or SteppedClock(start),
    )


def _count(storage: Storage, model: type, **where: object) -> int:
    with storage.unit_of_work() as uow:
        assert uow.session is not None
        query = select(func.count()).select_from(model)
        for name, value in where.items():
            query = query.where(getattr(model, name) == value)
        return int(uow.session.scalar(query) or 0)


def test_culture_campaign_runs_to_completion_with_full_evidence_trail(tmp_path: Path) -> None:
    rig = ScriptedRig()
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))
    status = asyncio.run(service.run(campaign_id))

    assert status.state is CampaignState.COMPLETED
    assert status.experiment_count == 4
    assert status.valid_observation_count == 4
    assert status.pareto_frontier
    assert _count(storage, ExperimentRecord, state=ExperimentState.OBSERVED.value) == 4
    assert _count(storage, ExecutionRunRecord, status="succeeded") == 4
    # The run journal: every command, ack, hold start, telemetry sample, reading and
    # safe-state result was committed as it happened.
    assert _count(storage, ActionEventRecord, event_type="setpoint_command") == 4
    assert _count(storage, ActionEventRecord, event_type="setpoint_ack") == 4
    assert _count(storage, ActionEventRecord, event_type="hold_started") == 4
    assert _count(storage, ActionEventRecord, event_type="telemetry") == 4 * 25  # hourly, 0-24 h
    assert _count(storage, ActionEventRecord, event_type="reading") == 16
    assert _count(storage, ActionEventRecord, event_type="safe_state") == 4
    assert _count(storage, MeasurementRecord) == 16  # readings at 6, 12, 18, 24 h
    assert _count(storage, QCRunRecord, status="passed") == 4
    assert _count(storage, DerivedMetricRecord) == 8  # final_od750 + energy_kwh
    assert _count(storage, OptimizerObservationRecord) == 4
    # Every safe-state request was confirmed.
    with storage.unit_of_work() as uow:
        assert uow.session is not None
        safe = list(uow.session.scalars(select(ExecutionRunRecord.safe_state_reached)))
        artifacts = list(uow.session.execute(select(ArtifactRecord.uri, ArtifactRecord.sha256)))
    assert safe == [True] * 4
    # The complete run record is content-addressed: its bytes hash to the stored SHA.
    assert len(artifacts) == 4
    for uri, sha256 in artifacts:
        content = Path(urlparse(uri).path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == sha256
    report = service.report(campaign_id)
    assert report["valid_observations"] == 4
    assert report["evidence_origins"]["od_readings_from_simulated_or_mock_sensors"] == 16  # type: ignore[index]


def test_pause_and_resume_at_experiment_boundaries(tmp_path: Path) -> None:
    rig = ScriptedRig()
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))
    paused = asyncio.run(service.run(campaign_id, max_new_experiments=2))
    assert paused.state is CampaignState.PAUSED and paused.experiment_count == 2

    _, resumed_service = _service(tmp_path, rig, start=START.replace(day=10))
    done = asyncio.run(resumed_service.run(campaign_id))
    assert done.state is CampaignState.COMPLETED and done.experiment_count == 4
    assert _count(storage, OptimizerObservationRecord) == 4


def test_execution_failure_pauses_the_campaign_with_evidence(tmp_path: Path) -> None:
    rig = ScriptedRig(controller_kwargs={"internal_maximum_temperature": Decimal(10)})
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))
    status = asyncio.run(service.run(campaign_id))

    assert status.state is CampaignState.PAUSED
    assert status.execution_failed_count == 1 and status.experiment_count == 1
    with storage.unit_of_work() as uow:
        assert uow.session is not None
        run = uow.session.execute(
            select(ExecutionRunRecord.status, ExecutionRunRecord.safe_state_reached)
        ).one()
        failure = uow.session.scalars(select(ExperimentRecord.failure_message)).one()
    assert tuple(run) == ("failed", True)
    assert failure == "setpoint_out_of_device_range"
    assert rig.controllers[0].safe_state_calls  # safe state requested


def test_qc_failures_are_excluded_but_the_campaign_continues(tmp_path: Path) -> None:
    rig = ScriptedRig(controller_kwargs={"trip_after_hours": Decimal(8)})
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))
    status = asyncio.run(service.run(campaign_id))

    assert status.state is CampaignState.COMPLETED
    assert status.qc_failed_count == 4 and status.valid_observation_count == 0
    assert _count(storage, QCRunRecord, status="failed") == 4
    assert _count(storage, DerivedMetricRecord) == 0


def test_simulated_clock_is_refused_for_real_hardware(tmp_path: Path) -> None:
    rig = ScriptedRig(controller_kwargs={"manifest": capabilities(is_simulated=False)})
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))
    with pytest.raises(CampaignExecutionError, match="simulated clock"):
        asyncio.run(service.run(campaign_id))
    assert service.status(campaign_id).state is CampaignState.READY
    assert _count(storage, ExperimentRecord) == 0


def test_crash_mid_hold_is_closed_safely_on_the_next_run(tmp_path: Path) -> None:
    rig = ScriptedRig(sensor_fault=RuntimeError("adapter bug"))
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))
    with pytest.raises(RuntimeError, match="adapter bug"):
        asyncio.run(service.run(campaign_id))
    # Pre-dispatch facts and the journal were durable before the crash.
    assert _count(storage, ExperimentRecord, state=ExperimentState.RUNNING.value) == 1
    assert rig.controllers[0].safe_state_calls == ["execution interrupted"]
    assert _count(storage, ActionEventRecord, event_type="safe_state") == 1

    rig.sensor_fault = None
    status = asyncio.run(service.run(campaign_id))
    # The journal shows the hold ended safely, so the run closes without an operator:
    # it is excluded (readings missing) and the campaign carries on.
    assert status.state is CampaignState.COMPLETED
    assert status.qc_failed_count == 1 and status.valid_observation_count == 3


def test_assay_and_culture_paths_refuse_each_others_campaigns(tmp_path: Path) -> None:
    rig = ScriptedRig()
    storage, service = _service(tmp_path, rig)
    path = _write(tmp_path, _document())
    campaign_id = service.create(load_culture_campaign(path))
    with pytest.raises(ConfigurationLoadError, match="culture"):
        load_campaign(path)
    with pytest.raises(ConfigurationLoadError, match="not a culture campaign"):
        load_culture_campaign(PROJECT_ROOT / "configs" / "assay-reagent-reduction.yaml")
    from autonomous_lab.campaigns.loader import load_validation_policy

    assay = CampaignService(
        storage,
        validation_policy=load_validation_policy(PROJECT_ROOT / "configs/validation-policy.yaml"),
    )
    with pytest.raises(CampaignExecutionError, match="culture"):
        assay.status(campaign_id)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "objective": {
                    "metrics": [{"name": "cost_per_test", "direction": "minimize", "unit": "GBP"}]
                }
            },
            "does not produce",
        ),
        (
            {
                "parameter_space": {
                    "Bad Name": {
                        "kind": "continuous",
                        "unit": "dimensionless",
                        "minimum": 6,
                        "maximum": 8,
                    }
                }
            },
            "not a valid controlled-variable name",
        ),
    ],
)
def test_culture_config_rejects_unmeasurable_objectives_and_wrong_parameters(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigurationLoadError, match=message):
        load_culture_campaign(_write(tmp_path, _document(**overrides)))


def test_shipped_example_config_and_policy_load() -> None:
    from autonomous_lab.campaigns.loader import load_culture_policy

    loaded = load_culture_campaign(PROJECT_ROOT / "configs" / "culture-algae-sim.yaml")
    assert loaded.config.execution.controller.adapter == "algae-sim-reactor"
    load_culture_policy(PROJECT_ROOT / "configs" / "culture-policy.yaml")


def test_cli_creates_runs_and_reports_a_culture_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = ScriptedRig()
    monkeypatch.setattr(culture_service_module, "installed_rig", rig)
    monkeypatch.setattr(
        culture_service_module, "default_clock", lambda _config: SteppedClock(START)
    )
    database = str(tmp_path / "cli.sqlite3")
    policy_path = PROJECT_ROOT / "configs" / "culture-policy.yaml"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--database", database]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "culture",
            "create",
            str(_write(tmp_path, _document())),
            "--database",
            database,
            "--policy",
            str(policy_path),
            "--optimizer-root",
            str(tmp_path / "opt"),
            "--id-only",
        ],
    )
    assert created.exit_code == 0, created.output
    campaign_id = created.output.strip()
    ran = runner.invoke(
        app,
        [
            "culture",
            "run",
            campaign_id,
            "--database",
            database,
            "--policy",
            str(policy_path),
            "--optimizer-root",
            str(tmp_path / "opt"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--quiet",
        ],
    )
    assert ran.exit_code == 0, ran.output
    assert "completed" in ran.output
    status = runner.invoke(app, ["campaign", "status", campaign_id, "--database", database])
    assert status.exit_code == 0 and "observed" in status.output
    report = runner.invoke(
        app,
        ["culture", "report", campaign_id, "--database", database, "--policy", str(policy_path)],
    )
    assert report.exit_code == 0 and "AutoLab culture campaign results" in report.output
    wrong = runner.invoke(app, ["campaign", "report", campaign_id, "--database", database])
    assert wrong.exit_code != 0
    # Operator commands validate their targets and need an operator and reason.
    missing = str(UUID(int=0))
    for command in ("reconcile", "flag-contamination"):
        refused = runner.invoke(
            app,
            [
                "culture",
                command,
                campaign_id,
                missing,
                "--operator",
                "steven",
                "--reason",
                "check",
                "--database",
                database,
                "--policy",
                str(policy_path),
            ],
        )
        assert refused.exit_code == 2, refused.output


def _replicated(**design_overrides: Any) -> dict[str, Any]:
    document = _document()
    document["culture_design"] = {
        **document["culture_design"],
        "reactor_ids": ["reactor-1", "reactor-2", "reactor-3"],
        "minimum_passing_replicates": 2,
        **design_overrides,
    }
    document["campaign"] = {**document["campaign"], "max_experiments": 2}
    return document


def test_replicates_run_in_parallel_and_one_bad_reactor_is_tolerated(tmp_path: Path) -> None:
    # reactor-2's heater runs 3 degC hot: that replicate fails QC, the other two pass.
    rig = ScriptedRig(controller_kwargs={"reactor_error": {"reactor-2": Decimal(3)}})
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _replicated())))
    status = asyncio.run(service.run(campaign_id))

    assert status.state is CampaignState.COMPLETED
    assert status.valid_observation_count == 2
    assert _count(storage, MeasurementRecord) == 2 * 3 * 4  # experiments x reactors x readings
    with storage.unit_of_work() as uow:
        assert uow.session is not None
        uncertainty = list(
            uow.session.scalars(
                select(DerivedMetricRecord.uncertainty).where(
                    DerivedMetricRecord.name == "final_od750"
                )
            )
        )
        observations = list(
            uow.session.scalars(select(OptimizerObservationRecord.observation_snapshot))
        )
    # Mean over the two passing replicates, with their spread kept as uncertainty.
    assert all(item is not None and item["n"] == 2 for item in uncertainty)
    assert all("final_od750" in item["measurement_uncertainty"] for item in observations)


def test_too_few_passing_replicates_fails_qc(tmp_path: Path) -> None:
    hot = {"reactor-2": Decimal(3), "reactor-3": Decimal(3)}
    rig = ScriptedRig(controller_kwargs={"reactor_error": hot})
    _, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _replicated())))
    status = asyncio.run(service.run(campaign_id))
    assert status.qc_failed_count == 2 and status.valid_observation_count == 0


def _interrupted_campaign(
    tmp_path: Path, *, on_interrupt: str = "keep_holding"
) -> tuple[Storage, CultureCampaignService, ScriptedRig, SteppedClock, UUID]:
    """Start a campaign and stop AutoLab (Ctrl-C) after the first reading."""

    document = _document()
    document["execution"] = {**document["execution"], "on_interrupt": on_interrupt}
    rig = ScriptedRig(
        sensor_fault=asyncio.CancelledError(), fault_after_reads=1, reuse_controller=True
    )
    clock = SteppedClock(START)
    storage, service = _service(tmp_path, rig, clock=clock)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, document)))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run(campaign_id))
    rig.sensor_fault = None
    return storage, service, rig, clock, campaign_id


def test_detached_culture_resumes_where_it_left_off(tmp_path: Path) -> None:
    storage, service, rig, clock, campaign_id = _interrupted_campaign(tmp_path)
    controller = rig.controllers[0]
    assert controller.safe_state_calls == []  # the device kept holding locally
    assert _count(storage, ExperimentRecord, state=ExperimentState.RUNNING.value) == 1

    # AutoLab was stopped while taking the 12 h reading and is back 5 minutes
    # later, still inside that reading's 10-minute window, so nothing is lost.
    asyncio.run(clock.sleep_until(clock.now() + timedelta(minutes=5)))
    status = asyncio.run(service.run(campaign_id))

    assert status.state is CampaignState.COMPLETED
    assert status.valid_observation_count == 4, service.report(campaign_id)
    with storage.unit_of_work() as uow:
        assert uow.session is not None
        resumed = uow.session.scalar(
            select(func.count())
            .select_from(ActionEventRecord)
            .where(ActionEventRecord.event_type == "event")
            .where(ActionEventRecord.response_snapshot["code"].as_string() == "resumed")
        )
    assert resumed == 1
    # One setpoint per experiment: resume never re-sent the command.
    assert len(controller.commands) == 4


def test_resume_closes_the_run_if_the_device_lost_its_hold(tmp_path: Path) -> None:
    storage, service, rig, _clock, campaign_id = _interrupted_campaign(tmp_path)
    rig.controllers[0].forget_commands = True  # e.g. a power cut reset the controller
    status = asyncio.run(service.run(campaign_id))
    assert status.qc_failed_count >= 1
    first = service.report(campaign_id)
    assert first["qc_failures"] >= 1  # type: ignore[operator]
    assert rig.controllers[0].safe_state_calls[0] == "hold not confirmed after restart"
    del storage


def test_unreachable_controller_needs_an_operator_to_reconcile(tmp_path: Path) -> None:
    from autonomous_lab.culture import ControllerError

    storage, service, rig, _clock, campaign_id = _interrupted_campaign(tmp_path)
    rig.controllers[0].telemetry_failure = ControllerError("link_down", "no route to controller")
    with pytest.raises(CampaignExecutionError, match="reconcile"):
        asyncio.run(service.run(campaign_id))
    assert service.status(campaign_id).state is CampaignState.PAUSED
    with storage.unit_of_work() as uow:
        experiment_id = UUID(uow.experiments.list_for_campaign(str(campaign_id))[0].id)
    with pytest.raises(ValueError, match="operator"):
        service.reconcile(campaign_id, experiment_id, operator=" ", reason="x")
    service.reconcile(
        campaign_id, experiment_id, operator="steven", reason="reactor checked: heater off"
    )
    rig.controllers[0].telemetry_failure = None
    status = asyncio.run(service.run(campaign_id))
    assert status.state is CampaignState.COMPLETED
    assert status.aborted_count == 1 and status.valid_observation_count == 3


def test_contamination_flag_excludes_a_run_from_the_report(tmp_path: Path) -> None:
    rig = ScriptedRig()
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))
    asyncio.run(service.run(campaign_id))
    frontier = service.report(campaign_id)["pareto_frontier"]
    assert frontier
    flagged = UUID(str(frontier[0]["experiment_id"]))  # type: ignore[index]
    service.flag_contamination(
        campaign_id, flagged, operator="steven", reason="ciliates seen under microscope"
    )
    report = service.report(campaign_id)
    assert str(flagged) not in {str(item["experiment_id"]) for item in report["pareto_frontier"]}  # type: ignore[union-attr]
    assert report["contamination_flags"][0]["reason"] == "ciliates seen under microscope"  # type: ignore[index]
    assert report["contamination_note"] is not None
    del storage


def test_agent_reads_a_real_campaign_and_its_draft_becomes_a_creatable_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 7 end to end: digest -> interpret -> draft -> review -> person creates."""

    rig = ScriptedRig()
    monkeypatch.setattr(culture_service_module, "installed_rig", rig)
    monkeypatch.setattr(
        culture_service_module, "default_clock", lambda _config: SteppedClock(START)
    )
    database = str(tmp_path / "agent.sqlite3")
    policy_path = str(PROJECT_ROOT / "configs" / "culture-policy.yaml")
    template = _write(tmp_path, _document())
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--database", database]).exit_code == 0
    campaign_id = runner.invoke(
        app,
        [
            "culture",
            "create",
            str(template),
            "--database",
            database,
            "--policy",
            policy_path,
            "--optimizer-root",
            str(tmp_path / "opt"),
            "--id-only",
        ],
    ).output.strip()
    ran = runner.invoke(
        app,
        [
            "culture",
            "run",
            campaign_id,
            "--database",
            database,
            "--policy",
            policy_path,
            "--optimizer-root",
            str(tmp_path / "opt"),
            "--artifact-root",
            str(tmp_path / "art"),
            "--quiet",
        ],
    )
    assert ran.exit_code == 0, ran.output

    digest = runner.invoke(app, ["agent", "digest", campaign_id, "--database", database])
    assert digest.exit_code == 0, digest.output
    data = json.loads(digest.output)
    assert data["experiment_type"] == "culture" and len(data["experiments"]) == 4
    assert all(item["metrics"]["final_od750"] > 0 for item in data["experiments"])
    assert "uri" not in digest.output and "sqlite" not in digest.output  # values only

    interpreted = runner.invoke(app, ["agent", "interpret", campaign_id, "--database", database])
    assert interpreted.exit_code == 0 and "Best final_od750" in interpreted.output

    goal = tmp_path / "goal.yaml"
    goal.write_text(
        yaml.safe_dump(
            {
                "title": "Follow-up near the optimum",
                "statement": "Narrow temperature around the best result.",
                "experiment_type": "culture",
                "max_experiments": 3,
                "parameter_focus": {"temperature": [26, 30]},
            }
        ),
        encoding="utf-8",
    )
    draft_path = tmp_path / "draft.yaml"
    drafted = runner.invoke(
        app,
        [
            "agent",
            "draft",
            str(goal),
            "--template",
            str(template),
            "--output",
            str(draft_path),
            "--policy",
            policy_path,
        ],
    )
    assert drafted.exit_code == 0 and "ACCEPTED" in drafted.output, drafted.output

    accepted = tmp_path / "follow-up.yaml"
    reviewed = runner.invoke(
        app,
        [
            "agent",
            "review",
            str(draft_path),
            "--policy",
            policy_path,
            "--template",
            str(template),
            "--write-config",
            str(accepted),
        ],
    )
    assert reviewed.exit_code == 0, reviewed.output
    created = runner.invoke(
        app,
        [
            "culture",
            "create",
            str(accepted),
            "--database",
            database,
            "--policy",
            policy_path,
            "--optimizer-root",
            str(tmp_path / "opt"),
            "--id-only",
        ],
    )
    assert created.exit_code == 0, created.output
    # The agent's provenance travels with the campaign source text.
    with Storage(Database(tmp_path / "agent.sqlite3")).unit_of_work() as uow:
        source = uow.campaigns.get(created.output.strip()).config_source
    assert "# Drafted by: autolab-template-agent@1" in source

    # A tampered draft is refused with exit code 2.
    tampered = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    tampered["document"]["parameter_space"]["temperature"]["maximum"] = 40
    draft_path.write_text(yaml.safe_dump(tampered), encoding="utf-8")
    refused = runner.invoke(app, ["agent", "review", str(draft_path), "--policy", policy_path])
    assert refused.exit_code == 2 and "REJECTED" in refused.output


def test_campaign_range_outside_the_device_is_refused_before_any_experiment(
    tmp_path: Path,
) -> None:
    """Effective range = policy ∩ device: a heater-only device cannot go below 20 degC."""

    document = _document()
    document["parameter_space"]["temperature"]["minimum"] = 18  # inside the policy (18-36)
    heater_only = capabilities(controlled_variables=variables(temperature_range=(20, 36)))
    rig = ScriptedRig(controller_kwargs={"manifest": heater_only})
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, document)))
    with pytest.raises(CampaignExecutionError, match="outside the device range"):
        asyncio.run(service.run(campaign_id))
    assert _count(storage, ExperimentRecord) == 0
    assert service.status(campaign_id).state is CampaignState.READY


def test_a_ph_holding_device_runs_with_ph_optimised_and_co2_minimised(tmp_path: Path) -> None:
    document = _document()
    document["parameter_space"]["ph"] = {
        "kind": "continuous",
        "unit": "dimensionless",
        "minimum": 6.5,
        "maximum": 8.5,
        "resolution": 0.05,
    }
    document["objective"]["metrics"].append(
        {"name": "co2_g", "direction": "minimize", "unit": "gram"}
    )
    rig = ScriptedRig(controller_kwargs={"manifest": capabilities(ph=True)})
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, document)))
    status = asyncio.run(service.run(campaign_id))
    assert status.state is CampaignState.COMPLETED and status.valid_observation_count == 4
    with storage.unit_of_work() as uow:
        assert uow.session is not None
        names = set(uow.session.scalars(select(DerivedMetricRecord.name)))
    assert names == {"final_od750", "energy_kwh", "co2_g"}
    assert all("ph" in command.setpoints for command in rig.controllers[0].commands)


_BENCHMARK_GRID = {
    "temperature": [22, 28, 34],
    "light_intensity": [60, 180, 400],
    "ph": [6.5, 8.0],
}


def _grid_document(**optimization: Any) -> dict[str, Any]:
    document = _document()
    document["parameter_space"]["ph"] = {
        "kind": "continuous",
        "unit": "dimensionless",
        "minimum": 6.5,
        "maximum": 8.5,
        "resolution": 0.05,
    }
    document["campaign"]["max_experiments"] = 18
    document["optimization"] = {"strategy": "grid", "grid_levels": _BENCHMARK_GRID, **optimization}
    return document


def test_grid_levels_campaign_proposes_exactly_the_listed_grid(tmp_path: Path) -> None:
    rig = ScriptedRig(controller_kwargs={"manifest": capabilities(ph=True)})
    _storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _grid_document())))
    status = asyncio.run(service.run(campaign_id))
    assert status.state is CampaignState.COMPLETED and status.experiment_count == 18

    records = sorted(
        service.ledger.trial_records(campaign_id), key=lambda r: r.proposal.suggestion_index
    )
    proposed = [
        tuple(
            record.proposal.parameter_values[name].value
            for name in ("light_intensity", "ph", "temperature")
        )
        for record in records
    ]
    # Lexicographic over sorted names, levels in listed order; temperature varies fastest.
    expected = [
        (Decimal(str(light)), Decimal(str(ph)), Decimal(str(temperature)))
        for light in _BENCHMARK_GRID["light_intensity"]
        for ph in _BENCHMARK_GRID["ph"]
        for temperature in _BENCHMARK_GRID["temperature"]
    ]
    assert proposed == expected
    assert {record.proposal.strategy_name for record in records} == {"grid"}
    applied = [command.setpoints for command in rig.controllers[0].commands]
    assert {
        tuple(setpoints[name].value for name in ("light_intensity", "ph", "temperature"))
        for setpoints in applied
    } == set(expected)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["optimization"]["grid_levels"].update(ph=[6.5, 8.02]), "not on the 0.05"),
        (lambda d: d["optimization"]["grid_levels"].update(temperature=[22, 36]), "outside"),
        (lambda d: d["optimization"]["grid_levels"].pop("ph"), "cover every search parameter"),
        (lambda d: d["optimization"]["grid_levels"].update(ph=[6.5, 6.5]), "repeats a value"),
        (lambda d: d["optimization"].update(strategy="random"), "only valid with strategy: grid"),
        (lambda d: d["campaign"].update(max_experiments=20), "exceeds finite grid size 18"),
        (lambda d: d["optimization"].update(maximum_grid_size=10), "maximum_grid_size is 10"),
    ],
)
def test_invalid_grid_levels_are_refused_at_load(tmp_path: Path, change: Any, message: str) -> None:
    document = json.loads(json.dumps(_grid_document()))
    change(document)
    with pytest.raises(ConfigurationLoadError, match=message):
        load_culture_campaign(_write(tmp_path, document))


def test_absent_grid_levels_leave_the_document_unchanged() -> None:
    loaded = load_culture_campaign(PROJECT_ROOT / "configs" / "culture-algae-sim.yaml")
    assert "grid_levels" not in loaded.config.optimization.model_dump()


# --------------------------------------------------- parallel reactor batches

_BANK = tuple(f"reactor-{index}" for index in range(1, 10))
_BATCH_CAMPAIGN = UUID("5b1f0c7e-3a51-4c43-9d43-2f0a3c6b8e11")


def _batch_document(**optimization: Any) -> dict[str, Any]:
    document = _document()
    document["campaign"] = {**document["campaign"], "max_experiments": 9}
    document["culture_design"] = {
        **document["culture_design"],
        "reactor_ids": list(_BANK),
        "minimum_passing_replicates": 2,
    }
    document["optimization"] = {
        "strategy": "bayesian",
        "startup_experiments": 4,
        "batch_size": 3,
        **optimization,
    }
    return document


def _bank_rig(**controller: Any) -> ScriptedRig:
    manifest = capabilities(reactor_ids=frozenset(_BANK))
    return ScriptedRig(controller_kwargs={"manifest": manifest, **controller})


def _run_batch_campaign(
    tmp_path: Path, rig: ScriptedRig, document: dict[str, Any] | None = None
) -> tuple[Storage, CultureCampaignService, UUID]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(
        load_culture_campaign(_write(tmp_path, document or _batch_document())),
        campaign_id=_BATCH_CAMPAIGN,
    )
    asyncio.run(service.run(campaign_id))
    return storage, service, campaign_id


def _outcomes(service: CultureCampaignService, campaign_id: UUID) -> list[tuple[Any, ...]]:
    """Per experiment, in order: parameter values and the observed objective values."""

    with service.storage.unit_of_work() as uow:
        assert uow.session is not None
        observed = {
            row.experiment_id: tuple(sorted(row.objective_values.items()))
            for row in uow.session.scalars(select(OptimizerObservationRecord))
        }
    records = sorted(
        service.ledger.trial_records(campaign_id), key=lambda r: r.proposal.suggestion_index
    )
    return [
        (
            tuple(
                (name, value.value) for name, value in sorted(r.proposal.parameter_values.items())
            ),
            observed.get(str(r.proposal.experiment_id)),
        )
        for r in records
    ]


def test_batch_of_three_runs_each_condition_on_its_own_reactor_group(tmp_path: Path) -> None:
    rig = _bank_rig()
    _, service, campaign_id = _run_batch_campaign(tmp_path / "a", rig)
    status = service.status(campaign_id)
    assert status.state is CampaignState.COMPLETED
    assert status.experiment_count == 9 and status.valid_observation_count == 9

    report = service.report(campaign_id)
    batching: Any = report["batching"]
    assert report["replicates_per_experiment"] == 3
    assert batching["batch_size"] == 3 and batching["rounds"] == 3
    assert batching["reactor_groups"] == [list(_BANK[0:3]), list(_BANK[3:6]), list(_BANK[6:9])]
    # startup 4 with k = 3: rounds 0 and 1 (6 trials) are random, not 4.
    assert batching["planned_random_startup"] == 6 and batching["random_startup"] == 6
    rows = batching["experiments"]
    assert [(row["round"], row["slot"]) for row in rows] == [
        (r, s) for r in range(3) for s in range(3)
    ]
    assert [row["random_startup"] for row in rows] == [True] * 6 + [False] * 3

    controller = rig.controllers[0]
    by_experiment: dict[UUID, list[int]] = {}
    for index, command in enumerate(controller.commands):
        by_experiment.setdefault(command.experiment_id, []).append(index)
    records = sorted(
        service.ledger.trial_records(campaign_id), key=lambda r: r.proposal.suggestion_index
    )
    for record in records:
        indices = by_experiment[record.proposal.experiment_id]
        slot = record.proposal.suggestion_index % 3
        # Replicates stay within the slot's group, all at one condition.
        assert {controller.commands[i].reactor_id for i in indices} == set(
            _BANK[3 * slot : 3 * slot + 3]
        )
        assert (
            len(
                {
                    tuple(sorted((k, v.value) for k, v in controller.commands[i].setpoints.items()))
                    for i in indices
                }
            )
            == 1
        )
    for round_index in range(3):
        round_records = records[3 * round_index : 3 * round_index + 3]
        times = {
            controller.command_times[i]
            for r in round_records
            for i in by_experiment[r.proposal.experiment_id]
        }
        # The round's three conditions are distinct, and start together.
        assert len({tuple(sorted(r.proposal.parameter_values.items())) for r in round_records}) == 3
        assert times == {START + timedelta(hours=24 * round_index)}

    # Same seed, same world: identical proposals and observations.
    _, again, again_id = _run_batch_campaign(tmp_path / "b", _bank_rig())
    assert _outcomes(again, again_id) == _outcomes(service, campaign_id)


def test_batch_size_one_is_today_and_keeps_the_hash() -> None:
    sequential = load_culture_campaign(PROJECT_ROOT / "configs" / "culture-algae-sim.yaml")
    assert "batch_size" not in sequential.config.optimization.model_dump()
    assert sequential.config.reactor_groups() == (sequential.config.culture_design.reactor_ids,)
    assert sequential.config.group_design(0) is sequential.config.culture_design


def test_a_failing_group_does_not_sink_the_rest_of_its_round(tmp_path: Path) -> None:
    rig = _bank_rig(refused_reactors=frozenset({"reactor-5"}))  # slot 1's group
    storage, service, campaign_id = _run_batch_campaign(tmp_path, rig)
    status = service.status(campaign_id)

    # The round finishes: slots 0 and 2 are observed and told; then the campaign pauses.
    assert status.state is CampaignState.PAUSED
    assert status.experiment_count == 3
    assert status.execution_failed_count == 1 and status.valid_observation_count == 2
    assert _count(storage, OptimizerObservationRecord) == 2
    with storage.unit_of_work() as uow:
        assert uow.session is not None
        rows = uow.session.execute(select(ExperimentRecord.ordinal, ExperimentRecord.state)).all()
        states = {ordinal: state for ordinal, state in rows}
    assert states == {0: "observed", 1: "failed", 2: "observed"}
    controller = rig.controllers[0]
    # Applying stopped at the refused reactor; what was applied went to safe state.
    assert "reactor-4" in controller.safe_state_reactors
    assert "reactor-6" not in {command.reactor_id for command in controller.commands}


def test_a_qc_failing_group_is_excluded_and_the_campaign_continues(tmp_path: Path) -> None:
    hot = {"reactor-7": Decimal(3), "reactor-8": Decimal(3)}  # slot 2: 1 of 3 passes
    _, service, campaign_id = _run_batch_campaign(tmp_path, _bank_rig(reactor_error=hot))
    status = service.status(campaign_id)
    assert status.state is CampaignState.COMPLETED
    assert status.qc_failed_count == 3 and status.valid_observation_count == 6


def test_batch_campaign_resumes_mid_round_and_matches_an_uninterrupted_run(
    tmp_path: Path,
) -> None:
    _, reference, reference_id = _run_batch_campaign(tmp_path / "reference", _bank_rig())

    document = _batch_document()
    document["execution"] = {**document["execution"], "on_interrupt": "keep_holding"}
    rig = _bank_rig()
    rig.reuse_controller = True
    clock = SteppedClock(START)
    run_dir = tmp_path / "interrupted"
    run_dir.mkdir()
    storage, service = _service(run_dir, rig, clock=clock)
    campaign_id = service.create(
        load_culture_campaign(_write(run_dir, document)), campaign_id=_BATCH_CAMPAIGN
    )
    midway = START + timedelta(hours=36)  # 12 h into round 1

    async def stop_midway() -> None:
        task = asyncio.ensure_future(service.run(campaign_id))
        while clock.now() < midway:
            await asyncio.sleep(0)
        task.cancel()  # Ctrl-C: every group is cancelled together
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(stop_midway())
    assert _count(storage, ExperimentRecord, state=ExperimentState.RUNNING.value) == 3
    controller = rig.controllers[0]
    # Round 0 ended (each reactor safed exactly once); round 1 holds. The groups'
    # sweeps run concurrently, so only the multiset is meaningful, not the order.
    assert sorted(controller.safe_state_reactors) == sorted(_BANK)

    status = asyncio.run(service.run(campaign_id))
    assert status.state is CampaignState.COMPLETED and status.valid_observation_count == 9
    assert len(controller.commands) == 27  # resume never re-sent a setpoint
    assert _outcomes(service, campaign_id) == _outcomes(reference, reference_id)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["optimization"].update(batch_size=2), "cannot be split"),
        (
            lambda d: d["culture_design"].update(minimum_passing_replicates=4),
            "exceeds the reactor group size 3",
        ),
        (lambda d: d["optimization"].update(batch_size=0), "batch_size"),
    ],
)
def test_invalid_batch_configs_are_refused_at_load(
    tmp_path: Path, change: Any, message: str
) -> None:
    document = json.loads(json.dumps(_batch_document()))
    change(document)
    with pytest.raises(ConfigurationLoadError, match=message):
        load_culture_campaign(_write(tmp_path, document))


def test_assay_campaigns_refuse_batches(tmp_path: Path) -> None:
    document = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "assay-reagent-reduction.yaml").read_text(encoding="utf-8")
    )
    document["optimization"]["batch_size"] = 3
    path = tmp_path / "assay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ConfigurationLoadError, match="batch_size must be 1"):
        load_campaign(path)


def test_a_second_run_on_a_live_campaign_refuses_instead_of_resuming_it(
    tmp_path: Path,
) -> None:
    """Audit finding: a second ``culture run`` used to "resume" the live run."""

    from autonomous_lab.campaigns.writer_lock import CampaignLockedError

    rig = ScriptedRig(reuse_controller=True)
    clock = SteppedClock(START)
    storage, service = _service(tmp_path, rig, clock=clock)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))
    # A second AutoLab process: its own engine and service on the same database file.
    _, intruder = _service(tmp_path, rig, clock=clock)

    async def race() -> object:
        first = asyncio.ensure_future(service.run(campaign_id))
        while clock.now() < START + timedelta(hours=3):  # mid-hold of experiment 1
            await asyncio.sleep(0)
        with pytest.raises(CampaignLockedError, match="already being run"):
            await intruder.run(campaign_id)
        with storage.unit_of_work() as uow:
            live = UUID(uow.experiments.list_for_campaign(str(campaign_id))[0].id)
        with pytest.raises(CampaignLockedError):
            intruder.reconcile(campaign_id, live, operator="steven", reason="wrong campaign")
        return await first

    status = asyncio.run(race())
    assert isinstance(status, type(service.status(campaign_id)))
    assert status.state is CampaignState.COMPLETED
    # The intruder never touched the rig: one setpoint per reactor per experiment.
    assert len(rig.controllers[0].commands) == 4
    assert _count(storage, ExperimentRecord, state=ExperimentState.OBSERVED.value) == 4
    # The lock is released at the end, so a later run is allowed (and is a no-op).
    assert asyncio.run(intruder.run(campaign_id)).state is CampaignState.COMPLETED


# ------------------------------------------------------------------------------
# Resume edge cases. Each kills a mutant that survived the whole suite in an
# external audit (named in the docstring).


def _artifact_records(storage: Storage) -> list[dict[str, Any]]:
    with storage.unit_of_work() as uow:
        assert uow.session is not None
        uris = list(uow.session.scalars(select(ArtifactRecord.uri)))
    return [json.loads(Path(urlparse(uri).path).read_text(encoding="utf-8")) for uri in uris]



def test_a_protection_trip_during_the_outage_stops_the_hold_at_once_on_resume(
    tmp_path: Path,
) -> None:
    """M1: resume must not trust a hold whose device protection has tripped."""

    storage, service, rig, clock, campaign_id = _interrupted_campaign(tmp_path)
    controller = rig.controllers[0]
    controller.trip_after_hours = Decimal(13)  # tripped while AutoLab was down
    asyncio.run(clock.sleep_until(START + timedelta(hours=14)))
    asyncio.run(service.run(campaign_id))

    # Made safe on resume itself, not left holding until the next telemetry poll.
    assert controller.safe_state_calls[0] == "hold not confirmed after restart"
    (resumed,) = [item for item in _artifact_records(storage) if item["evidence"]["resumed_at"]]
    assert resumed["evidence"]["replicates"][0]["hold_interrupted"] is True
    assert "hold_not_confirmed" in {event["code"] for event in resumed["evidence"]["events"]}


def test_journal_replay_remembers_a_protection_trip(tmp_path: Path) -> None:
    """M8: a reactor that tripped before the restart stays stopped in the evidence."""

    document = _document()
    document["culture_design"] = {
        **document["culture_design"],
        "reactor_ids": ["reactor-1", "reactor-2"],
        "minimum_passing_replicates": 1,
    }
    document["campaign"] = {**document["campaign"], "max_experiments": 1}
    document["optimization"] = {"strategy": "random", "startup_experiments": 1}
    document["execution"] = {**document["execution"], "on_interrupt": "keep_holding"}
    rig = ScriptedRig(
        controller_kwargs={
            "trip_after_hours": Decimal(2),
            "trip_reactors": frozenset({"reactor-1"}),
        },
        sensor_fault=asyncio.CancelledError(),
        fault_after_reads=1,  # Ctrl-C at reactor-2's 12 h reading
        reuse_controller=True,
    )
    clock = SteppedClock(START)
    storage, service = _service(tmp_path, rig, clock=clock)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, document)))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run(campaign_id))
    rig.sensor_fault = None
    asyncio.run(service.run(campaign_id))

    (record,) = _artifact_records(storage)
    replicates = {item["reactor_id"]: item for item in record["evidence"]["replicates"]}
    assert replicates["reactor-1"]["stopped_early"] is True
    assert replicates["reactor-2"]["stopped_early"] is False


def test_a_reading_whose_window_closed_during_the_outage_is_missing_not_late(
    tmp_path: Path,
) -> None:
    """M10: AutoLab must not take a reading after its window closed."""

    storage, service, _rig, clock, campaign_id = _interrupted_campaign(tmp_path)
    # Stopped at the 12 h reading; back an hour later, long after its 10-minute window.
    asyncio.run(clock.sleep_until(clock.now() + timedelta(hours=1)))
    asyncio.run(service.run(campaign_id))

    # Experiment 1 has its 6, 18 and 24 h readings only; the other three have four.
    assert _count(storage, ActionEventRecord, event_type="reading") == 3 + 3 * 4
    entries = [
        entry["status"]
        for record in _artifact_records(storage)
        if record["evidence"]["resumed_at"]
        for entry in record["reconciliations"][0]["entries"]
    ]
    assert entries == ["matched", "missing", "matched", "matched"]


class _ProcessKilled(BaseException):
    """Stands in for SIGKILL or a power cut: nothing after it runs."""


def test_a_process_killed_mid_dispatch_is_made_safe_on_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2 end to end: the journalled command is the only sign it may have been sent."""

    rig = ScriptedRig(reuse_controller=True)
    storage, service = _service(tmp_path, rig)
    campaign_id = service.create(load_culture_campaign(_write(tmp_path, _document())))

    async def killed_while_sending(_self: object, _command: object) -> object:
        raise _ProcessKilled

    async def no_cleanup(*_args: object, **_kwargs: object) -> None:
        return None  # a killed process runs no safe-state sweep

    with monkeypatch.context() as patch:
        patch.setattr(culture_service_module.CultureExperimentRunner, "_safe_all", no_cleanup)
        patch.setattr(ScriptedController, "apply_setpoints", killed_while_sending)
        with pytest.raises(_ProcessKilled):
            asyncio.run(service.run(campaign_id))
    controller = rig.controllers[0]
    assert controller.safe_state_calls == []
    assert _count(storage, ActionEventRecord, event_type="setpoint_command") == 1
    assert _count(storage, ActionEventRecord, event_type="setpoint_ack") == 0

    status = asyncio.run(service.run(campaign_id))
    assert controller.safe_state_calls == ["resume after interrupted dispatch"]
    assert status.state is CampaignState.PAUSED
    assert status.execution_failed_count == 1

