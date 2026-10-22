"""Runtime discovery for independently packaged read-only sensor adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib.metadata import entry_points
from typing import cast

from autonomous_lab.hardware.sensors.contracts import ReadOnlySensor
from autonomous_lab.hardware.sensors.errors import SensorAdapterError

SensorAdapterFactory = Callable[..., ReadOnlySensor]
SENSOR_ADAPTER_ENTRY_POINT_GROUP = "autonomous_lab.sensor_adapters"


class SensorAdapterRegistry:
    """Resolve adapters without coupling campaigns, QC, or analysis to vendors.

    Third-party packages expose one entry point in
    ``autonomous_lab.sensor_adapters``. The loaded callable receives only a
    sensor identity and adapter-owned settings and returns ``ReadOnlySensor``.
    """

    def __init__(self) -> None:
        self._factories: dict[str, SensorAdapterFactory] = {}

    def register(self, name: str, factory: SensorAdapterFactory) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("sensor adapter name cannot be blank")
        if normalized in self._factories:
            raise ValueError(f"sensor adapter {normalized!r} is already registered")
        self._factories[normalized] = factory

    def discover(self) -> None:
        for point in entry_points(group=SENSOR_ADAPTER_ENTRY_POINT_GROUP):
            if point.name not in self._factories:
                loaded = point.load()
                if not callable(loaded):
                    raise SensorAdapterError(
                        "sensor_adapter_invalid",
                        f"entry point {point.name!r} is not callable",
                    )
                self._factories[point.name] = cast(SensorAdapterFactory, loaded)

    def create(
        self,
        name: str,
        *,
        sensor_id: str,
        settings: Mapping[str, object] | None = None,
    ) -> ReadOnlySensor:
        self.discover()
        try:
            factory = self._factories[name]
        except KeyError as exc:
            raise SensorAdapterError(
                "sensor_adapter_unavailable",
                f"sensor adapter {name!r} is not installed",
            ) from exc
        sensor = factory(sensor_id=sensor_id, settings=dict(settings or {}))
        return sensor


def load_sensor_adapter(
    name: str,
    *,
    sensor_id: str,
    settings: Mapping[str, object] | None = None,
) -> ReadOnlySensor:
    """Load one installed adapter through the stable package entry-point seam."""

    return SensorAdapterRegistry().create(name, sensor_id=sensor_id, settings=settings)
