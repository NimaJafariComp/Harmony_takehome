"""Concrete infrastructure adapters that implement provider-neutral ports."""

from .identity import IdentityNotFoundError, PostgresIdentityAdapter
from .providers import (
    PostgresCalendarAdapter,
    PostgresErpAdapter,
    PostgresMailAdapter,
    UnsupportedEvidenceTypeError,
)

__all__ = [
    "IdentityNotFoundError",
    "PostgresCalendarAdapter",
    "PostgresErpAdapter",
    "PostgresIdentityAdapter",
    "PostgresMailAdapter",
    "UnsupportedEvidenceTypeError",
]
