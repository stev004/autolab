from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from autonomous_lab.hardware.sensors import (
    CalibrationRecord,
    CalibrationStatus,
    validate_calibration,
)

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def calibration(**changes: object) -> CalibrationRecord:
    values: dict[str, object] = {
        "calibration_id": uuid4(),
        "sensor_id": "sensor-1",
        "measurement_type": "absorbance",
        "calibrated_at": NOW - timedelta(days=5),
        "expires_at": NOW + timedelta(days=5),
        "method": "mock two-point optical reference",
        "reference_material": "neutral-density-reference",
        "coefficients": {"slope": Decimal("1.001"), "intercept": Decimal("0.0002")},
        "raw_artifact_id": "artifact-cal-1",
        "raw_artifact_sha256": "a" * 64,
        "created_at": NOW - timedelta(days=5),
    }
    values.update(changes)
    return CalibrationRecord.model_validate(values)


def test_calibration_hash_and_validation_are_deterministic() -> None:
    record = calibration()

    first = validate_calibration(
        record,
        sensor_id="sensor-1",
        measurement_type="absorbance",
        at=NOW,
    )
    second = validate_calibration(
        record,
        sensor_id="sensor-1",
        measurement_type="absorbance",
        at=NOW,
    )

    assert first.valid
    assert first.effective_status is CalibrationStatus.VALID
    assert first.calibration_sha256 == record.sha256 == second.calibration_sha256


@pytest.mark.parametrize(
    ("record", "sensor_id", "measurement_type", "code"),
    [
        (None, "sensor-1", "absorbance", "calibration_missing"),
        (
            calibration(expires_at=NOW - timedelta(seconds=1)),
            "sensor-1",
            "absorbance",
            "calibration_expired",
        ),
        (
            calibration(status=CalibrationStatus.INVALIDATED),
            "sensor-1",
            "absorbance",
            "calibration_invalidated",
        ),
        (calibration(), "sensor-2", "absorbance", "calibration_sensor_mismatch"),
        (calibration(), "sensor-1", "fluorescence", "calibration_measurement_mismatch"),
    ],
)
def test_calibration_rejection_reasons_are_machine_readable(
    record: CalibrationRecord | None,
    sensor_id: str,
    measurement_type: str,
    code: str,
) -> None:
    result = validate_calibration(
        record,
        sensor_id=sensor_id,
        measurement_type=measurement_type,
        at=NOW,
    )

    assert not result.valid
    assert code in {issue.code for issue in result.issues}


def test_calibration_rejects_inconsistent_artifact_and_expiry() -> None:
    with pytest.raises(ValidationError, match="expiry"):
        calibration(expires_at=NOW - timedelta(days=6))
    with pytest.raises(ValidationError, match="supplied together"):
        calibration(raw_artifact_sha256=None)
