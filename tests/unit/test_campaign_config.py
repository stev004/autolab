from pathlib import Path

import pytest
import yaml

from autonomous_lab.campaigns.loader import (
    ConfigurationLoadError,
    load_campaign,
    load_validation_policy,
)
from autonomous_lab.domain.enums import CommandType, ConstraintOperator, ObjectiveDirection

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_PATH = ROOT / "configs" / "assay-reagent-reduction.yaml"
POLICY_PATH = ROOT / "configs" / "validation-policy.yaml"


def _write_campaign(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "campaign.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_supplied_campaign_loads_with_strict_enum_strings() -> None:
    loaded = load_campaign(CAMPAIGN_PATH)

    assert loaded.config.objective.metrics[0].direction is ObjectiveDirection.MINIMIZE
    constraint = loaded.config.objective.performance_constraints[0]
    assert constraint.operator is ConstraintOperator.LTE
    assert loaded.config.campaign.max_experiments == 60
    assert len(loaded.source_sha256) == 64
    assert len(loaded.canonical_sha256) == 64


def test_supplied_policy_loads_command_enum_strings() -> None:
    policy = load_validation_policy(POLICY_PATH)

    assert CommandType.DISPENSE in policy.allowed_command_types
    assert policy.wavelength_range.minimum.to("nanometer").value == 400


def test_numeric_strings_remain_invalid_under_yaml_loader(tmp_path: Path) -> None:
    raw = yaml.safe_load(CAMPAIGN_PATH.read_text(encoding="utf-8"))
    raw["campaign"]["max_experiments"] = "60"
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ConfigurationLoadError, match="failed validation"):
        load_campaign(path)


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    raw = yaml.safe_load(CAMPAIGN_PATH.read_text(encoding="utf-8"))
    raw["campaign"]["surprise"] = True
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ConfigurationLoadError, match="extra_forbidden"):
        load_campaign(path)


def test_repeated_load_has_stable_canonical_hash() -> None:
    first = load_campaign(CAMPAIGN_PATH)
    second = load_campaign(CAMPAIGN_PATH)

    assert first.canonical_sha256 == second.canonical_sha256


@pytest.mark.parametrize(
    ("mutation_path", "value", "message"),
    [
        (("parameter_space", "reagent_volume", "maximum"), 201, "simulator domain"),
        (("parameter_space", "sample_volume", "unit"), "minute", "not compatible"),
        (("objective", "metrics", 0, "name"), "invented_cost", "unknown scientific metric"),
        (("objective", "metrics", 0, "unit"), "USD", "unknown or invalid unit"),
        (
            ("objective", "performance_constraints", 0, "unit"),
            "percent",
            "canonical unit",
        ),
        (
            ("objective", "performance_constraints", 0, "operator"),
            "gte",
            "does not support constraint operator",
        ),
        (("execution", "backend"), "physical", "literal_error"),
        (("execution", "capability_profile"), "other", "literal_error"),
        (("simulation", "model"), "unknown_model", "literal_error"),
        (
            ("simulation", "cost", "fixed_consumables", "unit"),
            "milligram",
            "not compatible",
        ),
        (
            ("simulation", "cost", "fixed_consumables", "value"),
            -0.01,
            "cannot be negative",
        ),
        (("assay_design", "wavelength", "value"), 600, "fixed at 520"),
        (("assay_design", "check_standard_level"), 0.8, "independent"),
        (
            ("assay_design", "maximum_well_volume", "value"),
            250,
            "exceed configured well capacity",
        ),
    ],
)
def test_v0_semantic_contract_rejects_invalid_campaigns(
    tmp_path: Path,
    mutation_path: tuple[str | int, ...],
    value: object,
    message: str,
) -> None:
    raw = yaml.safe_load(CAMPAIGN_PATH.read_text(encoding="utf-8"))
    target = raw
    for key in mutation_path[:-1]:
        target = target[key]
    target[mutation_path[-1]] = value

    with pytest.raises(ConfigurationLoadError, match=message):
        load_campaign(_write_campaign(tmp_path, raw))


def test_v0_requires_exact_parameter_names(tmp_path: Path) -> None:
    raw = yaml.safe_load(CAMPAIGN_PATH.read_text(encoding="utf-8"))
    raw["parameter_space"]["invented_parameter"] = raw["parameter_space"].pop("temperature")

    with pytest.raises(ConfigurationLoadError, match="requires exactly"):
        load_campaign(_write_campaign(tmp_path, raw))


def test_v0_mandates_an_independent_check_standard(tmp_path: Path) -> None:
    raw = yaml.safe_load(CAMPAIGN_PATH.read_text(encoding="utf-8"))
    del raw["assay_design"]["check_standard_level"]

    with pytest.raises(ConfigurationLoadError, match="Field required"):
        load_campaign(_write_campaign(tmp_path, raw))


def test_grid_campaign_checks_size_and_exhaustion_against_budget(tmp_path: Path) -> None:
    raw = yaml.safe_load(CAMPAIGN_PATH.read_text(encoding="utf-8"))
    raw["optimization"]["strategy"] = "grid"
    raw["optimization"]["maximum_grid_size"] = 100
    resolutions = {
        "reagent_concentration": 0.3,
        "reagent_volume": 180,
        "sample_volume": 90,
        "incubation_time": 29,
        "temperature": 22,
    }
    for name, resolution in resolutions.items():
        raw["parameter_space"][name]["resolution"] = resolution

    assert load_campaign(_write_campaign(tmp_path, raw)).config.optimization.strategy == "grid"

    raw["campaign"]["max_experiments"] = 65
    with pytest.raises(ConfigurationLoadError, match="exceeds finite grid size 64"):
        load_campaign(_write_campaign(tmp_path, raw))

    raw["campaign"]["max_experiments"] = 60
    raw["optimization"]["maximum_grid_size"] = 63
    with pytest.raises(ConfigurationLoadError, match="maximum_grid_size=63"):
        load_campaign(_write_campaign(tmp_path, raw))
