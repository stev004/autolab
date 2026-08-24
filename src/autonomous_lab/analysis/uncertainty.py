"""Simple V0 measurement uncertainty estimates.

These are standard-error estimates under independent replicate noise.  They are
not a full metrological uncertainty budget and reports should describe them as such.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UncertaintyEstimate:
    standard_uncertainty: float
    expanded_uncertainty_95: float
    method: str = "replicate_standard_error_normal_k2"


def difference_of_means_uncertainty(
    *,
    first_standard_deviation: float,
    first_count: int,
    second_standard_deviation: float,
    second_count: int,
) -> UncertaintyEstimate:
    if first_count < 1 or second_count < 1:
        raise ValueError("replicate counts must be positive")
    if first_standard_deviation < 0 or second_standard_deviation < 0:
        raise ValueError("standard deviations must be non-negative")
    standard = math.sqrt(
        first_standard_deviation**2 / first_count + second_standard_deviation**2 / second_count
    )
    return UncertaintyEstimate(standard, 2.0 * standard)
