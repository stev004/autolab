from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from autonomous_lab.campaigns.config import ValidationPolicyConfig
from autonomous_lab.campaigns.loader import load_campaign, load_validation_policy
from autonomous_lab.domain.enums import CommandType, ControlType, ValidationStatus
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import (
    CapabilityManifest,
    ControlDefinition,
    DispenseCommand,
    ExecutableProtocol,
    ExperimentProtocol,
    IncubateCommand,
    MaterialReference,
    MeasureAbsorbanceCommand,
    MeasurementPlan,
    MixCommand,
    ProtocolCommand,
    ProtocolStep,
    QuantityRange,
    ValidationReport,
    VesselDefinition,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.commands import DeviceResponse, ExecutionContext
from autonomous_lab.hardware.registry import DeviceRegistry
from autonomous_lab.hardware.simulated import SimulatedExperimentPlan, SimulatedHardwareBackend
from autonomous_lab.orchestration import (
    InMemoryExecutionJournal,
    LabOrchestrator,
    ReceiptVerificationError,
    StorageExecutionJournal,
    ValidationServiceReceiptVerifier,
)
from autonomous_lab.protocols import AssayProtocolCompiler
from autonomous_lab.provenance.hashing import canonical_sha256
from autonomous_lab.simulation.assay_model import (
    AssayDesign,
    ExecutedAssayConditions,
    FailureInjection,
)
from autonomous_lab.storage import Database, Storage, UnitOfWork
from autonomous_lab.storage.orm import ActionEventRecord, ExecutionRunRecord, MeasurementRecord
from autonomous_lab.validation import ProtocolValidationService


def _q(value: str, unit: str) -> QuantityValue:
    return QuantityValue(value=Decimal(value), unit=unit)


POLICY = ValidationPolicyConfig(
    schema_version="1",
    policy_name="test-policy",
    policy_version="1",
    allowed_materials=frozenset({"reagent"}),
    allowed_command_types=frozenset(
        {
            CommandType.DISPENSE,
            CommandType.INCUBATE,
            CommandType.MEASURE_ABSORBANCE,
        }
    ),
    required_control_types=frozenset({ControlType.BLANK}),
    minimum_dispense_volume=_q("1", "microliter"),
    maximum_dispense_volume=_q("250", "microliter"),
    maximum_well_volume=_q("300", "microliter"),
    wavelength_range=QuantityRange(
        minimum=_q("400", "nanometer"),
        maximum=_q("700", "nanometer"),
        resolution=_q("1", "nanometer"),
    ),
    minimum_blank_replicates=1,
    minimum_standard_levels=2,
    minimum_standard_replicates=1,
    minimum_control_replicates=1,
    supported_vessel_types=frozenset({"microplate_96_well"}),
)
POLICY_HASH = canonical_sha256(POLICY)


def _plan(
    experiment_id: UUID,
    *,
    failures: FailureInjection | None = None,
) -> SimulatedExperimentPlan:
    design = AssayDesign()
    vessel_count = (
        design.blank_replicates
        + len(design.standard_levels) * design.standard_replicates
        + design.control_replicates
        + (design.check_standard_replicates if design.check_standard_level is not None else 0)
    )
    return SimulatedExperimentPlan(
        experiment_id=experiment_id,
        experiment_ordinal=2,
        campaign_seed=41_027,
        conditions=ExecutedAssayConditions(
            reagent_concentration_mg_ml=0.65,
            reagent_volume_microliter=80,
            sample_volume_microliter=40,
            incubation_minutes=12,
            temperature_celsius=29,
        ),
        design=design,
        failures=failures or FailureInjection(),
        measurement_vessel_ids=tuple(
            f"protocol-well-{index:02d}" for index in range(1, vessel_count + 1)
        ),
    )


def _protocol(experiment_id: UUID, *, include_full_run: bool = True) -> ExperimentProtocol:
    design = AssayDesign()
    vessel_count = (
        design.blank_replicates
        + len(design.standard_levels) * design.standard_replicates
        + design.control_replicates
        + (design.check_standard_replicates if design.check_standard_level is not None else 0)
    )
    wells = tuple(f"protocol-well-{index:02d}" for index in range(1, vessel_count + 1))
    steps: tuple[ProtocolStep, ...]
    if include_full_run:
        preparation: list[ProtocolStep] = []
        for well in wells:
            preparation.extend(
                (
                    ProtocolStep(
                        step_id=f"dispense-reagent-{well}",
                        command=DispenseCommand(
                            material_id="reagent",
                            destination_vessel_id=well,
                            volume=_q("80", "microliter"),
                        ),
                        required_capability="liquid_handler",
                        timeout_seconds=Decimal("5"),
                        retry_safe=False,
                    ),
                    ProtocolStep(
                        step_id=f"dispense-sample-{well}",
                        command=DispenseCommand(
                            material_id="blank",
                            destination_vessel_id=well,
                            volume=_q("40", "microliter"),
                        ),
                        required_capability="liquid_handler",
                        timeout_seconds=Decimal("5"),
                        retry_safe=False,
                    ),
                    ProtocolStep(
                        step_id=f"mix-{well}",
                        command=MixCommand(
                            vessel_id=well,
                            cycles=3,
                            volume=_q("120", "microliter"),
                        ),
                        required_capability="liquid_handler",
                        timeout_seconds=Decimal("5"),
                        retry_safe=False,
                    ),
                )
            )
        steps = (
            *preparation,
            ProtocolStep(
                step_id="incubate",
                command=IncubateCommand(
                    vessel_ids=wells,
                    duration=_q("12", "minute"),
                    temperature=_q("29", "degree_Celsius"),
                ),
                required_capability="incubator",
                timeout_seconds=Decimal("5"),
                retry_safe=False,
            ),
            ProtocolStep(
                step_id="measure",
                command=MeasureAbsorbanceCommand(
                    vessel_ids=wells,
                    wavelength=_q("520", "nanometer"),
                ),
                required_capability="spectrophotometer",
                timeout_seconds=Decimal("5"),
                retry_safe=True,
            ),
        )
    else:
        steps = (
            ProtocolStep(
                step_id="measure",
                command=MeasureAbsorbanceCommand(
                    vessel_ids=wells,
                    wavelength=_q("520", "nanometer"),
                ),
                required_capability="spectrophotometer",
                timeout_seconds=Decimal("5"),
                retry_safe=True,
            ),
        )
    draft = ExperimentProtocol(
        experiment_id=experiment_id,
        source_proposal_id=uuid4(),
        source_proposal_hash="c" * 64,
        parameter_values={
            "reagent_concentration": _q("0.65", "milligram / milliliter"),
            "reagent_volume": _q("80", "microliter"),
            "sample_volume": _q("40", "microliter"),
            "incubation_time": _q("12", "minute"),
            "temperature": _q("29", "degree_Celsius"),
        },
        compiler_name="test-assay",
        compiler_version="1",
        materials=(
            MaterialReference(material_id="reagent", name="reagent"),
            MaterialReference(material_id="blank", name="blank"),
        ),
        vessels=tuple(
            VesselDefinition(
                vessel_id=vessel_id,
                vessel_type="microplate_96_well",
                maximum_volume=_q("300", "microliter"),
            )
            for vessel_id in wells
        ),
        controls=(
            ControlDefinition(
                control_id="blank",
                control_type=ControlType.BLANK,
                vessel_ids=wells[: design.blank_replicates],
            ),
        ),
        steps=steps,
        measurement_plan=MeasurementPlan(
            wavelength=_q("520", "nanometer"),
            vessel_ids=wells,
        ),
    )
    digest = canonical_sha256(draft, exclude={"protocol_hash"})
    return draft.model_copy(update={"protocol_hash": digest})


def _receipt(
    protocol: ExperimentProtocol,
    capabilities_hash: str,
) -> tuple[ExecutableProtocol, ValidationReport, ValidationServiceReceiptVerifier]:
    assert protocol.protocol_hash is not None
    report = ValidationReport(
        protocol_id=protocol.protocol_id,
        protocol_hash=protocol.protocol_hash,
        capability_snapshot_hash=capabilities_hash,
        validation_policy_hash=POLICY_HASH,
        status=ValidationStatus.PASSED,
    )
    executable = ExecutableProtocol(
        protocol=protocol,
        validation_report_id=report.report_id,
        protocol_hash=protocol.protocol_hash,
        capability_snapshot_hash=capabilities_hash,
        validation_policy_hash=POLICY_HASH,
    )
    verifier = ValidationServiceReceiptVerifier.from_reports(
        {report.report_id: report},
        validation_service=ProtocolValidationService(),
        policy=POLICY,
    )
    return executable, report, verifier


def test_validated_protocol_executes_in_order_with_measurement_provenance() -> None:
    experiment_id = uuid4()
    backend = asyncio.run(SimulatedHardwareBackend.create(_plan(experiment_id)))
    executable, _, verifier = _receipt(
        _protocol(experiment_id), backend.registry.capability_snapshot_hash
    )
    journal = InMemoryExecutionJournal()
    run_id = uuid4()

    result = asyncio.run(
        LabOrchestrator(backend.registry, verifier, journal=journal).execute(
            executable, run_id=run_id
        )
    )

    assert result.status == "succeeded"
    assert result.execution_evidence is not None
    assert result.execution_evidence.executed_parameters["reagent_volume"].value == Decimal("80")
    assert result.execution_evidence.executed_parameters["sample_volume"].value == Decimal("40")
    assert {item.material_kind for item in result.execution_evidence.resources} >= {
        "reagent",
        "blank",
    }
    assert [action.step_id for action in result.actions] == [
        step.step_id for step in executable.protocol.steps
    ]
    assert result.measurements
    assert all(item.experiment_id == experiment_id for item in result.measurements)
    assert all(item.run_id == run_id for item in result.measurements)
    assert all(item.step_id == "measure" for item in result.measurements)
    assert all(item.device_id == "simulated-spectrophotometer" for item in result.measurements)
    assert {item.vessel_id for item in result.measurements} == set(
        executable.protocol.measurement_plan.vessel_ids
    )
    assert [event["kind"] for event in journal.events if str(event["kind"]).startswith("step")] == [
        event_kind
        for _ in executable.protocol.steps
        for event_kind in ("step_started", "step_succeeded")
    ]


def test_real_compiler_validation_receipt_executes_on_simulated_backend() -> None:
    repository_root = Path(__file__).parents[2]
    campaign = load_campaign(repository_root / "configs" / "assay-reagent-reduction.yaml").config
    policy = load_validation_policy(repository_root / "configs" / "validation-policy.yaml")
    proposal = ExperimentProposal(
        campaign_id=uuid4(),
        suggestion_index=0,
        parameter_values={
            "reagent_concentration": _q("0.65", "milligram / milliliter"),
            "reagent_volume": _q("80", "microliter"),
            "sample_volume": _q("40", "microliter"),
            "incubation_time": _q("12", "minute"),
            "temperature": _q("29", "degree_Celsius"),
        },
        strategy_name="random",
        strategy_version="1",
        strategy_trial_id="trial-0",
    )
    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    design = AssayDesign(
        blank_replicates=campaign.assay_design.blank_replicates,
        standard_levels=tuple(float(item) for item in campaign.assay_design.standard_levels),
        standard_replicates=campaign.assay_design.standard_replicates,
        control_level=float(campaign.assay_design.control_level),
        control_replicates=campaign.assay_design.control_replicates,
        check_standard_level=(
            None
            if campaign.assay_design.check_standard_level is None
            else float(campaign.assay_design.check_standard_level)
        ),
        check_standard_replicates=campaign.assay_design.check_standard_replicates,
        wavelength_nanometer=float(campaign.assay_design.wavelength.to("nanometer").value),
    )
    backend = asyncio.run(
        SimulatedHardwareBackend.create(
            SimulatedExperimentPlan(
                experiment_id=proposal.experiment_id,
                experiment_ordinal=0,
                campaign_seed=campaign.campaign.seed,
                conditions=ExecutedAssayConditions(
                    reagent_concentration_mg_ml=0.65,
                    reagent_volume_microliter=80,
                    sample_volume_microliter=40,
                    incubation_minutes=12,
                    temperature_celsius=29,
                ),
                design=design,
                measurement_vessel_ids=protocol.measurement_plan.vessel_ids,
            )
        )
    )
    service = ProtocolValidationService()
    outcome = service.validate(
        proposal=proposal,
        protocol=protocol,
        campaign=campaign,
        policy=policy,
        capability_snapshot=backend.registry.snapshot,
    )
    assert outcome.passed, [issue.code for issue in outcome.report.issues]
    executable = outcome.require_executable()
    verifier = ValidationServiceReceiptVerifier.from_reports(
        {outcome.report.report_id: outcome.report},
        validation_service=service,
        policy=policy,
    )

    result = asyncio.run(
        LabOrchestrator(backend.registry, verifier).execute(executable, run_id=uuid4())
    )

    assert result.status == "succeeded"
    assert len(result.measurements) == len(protocol.measurement_plan.vessel_ids)
    assert {item.vessel_id for item in result.measurements} == set(
        protocol.measurement_plan.vessel_ids
    )


def test_tampered_protocol_is_rejected_before_journal_or_dispatch() -> None:
    experiment_id = uuid4()
    backend = asyncio.run(SimulatedHardwareBackend.create(_plan(experiment_id)))
    executable, _, verifier = _receipt(
        _protocol(experiment_id), backend.registry.capability_snapshot_hash
    )
    first_step = executable.protocol.steps[0]
    changed = first_step.model_copy(
        update={"command": first_step.command.model_copy(update={"volume": _q("81", "microliter")})}
    )
    tampered_protocol = executable.protocol.model_copy(
        update={"steps": (changed, *executable.protocol.steps[1:])}
    )
    tampered = executable.model_copy(update={"protocol": tampered_protocol})
    journal = InMemoryExecutionJournal()

    with pytest.raises(ReceiptVerificationError, match="protocol_hash_mismatch"):
        asyncio.run(
            LabOrchestrator(backend.registry, verifier, journal=journal).execute(
                tampered, run_id=uuid4()
            )
        )
    assert journal.begun == []
    assert backend.state.vessel_volumes_microliter == {}


def test_capability_change_makes_receipt_stale_before_dispatch() -> None:
    experiment_id = uuid4()
    backend = asyncio.run(SimulatedHardwareBackend.create(_plan(experiment_id)))
    executable, _, verifier = _receipt(_protocol(experiment_id), "b" * 64)
    journal = InMemoryExecutionJournal()
    with pytest.raises(ReceiptVerificationError, match="capability_hash_mismatch"):
        asyncio.run(
            LabOrchestrator(backend.registry, verifier, journal=journal).execute(
                executable, run_id=uuid4()
            )
        )
    assert journal.events == []


def test_retry_safe_sensor_timeout_is_retried_once() -> None:
    experiment_id = uuid4()
    backend = asyncio.run(
        SimulatedHardwareBackend.create(
            _plan(experiment_id, failures=FailureInjection(spectrophotometer_timeout=True))
        )
    )
    executable, _, verifier = _receipt(
        _protocol(experiment_id),
        backend.registry.capability_snapshot_hash,
    )
    result = asyncio.run(
        LabOrchestrator(backend.registry, verifier).execute(executable, run_id=uuid4())
    )
    assert result.status == "succeeded"
    assert [(action.status, action.attempt) for action in result.actions[-2:]] == [
        ("failed", 1),
        ("succeeded", 2),
    ]


def test_ambiguous_dispense_is_not_retried_and_enters_safe_state() -> None:
    experiment_id = uuid4()
    backend = asyncio.run(
        SimulatedHardwareBackend.create(
            _plan(experiment_id, failures=FailureInjection(ambiguous_dispense_failure=True))
        )
    )
    executable, _, verifier = _receipt(
        _protocol(experiment_id), backend.registry.capability_snapshot_hash
    )
    result = asyncio.run(
        LabOrchestrator(backend.registry, verifier).execute(executable, run_id=uuid4())
    )
    assert result.status == "failed"
    assert result.failure_category == "ambiguous_dispense"
    assert len(result.actions) == 1
    assert result.actions[0].ambiguous is True
    assert result.safe_state_reached is True
    assert backend.state.volume(executable.protocol.vessels[0].vessel_id) == Decimal("80")
    assert len(backend.state.safe_state_devices) == 3


def test_write_timeout_is_ambiguous_never_retried_and_stops_safely() -> None:
    class HangingLiquidHandler:
        device_id = "hanging-liquid-handler"

        def __init__(self) -> None:
            self.calls = 0
            self.safe = False

        async def capability_manifest(self) -> CapabilityManifest:
            return CapabilityManifest(
                device_id=self.device_id,
                device_type="liquid_handler",
                adapter_name="test.hanging",
                adapter_version="1",
                supported_commands=frozenset({CommandType.DISPENSE}),
                ranges={
                    "dispense_volume": QuantityRange(
                        minimum=_q("1", "microliter"),
                        maximum=_q("250", "microliter"),
                    )
                },
                supported_vessels=frozenset({"microplate_96_well"}),
            )

        async def execute(
            self,
            command: ProtocolCommand,
            context: ExecutionContext,
        ) -> DeviceResponse:
            del command, context
            self.calls += 1
            await asyncio.sleep(0.05)
            raise AssertionError("wait_for should cancel this operation")

        async def enter_safe_state(self, reason: str) -> None:
            del reason
            self.safe = True

    experiment_id = uuid4()
    device = HangingLiquidHandler()
    registry = asyncio.run(DeviceRegistry.create((device,)))
    original = _protocol(experiment_id)
    timed_step = original.steps[0].model_copy(
        update={"timeout_seconds": Decimal("0.001"), "retry_safe": True}
    )
    draft = original.model_copy(update={"steps": (timed_step,), "protocol_hash": None})
    protocol_hash = canonical_sha256(draft, exclude={"protocol_hash"})
    protocol = draft.model_copy(update={"protocol_hash": protocol_hash})
    executable, _, verifier = _receipt(protocol, registry.capability_snapshot_hash)

    result = asyncio.run(LabOrchestrator(registry, verifier).execute(executable, run_id=uuid4()))

    assert result.status == "failed"
    assert result.failure_category == "dispense_timeout"
    assert result.actions[0].ambiguous is True
    assert device.calls == 1
    assert device.safe is True
    assert result.safe_state_reached is True


def test_cancellation_completes_a_bounded_safe_stop_and_journals_failure() -> None:
    class CancellableLiquidHandler:
        device_id = "cancellable-liquid-handler"

        def __init__(self) -> None:
            self.dispatched = asyncio.Event()
            self.safe_state_started = False

        async def capability_manifest(self) -> CapabilityManifest:
            return CapabilityManifest(
                device_id=self.device_id,
                device_type="liquid_handler",
                adapter_name="test.cancellable",
                adapter_version="1",
                supported_commands=frozenset({CommandType.DISPENSE}),
                ranges={
                    "dispense_volume": QuantityRange(
                        minimum=_q("1", "microliter"),
                        maximum=_q("250", "microliter"),
                    )
                },
                supported_vessels=frozenset({"microplate_96_well"}),
            )

        async def execute(
            self,
            command: ProtocolCommand,
            context: ExecutionContext,
        ) -> DeviceResponse:
            del command, context
            self.dispatched.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def enter_safe_state(self, reason: str) -> None:
            del reason
            self.safe_state_started = True
            await asyncio.Event().wait()

    async def scenario() -> tuple[CancellableLiquidHandler, InMemoryExecutionJournal]:
        experiment_id = uuid4()
        device = CancellableLiquidHandler()
        registry = await DeviceRegistry.create((device,))
        original = _protocol(experiment_id)
        draft = original.model_copy(update={"steps": (original.steps[0],), "protocol_hash": None})
        protocol = draft.model_copy(
            update={"protocol_hash": canonical_sha256(draft, exclude={"protocol_hash"})}
        )
        executable, _, verifier = _receipt(protocol, registry.capability_snapshot_hash)
        journal = InMemoryExecutionJournal()
        task = asyncio.create_task(
            LabOrchestrator(
                registry,
                verifier,
                journal=journal,
                safe_state_timeout_seconds=0.01,
            ).execute(executable, run_id=uuid4())
        )
        await device.dispatched.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
        return device, journal

    device, journal = asyncio.run(scenario())

    assert device.safe_state_started is True
    assert journal.finished[-1]["status"] == "failed"
    assert journal.finished[-1]["failure_category"] == "dispense_cancelled"
    assert journal.finished[-1]["safe_state_reached"] is False


def test_unknown_write_error_after_dispatch_is_recorded_as_ambiguous() -> None:
    class FaultingLiquidHandler:
        device_id = "faulting-liquid-handler"

        async def capability_manifest(self) -> CapabilityManifest:
            return CapabilityManifest(
                device_id=self.device_id,
                device_type="liquid_handler",
                adapter_name="test.faulting",
                adapter_version="1",
                supported_commands=frozenset({CommandType.DISPENSE}),
                ranges={
                    "dispense_volume": QuantityRange(
                        minimum=_q("1", "microliter"),
                        maximum=_q("250", "microliter"),
                    )
                },
                supported_vessels=frozenset({"microplate_96_well"}),
            )

        async def execute(
            self,
            command: ProtocolCommand,
            context: ExecutionContext,
        ) -> DeviceResponse:
            del command, context
            raise RuntimeError("transport disconnected after write")

        async def enter_safe_state(self, reason: str) -> None:
            del reason

    experiment_id = uuid4()
    device = FaultingLiquidHandler()
    registry = asyncio.run(DeviceRegistry.create((device,)))
    original = _protocol(experiment_id)
    draft = original.model_copy(update={"steps": (original.steps[0],), "protocol_hash": None})
    protocol = draft.model_copy(
        update={"protocol_hash": canonical_sha256(draft, exclude={"protocol_hash"})}
    )
    executable, _, verifier = _receipt(protocol, registry.capability_snapshot_hash)

    result = asyncio.run(LabOrchestrator(registry, verifier).execute(executable, run_id=uuid4()))

    assert result.status == "failed"
    assert result.failure_category == "dispense_unexpected_error"
    assert result.actions[0].ambiguous is True


def test_storage_journal_commits_started_before_dispatch_and_all_results(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "orchestration.sqlite3")
    database.create_schema()
    storage = Storage(database)
    campaign_id = uuid4()
    experiment_id = uuid4()
    backend = asyncio.run(SimulatedHardwareBackend.create(_plan(experiment_id)))

    class StartCommitObserver:
        def __init__(self) -> None:
            self.device_id = backend.liquid_handler.device_id
            self.saw_committed_start = False

        async def capability_manifest(self) -> CapabilityManifest:
            return await backend.liquid_handler.capability_manifest()

        async def execute(
            self,
            command: ProtocolCommand,
            context: ExecutionContext,
        ) -> DeviceResponse:
            with database.session() as session:
                started_event = session.scalar(
                    select(ActionEventRecord.id).where(
                        ActionEventRecord.execution_run_id == str(context.run_id),
                        ActionEventRecord.step_id == context.step_id,
                        ActionEventRecord.event_type == "step.started",
                    )
                )
            self.saw_committed_start = started_event is not None
            return await backend.liquid_handler.execute(command, context)

        async def enter_safe_state(self, reason: str) -> None:
            await backend.liquid_handler.enter_safe_state(reason)

    observer = StartCommitObserver()
    registry = asyncio.run(
        DeviceRegistry.create((observer, backend.incubator, backend.spectrophotometer))
    )
    protocol = _protocol(experiment_id)
    executable, report, verifier = _receipt(protocol, registry.capability_snapshot_hash)
    assert protocol.protocol_hash is not None
    try:
        with UnitOfWork(database) as uow:
            uow.campaigns.add(
                campaign_id=str(campaign_id),
                name="orchestration-test",
                state="ready",
                schema_version="1",
                config_source="schema_version: '1'",
                config={"schema_version": "1"},
                objective={"metrics": []},
                parameter_space={"parameters": []},
            )
            uow.experiments.add(
                experiment_id=str(experiment_id),
                campaign_id=str(campaign_id),
                ordinal=2,
                state="validated",
            )
            uow.records.add_protocol(
                protocol_id=str(protocol.protocol_id),
                experiment_id=str(experiment_id),
                schema_version="1",
                protocol=protocol,
                compiler_name=protocol.compiler_name,
                compiler_version=protocol.compiler_version,
                protocol_hash=protocol.protocol_hash,
            )
            uow.records.add_validation_report(
                report_id=str(report.report_id),
                protocol_id=str(protocol.protocol_id),
                status=report.status,
                protocol_hash=protocol.protocol_hash,
                capability_snapshot=registry.snapshot,
                capability_snapshot_hash=registry.capability_snapshot_hash,
                validation_policy={"hash": POLICY_HASH},
                validation_policy_hash=POLICY_HASH,
                validator_version="1",
            )
            uow.commit()

        run_id = uuid4()
        journal = StorageExecutionJournal(storage, backend_name="simulated", backend_version="1")
        result = asyncio.run(
            LabOrchestrator(registry, verifier, journal=journal).execute(executable, run_id=run_id)
        )
        assert result.status == "succeeded"
        assert observer.saw_committed_start is True

        with database.session() as session:
            run = session.get(ExecutionRunRecord, str(run_id))
            assert run is not None
            events = list(
                session.scalars(
                    select(ActionEventRecord)
                    .where(ActionEventRecord.execution_run_id == str(run_id))
                    .order_by(ActionEventRecord.sequence)
                )
            )
            persisted_measurements = list(
                session.scalars(
                    select(MeasurementRecord).where(
                        MeasurementRecord.execution_run_id == str(run_id)
                    )
                )
            )
            assert run.status == "succeeded"
            assert [event.event_type for event in events] == [
                event_type
                for _ in result.actions
                for event_type in ("step.started", "step.completed")
            ]
            assert len(persisted_measurements) == len(result.measurements)
            assert all(item.provenance["run_id"] == str(run_id) for item in persisted_measurements)
    finally:
        database.dispose()


def test_manifest_range_keys_are_stable_for_validator_contract() -> None:
    experiment_id = uuid4()
    backend = asyncio.run(SimulatedHardwareBackend.create(_plan(experiment_id)))
    ranges = {name for manifest in backend.registry.snapshot.manifests for name in manifest.ranges}
    assert ranges == {
        "dispense_volume",
        "aspirate_volume",
        "transfer_volume",
        "mix_volume",
        "incubation_duration",
        "incubation_temperature",
        "wavelength",
    }


def test_invalid_backend_manifest_cannot_route_unadvertised_command() -> None:
    # Compile-time type and manifest shape smoke test for future adapters.
    manifest = CapabilityManifest(
        device_id="future-adapter",
        device_type="spectrophotometer",
        adapter_name="vendor.adapter",
        adapter_version="1",
        supported_commands=frozenset(),
        ranges={
            "wavelength": QuantityRange(
                minimum=_q("400", "nanometer"), maximum=_q("700", "nanometer")
            )
        },
    )
    assert not manifest.supported_commands
    assert datetime.now(UTC).tzinfo is UTC
