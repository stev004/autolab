"""Domain enumerations with stable serialized values."""

from enum import StrEnum


class ObjectiveDirection(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class ConstraintOperator(StrEnum):
    LTE = "lte"
    GTE = "gte"
    LT = "lt"
    GT = "gt"
    EQ = "eq"


class ParameterScale(StrEnum):
    LINEAR = "linear"
    LOG = "log"


class CampaignState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionMode(StrEnum):
    """How protocol operations and measurements cross the lab boundary."""

    SIMULATION = "simulation"
    HUMAN_MANUAL = "human_manual"
    HUMAN_SENSOR = "human_sensor"
    AUTOMATED_PHYSICAL = "automated_physical"


class ExecutorType(StrEnum):
    SIMULATED_DEVICE = "simulated_device"
    PHYSICAL_DEVICE = "physical_device"
    HUMAN_OPERATOR = "human_operator"
    EXTERNAL_SYSTEM = "external_system"


class MeasurementSourceType(StrEnum):
    SIMULATED = "simulated"
    MANUAL_ENTRY = "manual_entry"
    FILE_IMPORT = "file_import"
    PHYSICAL_SENSOR = "physical_sensor"
    EXTERNAL_LAB = "external_lab"


class ExperimentState(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    QUEUED = "queued"
    AWAITING_EXECUTION = "awaiting_execution"
    RUNNING = "running"
    AWAITING_MEASUREMENTS = "awaiting_measurements"
    MEASURED = "measured"
    FAILED = "failed"
    ABORTED = "aborted"
    QC_PASSED = "qc_passed"
    QC_FAILED = "qc_failed"
    ANALYSED = "analysed"
    OBSERVED = "observed"


class QCStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class ValidationStatus(StrEnum):
    PASSED = "passed"
    REJECTED = "rejected"


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class MeasurementRole(StrEnum):
    BLANK = "blank"
    STANDARD = "standard"
    CONTROL = "control"
    CHECK_STANDARD = "check_standard"
    SAMPLE = "sample"


class ControlType(StrEnum):
    BLANK = "blank"
    STANDARD = "standard"
    POSITIVE = "positive"
    CHECK_STANDARD = "check_standard"


class CommandType(StrEnum):
    DISPENSE = "dispense"
    ASPIRATE = "aspirate"
    TRANSFER = "transfer"
    MIX = "mix"
    INCUBATE = "incubate"
    MEASURE_ABSORBANCE = "measure_absorbance"
