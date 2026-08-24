from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from autonomous_lab.campaigns.loader import load_campaign, load_validation_policy
from autonomous_lab.domain.enums import CommandType, ControlType, ValidationStatus
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import (
    CapabilityManifest,
    QuantityRange,
)
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.capabilities import CapabilitySnapshot
from autonomous_lab.protocols.assay_compiler import AssayProtocolCompiler
from autonomous_lab.provenance.hashing import canonical_sha256
from autonomous_lab.validation import ProtocolValidationService, ReceiptVerificationError
from autonomous_lab.validation.rules.parameters import parameter_fingerprint
from autonomous_lab.validation.validator import calculate_protocol_hash

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _campaign():
    return load_campaign(ROOT / "configs" / "assay-reagent-reduction.yaml").config


def _policy():
    return load_validation_policy(ROOT / "configs" / "validation-policy.yaml")


def _proposal(**overrides: float) -> ExperimentProposal:
    campaign = _campaign()
    definitions = campaign.parameter_space.parameters
    values = {
        "reagent_concentration": 0.5,
        "reagent_volume": 100.0,
        "sample_volume": 50.0,
        "incubation_time": 10.0,
        "temperature": 25.0,
    }
    values.update(overrides)
    return ExperimentProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000201"),
        experiment_id=UUID("00000000-0000-0000-0000-000000000202"),
        campaign_id=UUID("00000000-0000-0000-0000-000000000203"),
        suggestion_index=0,
        parameter_values={
            name: (
                definitions[name].quantity(value)
                if name != "reagent_volume" or value <= 200
                else QuantityValue(
                    value=Decimal(str(value)),
                    unit=definitions[name].unit,
                )
            )
            for name, value in values.items()
        },
        strategy_name="random",
        strategy_version="1",
        strategy_trial_id="trial-0",
        created_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
    )


def _range(
    minimum: str,
    maximum: str,
    unit: str,
    resolution: str,
    *,
    resolution_unit: str | None = None,
) -> QuantityRange:
    return QuantityRange(
        minimum=QuantityValue(value=Decimal(minimum), unit=unit),
        maximum=QuantityValue(value=Decimal(maximum), unit=unit),
        resolution=QuantityValue(
            value=Decimal(resolution),
            unit=resolution_unit or unit,
        ),
    )


def _capabilities(*, wavelength_maximum: str = "700") -> CapabilitySnapshot:
    vessel = frozenset({"microplate_96_well"})
    volume = _range("1", "250", "microliter", "0.1")
    manifests = (
        CapabilityManifest(
            device_id="incubator",
            device_type="incubator",
            adapter_name="test",
            adapter_version="1",
            supported_commands=frozenset({CommandType.INCUBATE}),
            ranges={
                "incubation_duration": _range("0.1", "1440", "minute", "0.1"),
                "incubation_temperature": _range(
                    "4",
                    "80",
                    "degree_Celsius",
                    "0.1",
                    resolution_unit="delta_degree_Celsius",
                ),
            },
            supported_vessels=vessel,
        ),
        CapabilityManifest(
            device_id="liquid-handler",
            device_type="liquid_handler",
            adapter_name="test",
            adapter_version="1",
            supported_commands=frozenset(
                {CommandType.DISPENSE, CommandType.ASPIRATE, CommandType.TRANSFER, CommandType.MIX}
            ),
            ranges={
                "dispense_volume": volume,
                "aspirate_volume": volume,
                "transfer_volume": volume,
                "mix_volume": _range("1", "250", "microliter", "0.1"),
            },
            supported_vessels=vessel,
        ),
        CapabilityManifest(
            device_id="spectrophotometer",
            device_type="spectrophotometer",
            adapter_name="test",
            adapter_version="1",
            supported_commands=frozenset({CommandType.MEASURE_ABSORBANCE}),
            ranges={"wavelength": _range("400", wavelength_maximum, "nanometer", "1")},
            supported_vessels=vessel,
            calibration_id="calibration-1",
        ),
    )
    return CapabilitySnapshot(manifests=manifests)


def _validated():
    proposal = _proposal()
    campaign = _campaign()
    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    service = ProtocolValidationService(clock=lambda: NOW)
    outcome = service.validate(
        proposal=proposal,
        protocol=protocol,
        campaign=campaign,
        policy=_policy(),
        capability_snapshot=_capabilities(),
    )
    return service, proposal, outcome


def _codes(outcome) -> set[str]:
    return {issue.code for issue in outcome.report.issues}


def test_valid_protocol_receives_exact_hash_bound_executable() -> None:
    service, _, outcome = _validated()

    assert outcome.report.status is ValidationStatus.PASSED
    executable = outcome.require_executable()
    assert executable.protocol_hash == calculate_protocol_hash(executable.protocol)
    assert executable.protocol_hash == executable.protocol.protocol_hash
    assert executable.capability_snapshot_hash == _capabilities().sha256
    assert executable.validation_policy_hash == canonical_sha256(_policy())
    verified = service.verify_executable(
        executable,
        report=outcome.report,
        policy=_policy(),
        capability_snapshot=_capabilities(),
    )
    assert verified == executable.protocol


def test_out_of_bounds_proposal_is_rejected_without_executable() -> None:
    proposal = _proposal(reagent_volume=220.0)
    campaign = _campaign()
    protocol = AssayProtocolCompiler().compile(proposal, campaign)

    outcome = ProtocolValidationService(clock=lambda: NOW).validate(
        proposal=proposal,
        protocol=protocol,
        campaign=campaign,
        policy=_policy(),
        capability_snapshot=_capabilities(),
    )

    assert outcome.executable is None
    assert "parameter.out_of_bounds" in _codes(outcome)


