from decimal import Decimal

import pytest
from pydantic import ValidationError

from autonomous_lab.domain.parameters import ContinuousParameter, ParameterSpace
from autonomous_lab.domain.quantities import QuantityValue


def test_parameter_bounds_are_inclusive_and_unit_aware() -> None:
    parameter = ContinuousParameter(unit="microliter", minimum=20, maximum=200)

    assert parameter.contains(QuantityValue(value=0.02, unit="milliliter"))
    assert parameter.contains(QuantityValue(value=200, unit="microliter"))
    assert not parameter.contains(QuantityValue(value=201, unit="microliter"))


def test_rejects_invalid_parameter_ranges() -> None:
    with pytest.raises(ValidationError, match="minimum must be less"):
        ContinuousParameter(unit="microliter", minimum=20, maximum=20)

    with pytest.raises(ValidationError, match="log-scaled"):
        ContinuousParameter(unit="dimensionless", minimum=0, maximum=1, scale="log")


def test_concise_parameter_space_validates_complete_values() -> None:
    space = ParameterSpace.model_validate(
        {
            "volume": ContinuousParameter(unit="microliter", minimum=1, maximum=10),
            "time": ContinuousParameter(unit="minute", minimum=1, maximum=5),
        }
    )
    values = {
        "volume": QuantityValue(value=5, unit="microliter"),
        "time": QuantityValue(value=2, unit="minute"),
    }

    space.validate_values(values)
    assert space.parameters["volume"].minimum == Decimal("1")


def test_parameter_space_rejects_missing_values() -> None:
    space = ParameterSpace(
        parameters={"volume": ContinuousParameter(unit="microliter", minimum=1, maximum=10)}
    )

    with pytest.raises(ValueError, match=r"missing=.*volume"):
        space.validate_values({})
