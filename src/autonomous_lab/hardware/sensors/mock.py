"""Fixture-driven external sensor double with no assay-simulator dependency."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import utc_now
from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue
from autonomous_lab.hardware.sensors.calibration import (
    CalibrationRecord,
    CalibrationReference,
    validate_calibration,
)
from autonomous_lab.hardware.sensors.contracts import (
    ConnectionStatus,
    MeasurementRequest,
    RawSensorMeasurement,
    ReadOnlySensor,
    SelfTestCheck,
    SelfTestResult,
    SelfTestStatus,
    SensorCapabilities,
    SensorConnection,
    SensorSourceEvidence,
    WavelengthCapabilities,
)
from autonomous_lab.hardware.sensors.errors import (
    SensorCalibrationError,
    SensorCapabilityError,
    SensorConnectionError,
    SensorMalformedResponseError,
    SensorNotReadyError,
    SensorReadTimeoutError,
)


class MockSensorFixture(DomainModel):
    sample_id: str
    measurement_type: str
    value: FiniteDecimal
    unit: str
    wavelength: QuantityValue | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MockSensorScenario(DomainModel):
    fixtures: tuple[MockSensorFixture, ...]
    timeout_sample_ids: frozenset[str] = frozenset()
    malformed_sample_ids: frozenset[str] = frozenset()
    disconnect_sample_ids: frozenset[str] = frozenset()
    self_test_fails: bool = False
    malformed_payload: str = "not-a-number"

    @model_validator(mode="after")
    def _unique_fixture_keys(self) -> MockSensorScenario:
        keys = [
            (item.sample_id, item.measurement_type, _wavelength_key(item.wavelength))
            for item in self.fixtures
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("mock sensor fixture keys must be unique")
        known_samples = {item.sample_id for item in self.fixtures}
        scenario_samples = (
            set(self.timeout_sample_ids)
            | set(self.malformed_sample_ids)
            | set(self.disconnect_sample_ids)
        )
        if unknown := scenario_samples - known_samples:
            raise ValueError(f"failure scenario references unknown samples: {sorted(unknown)}")
        return self


class MockPhysicalSensor(ReadOnlySensor):
    """A lifecycle-accurate read-only sensor driven solely by external fixtures."""

    def __init__(
        self,
        *,
        sensor_id: str,
        scenario: MockSensorScenario,
        measurement_ranges: dict[str, QuantityRange],
        calibration: CalibrationRecord | None,
        wavelength: WavelengthCapabilities | None = None,
        resolutions: dict[str, QuantityValue] | None = None,
        precisions: dict[str, QuantityValue] | None = None,
        adapter_name: str = "mock-physical-sensor",
        adapter_version: str = "0.1.0",
        firmware_version: str = "mock-fw-1",
        calibration_required: bool = True,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not sensor_id:
            raise ValueError("sensor_id must not be empty")
        fixture_types = {fixture.measurement_type for fixture in scenario.fixtures}
        if fixture_types != set(measurement_ranges):
            raise ValueError("fixture measurement types must exactly match declared ranges")
        self._sensor_id = sensor_id
        self._scenario = scenario
        self._measurement_ranges = dict(measurement_ranges)
        self._calibration = calibration
        self._wavelength = wavelength
        self._resolutions = dict(resolutions or {})
        self._precisions = dict(precisions or {})
        self._adapter_name = adapter_name
        self._adapter_version = adapter_version
        self._firmware_version = firmware_version
        self._calibration_required = calibration_required
        self._clock = clock
        self._connection: SensorConnection | None = None
        self._self_test: SelfTestResult | None = None

    @property
    def sensor_id(self) -> str:
        return self._sensor_id

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    async def open(self) -> SensorConnection:
        if self._connection is not None:
            return self._connection
        self._connection = SensorConnection(
            sensor_id=self.sensor_id,
            status=ConnectionStatus.OPEN,
            changed_at=self._clock(),
            transport_metadata={"transport": "in-memory-fixture", "is_mock": True},
        )
        self._self_test = None
        return self._connection

    async def close(self) -> SensorConnection:
        if self._connection is None:
            raise SensorConnectionError("cannot close a sensor that is not connected")
        closed = SensorConnection(
            session_id=self._connection.session_id,
            sensor_id=self.sensor_id,
            status=ConnectionStatus.CLOSED,
            changed_at=self._clock(),
            transport_metadata=self._connection.transport_metadata,
        )
        self._connection = None
        self._self_test = None
        return closed

    async def capability_manifest(self) -> SensorCapabilities:
        calibration = (
            ()
            if self._calibration is None
            else (CalibrationReference.from_record(self._calibration, at=self._clock()),)
        )
        return SensorCapabilities(
            sensor_id=self.sensor_id,
            sensor_type="mock_read_only",
            adapter_name=self._adapter_name,
            adapter_version=self._adapter_version,
            firmware_version=self._firmware_version,
            measurement_types=frozenset(self._measurement_ranges),
            measurement_ranges=self._measurement_ranges,
            resolutions=self._resolutions,
            precisions=self._precisions,
            wavelength=self._wavelength,
            calibration_required=self._calibration_required,
            calibrations=calibration,
            metadata={"external_boundary": True, "is_mock": True},
        )

    async def self_test(self) -> SelfTestResult:
        if self._connection is None:
            raise SensorConnectionError()
        started_at = self._clock()
        checks = (
            SelfTestCheck(
                name="fixture_transport",
                passed=not self._scenario.self_test_fails,
                message=("forced fixture failure" if self._scenario.self_test_fails else None),
            ),
        )
        result = SelfTestResult(
            sensor_id=self.sensor_id,
            status=(
                SelfTestStatus.FAILED
                if self._scenario.self_test_fails
                else SelfTestStatus.PASSED
            ),
            started_at=started_at,
            completed_at=self._clock(),
            checks=checks,
            metadata={"adapter": self._adapter_name, "is_mock": True},
        )
        self._self_test = result
        return result

    async def read(self, request: MeasurementRequest) -> RawSensorMeasurement:
        connection = self._require_ready()
        if request.sample_id in self._scenario.disconnect_sample_ids:
            self._connection = None
            self._self_test = None
            raise SensorConnectionError("mock sensor disconnected during read")
        if request.sample_id in self._scenario.timeout_sample_ids:
            raise SensorReadTimeoutError()
        if request.sample_id in self._scenario.malformed_sample_ids:
            raise SensorMalformedResponseError(self._scenario.malformed_payload)

        manifest = await self.capability_manifest()
        self._validate_request(request, manifest)
        calibration_id: UUID | None = None
        calibration_sha256: str | None = None
        if self._calibration_required:
            result = validate_calibration(
                self._calibration,
                sensor_id=self.sensor_id,
                measurement_type=request.measurement_type,
                at=self._clock(),
            )
            if not result.valid:
                issue = result.issues[0]
                raise SensorCalibrationError(issue.code, issue.message)
            calibration_id = result.calibration_id
            calibration_sha256 = result.calibration_sha256

        fixture = self._find_fixture(request)
        value = QuantityValue(value=fixture.value, unit=fixture.unit).to(request.expected_unit)
        allowed = manifest.measurement_ranges[request.measurement_type]
        minimum = allowed.minimum.to(request.expected_unit)
        maximum = allowed.maximum.to(request.expected_unit)
        if not minimum.value <= value.value <= maximum.value:
            raise SensorMalformedResponseError(
                f"{fixture.value} {fixture.unit}",
                "sensor response is outside its declared measurement range",
            )
        measured_at = self._clock()
        assert self._self_test is not None
        evidence = SensorSourceEvidence(
            sensor_id=self.sensor_id,
            connection_session_id=connection.session_id,
            self_test_id=self._self_test.self_test_id,
            adapter_name=self._adapter_name,
            adapter_version=self._adapter_version,
            firmware_version=self._firmware_version,
            capability_sha256=manifest.sha256,
            calibration_id=calibration_id,
            calibration_sha256=calibration_sha256,
            is_mock=True,
            acquired_at=measured_at,
            metadata={"external_boundary": True, "fixture_key": fixture.sample_id},
        )
        return RawSensorMeasurement(
            request_id=request.request_id,
            experiment_id=request.experiment_id,
            run_id=request.run_id,
            sample_id=request.sample_id,
            measurement_type=request.measurement_type,
            raw_value=value.value,
            unit=value.unit,
            wavelength=request.wavelength,
            measured_at=measured_at,
            source_evidence=evidence,
            metadata=fixture.metadata,
        )

    def _require_ready(self) -> SensorConnection:
        if self._connection is None:
            raise SensorConnectionError()
        if self._self_test is None or self._self_test.status is not SelfTestStatus.PASSED:
            raise SensorNotReadyError()
        return self._connection

    def _validate_request(
        self,
        request: MeasurementRequest,
        manifest: SensorCapabilities,
    ) -> None:
        if request.measurement_type not in manifest.measurement_types:
            raise SensorCapabilityError(
                f"unsupported measurement type {request.measurement_type!r}"
            )
        allowed = manifest.measurement_ranges[request.measurement_type]
        # Validating unit compatibility here yields a capability error before a fixture is read.
        try:
            allowed.minimum.to(request.expected_unit)
        except ValueError as exc:
            raise SensorCapabilityError(
                f"expected unit {request.expected_unit!r} is incompatible with sensor range"
            ) from exc
        if request.wavelength is None:
            if manifest.wavelength is not None:
                raise SensorCapabilityError("this optical measurement requires a wavelength")
        elif manifest.wavelength is None:
            raise SensorCapabilityError("this sensor does not accept wavelength requests")
        elif not manifest.wavelength.supports(request.wavelength):
            raise SensorCapabilityError(
                f"wavelength {request.wavelength.value} {request.wavelength.unit} is unsupported"
            )

    def _find_fixture(self, request: MeasurementRequest) -> MockSensorFixture:
        target = (
            request.sample_id,
            request.measurement_type,
            _wavelength_key(request.wavelength),
        )
        for fixture in self._scenario.fixtures:
            key = (
                fixture.sample_id,
                fixture.measurement_type,
                _wavelength_key(fixture.wavelength),
            )
            if key == target:
                return fixture
        raise SensorCapabilityError(f"no fixture value supplied for request {target!r}")


def _wavelength_key(wavelength: QuantityValue | None) -> str | None:
    return None if wavelength is None else str(wavelength.to("nanometer").value.normalize())


def fixed_clock(at: datetime) -> Callable[[], datetime]:
    """Return a deterministic clock useful for zero-hardware scenarios."""

    return lambda: at
