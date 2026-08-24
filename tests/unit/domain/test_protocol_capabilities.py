from decimal import Decimal

import pytest

from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import QuantityValue


def test_offset_temperature_range_accepts_delta_resolution() -> None:
    supported = QuantityRange(
        minimum=QuantityValue(value=Decimal("4"), unit="degree_Celsius"),
        maximum=QuantityValue(value=Decimal("80"), unit="degree_Celsius"),
        resolution=QuantityValue(value=Decimal("0.1"), unit="delta_degree_Celsius"),
    )

    assert supported.resolution is not None
    assert supported.resolution.unit == "delta_degree_Celsius"


def test_range_rejects_resolution_with_wrong_dimension() -> None:
    with pytest.raises(ValueError, match="dimensionality"):
        QuantityRange(
            minimum=QuantityValue(value=Decimal("4"), unit="degree_Celsius"),
            maximum=QuantityValue(value=Decimal("80"), unit="degree_Celsius"),
            resolution=QuantityValue(value=Decimal("1"), unit="microliter"),
        )
