import pytest
from pydantic import ValidationError

from autonomous_lab.domain.enums import ObjectiveDirection
from autonomous_lab.domain.objectives import ObjectiveMetric, ScientificObjective


def test_persisted_objectives_get_unique_ids() -> None:
    metric = ObjectiveMetric(
        name="cost_per_test", direction=ObjectiveDirection.MINIMIZE, unit="GBP"
    )

    first = ScientificObjective(metrics=(metric,))
    second = ScientificObjective(metrics=(metric,))

    assert first.objective_id != second.objective_id


def test_domain_artifacts_are_frozen() -> None:
    metric = ObjectiveMetric(
        name="cost_per_test", direction=ObjectiveDirection.MINIMIZE, unit="GBP"
    )

    with pytest.raises(ValidationError, match="frozen"):
        metric.name = "changed"
