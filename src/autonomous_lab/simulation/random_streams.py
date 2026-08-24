"""Stable, independent NumPy random streams.

Python's :func:`hash` is intentionally never used: its output is process dependent.
Each stochastic component receives a stream derived from a campaign seed and a
length-prefixed sequence of stable identifiers.  Adding a new component therefore
does not advance or perturb existing streams.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

import numpy as np
from numpy.random import Generator

from autonomous_lab.simulation.model_versions import RANDOM_STREAM_ALGORITHM


def _encode_part(part: object) -> bytes:
    if isinstance(part, bytes):
        return part
    if isinstance(part, str):
        return part.encode("utf-8")
    if isinstance(part, bool):
        return b"true" if part else b"false"
    if isinstance(part, int):
        return str(part).encode("ascii")
    if isinstance(part, float):
        return part.hex().encode("ascii")
    return str(part).encode("utf-8")


def stable_seed_words(campaign_seed: int, *identifiers: object) -> tuple[int, ...]:
    """Return deterministic 32-bit seed words for a logical random stream."""

    if campaign_seed < 0:
        raise ValueError("campaign_seed must be non-negative")
    digest = hashlib.sha256()
    digest.update(RANDOM_STREAM_ALGORITHM.encode("ascii"))
    for part in (campaign_seed, *identifiers):
        encoded = _encode_part(part)
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
    raw = digest.digest()
    return tuple(int.from_bytes(raw[i : i + 4], "big") for i in range(0, 32, 4))


def independent_rng(campaign_seed: int, *identifiers: object) -> Generator:
    """Create a NumPy generator for a stable logical stream."""

    return np.random.default_rng(
        np.random.SeedSequence(stable_seed_words(campaign_seed, *identifiers))
    )


def stream_identifier(*identifiers: object) -> str:
    """Return a non-secret, stable identifier suitable for provenance."""

    digest = hashlib.sha256()
    for part in identifiers:
        encoded = _encode_part(part)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:24]


def spawn_generators(
    campaign_seed: int, common_identifiers: Iterable[object], names: Iterable[str]
) -> dict[str, Generator]:
    """Convenience helper returning independently keyed streams by name."""

    prefix = tuple(common_identifiers)
    return {name: independent_rng(campaign_seed, *prefix, name) for name in names}
