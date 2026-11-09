"""Contract fakes for culture tests.

These are *scripted* devices, not a biology or thermal model. They return the
values a test tells them to, so each test controls exactly one failure mode.
The real simulated reactor lives in the separate algae-sim project.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from autonomous_lab.culture import (
    ControllerError,
    CultureControllerCapabilities,
    CultureDesign,
    CulturePolicy,
    EnvironmentTelemetry,
    SafeStateAcknowledgement,
    SetpointAcknowledgement,
    SetpointCommand,
    SteppedClock,
)
from autonomous_lab.domain.experiments import ExperimentProposal
from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.sensors import (
    CalibrationRecord,
    CalibrationReference,
    ConnectionStatus,
    MeasurementRequest,
    RawSensorMeasurement,
    SelfTestCheck,
    SelfTestResult,
    SelfTestStatus,
    SensorCapabilities,
    SensorConnection,
    SensorConnectionError,
    SensorSourceEvidence,
    WavelengthCapabilities,
)

START = datetime(2026, 11, 1, 8, tzinfo=UTC)
TEMP = "degree_Celsius"
LIGHT = "micromole / meter ** 2 / second"
REACTOR = "reactor-1"
REACTORS = ("reactor-1", "reactor-2", "reactor-3")


def q(value: str | int, unit: str) -> QuantityValue:
    return QuantityValue(value=Decimal(str(value)), unit=unit)


def design(**overrides: object) -> CultureDesign:
    values: dict[str, object] = {
        "reactor_ids": (REACTOR,),
        "duration": q(24, "hour"),
        "sampling_interval": q(6, "hour"),
        "sample_tolerance": q(10, "minute"),
        "telemetry_interval": q(30, "minute"),
    }
    values.update(overrides)
    return CultureDesign.model_validate(values, strict=False)


def policy(**overrides: object) -> CulturePolicy:
    values: dict[str, object] = {
        "policy_name": "test-culture-policy",
        "policy_version": "1",
        "temperature_setpoint_range": QuantityRange(
            minimum=q(18, TEMP), maximum=q(36, TEMP), resolution=q("0.1", "delta_degree_Celsius")
        ),
        "light_intensity_range": QuantityRange(
            minimum=q(0, LIGHT), maximum=q(500, LIGHT), resolution=q(1, LIGHT)
        ),
        "maximum_duration": q(72, "hour"),
        "minimum_sampling_interval": q(30, "minute"),
        "maximum_sample_points": 200,
    }
    values.update(overrides)
    return CulturePolicy.model_validate(values, strict=False)


def capabilities(**overrides: object) -> CultureControllerCapabilities:
    values: dict[str, object] = {
        "controller_id": "ctrl-1",
        "device_type": "photobioreactor_environment",
        "adapter_name": "scripted-test-controller",
        "adapter_version": "0.0.1",
        "reactor_ids": frozenset(REACTORS),
        "temperature_setpoint_range": QuantityRange(minimum=q(15, TEMP), maximum=q(40, TEMP)),
        "light_intensity_range": QuantityRange(minimum=q(0, LIGHT), maximum=q(600, LIGHT)),
        "maximum_hold_duration": q(96, "hour"),
        "over_temperature_trip": q(42, TEMP),
        "is_simulated": True,
    }
    values.update(overrides)
    return CultureControllerCapabilities.model_validate(values, strict=False)


def proposal(
    temperature: str = "30", light: str = "200", **extra: QuantityValue
) -> ExperimentProposal:
    return ExperimentProposal(
        campaign_id=uuid4(),
        suggestion_index=0,
        parameter_values={
            "temperature_setpoint": q(temperature, TEMP),
            "light_intensity": q(light, LIGHT),
            **extra,
        },
        strategy_name="manual",
        strategy_version="1",
        strategy_trial_id="t0",
    )


def hours_since_start(clock: SteppedClock) -> Decimal:
    return Decimal(str((clock.now() - START).total_seconds())) / Decimal(3600)


@dataclass
class ScriptedController:
    """Setpoint-accepting fake. Re-checks its own limits like a real adapter must."""

    clock: SteppedClock
    manifest: CultureControllerCapabilities = field(default_factory=capabilities)
    #: measured - setpoint, as a function of elapsed hours; None = probe dropout.
    temperature_error: Callable[[Decimal], Decimal | None] = lambda _h: Decimal("0.1")
    trip_after_hours: Decimal | None = None
    applied_temperature_override: QuantityValue | None = None
    power_kw: Decimal = Decimal("0.05")
    setpoint_failure: ControllerError | None = None
    #: Adapter-side hard limit, checked even if upstream validation passed.
    internal_maximum_temperature: Decimal | None = None
    safe_state_failure: ControllerError | None = None
    #: reactor_id -> extra measured-minus-setpoint error (per-replicate faults).
    reactor_error: dict[str, Decimal] = field(default_factory=dict)
    #: reactor_id -> device fault codes reported in every telemetry sample.
    reactor_faults: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Simulates a device that forgot what it was holding (e.g. after a power cut).
    forget_commands: bool = False
    #: Raised by read_telemetry, e.g. to simulate an unreachable controller.
    telemetry_failure: ControllerError | None = None
    commands: list[SetpointCommand] = field(default_factory=list)
    safe_state_calls: list[str] = field(default_factory=list)
    safe_state_reactors: list[str] = field(default_factory=list)
    applied: dict[str, SetpointCommand] = field(default_factory=dict)
    started: dict[str, datetime] = field(default_factory=dict)

    @property
    def controller_id(self) -> str:
        return self.manifest.controller_id

    async def capability_manifest(self) -> CultureControllerCapabilities:
        return self.manifest

    async def apply_setpoints(self, command: SetpointCommand) -> SetpointAcknowledgement:
        self.commands.append(command)
        if self.setpoint_failure is not None:
            raise self.setpoint_failure
        temperature = command.temperature_setpoint.to(TEMP).value
        limits = self.manifest.temperature_setpoint_range
        ceiling = self.internal_maximum_temperature
        if not limits.minimum.value <= temperature <= limits.maximum.value or (
            ceiling is not None and temperature > ceiling
        ):
            return SetpointAcknowledgement(
                command_id=command.command_id,
                controller_id=self.controller_id,
                reactor_id=command.reactor_id,
                accepted=False,
                rejection_code="setpoint_out_of_device_range",
                acknowledged_at=self.clock.now(),
            )
        self.applied[command.reactor_id] = command
        self.started[command.reactor_id] = self.clock.now()
        return SetpointAcknowledgement(
            command_id=command.command_id,
            controller_id=self.controller_id,
            reactor_id=command.reactor_id,
            accepted=True,
            applied_temperature_setpoint=self.applied_temperature_override
            or command.temperature_setpoint,
            applied_light_intensity=command.light_intensity,
            acknowledged_at=self.clock.now(),
        )

    async def read_telemetry(self, reactor_id: str) -> EnvironmentTelemetry:
        if self.telemetry_failure is not None:
            raise self.telemetry_failure
        applied = self.applied[reactor_id]
        elapsed = Decimal(str((self.clock.now() - self.started[reactor_id]).total_seconds())) / 3600
        tripped = self.trip_after_hours is not None and elapsed >= self.trip_after_hours
        error = self.temperature_error(elapsed)
        if error is not None:
            error += self.reactor_error.get(reactor_id, Decimal(0))
        setpoint = applied.temperature_setpoint.to(TEMP).value
        return EnvironmentTelemetry(
            controller_id=self.controller_id,
            reactor_id=reactor_id,
            sampled_at=self.clock.now(),
            measured_temperature=None if error is None else q(str(setpoint + error), TEMP),
            applied_temperature_setpoint=applied.temperature_setpoint,
            applied_light_intensity=applied.light_intensity,
            protection_tripped=tripped,
            trip_count=1 if tripped else 0,
            cumulative_energy=q(str(self.power_kw * elapsed), "kilowatt_hour"),
            faults=self.reactor_faults.get(reactor_id, ()),
            active_command_id=None if self.forget_commands else applied.command_id,
        )

    async def enter_safe_state(self, reactor_id: str, reason: str) -> SafeStateAcknowledgement:
        self.safe_state_calls.append(reason)
        self.safe_state_reactors.append(reactor_id)
        if self.safe_state_failure is not None:
            raise self.safe_state_failure
        return SafeStateAcknowledgement(
            controller_id=self.controller_id,
            reactor_id=reactor_id,
            reason=reason,
            reached=True,
            acknowledged_at=self.clock.now(),
        )


@dataclass
class ScriptedODSensor:
    """Read-only OD750 fake returning ``od(elapsed_hours, sample_id)`` from a script."""

    clock: SteppedClock
    od: Callable[[Decimal, str], Decimal] = lambda h, _s: Decimal("0.1") + Decimal("0.05") * h
    sensor_id: str = "od-1"
    maximum_od: Decimal = Decimal("4")
    fail_hours: frozenset[int] = frozenset()
    self_test_passes: bool = True
    calibration_expires_at: datetime = START + timedelta(days=30)
    read_delay: timedelta = timedelta(0)
    _open: bool = False
    _session: SensorConnection | None = None
    reads: list[MeasurementRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.calibration = CalibrationRecord(
            sensor_id=self.sensor_id,
            measurement_type="optical_density",
            calibrated_at=START - timedelta(days=1),
            expires_at=self.calibration_expires_at,
            method="scripted",
            reference_material="none (test fake)",
            created_at=START - timedelta(days=1),
        )

    @property
    def is_open(self) -> bool:
        return self._open

    async def open(self) -> SensorConnection:
        self._open = True
        self._session = SensorConnection(
            sensor_id=self.sensor_id, status=ConnectionStatus.OPEN, changed_at=self.clock.now()
        )
        return self._session

    async def close(self) -> SensorConnection:
        self._open = False
        assert self._session is not None
        return SensorConnection(
            session_id=self._session.session_id,
            sensor_id=self.sensor_id,
            status=ConnectionStatus.CLOSED,
            changed_at=self.clock.now(),
        )

    async def capability_manifest(self) -> SensorCapabilities:
        return SensorCapabilities(
            sensor_id=self.sensor_id,
            sensor_type="optical_density_probe",
            adapter_name="scripted-od",
            adapter_version="0.0.1",
            measurement_types=frozenset({"optical_density"}),
            measurement_ranges={
                "optical_density": QuantityRange(
                    minimum=q(0, "dimensionless"),
                    maximum=QuantityValue(value=self.maximum_od, unit="dimensionless"),
                )
            },
            wavelength=WavelengthCapabilities(discrete_wavelengths=(q(750, "nanometer"),)),
            calibrations=(CalibrationReference.from_record(self.calibration, at=self.clock.now()),),
        )

    async def self_test(self) -> SelfTestResult:
        return SelfTestResult(
            sensor_id=self.sensor_id,
            status=SelfTestStatus.PASSED if self.self_test_passes else SelfTestStatus.FAILED,
            started_at=self.clock.now(),
            completed_at=self.clock.now(),
            checks=(SelfTestCheck(name="lamp", passed=self.self_test_passes),),
        )

    async def read(self, request: MeasurementRequest) -> RawSensorMeasurement:
        if not self._open or self._session is None:
            raise SensorConnectionError()
        self.reads.append(request)
        elapsed = hours_since_start(self.clock)
        if int(elapsed) in self.fail_hours:
            raise SensorConnectionError("scripted dropout")
        value = min(self.od(elapsed, request.sample_id), self.maximum_od)
        measured_at = self.clock.now() + self.read_delay
        return RawSensorMeasurement(
            request_id=request.request_id,
            experiment_id=request.experiment_id,
            run_id=request.run_id,
            sample_id=request.sample_id,
            measurement_type=request.measurement_type,
            raw_value=value,
            unit="dimensionless",
            wavelength=request.wavelength,
            measured_at=measured_at,
            source_evidence=SensorSourceEvidence(
                sensor_id=self.sensor_id,
                connection_session_id=self._session.session_id,
                self_test_id=uuid4(),
                adapter_name="scripted-od",
                adapter_version="0.0.1",
                capability_sha256="0" * 64,
                calibration_id=self.calibration.calibration_id,
                calibration_sha256=self.calibration.sha256,
                is_mock=True,
                acquired_at=measured_at,
            ),
            metadata={"saturated": value >= self.maximum_od},
        )
