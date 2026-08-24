from decimal import Decimal

import pytest
from pydantic import ValidationError

from autonomous_lab.domain.quantities import QuantityValue


def test_converts_compatible_volume_units() -> None:
    converted = QuantityValue(value=1, unit="milliliter").to("microliter")

    assert converted.value == Decimal("1000")
    assert converted.unit == "microliter"


def test_rejects_dimensional_mismatch() -> None:
    with pytest.raises(ValueError, match="not compatible"):
        QuantityValue(value=10, unit="minute").to("microliter")


def test_rejects_unknown_unit() -> None:
    with pytest.raises(ValidationError, match="unknown or invalid unit"):
        QuantityValue(value=1, unit="sprockets_per_unicorn")


@pytest.mark.parametrize("value", ["1.0", True, float("nan"), float("inf")])
def test_rejects_coerced_or_nonfinite_magnitudes(value: object) -> None:
    with pytest.raises(ValidationError):
        QuantityValue(value=value, unit="microliter")


def test_offset_temperature_canonicalization_preserves_celsius_value() -> None:
    canonical = QuantityValue(value=77, unit="degree_Fahrenheit").canonical()

    assert canonical.unit == "degree_Celsius"
    assert canonical.value.quantize(Decimal("0.000001")) == Decimal("25.000000")


def test_common_units_use_explicit_project_canonical_units() -> None:
    assert QuantityValue(value=0.001, unit="liter").canonical() == QuantityValue(
        value=1000, unit="microliter"
    )
    assert QuantityValue(value=60, unit="second").canonical() == QuantityValue(
        value=1, unit="minute"
    )

