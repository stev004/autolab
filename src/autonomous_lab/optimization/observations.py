"""Construction of optimiser observations from QC-valid derived metrics."""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID

from autonomous_lab.domain.enums import ConstraintOperator
from autonomous_lab.domain.optimization import OptimizationObservation
from autonomous_lab.optimization.interface import OptimizationProblem


def build_optimization_observation(
    *,
    problem: OptimizationProblem,
    campaign_id: UUID,
    experiment_id: UUID,
    result_id: UUID,
    metric_values: Mapping[str, float | Decimal],
    measurement_uncertainty: Mapping[str, float | Decimal] | None = None,
) -> OptimizationObservation:
    """Build a QC-eligible scientific observation.

    Integrity-failed or execution-failed runs must instead be sent as excluded
    ``StrategyFeedback`` and never enter this function.
    """

    required = set(problem.objective_names) | set(problem.constraint_names)
    missing = required - set(metric_values)
    if missing:
        raise KeyError(f"required optimisation metrics are missing: {sorted(missing)}")
    converted: dict[str, Decimal] = {}
    for name in required:
        value = float(metric_values[name])
        if not math.isfinite(value):
            raise ValueError(f"optimisation metric must be finite: {name}")
        converted[name] = Decimal(str(metric_values[name]))

    residuals: dict[str, Decimal] = {}
    for constraint in problem.performance_constraints:
        observed = converted[constraint.metric]
        if constraint.operator is ConstraintOperator.LTE:
            residual = observed - constraint.threshold
        elif constraint.operator is ConstraintOperator.GTE:
            residual = constraint.threshold - observed
        else:
            raise ValueError(f"unsupported performance operator: {constraint.operator.value}")
        residuals[constraint.metric] = residual

    uncertainty_values = measurement_uncertainty or {}
    converted_uncertainty: dict[str, Decimal] = {}
    for name, raw_value in uncertainty_values.items():
        value = float(raw_value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"measurement uncertainty must be finite and non-negative: {name}")
        converted_uncertainty[name] = Decimal(str(raw_value))

    return OptimizationObservation(
        campaign_id=campaign_id,
        experiment_id=experiment_id,
        result_id=result_id,
        objective_values={name: converted[name] for name in problem.objective_names},
        constraint_residuals=residuals,
        feasible=all(residual <= 0 for residual in residuals.values()),
        qc_eligible=True,
        measurement_uncertainty=converted_uncertainty,
    )
