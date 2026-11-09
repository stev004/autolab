"""Everything a culture run produced, as one hashable evidence artifact.

The runner collects only what devices attested to: the controller's setpoint
acknowledgement, its telemetry and safe-state acknowledgement, and the sensor's
raw readings. QC and analysis are computed *from* this record, never into it.
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
    """An execution-level fact worth keeping (errors, early stops)."""

    at: datetime
    code: str
    message: str
    ambiguous: bool = False


class CultureRunEvidence(DomainModel):
    schema_version: Literal["1"] = "1"
    run_id: UUID
    experiment_id: UUID
    receipt: ExecutableCultureProtocol
    controller_capability_sha256: str
    sensor_capabilities: SensorCapabilities
    setpoint_command: SetpointCommand
    acknowledgement: SetpointAcknowledgement | None
    hold_started_at: datetime
    ended_at: datetime
    telemetry: tuple[EnvironmentTelemetry, ...] = ()
    readings: tuple[SampleReading, ...] = ()
    events: tuple[RunEvent, ...] = Field(default=())
    stopped_early: bool = False
    safe_state: SafeStateAcknowledgement | None = None

    @property
    def sha256(self) -> str:
        return canonical_sha256(self)
