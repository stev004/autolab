"""The commandable-device seam: a controller that accepts bounded setpoints.

Until now AutoLab's only physical plug-in was a read-only sensor. A culture needs
a device that *changes the world* (heater, lights), so this contract is stricter,
following ``docs/hardware-interface.md``:

- commands are validated upstream **and** re-checked by the adapter itself;
- every command is acknowledged with the values the device actually applied;
- the device reports telemetry it can attest to (measured temperature, protection
  trips, energy) and nothing it cannot;
- ``enter_safe_state`` is always available and is called on every exit path.

Local protection (for example an over-temperature trip) belongs to the device and
keeps working with AutoLab disconnected. AutoLab only reads whether it fired.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from importlib.metadata import entry_points
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from autonomous_lab.culture.models import CultureControllerCapabilities
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.domain.identifiers import new_uuid, require_utc, utc_now
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.sensors.contracts import ReadOnlySensor

CULTURE_CONTROLLER_ENTRY_POINT_GROUP = "autonomous_lab.culture_controllers"


class SetpointCommand(DomainModel):
    """One bounded request to change a reactor's environment."""

    schema_version: Literal["1"] = "1"
    command_id: UUID = Field(default_factory=new_uuid)
    experiment_id: UUID
    run_id: UUID
    reactor_id: str = Field(min_length=1)
    temperature_setpoint: QuantityValue
    light_intensity: QuantityValue
    protocol_hash: str
    issued_at: datetime = Field(default_factory=utc_now)

    @field_validator("issued_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)


class SetpointAcknowledgement(DomainModel):
    """What the device says it did. ``applied_*`` may differ from what was asked."""

    schema_version: Literal["1"] = "1"
    command_id: UUID
    controller_id: str
    reactor_id: str
    accepted: bool
    applied_temperature_setpoint: QuantityValue | None = None
    applied_light_intensity: QuantityValue | None = None
    rejection_code: str | None = None
    rejection_message: str | None = None
    acknowledged_at: datetime

    @field_validator("acknowledged_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _consistent(self) -> SetpointAcknowledgement:
        applied = (self.applied_temperature_setpoint, self.applied_light_intensity)
        if self.accepted and None in applied:
            raise ValueError("an accepted setpoint must report the applied values")
        if not self.accepted and self.rejection_code is None:
            raise ValueError("a rejected setpoint must give a rejection code")
        return self


class EnvironmentTelemetry(DomainModel):
    """One device-attested telemetry sample. ``None`` means the probe gave no value."""

    schema_version: Literal["1"] = "1"
    controller_id: str
    reactor_id: str
    sampled_at: datetime
    measured_temperature: QuantityValue | None
    applied_temperature_setpoint: QuantityValue
    applied_light_intensity: QuantityValue
    protection_tripped: bool = False
    trip_count: int = Field(default=0, ge=0)
    cumulative_energy: QuantityValue
    faults: tuple[str, ...] = ()

    @field_validator("sampled_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)


class SafeStateAcknowledgement(DomainModel):
    controller_id: str
    reactor_id: str
    reason: str
    reached: bool
    acknowledged_at: datetime
    detail: str | None = None

    @field_validator("acknowledged_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return require_utc(value)


class ControllerError(RuntimeError):
    """Controller failure with explicit recovery metadata.

    ``ambiguous`` means the device may or may not have acted (for example a
    timeout after sending a setpoint). The runner never retries an ambiguous
    command; it requests safe state and fails the run.
    """

    def __init__(
        self, code: str, message: str, *, retry_safe: bool = False, ambiguous: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_safe = retry_safe
        self.ambiguous = ambiguous


@runtime_checkable
class CultureEnvironmentController(Protocol):
    """Contract a setpoint-accepting reactor adapter implements.

    Adapters must reject commands outside their own manifest limits even when
    upstream validation passed, must not choose experiments, and must not read
    campaign objectives or optimiser state.
    """

    @property
    def controller_id(self) -> str: ...

    async def capability_manifest(self) -> CultureControllerCapabilities: ...

    async def apply_setpoints(self, command: SetpointCommand) -> SetpointAcknowledgement: ...

    async def read_telemetry(self, reactor_id: str) -> EnvironmentTelemetry: ...

    async def enter_safe_state(self, reactor_id: str, reason: str) -> SafeStateAcknowledgement: ...


# Factories are third-party code, so their return value is checked at runtime.
ControllerFactory = Callable[..., object]


@runtime_checkable
class IntegratedCultureSensor(Protocol):
    """A controller with a built-in OD probe exposes it as a read-only sensor.

    Many bioreactors integrate the probe, so a campaign may omit a separate
    sensor adapter. The probe still goes through the read-only sensor contract.
    """

    def culture_sensor(self) -> ReadOnlySensor: ...


class CultureControllerRegistry:
    """Resolve installed controller adapters through the entry-point group.

    Mirrors ``SensorAdapterRegistry``: a third-party package exports a factory
    that accepts ``controller_id``, adapter-owned ``settings`` and ``clock``.
    """

    def __init__(self) -> None:
        self._factories: dict[str, ControllerFactory] = {}

    def register(self, name: str, factory: ControllerFactory) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("controller adapter name cannot be blank")
        if normalized in self._factories:
            raise ValueError(f"controller adapter {normalized!r} is already registered")
        self._factories[normalized] = factory

    def discover(self) -> None:
        for point in entry_points(group=CULTURE_CONTROLLER_ENTRY_POINT_GROUP):
            if point.name not in self._factories:
                loaded = point.load()
                if not callable(loaded):
                    raise ControllerError(
                        "controller_adapter_invalid", f"entry point {point.name!r} is not callable"
                    )
                self._factories[point.name] = loaded

    def create(
        self,
        name: str,
        *,
        controller_id: str,
        settings: Mapping[str, object] | None = None,
        clock: object | None = None,
    ) -> CultureEnvironmentController:
        """Build an adapter. ``clock`` is the run's ``CultureClock``.

        Simulated adapters advance their world from it; hardware adapters ignore it.
        """

        self.discover()
        try:
            factory = self._factories[name]
        except KeyError as exc:
            raise ControllerError(
                "controller_adapter_unavailable", f"controller adapter {name!r} is not installed"
            ) from exc
        controller: object = factory(
            controller_id=controller_id, settings=dict(settings or {}), clock=clock
        )
        if not isinstance(controller, CultureEnvironmentController):
            raise ControllerError(
                "controller_adapter_invalid",
                f"adapter {name!r} did not return a CultureEnvironmentController",
            )
        if controller.controller_id != controller_id:
            raise ControllerError(
                "controller_identity_mismatch",
                f"adapter {name!r} returned controller {controller.controller_id!r}",
            )
        return controller
