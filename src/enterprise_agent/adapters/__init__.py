"""Concrete infrastructure adapters that implement provider-neutral ports."""

from enterprise_agent.domain import InvalidAttentionTransitionError

from .attention import PostgresAttentionAdapter
from .audit import AuditEventError, PostgresAuditAdapter
from .claude import ClaudeMessagesAdapter, ClaudeMessagesTransport, UrllibClaudeMessagesTransport
from .clock import DemoClockNotInitializedError, PostgresDemoClock
from .identity import IdentityNotFoundError, PostgresIdentityAdapter
from .openai import OpenAIResponsesAdapter, OpenAIResponsesTransport, UrllibOpenAIResponsesTransport
from .plan_approvals import PostgresPlanApprovalAdapter
from .providers import (
    PostgresCalendarAdapter,
    PostgresErpAdapter,
    PostgresMailAdapter,
    PostgresQualityAdapter,
    UnsupportedEvidenceTypeError,
)
from .scheduler import (
    PostgresSchedulerAdapter,
    ScheduledTaskClaimLostError,
    ScheduledTaskIdempotencyError,
)
from .tools import PostgresScenarioAToolAdapter, PostgresToolAdapter, ToolExecutionError
from .workflow_state import PostgresWorkflowStateAdapter

__all__ = [
    "AuditEventError",
    "ClaudeMessagesAdapter",
    "ClaudeMessagesTransport",
    "DemoClockNotInitializedError",
    "IdentityNotFoundError",
    "InvalidAttentionTransitionError",
    "OpenAIResponsesAdapter",
    "OpenAIResponsesTransport",
    "PostgresAttentionAdapter",
    "PostgresAuditAdapter",
    "PostgresCalendarAdapter",
    "PostgresDemoClock",
    "PostgresErpAdapter",
    "PostgresIdentityAdapter",
    "PostgresMailAdapter",
    "PostgresPlanApprovalAdapter",
    "PostgresQualityAdapter",
    "PostgresScenarioAToolAdapter",
    "PostgresSchedulerAdapter",
    "PostgresToolAdapter",
    "PostgresWorkflowStateAdapter",
    "ScheduledTaskClaimLostError",
    "ScheduledTaskIdempotencyError",
    "ToolExecutionError",
    "UnsupportedEvidenceTypeError",
    "UrllibClaudeMessagesTransport",
    "UrllibOpenAIResponsesTransport",
]
