"""Capability-oriented hardware abstraction layer.

The public API is deliberately expressed in AutoLab domain commands.  Vendor
adapters belong behind :class:`Device`; neither protocol compilation nor the
optimiser needs to know how a command reaches a physical instrument.
"""

from autonomous_lab.hardware.capabilities import CapabilitySnapshot
from autonomous_lab.hardware.commands import (
    AcquisitionRecord,
    DeviceResponse,
    EnvironmentalExposure,
    ExecutionContext,
    ExecutionEvidence,
    ExecutionEvidenceFragment,
    ResourceLedgerDelta,
    ResourceLedgerEntry,
)
from autonomous_lab.hardware.errors import (
    AmbiguousDeviceStateError,
    DeviceCapabilityError,
    DeviceExecutionError,
    DeviceTimeoutError,
)
from autonomous_lab.hardware.evidence import (
    DefaultEvidenceFinalizer,
    EvidenceFinalizationError,
    EvidenceFinalizer,
    LinearAssayCostCalculator,
)
from autonomous_lab.hardware.interfaces import Device
from autonomous_lab.hardware.registry import DeviceRegistry

__all__ = [
    "AcquisitionRecord",
    "AmbiguousDeviceStateError",
    "CapabilitySnapshot",
    "DefaultEvidenceFinalizer",
    "Device",
    "DeviceCapabilityError",
    "DeviceExecutionError",
    "DeviceRegistry",
    "DeviceResponse",
    "DeviceTimeoutError",
    "EnvironmentalExposure",
    "EvidenceFinalizationError",
    "EvidenceFinalizer",
    "ExecutionContext",
    "ExecutionEvidence",
    "ExecutionEvidenceFragment",
    "LinearAssayCostCalculator",
    "ResourceLedgerDelta",
    "ResourceLedgerEntry",
]
