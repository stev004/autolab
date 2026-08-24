"""Outlier flagging with complete provenance and no implicit removal."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class OutlierFlag:
    measurement_id: str
    group: str
    method: str
    threshold: float
    score: float
    excluded_from_metrics: bool = False


def flag_modified_z_score_outliers(
    values: Sequence[tuple[str, float]], *, group: str, threshold: float = 3.5
) -> tuple[OutlierFlag, ...]:
    """Flag observations using median absolute deviation.

    For zero MAD, values equal to the median are retained and unequal values are
    assigned an infinite score.  The function flags only; it never removes data.
    """

    if threshold <= 0 or not math.isfinite(threshold):
        raise ValueError("outlier threshold must be positive and finite")
    if len(values) < 3:
        return ()
    identifiers = [item[0] for item in values]
    data = np.asarray([item[1] for item in values], dtype=float)
    if not np.all(np.isfinite(data)):
        raise ValueError("outlier input must be finite")
    median = float(np.median(data))
    deviations = np.abs(data - median)
    mad = float(np.median(deviations))
    if mad == 0:
        scores = np.where(deviations == 0, 0.0, math.inf)
    else:
        scores = 0.6744897501960817 * deviations / mad
    return tuple(
        OutlierFlag(
            measurement_id=measurement_id,
            group=group,
            method="modified_z_score_mad",
            threshold=threshold,
            score=float(score),
            excluded_from_metrics=False,
        )
        for measurement_id, score in zip(identifiers, scores, strict=True)
        if score > threshold
    )
