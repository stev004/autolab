"""Conversions between neutral scientific analysis and domain artifacts."""

from __future__ import annotations

import math
from collections.abc import Iterable
from decimal import Decimal
from uuid import UUID

from autonomous_lab.analysis.metrics import AnalysisReading, AssayAnalysis
from autonomous_lab.domain.measurements import DerivedMetric, Measurement
from autonomous_lab.domain.metric_registry import metric_definition


def readings_from_domain(measurements: Iterable[Measurement]) -> tuple[AnalysisReading, ...]:
    """Adapt persisted raw measurements without importing storage or orchestration."""

    converted: list[AnalysisReading] = []
    for fallback_index, item in enumerate(measurements):
        if item.role.value == "standard" and item.analyte_level is None:
            raise ValueError(f"standard measurement {item.measurement_id} has no analyte level")
        raw_index = item.metadata.get("acquisition_index", fallback_index)
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise ValueError("measurement acquisition_index metadata must be an integer")
        converted.append(
            AnalysisReading(
                measurement_id=str(item.measurement_id),
                role=item.role.value,
                concentration=float(item.analyte_level or 0),
                response=float(item.raw_value),
                acquisition_index=raw_index,
                status="ok",
            )
        )
    return tuple(converted)


_EXECUTED_QUANTITY_METRICS = frozenset({"cost_per_test", "reagent_consumption", "reagent_volume"})


def derived_metrics_from_analysis(
    analysis: AssayAnalysis, *, experiment_id: UUID
) -> tuple[DerivedMetric, ...]:
    """Create versioned domain metrics with explicit raw-measurement lineage."""

    source_ids = tuple(UUID(identifier) for identifier in analysis.included_measurement_ids)
    uncertainty_by_name = {
        "blank_mean": analysis.blank.standard_deviation / math.sqrt(analysis.blank.count),
        "control_mean": analysis.control.standard_deviation / math.sqrt(analysis.control.count),
        "calibration_slope": analysis.calibration.slope_standard_error,
        "sensitivity": analysis.calibration.slope_standard_error,
        "response_standard_uncertainty": analysis.response_uncertainty.standard_uncertainty,
    }
    converted: list[DerivedMetric] = []
    for name, value in analysis.metric_values().items():
        if not math.isfinite(value):
            raise ValueError(f"cannot persist non-finite derived metric: {name}")
        is_executed_quantity = name in _EXECUTED_QUANTITY_METRICS
        uncertainty = uncertainty_by_name.get(name)
        converted.append(
            DerivedMetric(
                experiment_id=experiment_id,
                name=name,
                value=Decimal(str(value)),
                unit=metric_definition(name).canonical_unit,
                source_measurement_ids=() if is_executed_quantity else source_ids,
                algorithm_name="autolab_assay_analysis",
                algorithm_version=analysis.method_version,
                uncertainty=Decimal(str(uncertainty)) if uncertainty is not None else None,
                metadata={
                    "excluded_measurement_ids": analysis.excluded_measurement_ids,
                    "source_kind": (
                        "executed_protocol_quantities"
                        if is_executed_quantity
                        else "raw_measurements"
                    ),
                },
            )
        )
    return tuple(converted)
