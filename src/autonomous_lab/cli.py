"""Command-line interface for local AutoLab campaigns."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Never, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import typer

from autonomous_lab.campaigns.config import AssayDesignConfig, CampaignConfig
from autonomous_lab.campaigns.loader import (
    ConfigurationLoadError,
    LoadedCampaign,
    load_campaign,
    load_validation_policy,
)
from autonomous_lab.campaigns.service import (
    CampaignExecutionError,
    CampaignService,
    CampaignStatus,
)
from autonomous_lab.domain.enums import ParameterScale
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.capabilities import CapabilitySnapshot
from autonomous_lab.hardware.sensors import SensorError, load_sensor_adapter
from autonomous_lab.human import (
    AcknowledgementDecision,
    DeviationCategory,
    HumanExecutionBackend,
    ProtocolDeviation,
    human_assay_v1_capability_snapshot,
    render_markdown,
)
from autonomous_lab.human.service import HumanWorkflowError, HumanWorkflowService
from autonomous_lab.measurement_ingestion import (
    ExpectedMeasurement,
    ImportIssue,
    ManualBatchBuilder,
    MeasurementBatchKind,
    MeasurementWorkflowService,
    ReadOnlySensorSource,
    acquisition_context,
    mock_physical_sensor_source_from_rows,
    parse_measurement_csv,
)
from autonomous_lab.measurement_ingestion.repository import MeasurementIngestionError
from autonomous_lab.protocols import AssayProtocolCompiler
from autonomous_lab.storage import RecordNotFoundError, Storage
from autonomous_lab.validation import ProtocolValidationService

DatabaseOption = Annotated[
    str,
    typer.Option(
        "--database",
        "-d",
        envvar="AUTOLAB_DATABASE_URL",
        help="SQLite path or SQLAlchemy database URL.",
    ),
]

app = typer.Typer(
    name="autolab",
    no_args_is_help=True,
    help="Reproducible, hardware-agnostic autonomous assay optimisation.",
)
campaign_app = typer.Typer(no_args_is_help=True, help="Create, run and inspect campaigns.")
experiment_app = typer.Typer(no_args_is_help=True, help="Inspect experiment evidence.")
execution_app = typer.Typer(no_args_is_help=True, help="Record human protocol execution.")
measurements_app = typer.Typer(no_args_is_help=True, help="Ingest external raw measurements.")
app.add_typer(campaign_app, name="campaign")
app.add_typer(experiment_app, name="experiment")
experiment_app.add_typer(execution_app, name="execution")
experiment_app.add_typer(measurements_app, name="measurements")


@app.command("init")
def initialize(
    database: DatabaseOption = "data/autolab.sqlite3",
) -> None:
    """Apply schema migrations and create local evidence directories."""

    storage = Storage.open(database)
    try:
        storage.initialize()
        for directory in _data_directories(database):
            directory.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        _abort(f"could not initialize database: {exc}")
    finally:
        storage.close()
    typer.echo(f"initialized AutoLab database: {database}")


def _data_directories(database: str) -> tuple[Path, ...]:
    if "://" in database or database == "sqlite://":
        root = Path("data")
    else:
        root = Path(database).expanduser().resolve().parent
    return (
        root / "raw",
        root / "processed",
        root / "optimizer",
        root / "worklists",
        root / "artifacts",
    )


@campaign_app.command("validate")
def validate_campaign(
    config: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    policy: Annotated[
        Path,
        typer.Option(
            "--policy",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Trusted installation validation policy.",
        ),
    ] = Path("configs/validation-policy.yaml"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable validation result."),
    ] = False,
) -> None:
    """Compile and validate a deterministic midpoint proposal without executing it."""

    try:
        loaded = load_campaign(config)
        trusted_policy = load_validation_policy(policy)
        proposal = _midpoint_proposal(loaded)
        protocol = AssayProtocolCompiler().compile(proposal, loaded.config)
        snapshot = (
            asyncio.run(_simulated_capabilities(loaded.config, proposal))
            if loaded.config.execution.mode == "simulation"
            else human_assay_v1_capability_snapshot()
        )
        outcome = ProtocolValidationService().validate(
            proposal=proposal,
            protocol=protocol,
            campaign=loaded.config,
            policy=trusted_policy,
            capability_snapshot=snapshot,
        )
    except (ConfigurationLoadError, ValueError) as exc:
        _abort(str(exc), code=2)

    payload = {
        "config": str(loaded.source_path),
        "config_sha256": loaded.canonical_sha256,
        "protocol_id": str(outcome.protocol.protocol_id),
        "protocol_hash": outcome.protocol.protocol_hash,
        "vessel_count": len(outcome.protocol.vessels),
        "step_count": len(outcome.protocol.steps),
        "report": outcome.report.model_dump(mode="json"),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        typer.echo(f"configuration: {loaded.source_path}")
        typer.echo(f"protocol:      {outcome.protocol.protocol_id}")
        typer.echo(f"protocol hash: {outcome.protocol.protocol_hash}")
        typer.echo(
            f"batch:         {len(outcome.protocol.vessels)} vessels, "
            f"{len(outcome.protocol.steps)} steps"
        )
        typer.echo(f"validation:    {outcome.report.status.value}")
        for issue in outcome.report.issues:
            typer.echo(f"{issue.severity.value.upper()} {issue.code} {issue.path}: {issue.message}")
    if not outcome.passed:
        raise typer.Exit(code=2)


@campaign_app.command("create")
def create_campaign(
    config: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    database: DatabaseOption = "data/autolab.sqlite3",
    policy: Annotated[
        Path,
        typer.Option(
            "--policy",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Trusted installation validation policy.",
        ),
    ] = Path("configs/validation-policy.yaml"),
    optimizer_root: Annotated[
        Path,
        typer.Option(help="Directory for optimiser-native checkpoint stores."),
    ] = Path("data/optimizer"),
    id_only: Annotated[
        bool,
        typer.Option("--id-only", help="Print only the new campaign UUID."),
    ] = False,
) -> None:
    """Validate and persist a new campaign."""

    storage = Storage.open(database)
    try:
        loaded = load_campaign(config)
        service = CampaignService(
            storage,
            validation_policy=load_validation_policy(policy),
            optimizer_root=optimizer_root,
        )
        campaign_id = service.create(loaded)
    except (ConfigurationLoadError, CampaignExecutionError, ValueError) as exc:
        _abort(str(exc), code=2)
    except Exception as exc:
        _abort(f"could not create campaign: {exc}", code=3)
    finally:
        storage.close()
    if id_only:
        typer.echo(str(campaign_id))
    else:
        typer.echo(f"created campaign: {campaign_id}")
        typer.echo(f"name:             {loaded.config.campaign.name}")
        typer.echo("state:            ready")


@campaign_app.command("run")
def run_campaign(
    campaign_id: Annotated[UUID, typer.Argument(help="Campaign UUID to run or resume.")],
    database: DatabaseOption = "data/autolab.sqlite3",
    policy: Annotated[
        Path,
        typer.Option(
            "--policy",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Trusted installation validation policy.",
        ),
    ] = Path("configs/validation-policy.yaml"),
    optimizer_root: Annotated[
        Path,
        typer.Option(help="Directory for optimiser-native checkpoint stores."),
    ] = Path("data/optimizer"),
    max_experiments: Annotated[
        int | None,
        typer.Option(
            "--max-experiments",
            min=1,
            help="Optional stop budget for this invocation.",
        ),
    ] = None,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", help="Suppress per-experiment progress output."),
    ] = False,
) -> None:
    """Advance a campaign to completion or its next external evidence boundary."""

    storage = Storage.open(database)
    try:
        service = CampaignService(
            storage,
            validation_policy=load_validation_policy(policy),
            optimizer_root=optimizer_root,
            progress_callback=None if quiet else _emit_campaign_progress,
        )
        status = asyncio.run(service.run(campaign_id, max_new_experiments=max_experiments))
    except (ConfigurationLoadError, CampaignExecutionError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    except Exception as exc:
        _abort(f"campaign execution failed: {exc}", code=3)
    finally:
        storage.close()
    _emit(_status_payload(status), json_output=False)


@campaign_app.command("status")
def campaign_status(
    campaign_id: Annotated[UUID, typer.Argument(help="Campaign UUID to inspect.")],
    database: DatabaseOption = "data/autolab.sqlite3",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show persisted campaign lifecycle and evidence counts."""

    storage = Storage.open(database)
    try:
        with storage.unit_of_work() as uow:
            summary = uow.campaigns.summary(str(campaign_id))
    except RecordNotFoundError as exc:
        _abort(str(exc), code=3)
    finally:
        storage.close()
    payload = {
        "campaign_id": summary.campaign_id,
        "state": summary.state,
        "experiment_count": summary.experiment_count,
        "experiment_states": dict(summary.state_counts),
        "qc_passed": summary.qc_passed_count,
        "qc_failed": summary.qc_failed_count,
        "observed": summary.observed_count,
        "feasible": summary.feasible_count,
    }
    _emit(payload, json_output=json_output)


