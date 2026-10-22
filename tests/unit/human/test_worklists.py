from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from autonomous_lab.campaigns.loader import load_campaign, load_validation_policy
from autonomous_lab.domain.enums import ExecutionMode, ExecutorType, MeasurementRole
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.human import (
    AcknowledgementDecision,
    DeviationCategory,
    ExecutionStatus,
    HumanExecutionBackend,
    ProtocolDeviation,
    VirtualOperator,
    WorklistGenerator,
    export_worklist,
    human_assay_v1_capability_snapshot,
    render_markdown,
    render_measurements_csv,
    render_preparation_csv,
)
from autonomous_lab.measurement_ingestion import (
    ExpectedMeasurement,
    make_expected_plan,
    parse_measurement_csv,
)
from autonomous_lab.protocols.assay_compiler import AssayProtocolCompiler
from autonomous_lab.provenance.hashing import sha256_file
from autonomous_lab.validation import ProtocolValidationService

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _validated():
    campaign = load_campaign(ROOT / "configs" / "assay-reagent-reduction.yaml").config
    definitions = campaign.parameter_space.parameters
    proposal = ExperimentProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000801"),
        experiment_id=UUID("00000000-0000-0000-0000-000000000802"),
        campaign_id=UUID("00000000-0000-0000-0000-000000000803"),
        suggestion_index=0,
        parameter_values={
            "reagent_concentration": definitions["reagent_concentration"].quantity(0.5),
            "reagent_volume": definitions["reagent_volume"].quantity(100),
            "sample_volume": definitions["sample_volume"].quantity(50),
            "incubation_time": definitions["incubation_time"].quantity(10),
            "temperature": definitions["temperature"].quantity(25),
        },
        strategy_name="test",
        strategy_version="1",
        strategy_trial_id="trial-0",
        created_at=NOW,
    )
    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    outcome = ProtocolValidationService(clock=lambda: NOW).validate(
        proposal=proposal,
        protocol=protocol,
        campaign=campaign,
        policy=load_validation_policy(ROOT / "configs" / "validation-policy.yaml"),
        capability_snapshot=human_assay_v1_capability_snapshot(),
    )
    return campaign, outcome.require_executable()


def _worklist():
    campaign, executable = _validated()
    return WorklistGenerator().generate(
        executable,
        campaign_id=UUID("00000000-0000-0000-0000-000000000803"),
        campaign_name=campaign.campaign.name,
        execution_mode=ExecutionMode.HUMAN_MANUAL,
        operator_id="operator-1",
        generated_at=NOW,
    )


def test_human_capability_profile_validates_but_does_not_claim_execution() -> None:
    snapshot = human_assay_v1_capability_snapshot()

    assert len(snapshot.manifests) == 3
    assert len(snapshot.sha256) == 64
    assert {manifest.adapter_name for manifest in snapshot.manifests} == {
        "human_assay_v1_validation_profile"
    }
    assert all("execution" not in manifest.model_dump() for manifest in snapshot.manifests)


def test_worklist_is_grouped_complete_and_operator_usable() -> None:
    worklist = _worklist()

    assert len(worklist.requested_steps) == 83
    assert len(worklist.procedure) == 5
    assert [len(group.step_ids) for group in worklist.procedure] == [27, 27, 27, 1, 1]
    assert len(worklist.wells) == len(worklist.preparation) == len(worklist.measurement_sheet) == 27
    assert sum(
        requirement.required_amount.to("microliter").value
        for requirement in worklist.materials
        if requirement.material_id == "reagent"
    ) == Decimal("2700")
    assert all(row.total_volume.to("microliter").value == 150 for row in worklist.preparation)
    assert len(worklist.content_hash) == 64

    markdown = render_markdown(worklist)
    assert "2.7 mL" in markdown
    assert "do not include dead-volume contingency" in markdown
    assert "Completion: [ ] confirmed" in markdown
    assert "Deviation category and notes" in markdown
    assert "never estimate them" in markdown
    assert "100 µL" in markdown and "50 µL" in markdown and "150 µL" in markdown


def test_worklist_csv_exports_are_canonical_and_hashed(tmp_path: Path) -> None:
    worklist = _worklist()
    preparation = render_preparation_csv(worklist)
    measurements = render_measurements_csv(worklist)

    assert preparation.count("\n") == 28
    assert measurements.count("\n") == 28
    assert "reagent_volume,reagent_unit" in preparation
    assert measurements.startswith(
        "sample_id,measurement_type,wavelength_nm,value,unit,timestamp,status,notes\n"
    )
    assert ",520,,absorbance_unit,,," in measurements

    exported = export_worklist(worklist, tmp_path)
    assert {artifact.artifact_type for artifact in exported.artifacts} == {
        "markdown",
        "preparation_csv",
        "measurements_csv",
    }
    for artifact in exported.artifacts:
        path = tmp_path / artifact.file_name
        assert path.is_file()
        assert sha256_file(path) == artifact.sha256
        assert path.stat().st_size == artifact.byte_count


