"""Compiler, sampling plan and validation receipts for culture protocols."""

from datetime import timedelta

import pytest
from culture_fakes import LIGHT, START, TEMP, capabilities, design, policy, proposal, q, variables
from culture_fakes import capabilities as make_capabilities
from culture_fakes import design as make_design
from culture_fakes import policy as make_policy
from culture_fakes import proposal as make_proposal

from autonomous_lab.culture import (
    ControlledVariable,
    CultureCompilationError,
    CultureControllerCapabilities,
    CultureDesign,
    CulturePolicy,
    CultureProtocol,
    CultureProtocolCompiler,
    CultureValidationResult,
    CultureValidationService,
    ProtectionLimit,
)
from autonomous_lab.domain.enums import ValidationStatus
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import QuantityRange


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
    offsets = [
        point.offset.to("hour").value for point in protocol.replicates[0].measurement_plan.points
    ]
    assert offsets == [6, 12, 18, 20]
    first = protocol.replicates[0].measurement_plan.points[0]
    assert first.window(START) == (
        START + timedelta(hours=6, minutes=-10),
        START + timedelta(hours=6, minutes=10),
    )


def test_compilation_is_deterministic_in_its_scientific_content() -> None:
    prop, des = proposal(), design()
    first = CultureProtocolCompiler().compile(prop, des)
    second = CultureProtocolCompiler().compile(prop, des)
    assert first.replicates == second.replicates
    assert first.protocol_hash == first.compute_hash()


def test_setpoints_must_name_exactly_the_device_variables() -> None:
    # An extra variable the device does not control is refused before dispatch...
    extra = _validate(proposal=proposal(stirring=q("100", "1 / minute")))
    assert "setpoints_do_not_match_device" in _codes(extra)
    # ...and so is a missing one: the device would have nothing to hold it at.
    missing = _validate(capabilities=capabilities(ph=True))
    assert "setpoints_do_not_match_device" in _codes(missing)
    # Fixing the missing variable in the design satisfies the device.
    fixed = design(fixed_setpoints={"ph": q("7.5", "dimensionless")})
    assert _validate(design=fixed, capabilities=capabilities(ph=True)).executable is not None


def test_a_variable_cannot_be_both_optimised_and_fixed() -> None:
    with pytest.raises(CultureCompilationError, match="both optimised and fixed"):
        CultureProtocolCompiler().compile(
            proposal(), design(fixed_setpoints={"temperature": q(30, TEMP)})
        )


def test_overlapping_sample_windows_are_rejected_by_the_design() -> None:
    with pytest.raises(ValueError, match="half the smallest sample gap"):
        CultureDesign.model_validate(
            {
                "reactor_ids": ["r"],
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
        (proposal(light="550"), "setpoint_outside_policy"),
    ],
)
def test_policy_limits_reject_unsafe_conditions(prop: ExperimentProposal, expected: str) -> None:
    result = _validate(proposal=prop)
    assert result.executable is None
    assert expected in _codes(result)


def test_device_limits_apply_even_inside_policy() -> None:
    declared = variables()
    declared["temperature"] = ControlledVariable(
        unit=TEMP, range=QuantityRange(minimum=q(20, TEMP), maximum=q(28, TEMP)), tracked=True
    )
    declared["light_intensity"] = ControlledVariable(
        unit=LIGHT, range=QuantityRange(minimum=q(0, LIGHT), maximum=q(150, LIGHT))
    )
    narrow = capabilities(controlled_variables=declared, maximum_hold_duration=q(12, "hour"))
    result = _validate(capabilities=narrow)
    out_of_range = {
        issue.path
        for issue in result.report.issues
        if issue.code == "setpoint_outside_device_range"
    }
    assert out_of_range == {
        "replicates[0].hold.setpoints.temperature",
        "replicates[0].hold.setpoints.light_intensity",
    }
    assert "duration_exceeds_device" in _codes(result)


def test_setpoint_must_keep_a_margin_below_the_device_trip() -> None:
    tight = capabilities(protection_limits={"temperature": ProtectionLimit(maximum=q(31, TEMP))})
    assert "setpoint_too_close_to_protection_limit" in _codes(_validate(capabilities=tight))
    # pH has limits on both sides: 6.0 sits 0.5 above the 5.5 trip (clear of the 0.3
    # margin), but 5.7 is only 0.2 above it.
    ph_design = design(fixed_setpoints={"ph": q("6.0", "dimensionless")})
    assert _validate(design=ph_design, capabilities=capabilities(ph=True)).executable is not None
    low = design(fixed_setpoints={"ph": q("5.7", "dimensionless")})
    codes = _codes(_validate(design=low, capabilities=capabilities(ph=True)))
    assert {"setpoint_too_close_to_protection_limit", "setpoint_outside_policy"} <= codes


def test_unknown_reactor_is_rejected() -> None:
    assert "unknown_reactor" in _codes(_validate(design=design(reactor_ids=("reactor-9",))))


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
    first = protocol.replicates[0]
    hotter = first.model_copy(
        update={
            "hold": first.hold.model_copy(
                update={"setpoints": {**first.hold.setpoints, "temperature": q(35, TEMP)}}
            )
        }
    )
    tampered = protocol.model_copy(update={"replicates": (hotter,)})
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


def test_explicit_sample_offsets_allow_denser_early_sampling() -> None:
    dense = design(
        sampling_interval=None,
        sample_offsets=(q(2, "hour"), q(4, "hour"), q(8, "hour"), q(24, "hour")),
    )
    protocol = CultureProtocolCompiler().compile(proposal(), dense)
    offsets = [p.offset.to("second").value for p in protocol.replicates[0].measurement_plan.points]
    assert offsets == [2 * 3600, 4 * 3600, 8 * 3600, 24 * 3600]
    assert _validate(design=dense).executable is not None


def test_schedule_must_be_given_one_way_only() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        design(sample_offsets=(q(24, "hour"),))


def test_each_replicate_gets_its_own_reactor_and_plan() -> None:
    three = design(
        reactor_ids=("reactor-1", "reactor-2", "reactor-3"), minimum_passing_replicates=2
    )
    protocol = CultureProtocolCompiler().compile(proposal(), three)
    assert [r.hold.reactor_id for r in protocol.replicates] == [
        "reactor-1",
        "reactor-2",
        "reactor-3",
    ]
    assert len({r.measurement_plan.plan_id for r in protocol.replicates}) == 3
    assert protocol.minimum_passing_replicates == 2
    assert _validate(design=three).executable is not None


def test_replicates_are_bounded_by_policy_and_device() -> None:
    three = design(reactor_ids=("reactor-1", "reactor-2", "reactor-9"))
    codes = _codes(_validate(design=three, policy=policy(maximum_replicates=2)))
    assert {"too_many_replicates", "unknown_reactor"} <= codes


def test_duplicate_reactors_are_rejected_by_the_design() -> None:
    with pytest.raises(ValueError, match="its own reactor"):
        design(reactor_ids=("reactor-1", "reactor-1"))
