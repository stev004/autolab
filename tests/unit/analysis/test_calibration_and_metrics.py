from __future__ import annotations

import math

import pytest

from autonomous_lab.analysis.calibration import fit_linear_calibration
from autonomous_lab.analysis.metrics import AnalysisReading, analyze_assay


def _reading(
    identifier: str, role: str, concentration: float, response: float, index: int
) -> AnalysisReading:
    return AnalysisReading(identifier, role, concentration, response, index)


def _synthetic_records() -> tuple[AnalysisReading, ...]:
    records: list[AnalysisReading] = []
    for index, response in enumerate((0.00, 0.02, 0.01)):
        records.append(_reading(f"b{index}", "blank", 0, response, index))
    offset = len(records)
    for level in (0.0, 0.5, 1.0):
        for replicate, residual in enumerate((-0.01, 0.0, 0.01)):
            records.append(
                _reading(
                    f"s{level}-{replicate}",
                    "standard",
                    level,
                    0.02 + 2 * level + residual,
                    offset + len(records),
                )
            )
    for replicate, response in enumerate((1.00, 1.02, 0.98, 1.00)):
        records.append(_reading(f"c{replicate}", "control", 0.5, response, len(records)))
    return tuple(records)


def test_calibration_reports_ols_and_slope_standard_error() -> None:
    calibration = fit_linear_calibration(
        [0, 0, 0.5, 0.5, 1, 1],
        [0.01, 0.03, 1.01, 1.03, 2.01, 2.03],
    )

    assert calibration.slope == pytest.approx(2.0)
    assert calibration.intercept == pytest.approx(0.02)
    assert calibration.r_squared > 0.999
    assert calibration.slope_standard_error > 0
    assert calibration.residual_standard_error > 0


def test_metrics_use_documented_sample_statistics_and_pooled_snr() -> None:
    analysis = analyze_assay(_synthetic_records())

    assert analysis.blank.mean == pytest.approx(0.01)
    assert analysis.blank.standard_deviation == pytest.approx(0.01)
    assert analysis.control.mean == pytest.approx(1.0)
    assert analysis.control.standard_deviation == pytest.approx(math.sqrt(0.0008 / 3))
    assert analysis.control.coefficient_of_variation == pytest.approx(math.sqrt(0.0008 / 3))
    assert analysis.calibration.slope == pytest.approx(2.0)
    pooled_sd = math.sqrt((2 * 0.01**2 + 3 * (0.0008 / 3)) / 5)
    assert analysis.signal_to_noise == pytest.approx((1.0 - 0.01) / pooled_sd)
    assert analysis.response_uncertainty.standard_uncertainty == pytest.approx(
        math.sqrt(0.01**2 / 3 + (0.0008 / 3) / 4)
    )


def test_outlier_like_reading_is_not_silently_removed() -> None:
    records = list(_synthetic_records())
    records[-1] = _reading("c3", "control", 0.5, 2.0, records[-1].acquisition_index)

    inclusive = analyze_assay(records)
    explicit = analyze_assay(records, excluded_measurement_ids=("c3",))

    assert "c3" in inclusive.included_measurement_ids
    assert inclusive.control.mean == pytest.approx(1.25)
    assert explicit.control.mean == pytest.approx(1.0)
    assert explicit.excluded_measurement_ids == ("c3",)


def test_missing_readings_block_analysis() -> None:
    records = list(_synthetic_records())
    records[0] = AnalysisReading("b0", "blank", 0, None, 0, "missing")

    with pytest.raises(ValueError, match="complete"):
        analyze_assay(records)


def test_exclusions_must_be_part_of_the_supplied_raw_evidence() -> None:
    with pytest.raises(ValueError, match="not part of the supplied evidence"):
        analyze_assay(_synthetic_records(), excluded_measurement_ids=("not-a-reading",))
