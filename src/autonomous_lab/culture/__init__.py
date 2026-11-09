"""Long-running biological culture experiments (roadmap stage 6, first slice).

A culture holds environmental conditions (temperature setpoint, light) for a
duration and samples optical density at intervals. This package provides the
protocol, deterministic validation and receipts, the setpoint-accepting
controller seam, time-series reconciliation, culture run-integrity QC, analysis
and a single-experiment runner. Campaign orchestration and persistence live in
``autonomous_lab.campaigns.culture_service``; see ``docs/culture.md``.
"""

from autonomous_lab.culture.analysis import CultureMetrics, analyse_culture
from autonomous_lab.culture.clock import CultureClock, SteppedClock, WallClock
from autonomous_lab.culture.compiler import CultureCompilationError, CultureProtocolCompiler
from autonomous_lab.culture.controller import (
    CULTURE_CONTROLLER_ENTRY_POINT_GROUP,
    ControllerError,
    CultureControllerRegistry,
    CultureEnvironmentController,
    EnvironmentTelemetry,
    IntegratedCultureSensor,
    SafeStateAcknowledgement,
    SetpointAcknowledgement,
    SetpointCommand,
)
from autonomous_lab.culture.evidence import CultureRunEvidence, RunEvent
from autonomous_lab.culture.models import (
    CultureControllerCapabilities,
    CultureDesign,
    CulturePolicy,
    CultureProtocol,
    CultureQCConfig,
    ExecutableCultureProtocol,
    HoldConditionsCommand,
    SamplePoint,
    TimeSeriesMeasurementPlan,
)
from autonomous_lab.culture.qc import CultureQCCheck, CultureQCReport, evaluate_culture_qc
from autonomous_lab.culture.runner import (
    CultureExperimentRecord,
    CultureExperimentRunner,
    CultureRunObserver,
)
from autonomous_lab.culture.timeseries import (
    SampleReading,
    TimeSeriesReconciliation,
    reconcile_time_series,
)
from autonomous_lab.culture.validation import CultureValidationResult, CultureValidationService

__all__ = [
    "CULTURE_CONTROLLER_ENTRY_POINT_GROUP",
    "ControllerError",
    "CultureClock",
    "CultureCompilationError",
    "CultureControllerCapabilities",
    "CultureControllerRegistry",
    "CultureDesign",
    "CultureEnvironmentController",
    "CultureExperimentRecord",
    "CultureExperimentRunner",
    "CultureMetrics",
    "CulturePolicy",
    "CultureProtocol",
    "CultureProtocolCompiler",
    "CultureQCCheck",
    "CultureQCConfig",
    "CultureQCReport",
    "CultureRunEvidence",
    "CultureRunObserver",
    "CultureValidationResult",
    "CultureValidationService",
    "EnvironmentTelemetry",
    "ExecutableCultureProtocol",
    "HoldConditionsCommand",
    "IntegratedCultureSensor",
    "RunEvent",
    "SafeStateAcknowledgement",
    "SamplePoint",
    "SampleReading",
    "SetpointAcknowledgement",
    "SetpointCommand",
    "SteppedClock",
    "TimeSeriesMeasurementPlan",
    "TimeSeriesReconciliation",
    "WallClock",
    "analyse_culture",
    "evaluate_culture_qc",
    "reconcile_time_series",
]
