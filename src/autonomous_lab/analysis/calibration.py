"""Transparent ordinary least-squares calibration statistics."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class CalibrationStatistics:
    count: int
    distinct_levels: int
    slope: float
    intercept: float
    r_squared: float
    residual_standard_error: float
    slope_standard_error: float
    intercept_standard_error: float
    concentration_minimum: float
    concentration_maximum: float


def fit_linear_calibration(
    concentrations: Sequence[float], responses: Sequence[float]
) -> CalibrationStatistics:
    """Fit unweighted OLS to individual calibration replicates.

    No points are removed or averaged before fitting.  Callers that exclude a
    reading must do so explicitly and retain that decision in provenance.
    """

    if len(concentrations) != len(responses):
        raise ValueError("concentrations and responses must have equal length")
    if len(concentrations) < 3:
        raise ValueError("at least three calibration readings are required")
    x = np.asarray(concentrations, dtype=float)
    y = np.asarray(responses, dtype=float)
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError("calibration values must be finite")
    distinct_levels = int(np.unique(x).size)
    if distinct_levels < 2:
        raise ValueError("at least two distinct calibration levels are required")

    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    centered_x = x - x_mean
    sxx = float(np.dot(centered_x, centered_x))
    if sxx <= 0:
        raise ValueError("calibration concentrations have no variation")
    slope = float(np.dot(centered_x, y - y_mean) / sxx)
    intercept = y_mean - slope * x_mean
    predictions = intercept + slope * x
    residuals = y - predictions
    sse = float(np.dot(residuals, residuals))
    centered_y = y - y_mean
    sst = float(np.dot(centered_y, centered_y))
    r_squared = 1.0 - sse / sst if sst > 0 else 1.0 if sse == 0 else 0.0

    degrees_of_freedom = len(x) - 2
    residual_variance = sse / degrees_of_freedom if degrees_of_freedom > 0 else math.nan
    residual_standard_error = math.sqrt(max(0.0, residual_variance))
    slope_standard_error = math.sqrt(max(0.0, residual_variance / sxx))
    intercept_standard_error = math.sqrt(
        max(0.0, residual_variance * (1.0 / len(x) + x_mean * x_mean / sxx))
    )
    return CalibrationStatistics(
        count=len(x),
        distinct_levels=distinct_levels,
        slope=slope,
        intercept=intercept,
        r_squared=max(0.0, min(1.0, r_squared)),
        residual_standard_error=residual_standard_error,
        slope_standard_error=slope_standard_error,
        intercept_standard_error=intercept_standard_error,
        concentration_minimum=float(np.min(x)),
        concentration_maximum=float(np.max(x)),
    )