def test_generated_measurement_sheet_round_trips_through_strict_importer() -> None:
    worklist = _worklist()
    measure_step = next(
        group.step_ids[0] for group in worklist.procedure if group.group_id == "measure"
    )
    role_counts: dict[str, int] = {}
    role_aliases = {"positive": MeasurementRole.CONTROL}
    entries = []
    for row in worklist.measurement_sheet:
        replicate = role_counts.get(row.role, 0)
        role_counts[row.role] = replicate + 1
        entries.append(
            ExpectedMeasurement(
                expected_measurement_id=f"{measure_step}:{row.vessel_id}",
                sample_id=row.vessel_id,
                vessel_id=row.vessel_id,
                step_id=measure_step,
                sample_role=(
                    role_aliases[row.role]
                    if row.role in role_aliases
                    else MeasurementRole(row.role)
                ),
                measurement_type=row.measurement_name,
                unit=row.unit,
                wavelength=row.wavelength,
                replicate_index=replicate,
            )
        )
    plan = make_expected_plan(
        experiment_id=worklist.experiment_id,
        protocol_id=worklist.protocol_id,
        protocol_hash=worklist.protocol_hash,
        entries=tuple(entries),
        created_at=worklist.generated_at,
    )
    lines = render_measurements_csv(worklist).splitlines()
    completed = [lines[0]]
    for line in lines[1:]:
        fields = line.split(",")
        fields[3] = "0.125"
        fields[5] = "2026-08-14T12:30:00Z"
        fields[6] = "available"
        completed.append(",".join(fields))

    result = parse_measurement_csv(
        ("\n".join(completed) + "\n").encode(),
        plan=plan,
        submitted_by="operator",
        source_filename="completed-worklist.csv",
    )

    assert result.accepted
    assert result.batch is not None
    assert len(result.batch.measurements) == len(worklist.measurement_sheet)


def test_quantity_deviation_requires_compatible_requested_and_actual_values() -> None:
    with pytest.raises(ValidationError, match="must be compatible"):
        ProtocolDeviation(
            category=DeviationCategory.QUANTITY_DEVIATION,
            description="Wrong dimensionality",
            requested_quantity=QuantityValue(value=Decimal("100"), unit="microliter"),
            actual_quantity=QuantityValue(value=Decimal("2"), unit="minute"),
            recorded_by="operator",
            recorded_at=NOW,
        )


def test_virtual_operator_creates_human_evidence_without_measurements() -> None:
    worklist = _worklist()
    deviation = ProtocolDeviation(
        category=DeviationCategory.QUANTITY_DEVIATION,
        description="Pipette delivered five microliters extra in well A1",
        requested_quantity=QuantityValue(value=Decimal("100"), unit="microliter"),
        actual_quantity=QuantityValue(value=Decimal("105"), unit="microliter"),
        material_id="reagent",
        vessel_id="blank-01",
        recorded_by="virtual-test",
        recorded_at=NOW,
    )
    record = VirtualOperator("virtual-test").confirm(
        worklist,
        started_at=NOW,
        completed_at=NOW,
        deviations_by_group={"dispense-reagent": (deviation,)},
    )

    assert record.status is ExecutionStatus.COMPLETED
    assert len(record.acknowledgements) == 5
    assert len(record.step_evidence) == 83
    assert record.deviations == (deviation,)
    assert all(item.executor_type is ExecutorType.HUMAN_OPERATOR for item in record.step_evidence)
    assert all(item.execution_mode is ExecutionMode.HUMAN_MANUAL for item in record.step_evidence)
    assert all("measurement" not in item.model_dump() for item in record.step_evidence)
    affected = [item for item in record.step_evidence if item.deviations]
    assert [item.step_id for item in affected] == ["dispense-reagent-blank-01"]


def test_human_backend_groups_acknowledgements_and_requires_complete_worklist() -> None:
    worklist = _worklist()
    backend = HumanExecutionBackend()
    session = backend.start(worklist, operator_id="operator-1", started_at=NOW)

    session = backend.acknowledge(
        session,
        group_id="dispense-reagent",
        decision=AcknowledgementDecision.CONFIRM,
        acknowledged_at=NOW,
    )
    with pytest.raises(ValueError, match="unacknowledged worklist groups"):
        backend.complete(session, completed_at=NOW)

    for group in worklist.procedure[1:]:
        session = backend.acknowledge(
            session,
            group_id=group.group_id,
            decision=AcknowledgementDecision.CONFIRM,
            acknowledged_at=NOW,
        )
    record = backend.complete(session, completed_at=NOW)

    assert record.status is ExecutionStatus.COMPLETED
    assert len(record.acknowledgements) == 5
    assert len(record.step_evidence) == 83
    assert all(item.acknowledgement_id is not None for item in record.step_evidence)


def test_human_backend_abort_is_explicit_and_partial() -> None:
    backend = HumanExecutionBackend()
    worklist = _worklist()
    session = backend.start(worklist, operator_id="operator-1", started_at=NOW)
    session = backend.acknowledge(
        session,
        group_id="dispense-reagent",
        decision=AcknowledgementDecision.ABORT,
        acknowledged_at=NOW,
        note="Reagent bottle label did not match the worklist.",
    )
    record = backend.terminal_record(session, completed_at=NOW)

    assert record.status is ExecutionStatus.ABORTED
    assert len(record.step_evidence) == 27
    assert all(item.status is ExecutionStatus.ABORTED for item in record.step_evidence)


def test_human_package_never_imports_simulation() -> None:
    package = ROOT / "src" / "autonomous_lab" / "human"
    imported: set[str] = set()
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)
    assert not any(name.startswith("autonomous_lab.simulation") for name in imported)
