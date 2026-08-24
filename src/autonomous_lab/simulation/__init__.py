"""Deterministic, hardware-neutral assay simulation.

Only raw instrument-like observations leave this package.  Optimisation code must
not import the private response-surface implementation.
"""

from autonomous_lab.simulation.assay_model import (
    AssayBatch,
    AssayDesign,
    CostModel,
    ExecutedAssayConditions,
    FailureInjection,
    NonlinearAssaySimulator,
    RawAssayMeasurement,
    SimulationConfig,
    WellRole,
)
from autonomous_lab.simulation.domain_adapters import measurements_from_simulated_batch

__all__ = [
    "AssayBatch",
    "AssayDesign",
    "CostModel",
    "ExecutedAssayConditions",
    "FailureInjection",
    "NonlinearAssaySimulator",
    "RawAssayMeasurement",
    "SimulationConfig",
    "WellRole",
    "measurements_from_simulated_batch",
]
