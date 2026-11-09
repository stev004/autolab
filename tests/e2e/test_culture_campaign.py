"""Culture campaigns end to end: ask -> culture -> evidence in SQLite -> tell -> resume.

The rig is scripted (see tests/support/culture_fakes.py). Its OD response is a
made-up function of the applied setpoint so the optimiser has something to
learn; it is not a growth model.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import pytest
import yaml
from culture_fakes import START, TEMP, ScriptedController, ScriptedODSensor, capabilities, policy
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
            "temperature_setpoint": {
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
        setpoint = applied.temperature_setpoint.to(TEMP).value
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
                    "pH": {
                        "kind": "continuous",
                        "unit": "dimensionless",
                        "minimum": 6,
                        "maximum": 8,
                    }
                }
            },
            "optimise exactly",
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
