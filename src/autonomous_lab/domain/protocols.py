"""Compiled protocol, validation receipt, and capability contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.enums import (
    CommandType,
    ControlType,
    IssueSeverity,
    ValidationStatus,
)
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue


class MaterialReference(DomainModel):
    material_id: str
    name: str
    lot_id: str | None = None
    properties: dict[str, QuantityValue] = Field(default_factory=dict)


class VesselDefinition(DomainModel):
    vessel_id: str
    vessel_type: str
    maximum_volume: QuantityValue
    location: str | None = None


class ControlDefinition(DomainModel):
    control_id: str
    control_type: ControlType
    vessel_ids: tuple[str, ...]
    nominal_level: FiniteDecimal | None = None


class MeasurementPlan(DomainModel):
    wavelength: QuantityValue
    vessel_ids: tuple[str, ...]
    measurement_name: str = "absorbance"


class DispenseCommand(DomainModel):
    command_type: Literal[CommandType.DISPENSE] = CommandType.DISPENSE
    material_id: str
    destination_vessel_id: str
    volume: QuantityValue


class AspirateCommand(DomainModel):
    command_type: Literal[CommandType.ASPIRATE] = CommandType.ASPIRATE
    source_vessel_id: str
    volume: QuantityValue


class TransferCommand(DomainModel):
    command_type: Literal[CommandType.TRANSFER] = CommandType.TRANSFER
    source_vessel_id: str
    destination_vessel_id: str
    volume: QuantityValue


class MixCommand(DomainModel):
    command_type: Literal[CommandType.MIX] = CommandType.MIX
    vessel_id: str
    cycles: int = Field(ge=1)
    volume: QuantityValue


class IncubateCommand(DomainModel):
    command_type: Literal[CommandType.INCUBATE] = CommandType.INCUBATE
    vessel_ids: tuple[str, ...]
    duration: QuantityValue
    temperature: QuantityValue


class MeasureAbsorbanceCommand(DomainModel):
    command_type: Literal[CommandType.MEASURE_ABSORBANCE] = CommandType.MEASURE_ABSORBANCE
    vessel_ids: tuple[str, ...]
    wavelength: QuantityValue


ProtocolCommand = Annotated[
    DispenseCommand
    | AspirateCommand
    | TransferCommand
    | MixCommand
    | IncubateCommand
    | MeasureAbsorbanceCommand,
    Field(discriminator="command_type"),
]


class ProtocolStep(DomainModel):
    step_id: str
    command: ProtocolCommand
    required_capability: str
    timeout_seconds: FiniteDecimal
    retry_safe: bool
    preconditions: tuple[str, ...] = ()
    expected_postconditions: tuple[str, ...] = ()

    @field_validator("timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: FiniteDecimal) -> FiniteDecimal:
        if value <= 0:
            raise ValueError("timeout_seconds must be positive")
        return value


class ExperimentProtocol(DomainModel):
    schema_version: Literal["1"] = "1"
    protocol_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    source_proposal_id: UUID
    source_proposal_hash: str
    parameter_values: dict[str, QuantityValue]
    compiler_name: str
    compiler_version: str
    materials: tuple[MaterialReference, ...]
    vessels: tuple[VesselDefinition, ...]
    controls: tuple[ControlDefinition, ...]
    steps: tuple[ProtocolStep, ...]
    measurement_plan: MeasurementPlan
    created_at: datetime = Field(default_factory=utc_now)
    protocol_hash: str | None = None

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _unique_ids(self) -> ExperimentProtocol:
        for label, ids in (
            ("vessel", [vessel.vessel_id for vessel in self.vessels]),
            ("control", [control.control_id for control in self.controls]),
            ("step", [step.step_id for step in self.steps]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} IDs must be unique")
        if not self.steps:
            raise ValueError("protocol must contain at least one step")
        return self


class QuantityRange(DomainModel):
    minimum: QuantityValue
    maximum: QuantityValue
    resolution: QuantityValue | None = None

    @model_validator(mode="after")
    def _compatible_ordered_range(self) -> QuantityRange:
        maximum = self.maximum.to(self.minimum.unit)
        if self.minimum.value >= maximum.value:
            raise ValueError("capability range minimum must be less than maximum")
        if self.resolution is not None:
            # Resolution is an interval. Offset quantities such as degC have a
            # distinct delta unit, so dimensional equality is the correct check;
            # converting delta_degree_Celsius to absolute degree_Celsius is not.
            if self.resolution.dimensionality != self.minimum.dimensionality:
                raise ValueError("capability resolution must match the range dimensionality")
            if self.resolution.value <= 0:
                raise ValueError("capability resolution must be positive")
        return self


class CapabilityManifest(DomainModel):
    schema_version: Literal["1"] = "1"
    device_id: str
    device_type: str
    adapter_name: str
    adapter_version: str
    supported_commands: frozenset[CommandType]
    ranges: dict[str, QuantityRange] = Field(default_factory=dict)
    supported_vessels: frozenset[str] = frozenset()
    channel_count: int = Field(default=1, ge=1)
    calibration_id: str | None = None
    calibration_expires_at: datetime | None = None
    safety_limits: dict[str, QuantityValue] = Field(default_factory=dict)

    @field_validator("calibration_expires_at")
    @classmethod
    def _utc_expiry(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_utc(value)


class ValidationIssue(DomainModel):
    code: str
    severity: IssueSeverity
    path: str
    message: str
    rule_name: str
    rule_version: str
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(DomainModel):
    schema_version: Literal["1"] = "1"
    report_id: UUID = Field(default_factory=new_uuid)
    protocol_id: UUID
    protocol_hash: str
    capability_snapshot_hash: str
    validation_policy_hash: str
    status: ValidationStatus
    issues: tuple[ValidationIssue, ...] = ()
    validated_at: datetime = Field(default_factory=utc_now)

    @field_validator("validated_at")
    @classmethod
    def _utc_validated_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _consistent_status(self) -> ValidationReport:
        has_errors = any(issue.severity is IssueSeverity.ERROR for issue in self.issues)
        if self.status is ValidationStatus.PASSED and has_errors:
            raise ValueError("a passing validation report cannot contain errors")
        if self.status is ValidationStatus.REJECTED and not has_errors:
            raise ValueError("a rejected validation report must contain at least one error")
        return self


class ExecutableProtocol(DomainModel):
    """Receipt binding one exact protocol to validation inputs.

    Application code should obtain this object from the validation service. The
    model still validates its internal receipt consistency when deserialized.
    """

    schema_version: Literal["1"] = "1"
    protocol: ExperimentProtocol
    validation_report_id: UUID
    protocol_hash: str
    capability_snapshot_hash: str
    validation_policy_hash: str
    issued_at: datetime = Field(default_factory=utc_now)

    @field_validator("issued_at")
    @classmethod
    def _utc_issued_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _hash_matches_protocol_artifact(self) -> ExecutableProtocol:
        if (
            self.protocol.protocol_hash is not None
            and self.protocol.protocol_hash != self.protocol_hash
        ):
            raise ValueError("executable receipt protocol hash does not match protocol artifact")
        return self
