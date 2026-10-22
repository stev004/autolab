"""Failures exposed by the read-only sensor boundary."""

from __future__ import annotations


class SensorError(RuntimeError):
    """Base failure with stable machine-readable recovery metadata."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_safe: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_safe = retry_safe


class SensorConnectionError(SensorError):
    def __init__(self, message: str = "sensor is not connected") -> None:
        super().__init__("sensor_disconnected", message, retry_safe=True)


class SensorNotReadyError(SensorError):
    def __init__(self, message: str = "sensor has not passed self-test") -> None:
        super().__init__("sensor_not_ready", message, retry_safe=True)


class SensorReadTimeoutError(SensorError, TimeoutError):
    def __init__(self, message: str = "sensor read timed out") -> None:
        super().__init__("sensor_timeout", message, retry_safe=True)


class SensorMalformedResponseError(SensorError):
    def __init__(self, raw_response: str, message: str = "sensor returned malformed data") -> None:
        super().__init__("malformed_sensor_response", message, retry_safe=False)
        self.raw_response = raw_response


class SensorCapabilityError(SensorError):
    def __init__(self, message: str) -> None:
        super().__init__("unsupported_measurement_request", message, retry_safe=False)


class SensorAdapterError(SensorError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retry_safe=False)


class SensorCalibrationError(SensorError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retry_safe=False)
