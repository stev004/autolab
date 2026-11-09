"""End-to-end culture experiments against scripted contract fakes."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from culture_fakes import (
    START,
    TEMP,
    ScriptedController,
    ScriptedODSensor,
    capabilities,
    design,
    policy,
    proposal,
    q,
)

from autonomous_lab.culture import (
    ControllerError,
    CultureExperimentRecord,
    CultureExperimentRunner,
    CultureQCConfig,
    SteppedClock,
)
from autonomous_lab.domain.experiments import ExperimentProposal


def _run(
    *,
    controller_kwargs: dict[str, object] | None = None,
    sensor_kwargs: dict[str, object] | None = None,
    prop: ExperimentProposal | None = None,
    qc: CultureQCConfig | None = None,
) -> tuple[CultureExperimentRecord, ScriptedController, ScriptedODSensor]:
    clock = SteppedClock(START)
    controller = ScriptedController(clock=clock, **(controller_kwargs or {}))  # type: ignore[arg-type]
    sensor = ScriptedODSensor(clock=clock, **(sensor_kwargs or {}))  # type: ignore[arg-type]
    runner = CultureExperimentRunner(
        controller=controller, sensor=sensor, clock=clock, policy=policy(), qc_config=qc
    )
    record = asyncio.run(runner.run(prop or proposal(), design()))
    return record, controller, sensor


def _states(record: CultureExperimentRecord) -> list[str]:
    return [item.to_state for item in record.transitions]


def test_clean_run_is_analysed_with_a_full_evidence_trail() -> None:
    record, controller, sensor = _run()
    assert record.disposition == "analysed", record.reason
    assert _states(record) == [
        "validated",
        "queued",
        "running",
        "measured",
        "qc_passed",
        "analysed",
    ]
    assert record.metrics is not None
    # Scripted OD = 0.1 + 0.05 * hours, read at 6, 12, 18 and 24 h.
    assert [p.od for p in record.metrics.od_series] == [
        Decimal("0.40"),
        Decimal("0.70"),
        Decimal("1.00"),
        Decimal("1.30"),
    ]
    assert record.metrics.final_od750.value == Decimal("1.30")
    assert record.metrics.energy_kwh.value == Decimal("1.2")  # 0.05 kW for 24 h
    assert len(sensor.reads) == 4
    assert controller.safe_state_calls == ["hold complete"]
    assert record.evidence is not None
    # Telemetry every 30 minutes, inclusive of both ends.
    assert len(record.evidence.telemetry) == 49
    assert record.evidence.setpoint_command.protocol_hash == record.protocol.protocol_hash  # type: ignore[union-attr]
    assert len(record.sha256) == 64


def test_rejected_proposal_never_reaches_the_device() -> None:
    record, controller, sensor = _run(prop=proposal(temperature="39"))
    assert record.disposition == "rejected"
    assert _states(record) == ["rejected"]
    assert controller.commands == [] and controller.safe_state_calls == []
    assert sensor.reads == []
    assert record.validation_report is not None


def test_receipt_is_refused_if_device_capabilities_change_before_dispatch() -> None:
    clock = SteppedClock(START)
    controller = ScriptedController(clock=clock)
    sensor = ScriptedODSensor(clock=clock)
    original = controller.capability_manifest
    calls = {"n": 0}

    async def changing_manifest():
        calls["n"] += 1
        if calls["n"] == 1:
            return await original()
        return capabilities(adapter_version="0.0.2")

    controller.capability_manifest = changing_manifest  # type: ignore[method-assign]
    runner = CultureExperimentRunner(
        controller=controller, sensor=sensor, clock=clock, policy=policy()
    )
    record = asyncio.run(runner.run(proposal(), design()))
    assert record.disposition == "failed"
    assert record.reason is not None and record.reason.startswith("receipt_stale")
    assert controller.commands == []


def test_device_refusal_fails_the_run_and_still_requests_safe_state() -> None:
    # The adapter re-checks its own hard limit even though validation passed.
    record, controller, sensor = _run(
        controller_kwargs={"internal_maximum_temperature": Decimal(25)}
    )
    assert record.disposition == "failed"
    assert record.reason == "setpoint_out_of_device_range"
    assert controller.safe_state_calls == ["setpoint rejected: setpoint_out_of_device_range"]
    assert sensor.reads == [] and not sensor.is_open


def test_ambiguous_setpoint_failure_is_never_learned_from() -> None:
    failure = ControllerError("setpoint_timeout", "no ack", ambiguous=True)
    record, controller, _ = _run(controller_kwargs={"setpoint_failure": failure})
    assert record.disposition == "failed"
    assert record.reason == "setpoint_timeout"
    assert controller.safe_state_calls == ["setpoint command failed: setpoint_timeout"]
    assert _states(record)[-1] == "failed"


def test_protection_trip_stops_the_hold_and_fails_qc() -> None:
    record, controller, sensor = _run(controller_kwargs={"trip_after_hours": Decimal(8)})
    assert record.disposition == "qc_failed"
    assert record.qc_report is not None
    assert {"no_protection_trip", "readings_complete"} <= set(record.qc_report.failed_codes)
    assert record.evidence is not None and record.evidence.stopped_early
    assert controller.safe_state_calls == ["device protection tripped"]
    assert len(sensor.reads) == 1  # only the 6 h reading happened before the trip
    assert record.metrics is None


def test_temperature_drift_after_warmup_fails_qc() -> None:
    def drifting(hours: Decimal) -> Decimal:
        return Decimal("0.1") if hours < 4 else Decimal("2.5")

    record, _, _ = _run(controller_kwargs={"temperature_error": drifting})
    assert record.disposition == "qc_failed"
    assert record.qc_report is not None
    assert record.qc_report.failed_codes == ("temperature_control",)


def test_warmup_transient_is_ignored() -> None:
    def overshoot(hours: Decimal) -> Decimal:
        return Decimal(5) if hours < 2 else Decimal("0.2")

    record, _, _ = _run(controller_kwargs={"temperature_error": overshoot})
    assert record.disposition == "analysed", record.reason


def test_temperature_probe_dropout_fails_telemetry_coverage() -> None:
    def dropout(hours: Decimal) -> Decimal | None:
        return None if hours > 10 else Decimal("0.1")

    record, _, _ = _run(controller_kwargs={"temperature_error": dropout})
    assert record.qc_report is not None
    assert "telemetry_coverage" in record.qc_report.failed_codes


def test_applied_setpoint_that_differs_from_the_request_fails_qc() -> None:
    record, _, _ = _run(controller_kwargs={"applied_temperature_override": q(28, TEMP)})
    assert record.qc_report is not None
    assert "setpoint_acknowledged" in record.qc_report.failed_codes


def test_sensor_saturation_fails_qc() -> None:
    record, _, _ = _run(sensor_kwargs={"maximum_od": Decimal("1.0")})
    assert record.qc_report is not None
    assert record.qc_report.failed_codes == ("no_sensor_saturation",)


def test_missing_final_reading_fails_qc_but_is_recorded_as_evidence() -> None:
    record, _, _ = _run(sensor_kwargs={"fail_hours": frozenset({24})})
    assert record.disposition == "qc_failed"
    assert record.reconciliation is not None
    final = record.reconciliation.entries[-1]
    assert final.status == "missing" and final.error_code == "sensor_disconnected"


def test_one_missing_intermediate_reading_is_tolerated_by_a_lenient_threshold() -> None:
    lenient = CultureQCConfig(maximum_missing_reading_fraction=Decimal("0.25"))
    record, _, _ = _run(sensor_kwargs={"fail_hours": frozenset({12})}, qc=lenient)
    assert record.disposition == "analysed", record.reason
    assert record.metrics is not None and len(record.metrics.od_series) == 3


def test_reading_outside_its_time_window_is_not_counted() -> None:
    record, _, _ = _run(sensor_kwargs={"read_delay": timedelta(minutes=20)})
    assert record.reconciliation is not None
    assert {entry.status for entry in record.reconciliation.entries} == {"out_of_window"}
    assert record.disposition == "qc_failed"


def test_calibration_expiring_mid_run_fails_qc() -> None:
    record, _, _ = _run(sensor_kwargs={"calibration_expires_at": START + timedelta(hours=20)})
    assert record.qc_report is not None
    assert "calibration_valid" in record.qc_report.failed_codes


def test_failed_sensor_self_test_stops_before_any_setpoint() -> None:
    record, controller, _ = _run(sensor_kwargs={"self_test_passes": False})
    assert record.disposition == "failed"
    assert record.reason == "sensor_self_test_failed"
    assert controller.commands == []


def test_safe_state_failure_marks_the_run_ambiguous() -> None:
    failure = ControllerError("safe_state_timeout", "no response")
    record, _, _ = _run(controller_kwargs={"safe_state_failure": failure})
    assert record.disposition == "failed"
    assert record.reason == "ambiguous_device_state"


def test_cancellation_still_requests_safe_state() -> None:
    clock = SteppedClock(START)
    controller = ScriptedController(clock=clock)
    sensor = ScriptedODSensor(clock=clock)

    async def cancel_on_first_read(_request):
        raise asyncio.CancelledError

    sensor.read = cancel_on_first_read  # type: ignore[method-assign]
    runner = CultureExperimentRunner(
        controller=controller, sensor=sensor, clock=clock, policy=policy()
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run(proposal(), design()))
    assert controller.safe_state_calls == ["execution interrupted"]
    assert not sensor.is_open
