"""Serializable summaries at the analysis boundary."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from autonomous_lab.analysis.metrics import AssayAnalysis


def analysis_report(analysis: AssayAnalysis) -> dict[str, Any]:
    """Return a JSON-compatible analysis payload with stated method semantics."""

    payload = asdict(analysis)
    payload["signal_to_noise_definition"] = (
        "(control_mean - blank_mean) / pooled_sample_standard_deviation"
    )
    payload["uncertainty_scope"] = "replicate standard error; not a full uncertainty budget"
    return payload
