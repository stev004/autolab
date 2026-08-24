"""Execution context and typed device response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import CommandType
from autonomous_lab.domain.identifiers import require_utc
from autonomous_lab.domain.measurements import Measurement
from autonomous_lab.domain.quantities import QuantityValue

MaterialKind = Literal["reagent", "blank", "standard", "control", "sample", "other"]


class ResourceLedgerEntry(DomainModel):
    """Actual material present in a vessel at acquisition time."""

    vessel_id: str
    material_id: str
    material_kind: MaterialKind
    volume: QuantityValue
    properties: dict[str, QuantityValue] = Field(default_factory=dict)


class ResourceLedgerDelta(DomainModel):
    """Actual material-volume change caused by one successful command."""

    vessel_id: str
    material_id: str
    material_kind: MaterialKind
    volume_delta: QuantityValue
    properties: dict[str, QuantityValue] = Field(default_factory=dict)


class EnvironmentalExposure(DomainModel):
    """Actual environmental exposure applied to the listed vessels."""

    vessel_ids: tuple[str, ...]
    duration: QuantityValue
    temperature: QuantityValue


class AcquisitionRecord(DomainModel):
    """One expected acquisition, including missing instrument results."""

    acquisition_id: UUID
    experiment_id: UUID
    run_id: UUID
    step_id: str
    device_id: str
    vessel_id: str
    measurement_name: str
    status: Literal["ok", "missing", "invalid"]
    value: QuantityValue | None
    acquired_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("acquired_at")
    @classmethod
    def _utc_acquired_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _value_matches_status(self) -> AcquisitionRecord:
        if self.status == "ok" and self.value is None:
            raise ValueError("successful acquisition requires a value")
        if self.status == "missing" and self.value is not None:
            raise ValueError("missing acquisition cannot contain a value")
        return self


class ExecutionEvidence(DomainModel):
    """Final neutral evidence artifact produced by an execution backend."""

    schema_version: Literal["1"] = "1"
    evidence_id: UUID
    experiment_id: UUID
    run_id: UUID
    protocol_id: UUID
    fragment_ids: tuple[UUID, ...]
    resources: tuple[ResourceLedgerEntry, ...]
    exposures: tuple[EnvironmentalExposure, ...]
    acquisitions: tuple[AcquisitionRecord, ...]
    executed_parameters: dict[str, QuantityValue]
    costs: dict[str, QuantityValue]
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)


class ExecutionEvidenceFragment(DomainModel):
    """Device-owned evidence facts composed by the orchestrator after execution."""

    schema_version: Literal["1"] = "1"
    fragment_id: UUID
    experiment_id: UUID
    run_id: UUID
    source_step_id: str
    source_device_id: str
    resource_deltas: tuple[ResourceLedgerDelta, ...] = ()
    exposures: tuple[EnvironmentalExposure, ...] = ()
    acquisitions: tuple[AcquisitionRecord, ...] = ()
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def _utc_fragment_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)


class ExecutionContext(DomainModel):
    """Provenance supplied with every device dispatch.

    The idempotency key identifies one logical operation across retry attempts;
    ``attempt`` records the individual dispatch.  Physical adapters can use the
    key to reconcile a command after a transport failure.
    """

    experiment_id: UUID
    run_id: UUID
    step_id: str
    sequence: int = Field(ge=0)
    attempt: int = Field(ge=1)
    idempotency_key: str
    dispatched_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("dispatched_at")
    @classmethod
    def _utc_dispatched_at(cls, value: datetime) -> datetime:
        return require_utc(value)


class DeviceResponse(DomainModel):
    """Successful response from a device command."""

    status: Literal["succeeded"] = "succeeded"
    device_id: str
    command_type: CommandType
    completed_at: datetime
    measurements: tuple[Measurement, ...] = ()
    evidence_fragments: tuple[ExecutionEvidenceFragment, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("completed_at")
    @classmethod
    def _utc_completed_at(cls, value: datetime) -> datetime:
        return require_utc(value)