@campaign_app.command("report")
def campaign_report(
    campaign_id: Annotated[UUID, typer.Argument(help="Campaign UUID to report.")],
    database: DatabaseOption = "data/autolab.sqlite3",
    policy: Annotated[
        Path,
        typer.Option(
            "--policy",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Trusted installation validation policy.",
        ),
    ] = Path("configs/validation-policy.yaml"),
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Optional JSON report destination."),
    ] = None,
) -> None:
    """Build a reproducible campaign summary and Pareto report."""

    storage = Storage.open(database)
    try:
        service = CampaignService(
            storage,
            validation_policy=load_validation_policy(policy),
        )
        report = service.report(campaign_id)
    except (ConfigurationLoadError, CampaignExecutionError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    except Exception as exc:
        _abort(f"could not build campaign report: {exc}", code=3)
    finally:
        storage.close()
    rendered = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    if output is None:
        typer.echo(rendered, nl=False)
        return
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        _abort(f"could not write report: {exc}", code=3)
    typer.echo(f"wrote campaign report: {output.resolve()}")


@experiment_app.command("worklist")
def experiment_worklist(
    experiment_id: Annotated[UUID, typer.Argument(help="Human experiment UUID.")],
    database: DatabaseOption = "data/autolab.sqlite3",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Display or export the exact validated operator worklist."""

    storage = Storage.open(database)
    try:
        worklist = HumanWorkflowService(storage).load_worklist(experiment_id)
        rendered = render_markdown(worklist)
    except (HumanWorkflowError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    if output is None:
        typer.echo(rendered)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"wrote worklist: {output.resolve()}")


@execution_app.command("start")
def start_human_execution(
    experiment_id: Annotated[UUID, typer.Argument(help="Human experiment UUID.")],
    operator_id: Annotated[str, typer.Option("--operator-id", prompt=True)],
    database: DatabaseOption = "data/autolab.sqlite3",
) -> None:
    """Open a persistent operator execution record; no step is yet complete."""

    storage = Storage.open(database)
    try:
        run_id = HumanWorkflowService(storage).start_execution(
            experiment_id, operator_id=operator_id
        )
    except (HumanWorkflowError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    typer.echo(f"started human execution: {run_id}")
    typer.echo("No physical step is recorded complete until execution complete is submitted.")


@execution_app.command("complete")
def complete_human_execution(
    experiment_id: Annotated[UUID, typer.Argument(help="Human experiment UUID.")],
    operator_id: Annotated[str, typer.Option("--operator-id", prompt=True)],
    database: DatabaseOption = "data/autolab.sqlite3",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm every worklist group non-interactively."),
    ] = False,
) -> None:
    """Acknowledge grouped worklist steps and persist exact per-step evidence."""

    storage = Storage.open(database)
    try:
        workflow = HumanWorkflowService(storage)
        worklist = workflow.load_worklist(experiment_id)
        run = workflow.execution_run(experiment_id)
        if run is None or run.status != "running":
            raise HumanWorkflowError("execution start must be recorded first")
        backend = HumanExecutionBackend()
        session = workflow.execution_session(
            experiment_id,
            operator_id=operator_id,
        )
        acknowledged = {item.group_id for item in session.acknowledgements}
        for group in worklist.procedure:
            if group.group_id in acknowledged:
                typer.echo(f"\n{group.sequence}. {group.title} — already recorded")
                continue
            if session.status.value in {"failed", "aborted"}:
                break
            typer.echo(f"\n{group.sequence}. {group.title}")
            typer.echo(group.instruction)
            decision = (
                AcknowledgementDecision.CONFIRM
                if yes
                else _prompt_acknowledgement(group.group_id)
            )
            note = None
            deviations: tuple[ProtocolDeviation, ...] = ()
            if decision is not AcknowledgementDecision.CONFIRM:
                note = typer.prompt("Note/reason")
            if decision is AcknowledgementDecision.RECORD_DEVIATION:
                deviations = (_prompt_deviation(operator_id, note or "protocol deviation"),)
            session = backend.acknowledge(
                session,
                group_id=group.group_id,
                decision=decision,
                deviations=deviations,
                note=note,
            )
            workflow.persist_execution_session(session)
            if session.status.value in {"failed", "aborted"}:
                break
        record = (
            backend.complete(session)
            if session.status.value == "in_progress"
            else backend.terminal_record(session)
        )
        workflow.complete_execution(record)
    except (HumanWorkflowError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    typer.echo(f"human execution recorded: {record.status.value}")
    if record.status.value == "completed":
        typer.echo(f"next: autolab experiment measurements status {experiment_id}")


@execution_app.command("status")
def human_execution_status(
    experiment_id: Annotated[UUID, typer.Argument(help="Human experiment UUID.")],
    operator_id: Annotated[str, typer.Option("--operator-id", prompt=True)],
    database: DatabaseOption = "data/autolab.sqlite3",
) -> None:
    """Show durable grouped progress and the next unacknowledged operation."""

    storage = Storage.open(database)
    try:
        workflow = HumanWorkflowService(storage)
        session = workflow.execution_session(experiment_id, operator_id=operator_id)
        acknowledged = {item.group_id for item in session.acknowledgements}
        pending = [
            item for item in session.worklist.procedure if item.group_id not in acknowledged
        ]
        payload = {
            "experiment_id": str(experiment_id),
            "operator_id": session.operator_id,
            "status": session.status.value,
            "acknowledged_groups": len(acknowledged),
            "total_groups": len(session.worklist.procedure),
            "next_group": None if not pending else pending[0].title,
            "uncertain_action_warning": (
                "If a physical action occurred but was not acknowledged before an "
                "interruption, do not repeat it automatically; record a deviation or abort."
            ),
        }
    except (HumanWorkflowError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    _emit(payload, json_output=False)


@execution_app.command("abort")
def abort_human_execution(
    experiment_id: Annotated[UUID, typer.Argument(help="Human experiment UUID.")],
    operator_id: Annotated[str, typer.Option("--operator-id", prompt=True)],
    reason: Annotated[str, typer.Option("--reason", prompt=True)],
    database: DatabaseOption = "data/autolab.sqlite3",
) -> None:
    """Record an operator abort; the experiment can never reach the optimiser."""

    storage = Storage.open(database)
    try:
        HumanWorkflowService(storage).abort_execution(
            experiment_id,
            operator_id=operator_id,
            reason=reason,
        )
    except (HumanWorkflowError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    typer.echo("human execution aborted")


@measurements_app.command("status")
def measurement_status(
    experiment_id: Annotated[UUID, typer.Argument(help="Experiment UUID.")],
    database: DatabaseOption = "data/autolab.sqlite3",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show expected, received, unavailable, and missing measurement counts."""

    storage = Storage.open(database)
    try:
        payload = MeasurementWorkflowService(storage).status(experiment_id)
    except (MeasurementIngestionError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    _emit(payload, json_output=json_output)


@measurements_app.command("enter")
def enter_measurements(
    experiment_id: Annotated[UUID, typer.Argument(help="Experiment UUID.")],
    operator_id: Annotated[str, typer.Option("--operator-id", prompt=True)],
    database: DatabaseOption = "data/autolab.sqlite3",
    correction: Annotated[bool, typer.Option("--correction")] = False,
    correction_reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Prompt for every expected raw value and append one immutable batch."""

    storage = Storage.open(database)
    try:
        service = MeasurementWorkflowService(storage)
        plan = service.plan(experiment_id)
        kind = (
            MeasurementBatchKind.CORRECTION
            if correction
            else MeasurementBatchKind.INITIAL
        )
        if correction and not correction_reason:
            correction_reason = typer.prompt("Correction reason")
        builder = ManualBatchBuilder(plan, operator_id, batch_kind=kind)
        entries = plan.entries if not correction else _select_correction_entries(plan.entries)
        now = datetime.now(UTC)
        for expected in entries:
            label = (
                f"{expected.sample_id} {expected.sample_role.value} "
                f"{expected.measurement_type} ({expected.unit})"
            )
            value = typer.prompt(label + " [number or unavailable:<reason>]")
            if value.lower().startswith("unavailable:"):
                builder.set_unavailable(
                    expected.expected_measurement_id,
                    value.split(":", 1)[1].strip(),
                    measured_at=now,
                    correction_reason=correction_reason,
                )
            else:
                builder.set_value(
                    expected.expected_measurement_id,
                    Decimal(value),
                    measured_at=now,
                    correction_reason=correction_reason,
                )
        result = builder.finalize()
        if not result.accepted or result.batch is None:
            raise MeasurementIngestionError(_format_import_issues(result.issues))
        service.submit(result.batch)
    except (MeasurementIngestionError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    typer.echo(f"accepted immutable manual batch: {result.batch.batch_id}")


@measurements_app.command("import")
def import_measurements(
    experiment_id: Annotated[UUID, typer.Argument(help="Experiment UUID.")],
    source: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    submitted_by: Annotated[str, typer.Option("--submitted-by", prompt=True)],
    database: DatabaseOption = "data/autolab.sqlite3",
    correction: Annotated[bool, typer.Option("--correction")] = False,
    correction_reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Strictly import a canonical CSV; invalid files are all-or-nothing."""

    storage = Storage.open(database)
    try:
        service = MeasurementWorkflowService(storage)
        plan = service.plan(experiment_id)
        result = parse_measurement_csv(
            source.read_bytes(),
            plan=plan,
            submitted_by=submitted_by,
            source_filename=source.name,
            batch_kind=(
                MeasurementBatchKind.CORRECTION
                if correction
                else MeasurementBatchKind.INITIAL
            ),
            correction_reason=correction_reason,
        )
        if not result.accepted or result.batch is None:
            raise MeasurementIngestionError(_format_import_issues(result.issues))
        service.submit(result.batch)
    except (MeasurementIngestionError, OSError, RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    typer.echo(f"accepted CSV batch: {result.batch.batch_id}")
    typer.echo(f"source SHA-256: {result.source_file_sha256}")


@measurements_app.command("acquire")
def acquire_sensor_measurements(
    experiment_id: Annotated[UUID, typer.Argument(help="Experiment UUID.")],
    operator_id: Annotated[str, typer.Option("--operator-id", prompt=True)],
    database: DatabaseOption = "data/autolab.sqlite3",
) -> None:
    """Acquire expected readings through the configured read-only mock sensor."""

    storage = Storage.open(database)
    try:
        workflow = MeasurementWorkflowService(storage)
        plan = workflow.plan(experiment_id)
        with storage.unit_of_work() as uow:
            experiment = uow.experiments.get(str(experiment_id))
            campaign = uow.campaigns.get(experiment.campaign_id)
            config = CampaignConfig.model_validate(campaign.config_json, strict=False)
            uow.commit()
        sensor_config = config.execution.sensor
        if config.execution.mode != "human_sensor" or sensor_config is None:
            raise ValueError("experiment is not configured for a read-only sensor")
        if sensor_config.adapter == "mock_physical":
            assert sensor_config.fixture_path is not None
            fixture_path = Path(sensor_config.fixture_path).expanduser().resolve()
            fixture_result = parse_measurement_csv(
                fixture_path.read_bytes(),
                plan=plan,
                submitted_by="mock-sensor-fixture-loader",
                source_filename=fixture_path.name,
            )
            if fixture_result.batch is None:
                raise MeasurementIngestionError(
                    _format_import_issues(fixture_result.issues)
                )
            source = mock_physical_sensor_source_from_rows(
                sensor_config.sensor_id,
                fixture_result.batch.measurements,
                fixture_name=fixture_path.name,
                fixture_sha256=fixture_result.source_file_sha256 or "",
            )
        else:
            sensor = load_sensor_adapter(
                sensor_config.adapter,
                sensor_id=sensor_config.sensor_id,
                settings=sensor_config.settings,
            )
            source = ReadOnlySensorSource(sensor)
        acquired_at = datetime.now(UTC)
        batch = asyncio.run(
            source.acquire(
                plan,
                acquisition_context(
                    experiment_id=experiment_id,
                    run_id=uuid5(
                        NAMESPACE_URL,
                        f"autolab:{experiment_id}:human-execution:1",
                    ),
                    submitted_by=operator_id,
                    acquired_at=acquired_at,
                ),
            )
        )
        workflow.submit(batch)
    except (
        MeasurementIngestionError,
        OSError,
        RecordNotFoundError,
        SensorError,
        ValueError,
    ) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    typer.echo(f"accepted physical-sensor-boundary batch: {batch.batch_id}")


@experiment_app.command("deviations")
def experiment_deviations(
    experiment_id: Annotated[UUID, typer.Argument(help="Experiment UUID.")],
    database: DatabaseOption = "data/autolab.sqlite3",
) -> None:
    """Show structured deviations from persisted human step evidence."""

    storage = Storage.open(database)
    try:
        with storage.unit_of_work() as uow:
            assert uow.session is not None
            from sqlalchemy import select

            from autonomous_lab.storage.orm import ActionEventRecord

            events = list(
                uow.session.scalars(
                    select(ActionEventRecord).where(
                        ActionEventRecord.execution_run_id
                        == str(uuid5(NAMESPACE_URL, f"autolab:{experiment_id}:human-execution:1"))
                    )
                )
            )
            uow.commit()
        deviations = [
            deviation
            for event in events
            for deviation in event.details.get("deviations", [])
        ]
    except (RecordNotFoundError, ValueError) as exc:
        _abort(str(exc), code=2)
    finally:
        storage.close()
    typer.echo(json.dumps(deviations, indent=2, sort_keys=True, default=str))


@experiment_app.command("show")
def experiment_show(
    experiment_id: Annotated[UUID, typer.Argument(help="Experiment UUID to inspect.")],
    database: DatabaseOption = "data/autolab.sqlite3",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the persisted evidence chain for one experiment."""

    storage = Storage.open(database)
    try:
        with storage.unit_of_work() as uow:
            evidence = uow.records.experiment_evidence(str(experiment_id))
            experiment = evidence["experiment"]
            proposal = evidence["proposal"]
            protocols = evidence["protocols"]
            validations = evidence["validations"]
            runs = evidence["runs"]
            measurements = evidence["measurements"]
            qc_runs = evidence["qc_runs"]
            metrics = evidence["metrics"]
            constraints = evidence["constraints"]
            payload = {
                "experiment_id": experiment.id,
                "campaign_id": experiment.campaign_id,
                "ordinal": experiment.ordinal,
                "state": experiment.state,
                "optimizer_trial_id": experiment.optimizer_trial_id,
                "failure_category": experiment.failure_category,
                "failure_message": experiment.failure_message,
                "proposal_id": None if proposal is None else proposal.id,
                "protocol_ids": [item.id for item in protocols],
                "validation": [
                    {"report_id": item.id, "status": item.status} for item in validations
                ],
                "execution_runs": [{"run_id": item.id, "status": item.status} for item in runs],
                "measurement_count": len(measurements),
                "qc": [{"qc_run_id": item.id, "status": item.status} for item in qc_runs],
                "metrics": {item.name: item.value for item in metrics},
                "constraints": {
                    item.metric_name: {
                        "residual": item.residual,
                        "feasible": item.feasible,
                    }
                    for item in constraints
                },
            }
    except RecordNotFoundError as exc:
        _abort(str(exc), code=3)
    finally:
        storage.close()
    _emit(payload, json_output=json_output)


def _midpoint_proposal(loaded: LoadedCampaign) -> ExperimentProposal:
    values: dict[str, QuantityValue] = {}
    for name, definition in sorted(loaded.config.parameter_space.parameters.items()):
        if definition.scale is ParameterScale.LOG:
            midpoint = (definition.minimum * definition.maximum).sqrt()
        else:
            midpoint = (definition.minimum + definition.maximum) / Decimal("2")
        values[name] = QuantityValue(value=midpoint, unit=definition.unit)
    namespace = f"autolab:validation:{loaded.canonical_sha256}"
    return ExperimentProposal(
        proposal_id=uuid5(NAMESPACE_URL, f"{namespace}:proposal"),
        experiment_id=uuid5(NAMESPACE_URL, f"{namespace}:experiment"),
        campaign_id=uuid5(NAMESPACE_URL, f"{namespace}:campaign"),
        suggestion_index=0,
        parameter_values=values,
        strategy_name="cli_midpoint_validation",
        strategy_version="1",
        strategy_trial_id="midpoint",
        created_at=datetime(2000, 1, 1, tzinfo=UTC),
    )


async def _simulated_capabilities(
    campaign: CampaignConfig,
    proposal: ExperimentProposal,
) -> CapabilitySnapshot:
    from autonomous_lab.hardware.simulated.backend import (
        SimulatedExperimentPlan,
        SimulatedHardwareBackend,
    )
    from autonomous_lab.simulation.assay_model import ExecutedAssayConditions

    values = proposal.parameter_values
    design = _simulation_design(campaign.assay_design)
    plan = SimulatedExperimentPlan(
        experiment_id=proposal.experiment_id,
        experiment_ordinal=proposal.suggestion_index,
        campaign_seed=campaign.campaign.seed,
        conditions=ExecutedAssayConditions(
            reagent_concentration_mg_ml=float(
                values["reagent_concentration"].to("milligram / milliliter").value
            ),
            reagent_volume_microliter=float(values["reagent_volume"].to("microliter").value),
            sample_volume_microliter=float(values["sample_volume"].to("microliter").value),
            incubation_minutes=float(values["incubation_time"].to("minute").value),
            temperature_celsius=float(values["temperature"].to("degree_Celsius").value),
        ),
        design=design,
    )
    backend = await SimulatedHardwareBackend.create(plan)
    return backend.registry.snapshot


def _simulation_design(config: AssayDesignConfig):
    from autonomous_lab.simulation.assay_model import AssayDesign

    return AssayDesign(
        blank_replicates=config.blank_replicates,
        standard_levels=tuple(float(level) for level in config.standard_levels),
        standard_replicates=config.standard_replicates,
        control_level=float(config.control_level),
        control_replicates=config.control_replicates,
        check_standard_level=float(config.check_standard_level),
        check_standard_replicates=config.check_standard_replicates,
        wavelength_nanometer=float(config.wavelength.to("nanometer").value),
    )


def _emit(payload: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    for key, value in payload.items():
        if isinstance(value, dict):
            typer.echo(f"{key}:")
            nested = cast(dict[object, object], value)
            for child_key, child_value in nested.items():
                typer.echo(f"  {child_key}: {child_value}")
        else:
            typer.echo(f"{key}: {value}")


def _prompt_acknowledgement(group_id: str) -> AcknowledgementDecision:
    choices = {
        "confirm": AcknowledgementDecision.CONFIRM,
        "deviation": AcknowledgementDecision.RECORD_DEVIATION,
        "skip": AcknowledgementDecision.SKIP,
        "abort": AcknowledgementDecision.ABORT,
        "failed": AcknowledgementDecision.MARK_FAILED,
    }
    while True:
        value = typer.prompt(
            f"{group_id}: confirm/deviation/skip/abort/failed",
            default="confirm",
        ).strip().lower()
        if value in choices:
            return choices[value]
        typer.echo("enter confirm, deviation, skip, abort, or failed", err=True)


def _select_correction_entries(
    entries: tuple[ExpectedMeasurement, ...],
) -> tuple[ExpectedMeasurement, ...]:
    by_sample = {item.sample_id: item for item in entries}
    selected = typer.prompt("Sample IDs to correct (comma separated)")
    identifiers = [item.strip() for item in selected.split(",") if item.strip()]
    unknown = sorted(set(identifiers) - set(by_sample))
    if unknown:
        raise ValueError(f"unknown correction sample IDs: {unknown}")
    if not identifiers:
        raise ValueError("at least one correction sample ID is required")
    return tuple(by_sample[item] for item in identifiers)


def _prompt_deviation(operator_id: str, description: str) -> ProtocolDeviation:
    values = {item.value: item for item in DeviationCategory}
    category_text = typer.prompt(
        "Deviation category (quantity_deviation/timing_deviation/missing_sample/"
        "contamination_suspected/operator_error/device_error/other)",
        default="other",
    ).strip()
    if category_text not in values:
        raise ValueError(f"unknown deviation category: {category_text!r}")
    category = values[category_text]
    requested = actual = None
    material_id = vessel_id = None
    if category in {
        DeviationCategory.QUANTITY_DEVIATION,
        DeviationCategory.TIMING_DEVIATION,
    }:
        unit = typer.prompt("Quantity unit (for example microliter or minute)")
        requested = QuantityValue(value=Decimal(typer.prompt("Requested value")), unit=unit)
        actual = QuantityValue(value=Decimal(typer.prompt("Actual value")), unit=unit)
    if category is DeviationCategory.QUANTITY_DEVIATION:
        material_id = typer.prompt("Material ID", default="reagent")
        vessel_id = typer.prompt("Vessel ID (blank for batch-wide)", default="") or None
    return ProtocolDeviation(
        category=category,
        description=description,
        requested_quantity=requested,
        actual_quantity=actual,
        material_id=material_id,
        vessel_id=vessel_id,
        recorded_by=operator_id,
    )


def _format_import_issues(issues: tuple[ImportIssue, ...]) -> str:
    return "; ".join(
        f"{item.code}{f' row {item.row_number}' if item.row_number else ''}: {item.message}"
        for item in issues
    )


def _status_payload(status: CampaignStatus) -> dict[str, object]:
    return {
        "campaign_id": str(status.campaign_id),
        "name": status.name,
        "state": status.state.value,
        "experiment_count": status.experiment_count,
        "rejected": status.rejected_count,
        "execution_failed": status.execution_failed_count,
        "aborted": status.aborted_count,
        "qc_failed": status.qc_failed_count,
        "observed": status.valid_observation_count,
        "feasible": status.feasible_count,
        "pareto_frontier": status.pareto_frontier,
    }


def _emit_campaign_progress(status: CampaignStatus) -> None:
    typer.echo(
        "progress: "
        f"experiments={status.experiment_count} "
        f"observed={status.valid_observation_count} "
        f"qc_failed={status.qc_failed_count} "
        f"feasible={status.feasible_count}"
    )


def _abort(message: str, *, code: int = 1) -> Never:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
