"""Common configuration for immutable, strict domain values."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Base for persisted domain artifacts.

    Strict input handling and forbidden extra keys make schema mistakes visible.
    Frozen models prevent an artifact from changing after its provenance hash was
    calculated (nested dictionaries should still be treated as immutable values).
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        validate_default=True,
        validate_assignment=True,
    )

