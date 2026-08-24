"""SQLAlchemy persistence model for the scientific record.

The ORM intentionally stores versioned JSON snapshots at subsystem boundaries.
Domain models can evolve independently while the database retains the exact
object that was proposed, validated, executed, or analysed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


def uuid_string() -> str:
    return str(uuid4())


class ImmutableRecordError(RuntimeError):
    """Raised when code attempts to rewrite append-only scientific evidence."""


class Base(DeclarativeBase):
    pass


class CampaignRecord(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_source: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    config_source_sha256: Mapped[str | None] = mapped_column(String(64))
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    application_version: Mapped[str | None] = mapped_column(String(128))
    git_commit: Mapped[str | None] = mapped_column(String(64))
    git_dirty: Mapped[bool | None] = mapped_column(Boolean)
    source_tree_diff_sha256: Mapped[str | None] = mapped_column(String(64))
    python_version: Mapped[str | None] = mapped_column(String(64))
    platform: Mapped[str | None] = mapped_column(String(255))
    uv_lock_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObjectiveSnapshotRecord(Base):
    __tablename__ = "objective_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ParameterSpaceSnapshotRecord(Base):
    __tablename__ = "parameter_space_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExperimentRecord(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("campaign_id", "ordinal", name="uq_experiment_campaign_ordinal"),
        Index("ix_experiments_campaign_state", "campaign_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    optimizer_trial_id: Mapped[str | None] = mapped_column(String(255))
    failure_category: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExperimentLineageRecord(Base):
    __tablename__ = "experiment_lineage"

    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), primary_key=True
    )
    parent_experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), primary_key=True
    )
    relationship: Mapped[str] = mapped_column(String(64), default="derived_from")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProposalRecord(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_version: Mapped[str | None] = mapped_column(String(128))
    strategy_trial_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProtocolRecord(Base):
    __tablename__ = "protocols"
    __table_args__ = (
        UniqueConstraint("experiment_id", "version", name="uq_protocol_experiment_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    protocol_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    compiler_name: Mapped[str] = mapped_column(String(128), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ValidationReportRecord(Base):
    __tablename__ = "validation_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    protocol_id: Mapped[str] = mapped_column(
        ForeignKey("protocols.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    protocol_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    capability_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    validation_policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(128), nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ValidationIssueRecord(Base):
    __tablename__ = "validation_issues"
    __table_args__ = (
        UniqueConstraint("validation_report_id", "ordinal", name="uq_validation_issue_ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    validation_report_id: Mapped[str] = mapped_column(
        ForeignKey("validation_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ExecutionRunRecord(Base):
    __tablename__ = "execution_runs"
    __table_args__ = (
        UniqueConstraint("experiment_id", "attempt", name="uq_execution_experiment_attempt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    protocol_id: Mapped[str] = mapped_column(
        ForeignKey("protocols.id", ondelete="RESTRICT"), nullable=False
    )
    validation_report_id: Mapped[str] = mapped_column(
        ForeignKey("validation_reports.id", ondelete="RESTRICT"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    backend_name: Mapped[str] = mapped_column(String(128), nullable=False)
    backend_version: Mapped[str | None] = mapped_column(String(128))
    device_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_category: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    safe_state_reached: Mapped[bool | None] = mapped_column(Boolean)


class ActionEventRecord(Base):
    __tablename__ = "action_events"
    __table_args__ = (
        UniqueConstraint("execution_run_id", "sequence", name="uq_action_run_sequence"),
        Index("ix_action_run_idempotency", "execution_run_id", "idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    execution_run_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    retry_safe: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    device_id: Mapped[str | None] = mapped_column(String(255))
    command_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MeasurementRecord(Base):
    __tablename__ = "measurements"
    __table_args__ = (Index("ix_measurements_experiment_role", "experiment_id", "sample_role"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    execution_run_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    action_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("action_events.id", ondelete="SET NULL")
    )
    measurement_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sample_role: Mapped[str] = mapped_column(String(64), nullable=False)
    well_id: Mapped[str | None] = mapped_column(String(128))
    analyte_level: Mapped[float | None] = mapped_column(Float)
    replicate_index: Mapped[int | None] = mapped_column(Integer)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(128), nullable=False)
    quality_flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExpectedMeasurementPlanRecord(Base):
    """Immutable expected-measurement contract bound to one protocol."""

    __tablename__ = "expected_measurement_plans"
    __table_args__ = (
        UniqueConstraint("experiment_id", "version", name="uq_measurement_plan_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    protocol_id: Mapped[str] = mapped_column(
        ForeignKey("protocols.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    plan_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MeasurementIngestionBatchRecord(Base):
    """One immutable manual/file/device submission envelope."""

    __tablename__ = "measurement_ingestion_batches"
    __table_args__ = (
        UniqueConstraint("experiment_id", "source_file_sha256", name="uq_measurement_source_file"),
        Index("ix_measurement_batches_experiment_source", "experiment_id", "source_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("expected_measurement_plans.id", ondelete="RESTRICT"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    batch_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_file_sha256: Mapped[str | None] = mapped_column(String(64))
    source_filename: Mapped[str | None] = mapped_column(String(512))
    batch_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    batch_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExternalMeasurementRevisionRecord(Base):
    """Append-only revision of one stable expected-measurement identity."""

    __tablename__ = "external_measurement_revisions"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "expected_measurement_id",
            "revision_number",
            name="uq_external_measurement_revision",
        ),
        Index(
            "ix_external_revision_experiment_expected",
            "experiment_id",
            "expected_measurement_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("expected_measurement_plans.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("measurement_ingestion_batches.id", ondelete="RESTRICT"), nullable=False
    )
    expected_measurement_id: Mapped[str] = mapped_column(String(255), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("external_measurement_revisions.id", ondelete="RESTRICT"), unique=True
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    availability: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    value_decimal: Mapped[str | None] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(128), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correction_reason: Mapped[str | None] = mapped_column(Text)
    revision_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    raw_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QCRunRecord(Base):
    __tablename__ = "qc_runs"
    __table_args__ = (
        UniqueConstraint("experiment_id", "version", name="uq_qc_experiment_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    execution_run_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    eligible_for_optimization: Mapped[bool] = mapped_column(Boolean, nullable=False)
    algorithm_name: Mapped[str] = mapped_column(String(128), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QCCheckRecord(Base):
    __tablename__ = "qc_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    qc_run_id: Mapped[str] = mapped_column(
        ForeignKey("qc_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(128), nullable=False)
    threshold: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    observed: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    flagged_measurement_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    recomputed_with_exclusions: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class DerivedMetricRecord(Base):
    __tablename__ = "derived_metrics"
    __table_args__ = (
        UniqueConstraint("experiment_id", "name", "algorithm_version", name="uq_metric_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qc_run_id: Mapped[str | None] = mapped_column(ForeignKey("qc_runs.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(128), nullable=False)
    uncertainty: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source_measurement_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    algorithm_name: Mapped[str] = mapped_column(String(128), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PerformanceConstraintRecord(Base):
    __tablename__ = "performance_constraints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    operator: Mapped[str] = mapped_column(String(16), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    actual: Mapped[float] = mapped_column(Float, nullable=False)
    residual: Mapped[float] = mapped_column(Float, nullable=False)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ExperimentResultRecord(Base):
    __tablename__ = "experiment_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    execution_run_id: Mapped[str] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="CASCADE"), nullable=False
    )
    qc_run_id: Mapped[str | None] = mapped_column(ForeignKey("qc_runs.id", ondelete="SET NULL"))
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    result_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    optimizer_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OptimizerRunRecord(Base):
    __tablename__ = "optimizer_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    external_storage_uri: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OptimizerObservationRecord(Base):
    __tablename__ = "optimizer_observations"
    __table_args__ = (
        UniqueConstraint("optimizer_run_id", "experiment_id", name="uq_observation_experiment"),
        UniqueConstraint("optimizer_run_id", "trial_id", name="uq_observation_trial"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    optimizer_run_id: Mapped[str] = mapped_column(
        ForeignKey("optimizer_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trial_id: Mapped[str] = mapped_column(String(255), nullable=False)
    objective_values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    constraint_residuals: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    qc_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    observation_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OptimizerCheckpointRecord(Base):
    __tablename__ = "optimizer_checkpoints"
    __table_args__ = (
        UniqueConstraint("optimizer_run_id", "ordinal", name="uq_checkpoint_ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    optimizer_run_id: Mapped[str] = mapped_column(
        ForeignKey("optimizer_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_string)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_id: Mapped[str | None] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_campaign_occurred", "campaign_id", "occurred_at"),)

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), default=uuid_string, unique=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    experiment_id: Mapped[str | None] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE")
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    previous_state: Mapped[str | None] = mapped_column(String(32))
    new_state: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


IMMUTABLE_MODELS = (
    ObjectiveSnapshotRecord,
    ParameterSpaceSnapshotRecord,
    ExperimentLineageRecord,
    ProposalRecord,
    ProtocolRecord,
    ValidationReportRecord,
    ValidationIssueRecord,
    ActionEventRecord,
    MeasurementRecord,
    ExpectedMeasurementPlanRecord,
    MeasurementIngestionBatchRecord,
    ExternalMeasurementRevisionRecord,
    QCRunRecord,
    QCCheckRecord,
    DerivedMetricRecord,
    PerformanceConstraintRecord,
    ExperimentResultRecord,
    OptimizerObservationRecord,
    OptimizerCheckpointRecord,
    ArtifactRecord,
    AuditEventRecord,
)


def _protect_immutable_records(
    session: Session, _flush_context: object, _instances: object
) -> None:
    for instance in session.dirty:
        if isinstance(instance, IMMUTABLE_MODELS) and session.is_modified(
            instance, include_collections=False
        ):
            raise ImmutableRecordError(
                f"{type(instance).__name__} is append-only and cannot be updated"
            )
    for instance in session.deleted:
        if isinstance(instance, IMMUTABLE_MODELS):
            raise ImmutableRecordError(
                f"{type(instance).__name__} is append-only and cannot be deleted"
            )


event.listen(Session, "before_flush", _protect_immutable_records)
