"""Stable identifiers and the explicitly supported V0 simulator domain."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

ASSAY_MODEL_ID = "nonlinear_colorimetric_assay"
ASSAY_MODEL_VERSION = "1.1.0"
ASSAY_MODEL_CONFIG_NAME = "nonlinear_colorimetric_assay_v1"
RANDOM_STREAM_ALGORITHM = "sha256-seedsequence-v1"


@dataclass(frozen=True, slots=True)
class SimulatorParameterDomain:
    canonical_unit: str
    minimum: Decimal
    maximum: Decimal


ASSAY_PARAMETER_DOMAIN: MappingProxyType[str, SimulatorParameterDomain] = MappingProxyType(
    {
        "reagent_concentration": SimulatorParameterDomain(
            "milligram / milliliter", Decimal("0.1"), Decimal("1.0")
        ),
        "reagent_volume": SimulatorParameterDomain("microliter", Decimal("20"), Decimal("200")),
        "sample_volume": SimulatorParameterDomain("microliter", Decimal("10"), Decimal("100")),
        "incubation_time": SimulatorParameterDomain("minute", Decimal("1"), Decimal("30")),
        "temperature": SimulatorParameterDomain("degree_Celsius", Decimal("18"), Decimal("40")),
    }
)

ASSAY_WAVELENGTH_NANOMETER = Decimal("520")
ASSAY_MAXIMUM_WELL_VOLUME_MICROLITER = Decimal("300")
