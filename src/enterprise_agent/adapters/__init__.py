"""Concrete infrastructure adapters that implement provider-neutral ports."""

from enterprise_agent.domain import InvalidAttentionTransitionError

from .attention import PostgresAttentionAdapter
from .audit import AuditEventError, PostgresAuditAdapter
from .clock import DemoClockNotInitializedError, PostgresDemoClock
from .identity import IdentityNotFoundError, PostgresIdentityAdapter
from .plan_approvals import PostgresPlanApprovalAdapter
from .providers import (
    PostgresCalendarAdapter,
    PostgresErpAdapter,
    PostgresMailAdapter,
    UnsupportedEvidenceTypeError,
)
from .scheduler import (
    PostgresSchedulerAdapter,
    ScheduledTaskClaimLostError,
    ScheduledTaskIdempotencyError,
)
from .tools import PostgresScenarioAToolAdapter, ToolExecutionError
from .workflow_state import PostgresWorkflowStateAdapter

__all__ = [
    "AuditEventError",
    "DemoClockNotInitializedError",
    "IdentityNotFoundError",
    "InvalidAttentionTransitionError",
    "PostgresAttentionAdapter",
    "PostgresAuditAdapter",
    "PostgresCalendarAdapter",
    "PostgresDemoClock",
    "PostgresErpAdapter",
    "PostgresIdentityAdapter",
    "PostgresMailAdapter",
    "PostgresPlanApprovalAdapter",
    "PostgresScenarioAToolAdapter",
    "PostgresSchedulerAdapter",
    "PostgresWorkflowStateAdapter",
    "ScheduledTaskClaimLostError",
    "ScheduledTaskIdempotencyError",
    "ToolExecutionError",
    "UnsupportedEvidenceTypeError",
]
