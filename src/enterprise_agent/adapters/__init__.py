"""Concrete infrastructure adapters that implement provider-neutral ports."""

from enterprise_agent.domain import InvalidAttentionTransitionError

from .attention import PostgresAttentionAdapter
from .identity import IdentityNotFoundError, PostgresIdentityAdapter
from .plan_approvals import PostgresPlanApprovalAdapter
from .providers import (
    PostgresCalendarAdapter,
    PostgresErpAdapter,
    PostgresMailAdapter,
    UnsupportedEvidenceTypeError,
)

__all__ = [
    "IdentityNotFoundError",
    "InvalidAttentionTransitionError",
    "PostgresAttentionAdapter",
    "PostgresCalendarAdapter",
    "PostgresErpAdapter",
    "PostgresIdentityAdapter",
    "PostgresMailAdapter",
    "PostgresPlanApprovalAdapter",
    "UnsupportedEvidenceTypeError",
]
