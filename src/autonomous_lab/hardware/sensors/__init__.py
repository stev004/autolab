"""Contracts and zero-hardware doubles for read-only physical sensors."""

from autonomous_lab.hardware.sensors.calibration import (
    CalibrationIssue,
    CalibrationRecord,
    CalibrationReference,
    CalibrationStatus,
    CalibrationValidationResult,
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
    SensorError,
    SensorMalformedResponseError,
    SensorNotReadyError,
    SensorReadTimeoutError,
)
from autonomous_lab.hardware.sensors.mock import (
    MockPhysicalSensor,
    MockSensorFixture,
    MockSensorScenario,
    fixed_clock,
)

__all__ = [
    "CalibrationIssue",
    "CalibrationRecord",
    "CalibrationReference",
    "CalibrationStatus",
    "CalibrationValidationResult",
    "ConnectionStatus",
    "MeasurementRequest",
    "MockPhysicalSensor",
    "MockSensorFixture",
    "MockSensorScenario",
    "RawSensorMeasurement",
    "ReadOnlySensor",
    "SelfTestCheck",
    "SelfTestResult",
    "SelfTestStatus",
    "SensorCalibrationError",
    "SensorCapabilities",
    "SensorCapabilityError",
    "SensorConnection",
    "SensorConnectionError",
    "SensorError",
    "SensorMalformedResponseError",
    "SensorNotReadyError",
    "SensorReadTimeoutError",
    "SensorSourceEvidence",
    "WavelengthCapabilities",
    "fixed_clock",
    "validate_calibration",
]
