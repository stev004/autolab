"""Declared human-workflow capability profile used only for protocol validation."""

from __future__ import annotations

from decimal import Decimal

from autonomous_lab.domain.enums import CommandType
from autonomous_lab.domain.protocols import CapabilityManifest, QuantityRange
from autonomous_lab.domain.quantities import QuantityValue
from autonomous_lab.hardware.capabilities import CapabilitySnapshot


def human_assay_v1_capability_snapshot() -> CapabilitySnapshot:
    """Return the declared V0.5 human assay validation profile.

    This snapshot says that AutoLab can express the operation in a human
    worklist and validate its deterministic ranges.  It is *not* evidence that a
    person performed a step, that an instrument was present, or that a reading
    exists.  Those claims require operator and measurement evidence separately.
    """

    plate = frozenset({"microplate_96_well"})
    volume_range = QuantityRange(
        minimum=QuantityValue(value=Decimal("0.1"), unit="microliter"),
        maximum=QuantityValue(value=Decimal("300"), unit="microliter"),
        resolution=QuantityValue(value=Decimal("0.1"), unit="microliter"),
    )
    return CapabilitySnapshot(
        manifests=(
            CapabilityManifest(
                device_id="human-worklist-liquid-handling",
                device_type="liquid_handler",
                adapter_name="human_assay_v1_validation_profile",
                adapter_version="1",
                supported_commands=frozenset(
                    {
                        CommandType.DISPENSE,
                        CommandType.ASPIRATE,
                        CommandType.TRANSFER,
                        CommandType.MIX,
                    }
                ),
                ranges={
                    "dispense_volume": volume_range,
                    "aspirate_volume": volume_range,
                    "transfer_volume": volume_range,
                    "mix_volume": volume_range,
                },
                supported_vessels=plate,
                channel_count=1,
            ),
            CapabilityManifest(
                device_id="human-worklist-incubation",
                device_type="incubator",
                adapter_name="human_assay_v1_validation_profile",
                adapter_version="1",
                supported_commands=frozenset({CommandType.INCUBATE}),
                ranges={
                    "incubation_duration": QuantityRange(
                        minimum=QuantityValue(value=Decimal("0.1"), unit="minute"),
                        maximum=QuantityValue(value=Decimal("1440"), unit="minute"),
                        resolution=QuantityValue(value=Decimal("0.1"), unit="minute"),
                    ),
                    "incubation_temperature": QuantityRange(
                        minimum=QuantityValue(value=Decimal("4"), unit="degree_Celsius"),
                        maximum=QuantityValue(value=Decimal("60"), unit="degree_Celsius"),
                        resolution=QuantityValue(value=Decimal("0.1"), unit="delta_degree_Celsius"),
                    ),
                },
                supported_vessels=plate,
                channel_count=1,
            ),
            CapabilityManifest(
                device_id="human-measurement-plan",
                device_type="spectrophotometer",
                adapter_name="human_assay_v1_validation_profile",
                adapter_version="1",
                supported_commands=frozenset({CommandType.MEASURE_ABSORBANCE}),
                ranges={
                    "wavelength": QuantityRange(
                        minimum=QuantityValue(value=Decimal("350"), unit="nanometer"),
                        maximum=QuantityValue(value=Decimal("800"), unit="nanometer"),
                        resolution=QuantityValue(value=Decimal("1"), unit="nanometer"),
                    )
                },
                supported_vessels=plate,
                channel_count=1,
            ),
        )
    )
