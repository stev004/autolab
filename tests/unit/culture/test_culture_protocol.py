"""Compiler, sampling plan and validation receipts for culture protocols."""

from datetime import timedelta

import pytest
from culture_fakes import LIGHT, START, TEMP, capabilities, design, policy, proposal, q
from culture_fakes import capabilities as make_capabilities
from culture_fakes import design as make_design
from culture_fakes import policy as make_policy
from culture_fakes import proposal as make_proposal

from autonomous_lab.culture import (
    CultureCompilationError,
    CultureControllerCapabilities,
    CultureDesign,
    CulturePolicy,
    CultureProtocol,
    CultureProtocolCompiler,
    CultureValidationResult,
    CultureValidationService,
)
from autonomous_lab.domain.enums import ValidationStatus
from autonomous_lab.domain.experiments import ExperimentProposal


def _validate(
    *,
    proposal: ExperimentProposal | None = None,
    design: CultureDesign | None = None,
    protocol: CultureProtocol | None = None,
    policy: CulturePolicy | None = None,
    capabilities: CultureControllerCapabilities | None = None,
) -> CultureValidationResult:
    """Validate with defaults from culture_fakes; override exactly one thing per test."""

    prop = proposal or make_proposal()
    des = design or make_design()
    return CultureValidationService().validate(
        proposal=prop,
        protocol=protocol or CultureProtocolCompiler().compile(prop, des),
        design=des,
        policy=policy or make_policy(),
        capabilities=capabilities or make_capabilities(),
    )


def _codes(result: CultureValidationResult) -> set[str]:
    return {issue.code for issue in result.report.issues}


def test_sampling_plan_reads_at_intervals_and_always_at_the_end() -> None:
    protocol = CultureProtocolCompiler().compile(
        proposal(), design(duration=q(20, "hour"), sampling_interval=q(6, "hour"))
    )
    offsets = [point.offset.to("hour").value for point in protocol.measurement_plan.points]
    assert offsets == [6, 12, 18, 20]
    first = protocol.measurement_plan.points[0]
    assert first.window(START) == (
        START + timedelta(hours=6, minutes=-10),
        START + timedelta(hours=6, minutes=10),
    )


def test_compilation_is_deterministic_in_its_scientific_content() -> None:
    prop, des = proposal(), design()
    first = CultureProtocolCompiler().compile(prop, des)
    second = CultureProtocolCompiler().compile(prop, des)
    assert first.hold == second.hold
    assert first.measurement_plan == second.measurement_plan
    assert first.protocol_hash == first.compute_hash()


def test_proposal_must_contain_exactly_the_culture_parameters() -> None:
    with pytest.raises(CultureCompilationError):
        CultureProtocolCompiler().compile(proposal(pH=q("7", "dimensionless")), design())


def test_overlapping_sample_windows_are_rejected_by_the_design() -> None:
    with pytest.raises(ValueError, match="half the sampling interval"):
        CultureDesign.model_validate(
            {
                "reactor_id": "r",
                "duration": q(10, "hour"),
                "sampling_interval": q(1, "hour"),
                "sample_tolerance": q(30, "minute"),
                "telemetry_interval": q(10, "minute"),
            },
            strict=False,
        )


def test_valid_protocol_receives_a_hash_bound_receipt() -> None:
    result = _validate()
    assert result.report.status is ValidationStatus.PASSED
    assert result.executable is not None
    assert result.executable.capability_snapshot_hash == capabilities().sha256
    assert result.executable.validation_policy_hash == policy().sha256
    assert result.executable.protocol_hash == result.protocol.protocol_hash


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        (proposal(temperature="37"), "setpoint_outside_policy"),
        (proposal(temperature="30.05"), "setpoint_off_resolution"),
        (proposal(light="550"), "light_outside_policy"),
    ],
)
def test_policy_limits_reject_unsafe_conditions(prop: ExperimentProposal, expected: str) -> None:
    result = _validate(proposal=prop)
    assert result.executable is None
    assert expected in _codes(result)


def test_device_limits_apply_even_inside_policy() -> None:
    narrow = capabilities(
        temperature_setpoint_range={"minimum": q(20, TEMP), "maximum": q(28, TEMP)},
        light_intensity_range={"minimum": q(0, LIGHT), "maximum": q(150, LIGHT)},
        maximum_hold_duration=q(12, "hour"),
    )
    codes = _codes(_validate(capabilities=narrow))
    assert {
        "setpoint_outside_device_range",
        "light_outside_device_range",
        "duration_exceeds_device",
    } <= codes


def test_setpoint_must_keep_a_margin_below_the_device_trip() -> None:
    codes = _codes(_validate(capabilities=capabilities(over_temperature_trip=q(31, TEMP))))
    assert "setpoint_too_close_to_trip" in codes


def test_unknown_reactor_is_rejected() -> None:
    assert "unknown_reactor" in _codes(_validate(design=design(reactor_id="reactor-9")))


def test_policy_bounds_duration_and_sampling_density() -> None:
    codes = _codes(
        _validate(
            design=design(
                duration=q(80, "hour"),
                sampling_interval=q(20, "minute"),
                sample_tolerance=q(5, "minute"),
            ),
            capabilities=capabilities(maximum_hold_duration=q(100, "hour")),
        )
    )
    assert {"duration_exceeds_policy", "sampling_too_frequent", "too_many_sample_points"} <= codes


def test_tampered_protocol_is_rejected_even_with_a_recomputed_hash() -> None:
    prop, des = proposal(), design()
    protocol = CultureProtocolCompiler().compile(prop, des)
    hotter = protocol.hold.model_copy(update={"temperature_setpoint": q(35, TEMP)})
    tampered = protocol.model_copy(update={"hold": hotter})
    tampered = tampered.model_copy(update={"protocol_hash": tampered.compute_hash()})
    codes = _codes(_validate(proposal=prop, design=des, protocol=tampered))
    assert "hold_not_from_proposal" in codes


def test_modified_protocol_hash_is_detected() -> None:
    prop, des = proposal(), design()
    protocol = CultureProtocolCompiler().compile(prop, des)
    edited = protocol.model_copy(update={"compiler_version": "9.9.9"})
    codes = _codes(_validate(proposal=prop, design=des, protocol=edited))
    assert {"protocol_hash_mismatch", "untrusted_compiler"} <= codes


def test_protocol_for_a_different_design_is_rejected() -> None:
    prop = proposal()
    protocol = CultureProtocolCompiler().compile(prop, design(sampling_interval=q(4, "hour")))
    codes = _codes(_validate(proposal=prop, design=design(), protocol=protocol))
    assert {"design_hash_mismatch", "plan_not_from_design"} <= codes


def test_resolution_uses_interval_units_for_temperature() -> None:
    # 0.1 delta_degC resolution: 30.1 degC is on-grid even though it is an offset unit.
    assert _validate(proposal=proposal(temperature="30.1")).executable is not None
