"""Everything a culture run produced, as one hashable evidence artifact.

The runner collects only what devices attested to: each reactor's setpoint
acknowledgement, telemetry and safe-state acknowledgement, and the sensor's raw
readings. QC and analysis are computed *from* this record, never into it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from autonomous_lab.culture.controller import (
    EnvironmentTelemetry,
    SafeStateAcknowledgement,
    SetpointAcknowledgement,
    SetpointCommand,
)
from autonomous_lab.culture.models import ExecutableCultureProtocol
from autonomous_lab.culture.timeseries import SampleReading
from autonomous_lab.domain.base import DomainModel
from autonomous_lab.hardware.sensors.contracts import SensorCapabilities
from autonomous_lab.provenance.hashing import canonical_sha256


class RunEvent(DomainModel):
    """An execution-level fact worth keeping (errors, early stops, resumes)."""

    at: datetime
    code: str
    message: str
    ambiguous: bool = False
    reactor_id: str | None = None


class ReplicateEvidence(DomainModel):
    """What one reactor (one biological replicate) did during the hold."""

    replicate_index: int
    reactor_id: str
    setpoint_command: SetpointCommand
    acknowledgement: SetpointAcknowledgement | None = None
    telemetry: tuple[EnvironmentTelemetry, ...] = ()
    readings: tuple[SampleReading, ...] = ()
    stopped_early: bool = False
    #: True if AutoLab could not confirm the device kept holding across a restart.
    hold_interrupted: bool = False
    safe_state: SafeStateAcknowledgement | None = None

    @property
    def accepted(self) -> bool:
        return self.acknowledgement is not None and self.acknowledgement.accepted


class CultureRunEvidence(DomainModel):
    schema_version: Literal["2"] = "2"
    run_id: UUID
    experiment_id: UUID
    receipt: ExecutableCultureProtocol
    controller_capability_sha256: str
    sensor_capabilities: SensorCapabilities
    hold_started_at: datetime
    ended_at: datetime
    replicates: tuple[ReplicateEvidence, ...]
    events: tuple[RunEvent, ...] = Field(default=())
    resumed_at: tuple[datetime, ...] = ()
    #: Sensor snapshots from later sessions (after a resume); the sensor may have
    #: been recalibrated meanwhile, so each reading is checked against its own.
    resumed_sensor_capabilities: tuple[SensorCapabilities, ...] = ()

    @property
    def sensor_sessions(self) -> tuple[SensorCapabilities, ...]:
        return (self.sensor_capabilities, *self.resumed_sensor_capabilities)

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)

    def replicate(self, reactor_id: str) -> ReplicateEvidence:
        return next(item for item in self.replicates if item.reactor_id == reactor_id)