def test_full_declared_volume_and_condition_domain_is_executable() -> None:
    proposal = _proposal(
        reagent_concentration=1.0,
        reagent_volume=200.0,
        sample_volume=100.0,
        incubation_time=30.0,
        temperature=40.0,
    )
    campaign = _campaign()
    protocol = AssayProtocolCompiler().compile(proposal, campaign)

    outcome = ProtocolValidationService(clock=lambda: NOW).validate(
        proposal=proposal,
        protocol=protocol,
        campaign=campaign,
        policy=_policy(),
        capability_snapshot=_capabilities(),
    )

    assert outcome.report.status is ValidationStatus.PASSED
    assert outcome.executable is not None


def test_missing_controls_and_replicates_are_machine_readable_rejections() -> None:
    proposal = _proposal()
    campaign = _campaign()
    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    controls = tuple(
        control for control in protocol.controls if control.control_type is not ControlType.BLANK
    )
    tampered = protocol.model_copy(update={"controls": controls})

    outcome = ProtocolValidationService(clock=lambda: NOW).validate(
        proposal=proposal,
        protocol=tampered,
        campaign=campaign,
        policy=_policy(),
        capability_snapshot=_capabilities(),
    )

    assert outcome.report.status is ValidationStatus.REJECTED
    assert outcome.executable is None
    assert "control.required_type_missing" in _codes(outcome)
    assert "control.insufficient_blank_replicates" in _codes(outcome)
    assert "control.blank_layout_mismatch" in _codes(outcome)


def test_vessel_capacity_overflow_is_rejected_before_execution() -> None:
    proposal = _proposal(reagent_volume=200.0, sample_volume=100.0)
    campaign = _campaign()
    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    first = protocol.steps[0]
    oversized = first.command.model_copy(
        update={"volume": QuantityValue(value=Decimal("201"), unit="microliter")}
    )
    steps = (first.model_copy(update={"command": oversized}), *protocol.steps[1:])
    tampered = protocol.model_copy(update={"steps": steps})

    outcome = ProtocolValidationService(clock=lambda: NOW).validate(
        proposal=proposal,
        protocol=tampered,
        campaign=campaign,
        policy=_policy(),
        capability_snapshot=_capabilities(),
    )

    assert outcome.executable is None
    assert "volume.vessel_capacity_exceeded" in _codes(outcome)
    assert "recipe.reagent_volume_mismatch" in _codes(outcome)


def test_unsupported_wavelength_capability_rejects_protocol() -> None:
    proposal = _proposal()
    campaign = _campaign()
    protocol = AssayProtocolCompiler().compile(proposal, campaign)

    outcome = ProtocolValidationService(clock=lambda: NOW).validate(
        proposal=proposal,
        protocol=protocol,
        campaign=campaign,
        policy=_policy(),
        capability_snapshot=_capabilities(wavelength_maximum="500"),
    )

    assert outcome.executable is None
    assert "capability.out_of_range" in _codes(outcome)


def test_illegal_operation_order_is_rejected() -> None:
    proposal = _proposal()
    campaign = _campaign()
    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    measurement = protocol.steps[-1]
    reordered = protocol.model_copy(update={"steps": (measurement, *protocol.steps[:-1])})

    outcome = ProtocolValidationService(clock=lambda: NOW).validate(
        proposal=proposal,
        protocol=reordered,
        campaign=campaign,
        policy=_policy(),
        capability_snapshot=_capabilities(),
    )

    assert outcome.executable is None
    assert "operation.illegal_order" in _codes(outcome)


def test_duplicate_parameter_fingerprint_is_rejected_across_equivalent_units() -> None:
    proposal = _proposal()
    campaign = _campaign()
    protocol = AssayProtocolCompiler().compile(proposal, campaign)
    fingerprint = parameter_fingerprint(proposal.parameter_values, campaign.parameter_space)

    outcome = ProtocolValidationService(clock=lambda: NOW).validate(
        proposal=proposal,
        protocol=protocol,
        campaign=campaign,
        policy=_policy(),
        capability_snapshot=_capabilities(),
        historical_parameter_hashes={fingerprint},
    )

    assert outcome.executable is None
    assert "experiment.duplicate_parameters" in _codes(outcome)


def test_tampered_protocol_and_stale_inputs_fail_receipt_verification() -> None:
    service, _, outcome = _validated()
    executable = outcome.require_executable()
    first_step = executable.protocol.steps[0]
    changed_command = first_step.command.model_copy(
        update={"volume": QuantityValue(value=Decimal("99"), unit="microliter")}
    )
    changed_step = first_step.model_copy(update={"command": changed_command})
    changed_protocol = executable.protocol.model_copy(
        update={"steps": (changed_step, *executable.protocol.steps[1:])}
    )
    tampered_receipt = executable.model_copy(update={"protocol": changed_protocol})

    with pytest.raises(ReceiptVerificationError) as tampered:
        service.verify_executable(
            tampered_receipt,
            report=outcome.report,
            policy=_policy(),
            capability_snapshot=_capabilities(),
        )
    assert "receipt.protocol_hash_mismatch" in {issue.code for issue in tampered.value.issues}

    with pytest.raises(ReceiptVerificationError) as stale:
        service.verify_executable(
            executable,
            report=outcome.report,
            policy=_policy(),
            capability_snapshot=_capabilities(wavelength_maximum="650"),
        )
    assert "receipt.capability_hash_mismatch" in {issue.code for issue in stale.value.issues}
