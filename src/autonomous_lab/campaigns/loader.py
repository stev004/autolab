"""Safe YAML/JSON loading with original-source provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ValidationError

from autonomous_lab.campaigns.config import CampaignConfig, ValidationPolicyConfig
from autonomous_lab.campaigns.culture_config import CultureCampaignConfig
from autonomous_lab.culture.models import CulturePolicy
from autonomous_lab.provenance.hashing import canonical_sha256, sha256_text


class ConfigurationLoadError(ValueError):
    """Raised for unsafe, malformed, or schema-invalid configuration."""


@dataclass(frozen=True, slots=True)
class LoadedCampaign:
    config: CampaignConfig
    source_path: Path
    raw_text: str
    source_sha256: str
    canonical_sha256: str


def load_campaign(path: str | Path) -> LoadedCampaign:
    source_path = Path(path).resolve()
    raw_text = _read_text(source_path)
    raw = _safe_mapping(raw_text, source_path)
    if raw.get("experiment_type", "assay") != "assay":
        raise ConfigurationLoadError(
            f"{source_path} is a {raw.get('experiment_type')!r} campaign; "
            "use `autolab culture ...` for culture campaigns"
        )
    config = _validate_json_semantics(CampaignConfig, raw, source_path)
    return LoadedCampaign(
        config=config,
        source_path=source_path,
        raw_text=raw_text,
        source_sha256=sha256_text(raw_text),
        canonical_sha256=canonical_sha256(config),
    )


@dataclass(frozen=True, slots=True)
class LoadedCultureCampaign:
    config: CultureCampaignConfig
    source_path: Path
    raw_text: str
    source_sha256: str
    canonical_sha256: str


def load_culture_campaign(path: str | Path) -> LoadedCultureCampaign:
    source_path = Path(path).resolve()
    raw_text = _read_text(source_path)
    raw = _safe_mapping(raw_text, source_path)
    if raw.get("experiment_type") != "culture":
        raise ConfigurationLoadError(
            f"{source_path} is not a culture campaign (experiment_type: culture)"
        )
    config = _validate_json_semantics(CultureCampaignConfig, raw, source_path)
    return LoadedCultureCampaign(
        config=config,
        source_path=source_path,
        raw_text=raw_text,
        source_sha256=sha256_text(raw_text),
        canonical_sha256=canonical_sha256(config),
    )


def load_culture_policy(path: str | Path) -> CulturePolicy:
    """Load the trusted culture installation policy (not part of any campaign)."""

    source_path = Path(path).resolve()
    raw = _safe_mapping(_read_text(source_path), source_path)
    return _validate_json_semantics(CulturePolicy, raw, source_path)


def load_validation_policy(path: str | Path) -> ValidationPolicyConfig:
    source_path = Path(path).resolve()
    raw = _safe_mapping(_read_text(source_path), source_path)
    return _validate_json_semantics(ValidationPolicyConfig, raw, source_path)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationLoadError(f"cannot read configuration {path}: {exc}") from exc


def _safe_mapping(raw_text: str, path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigurationLoadError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationLoadError(f"configuration {path} must contain a mapping at its root")
    raw_mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw_mapping):
        raise ConfigurationLoadError(f"configuration {path} must use string keys")
    return cast(dict[str, Any], raw_mapping)


def _validate_json_semantics[ConfigT: BaseModel](
    model_type: type[ConfigT], raw: dict[str, Any], path: Path
) -> ConfigT:
    """Validate YAML values with strict JSON semantics.

    This accepts serialized enum/date strings while retaining strict numeric and
    boolean checks. It also prevents Python-only YAML types from entering models.
    """

    try:
        serialized = json.dumps(raw, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ConfigurationLoadError(f"configuration {path} is not JSON-compatible: {exc}") from exc
    try:
        return model_type.model_validate_json(serialized, strict=True)
    except ValidationError as exc:
        raise ConfigurationLoadError(f"configuration {path} failed validation: {exc}") from exc
