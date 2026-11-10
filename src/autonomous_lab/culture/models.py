"""Wire contracts for long-running culture experiments (roadmap stage 6, contract v2).

A culture experiment is deliberately *not* an assay ``ExperimentProtocol``. An
assay is a short sequence of liquid-handling steps ending in one plate read; a
culture holds environmental conditions for hours and samples a time series while
it does. Forcing both into one step union would weaken validation for each, so
the culture path has its own protocol, plan and receipt while reusing AutoLab's
proposal, quantity, validation-issue and hashing types unchanged.

Contract v2: the environment is a set of *named controlled variables* (for example
``temperature``, ``light_intensity``, ``ph``) that the device declares in its
manifest, each with a unit, range and whether the device reports a measured value.
Adding a variable is configuration, not code. The device manifest is the source of
truth for what a device can do; the installation policy bounds what AutoLab allows.

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

#: Canonical units for the controlled variables AutoLab knows by name. Other names
#: are allowed if a device declares them; these just get a unit check up front.
KNOWN_VARIABLE_UNITS: dict[str, str] = {
    "temperature": TEMPERATURE_UNIT,
    "light_intensity": LIGHT_UNIT,
    "ph": "dimensionless",
}

#: Device consumption counters -> the scientific metric they produce and its unit.
RESOURCE_METRICS: dict[str, tuple[str, str]] = {
    "energy": ("energy_kwh", ENERGY_UNIT),
    "co2": ("co2_g", "gram"),
}


def interval_unit(unit: str) -> str:
    """The unit for *differences* of a quantity (offset temperatures need ``delta_``)."""

    return "delta_degree_Celsius" if unit == TEMPERATURE_UNIT else unit


class ControlledVariable(DomainModel):
    """One environmental variable a device can hold at a setpoint."""

    unit: str
    range: QuantityRange
    #: True if the device reports a measured value (so QC can check tracking).
    tracked: bool = False

    @model_validator(mode="after")
    def _range_in_unit(self) -> ControlledVariable:
        self.range.minimum.to(self.unit)
        self.range.maximum.to(self.unit)
        return self


class ProtectionLimit(DomainModel):
    """A device-enforced trip point. The device acts on it locally, without AutoLab."""

    minimum: QuantityValue | None = None
    maximum: QuantityValue | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ProtectionLimit:
        if self.minimum is None and self.maximum is None:
            raise ValueError("a protection limit needs a minimum or a maximum")
        return self


def to_timedelta(quantity: QuantityValue) -> timedelta:
    """Convert a time quantity to ``timedelta``; raises for non-time units."""

    return timedelta(seconds=float(quantity.to("second").value))


def seconds(quantity: QuantityValue) -> Decimal:
    return quantity.to("second").value


class CultureDesign(DomainModel):
    """Campaign-level culture design: what every experiment in a campaign shares.

    The optimiser varies only temperature setpoint and light. Duration, the
    sampling schedule and the replicate layout are fixed by the design so
    experiments stay comparable.

    Biological replicates: every reactor in ``reactor_ids`` receives the same
    conditions at the same time. Each replicate is quality-checked on its own;
    the experiment counts only if at least ``minimum_passing_replicates`` pass,
    and the optimiser learns from their mean.

    Schedule: either a regular ``sampling_interval`` (readings every interval and
    at the end) or explicit ``sample_offsets`` (for example denser early on).
    """

    schema_version: Literal["1"] = "1"
    reactor_ids: tuple[str, ...]
    #: Variables held constant (not optimised), e.g. {"ph": 7.5}. Every device
    #: variable must be either an optimised parameter or fixed here.
    fixed_setpoints: dict[str, QuantityValue] = Field(default_factory=dict)
    minimum_passing_replicates: int | None = Field(default=None, ge=1)
    duration: QuantityValue
    sampling_interval: QuantityValue | None = None
    sample_offsets: tuple[QuantityValue, ...] | None = None
    sample_tolerance: QuantityValue
    telemetry_interval: QuantityValue
    measurement_type: str = "optical_density"
    measurement_unit: str = "dimensionless"
    wavelength: QuantityValue = QuantityValue(value=Decimal(750), unit="nanometer")

    @model_validator(mode="after")
    def _coherent_design(self) -> CultureDesign:
        if not self.reactor_ids or any(not item.strip() for item in self.reactor_ids):
            raise ValueError("at least one non-blank reactor ID is required")
        if len(set(self.reactor_ids)) != len(self.reactor_ids):
            raise ValueError("each replicate needs its own reactor")
        if self.minimum_passing_replicates is not None and self.minimum_passing_replicates > len(
            self.reactor_ids
        ):
            raise ValueError("minimum_passing_replicates exceeds the number of reactors")
        duration = seconds(self.duration)
        tolerance = seconds(self.sample_tolerance)
        self.wavelength.to("nanometer")
        if duration <= 0 or seconds(self.telemetry_interval) <= 0:
            raise ValueError("duration and telemetry interval must be positive")
        if tolerance < 0:
            raise ValueError("sample tolerance cannot be negative")
        if (self.sampling_interval is None) == (self.sample_offsets is None):
            raise ValueError("give exactly one of sampling_interval or sample_offsets")
        offsets = self.offsets_seconds()
        if offsets[0] <= 0 or offsets[-1] > duration:
            raise ValueError("sample offsets must lie after the start and within the hold")
        if offsets != sorted(set(offsets)):
            raise ValueError("sample offsets must be strictly increasing")
        # Two windows must never overlap, or one reading could satisfy two entries.
        gaps = [
            later - earlier
            for earlier, later in zip((Decimal(0), *offsets[:-1]), offsets, strict=True)
        ]
        if 2 * tolerance >= min(gaps):
            raise ValueError("sample tolerance must be less than half the smallest sample gap")
        return self

    @property
    def required_passing_replicates(self) -> int:
        return self.minimum_passing_replicates or len(self.reactor_ids)

    def offsets_seconds(self) -> list[Decimal]:
        """Sample times after the hold starts, in seconds."""

        if self.sample_offsets is not None:
            return [seconds(item) for item in self.sample_offsets]
        assert self.sampling_interval is not None
        duration = seconds(self.duration)
        interval = seconds(self.sampling_interval)
        if interval <= 0:
            raise ValueError("sampling interval must be positive")
        offsets: list[Decimal] = []
        current = interval
        while current < duration:
            offsets.append(current)
            current += interval
        offsets.append(duration)
        return offsets


class HoldConditionsCommand(DomainModel):
    """Hold one reactor at a set of controlled-variable setpoints for a duration."""

    command_type: Literal["hold_conditions"] = "hold_conditions"
    reactor_id: str = Field(min_length=1)
    setpoints: dict[str, QuantityValue]
    duration: QuantityValue

    @model_validator(mode="after")
    def _units(self) -> HoldConditionsCommand:
        if not self.setpoints:
            raise ValueError("a hold needs at least one setpoint")
        for name, value in self.setpoints.items():
            if name in KNOWN_VARIABLE_UNITS:
                value.to(KNOWN_VARIABLE_UNITS[name])
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


class CultureReplicate(DomainModel):
    """One biological replicate: its own reactor, hold command and sampling plan."""

    replicate_index: int = Field(ge=0)
    hold: HoldConditionsCommand
    measurement_plan: TimeSeriesMeasurementPlan


class CultureProtocol(DomainModel):
    """Immutable, device-neutral culture recipe derived from one proposal."""

    schema_version: Literal["2"] = "2"
    protocol_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    source_proposal_id: UUID
    source_proposal_hash: str
    parameter_values: dict[str, QuantityValue]
    compiler_name: str
    compiler_version: str
    design_hash: str
    replicates: tuple[CultureReplicate, ...]
    minimum_passing_replicates: int = Field(ge=1)
    telemetry_interval: QuantityValue
    created_at: datetime = Field(default_factory=utc_now)
    protocol_hash: str | None = None

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _consistent_replicates(self) -> CultureProtocol:
        if not self.replicates:
            raise ValueError("a culture protocol needs at least one replicate")
        reactors = [item.hold.reactor_id for item in self.replicates]
        if len(set(reactors)) != len(reactors):
            raise ValueError("replicates must use distinct reactors")
        if [item.replicate_index for item in self.replicates] != list(range(len(reactors))):
            raise ValueError("replicate indices must be 0..n-1 in order")
        if self.minimum_passing_replicates > len(self.replicates):
            raise ValueError("minimum_passing_replicates exceeds the replicate count")
        return self

    @property
    def duration(self) -> QuantityValue:
        return self.replicates[0].hold.duration

    def compute_hash(self) -> str:
        """Hash everything except the circular hash field itself."""

        return canonical_sha256(self, exclude={"protocol_hash"})


class CulturePolicy(DomainModel):
    """Trusted installation policy. A campaign document cannot widen these limits."""

    schema_version: Literal["2"] = "2"
    policy_name: str
    policy_version: str
    #: The only variables AutoLab will set, and the range it allows for each.
    setpoint_limits: dict[str, QuantityRange]
    maximum_duration: QuantityValue
    minimum_sampling_interval: QuantityValue
    maximum_sample_points: int = Field(ge=1)
    maximum_replicates: int = Field(default=8, ge=1)
    #: Setpoints must stay at least this far inside each device protection limit.
    minimum_protection_margins: dict[str, QuantityValue] = Field(default_factory=dict)
    trusted_compilers: frozenset[str] = frozenset({"culture_hold_compiler@1.0.0"})

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)


class CultureControllerCapabilities(DomainModel):
    """Point-in-time manifest a setpoint-accepting controller publishes.

    Validation binds this snapshot's hash into the receipt. If the device reports
    different limits at dispatch time, the receipt no longer applies.
    """

    schema_version: Literal["2"] = "2"
    controller_id: str = Field(min_length=1)
    device_type: str
    adapter_name: str
    adapter_version: str
    firmware_version: str | None = None
    reactor_ids: frozenset[str]
    controlled_variables: dict[str, ControlledVariable]
    protection_limits: dict[str, ProtectionLimit] = Field(default_factory=dict)
    #: Cumulative consumption counters reported in telemetry, e.g. {"energy", "co2"}.
    resources: frozenset[str] = frozenset()
    maximum_hold_duration: QuantityValue
    is_simulated: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent(self) -> CultureControllerCapabilities:
        if not self.reactor_ids:
            raise ValueError("controller must expose at least one reactor")
        if not self.controlled_variables:
            raise ValueError("controller must declare at least one controlled variable")
        unknown = set(self.protection_limits) - set(self.controlled_variables)
        if unknown:
            raise ValueError(f"protection limits for undeclared variables: {sorted(unknown)}")
        for name, limit in self.protection_limits.items():
            unit = self.controlled_variables[name].unit
            for bound in (limit.minimum, limit.maximum):
                if bound is not None:
                    bound.to(unit)
        return self

    @property
    def tracked_variables(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, v in self.controlled_variables.items() if v.tracked))

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


def _default_warmups() -> dict[str, QuantityValue]:
    return {
        "temperature": QuantityValue(value=Decimal(3), unit="hour"),
        "ph": QuantityValue(value=Decimal(3), unit="hour"),
    }


def _default_tolerances() -> dict[str, Decimal]:
    return {"temperature": Decimal(1), "ph": Decimal("0.2")}


class CultureQCConfig(DomainModel):
    """Run-integrity thresholds. These are illustrative defaults, not validated limits.

    Tracking: for every variable the device reports a measured value for, QC
    scores samples after that variable's warm-up (``tracking_warmups``, default
    ``default_warmup``) against ``tracking_tolerances`` (mean absolute deviation
    from the setpoint, in the variable's interval unit). A tracked variable with no
    tolerance configured fails QC rather than passing unchecked.

    Contamination handling uses three signals, none of which is proof on its own:
    device or sensor fault codes (``contamination_fault_codes``), an OD drop between
    consecutive readings larger than ``maximum_od_drop`` (lysis, grazing or probe
    fouling), and an apparent specific growth rate above a plausibility ceiling
    (often a faster-growing contaminant or a fouled probe). Set a limit to ``None``
    to disable that check.
    """

    default_warmup: QuantityValue = QuantityValue(value=Decimal(3), unit="hour")
    tracking_warmups: dict[str, QuantityValue] = Field(default_factory=_default_warmups)
    tracking_tolerances: dict[str, FiniteDecimal] = Field(default_factory=_default_tolerances)
    minimum_telemetry_coverage: FiniteDecimal = Field(default=Decimal("0.9"), gt=0, le=1)
    maximum_missing_reading_fraction: FiniteDecimal = Field(default=Decimal("0.1"), ge=0, le=1)
    require_final_reading: bool = True
    contamination_fault_codes: frozenset[str] = frozenset({"contamination_suspected"})
    maximum_od_drop: FiniteDecimal | None = Field(default=Decimal("0.05"), ge=0)
    maximum_specific_growth_rate_per_hour: FiniteDecimal | None = Field(
        default=Decimal("0.5"), gt=0
    )

    def warmup_for(self, variable: str) -> QuantityValue:
        return self.tracking_warmups.get(variable, self.default_warmup)
