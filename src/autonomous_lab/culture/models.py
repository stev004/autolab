"""Wire contracts for long-running culture experiments (roadmap stage 6, slice A).

A culture experiment is deliberately *not* an assay ``ExperimentProtocol``. An
assay is a short sequence of liquid-handling steps ending in one plate read; a
culture holds environmental conditions for hours and samples a time series while
it does. Forcing both into one step union would weaken validation for each, so
the culture path has its own protocol, plan and receipt while reusing AutoLab's
proposal, quantity, validation-issue and hashing types unchanged.

Every model is frozen and hashable so the evidence chain can bind exact values.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.protocols import QuantityRange
from autonomous_lab.domain.quantities import FiniteDecimal, QuantityValue
from autonomous_lab.provenance.hashing import canonical_sha256

TEMPERATURE_UNIT = "degree_Celsius"
LIGHT_UNIT = "micromole / meter ** 2 / second"
ENERGY_UNIT = "kilowatt_hour"

#: Proposal parameter names a culture protocol consumes. Optimisers choose these
#: scientific conditions; they never choose device commands.
TEMPERATURE_PARAMETER = "temperature_setpoint"
LIGHT_PARAMETER = "light_intensity"
CULTURE_PARAMETERS = frozenset({TEMPERATURE_PARAMETER, LIGHT_PARAMETER})


def to_timedelta(quantity: QuantityValue) -> timedelta:
    """Convert a time quantity to ``timedelta``; raises for non-time units."""

    return timedelta(seconds=float(quantity.to("second").value))


def seconds(quantity: QuantityValue) -> Decimal:
    return quantity.to("second").value


class CultureDesign(DomainModel):
    """Campaign-level culture design: what every experiment in a campaign shares.

    The optimiser varies only temperature setpoint and light. Duration and the
    sampling schedule are fixed by the design so experiments stay comparable.
    """

    schema_version: Literal["1"] = "1"
    reactor_id: str = Field(min_length=1)
    duration: QuantityValue
    sampling_interval: QuantityValue
    sample_tolerance: QuantityValue
    telemetry_interval: QuantityValue
    measurement_type: str = "optical_density"
    measurement_unit: str = "dimensionless"
    wavelength: QuantityValue = QuantityValue(value=Decimal(750), unit="nanometer")

    @model_validator(mode="after")
    def _coherent_schedule(self) -> CultureDesign:
        duration = seconds(self.duration)
        interval = seconds(self.sampling_interval)
        tolerance = seconds(self.sample_tolerance)
        telemetry = seconds(self.telemetry_interval)
        self.wavelength.to("nanometer")
        if duration <= 0 or interval <= 0 or telemetry <= 0:
            raise ValueError("duration, sampling and telemetry intervals must be positive")
        if tolerance < 0:
            raise ValueError("sample tolerance cannot be negative")
        if interval > duration:
            raise ValueError("sampling interval cannot exceed the culture duration")
        # Two windows must never overlap, or one reading could satisfy two entries.
        if 2 * tolerance >= interval:
            raise ValueError("sample tolerance must be less than half the sampling interval")
        return self


class HoldConditionsCommand(DomainModel):
    """Hold one reactor at a temperature setpoint and light level for a duration."""

    command_type: Literal["hold_conditions"] = "hold_conditions"
    reactor_id: str = Field(min_length=1)
    temperature_setpoint: QuantityValue
    light_intensity: QuantityValue
    duration: QuantityValue

    @model_validator(mode="after")
    def _units(self) -> HoldConditionsCommand:
        self.temperature_setpoint.to(TEMPERATURE_UNIT)
        self.light_intensity.to(LIGHT_UNIT)
        if seconds(self.duration) <= 0:
            raise ValueError("hold duration must be positive")
        return self


class SamplePoint(DomainModel):
    """One expected reading, due at ``offset`` after the hold starts (± tolerance)."""

    expected_measurement_id: str = Field(min_length=1)
    offset: QuantityValue
    tolerance: QuantityValue
    measurement_type: str
    unit: str
    wavelength: QuantityValue | None = None
    required: bool = True

    def due_at(self, start: datetime) -> datetime:
        return start + to_timedelta(self.offset)

    def window(self, start: datetime) -> tuple[datetime, datetime]:
        due = self.due_at(start)
        tolerance = to_timedelta(self.tolerance)
        return due - tolerance, due + tolerance


class TimeSeriesMeasurementPlan(DomainModel):
    """Expected readings at intervals, not only an end point."""

    schema_version: Literal["1"] = "1"
    plan_id: UUID = Field(default_factory=new_uuid)
    sample_id: str = Field(min_length=1)
    points: tuple[SamplePoint, ...]

    @model_validator(mode="after")
    def _ordered_unique_points(self) -> TimeSeriesMeasurementPlan:
        if not self.points:
            raise ValueError("a time-series plan needs at least one sample point")
        ids = [point.expected_measurement_id for point in self.points]
        if len(ids) != len(set(ids)):
            raise ValueError("sample point IDs must be unique")
        offsets = [seconds(point.offset) for point in self.points]
        if offsets != sorted(offsets) or len(set(offsets)) != len(offsets):
            raise ValueError("sample points must be strictly increasing in time")
        return self


class CultureProtocol(DomainModel):
    """Immutable, device-neutral culture recipe derived from one proposal."""

    schema_version: Literal["1"] = "1"
    protocol_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    source_proposal_id: UUID
    source_proposal_hash: str
    parameter_values: dict[str, QuantityValue]
    compiler_name: str
    compiler_version: str
    design_hash: str
    hold: HoldConditionsCommand
    measurement_plan: TimeSeriesMeasurementPlan
    created_at: datetime = Field(default_factory=utc_now)
    protocol_hash: str | None = None

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    def compute_hash(self) -> str:
        """Hash everything except the circular hash field itself."""

        return canonical_sha256(self, exclude={"protocol_hash"})


class CulturePolicy(DomainModel):
    """Trusted installation policy. A campaign document cannot widen these limits."""

    schema_version: Literal["1"] = "1"
    policy_name: str
    policy_version: str
    temperature_setpoint_range: QuantityRange
    light_intensity_range: QuantityRange
    maximum_duration: QuantityValue
    minimum_sampling_interval: QuantityValue
    maximum_sample_points: int = Field(ge=1)
    #: Setpoints must stay this far below the device's own over-temperature trip.
    minimum_trip_margin: QuantityValue = QuantityValue(
        value=Decimal(2), unit="delta_degree_Celsius"
    )
    trusted_compilers: frozenset[str] = frozenset({"culture_hold_compiler@1.0.0"})

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


class CultureControllerCapabilities(DomainModel):
    """Point-in-time manifest a setpoint-accepting controller publishes.

    Validation binds this snapshot's hash into the receipt. If the device reports
    different limits at dispatch time, the receipt no longer applies.
    """

    schema_version: Literal["1"] = "1"
    controller_id: str = Field(min_length=1)
    device_type: str
    adapter_name: str
    adapter_version: str
    firmware_version: str | None = None
    reactor_ids: frozenset[str]
    temperature_setpoint_range: QuantityRange
    light_intensity_range: QuantityRange
    maximum_hold_duration: QuantityValue
    over_temperature_trip: QuantityValue
    is_simulated: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_reactors(self) -> CultureControllerCapabilities:
        if not self.reactor_ids:
            raise ValueError("controller must expose at least one reactor")
        self.over_temperature_trip.to(TEMPERATURE_UNIT)
        return self

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


class ExecutableCultureProtocol(DomainModel):
    """Receipt binding one exact culture protocol to the policy and device it passed."""

    schema_version: Literal["1"] = "1"
    protocol: CultureProtocol
    validation_report_id: UUID
    protocol_hash: str
    capability_snapshot_hash: str
    validation_policy_hash: str
    issued_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _hash_matches_protocol(self) -> ExecutableCultureProtocol:
        if self.protocol.protocol_hash != self.protocol_hash:
            raise ValueError("receipt protocol hash does not match the protocol artifact")
        return self


class CultureQCConfig(DomainModel):
    """Run-integrity thresholds. These are illustrative defaults, not validated limits."""

    warmup: QuantityValue = QuantityValue(value=Decimal(3), unit="hour")
    maximum_mean_temperature_deviation: FiniteDecimal = Field(default=Decimal(1), gt=0)
    minimum_telemetry_coverage: FiniteDecimal = Field(default=Decimal("0.9"), gt=0, le=1)
    maximum_missing_reading_fraction: FiniteDecimal = Field(default=Decimal("0.1"), ge=0, le=1)
    require_final_reading: bool = True
