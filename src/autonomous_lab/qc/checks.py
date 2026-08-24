"""Scientific performance constraints, separate from integrity QC."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

ConstraintOperator = Literal["lte", "gte"]


@dataclass(frozen=True, slots=True)
class MetricConstraint:
    metric: str
    operator: ConstraintOperator
    threshold: float

    def __post_init__(self) -> None:
        if not self.metric:
            raise ValueError("metric name must not be empty")
        if not math.isfinite(self.threshold):
            raise ValueError("constraint threshold must be finite")


@dataclass(frozen=True, slots=True)
class ConstraintEvaluation:
    metric: str
    operator: ConstraintOperator
    threshold: float
    observed_value: float
    residual: float
    feasible: bool


def evaluate_performance_constraints(
    metric_values: Mapping[str, float], constraints: tuple[MetricConstraint, ...]
) -> tuple[ConstraintEvaluation, ...]:
    """Evaluate valid scientific outcomes using residual <= 0 as feasible."""

    evaluations: list[ConstraintEvaluation] = []
    for constraint in constraints:
        if constraint.metric not in metric_values:
            raise KeyError(f"required performance metric is missing: {constraint.metric}")
        observed = float(metric_values[constraint.metric])
        if not math.isfinite(observed):
            residual = math.inf
        elif constraint.operator == "lte":
            residual = observed - constraint.threshold
        elif constraint.operator == "gte":
            residual = constraint.threshold - observed
        else:
            raise ValueError(f"unsupported constraint operator: {constraint.operator}")
        evaluations.append(
            ConstraintEvaluation(
                metric=constraint.metric,
                operator=constraint.operator,
                threshold=constraint.threshold,
                observed_value=observed,
                residual=residual,
                feasible=residual <= 0,
            )
        )
    return tuple(evaluations)
