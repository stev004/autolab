"""Stable JSON conversion at the storage boundary."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast
from uuid import UUID


def jsonable(value: Any) -> Any:
    """Convert domain/Pydantic objects into deterministic JSON-compatible data."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return jsonable(model_dump(mode="python"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): jsonable(item) for key, item in mapping.items()}
    if isinstance(value, (set, frozenset)):
        iterable = cast(Iterable[object], value)
        normalized = [jsonable(item) for item in iterable]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, (list, tuple)):
        iterable = cast(Iterable[object], value)
        return [jsonable(item) for item in iterable]
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, Decimal):
        # SQLite JSON has no decimal primitive.  Persist as a JSON number so
        # strict scientific models can be reconstructed without accepting
        # numeric strings. Canonical hashes still normalize decimal values at
        # their provenance boundary.
        return int(value) if value == value.to_integral_value() else float(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Cannot serialize {type(value).__name__} as a scientific snapshot")


def canonical_json(value: Any) -> str:
    return json.dumps(
        jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def snapshot_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def enum_value(value: Any) -> str:
    return str(value.value if isinstance(value, Enum) else value)
