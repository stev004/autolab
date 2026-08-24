"""Deterministic canonical JSON and SHA-256 helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence, Set
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel


def canonical_json(value: Any, *, exclude: set[str] | None = None) -> str:
    """Serialize supported artifacts deterministically for hashing.

    Decimal and float magnitudes become canonical decimal strings, preserving
    scientific values without binary-float representation noise. Sets are sorted
    by their own canonical representation.
    """

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude=exclude or set())
    normalized = _normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: Any, *, exclude: set[str] | None = None) -> str:
    return sha256_text(canonical_json(value, exclude=exclude))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be canonically serialized")
        return _canonical_decimal(Decimal(str(value)))
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetimes cannot be canonically serialized")
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in mapping):
            raise TypeError("canonical mappings must use string keys")
        string_mapping = cast(Mapping[str, object], mapping)
        return {key: _normalize(item) for key, item in string_mapping.items()}
    if isinstance(value, Set):
        values = cast(Set[object], value)
        normalized = [_normalize(item) for item in values]
        return sorted(normalized, key=_normalized_sort_key)
    if isinstance(value, Sequence):
        values = cast(Sequence[object], value)
        return [_normalize(item) for item in values]
    raise TypeError(f"unsupported value for canonical serialization: {type(value).__name__}")


def _normalized_sort_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite decimals cannot be canonically serialized")
    if value == 0:
        return "0"
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
