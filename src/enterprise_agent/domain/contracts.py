"""Immutable domain contracts shared across the application boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import NewType, cast

UserId = NewType("UserId", str)
PlantId = NewType("PlantId", str)
Scope = NewType("Scope", str)
AttentionId = NewType("AttentionId", str)
EvidenceId = NewType("EvidenceId", str)
PlanId = NewType("PlanId", str)
ApprovalId = NewType("ApprovalId", str)
WorkflowId = NewType("WorkflowId", str)
ToolInvocationId = NewType("ToolInvocationId", str)
ScheduledTaskId = NewType("ScheduledTaskId", str)
AuditEventId = NewType("AuditEventId", str)
RunId = NewType("RunId", str)


def _freeze_mapping[ValueT](values: Mapping[str, ValueT]) -> Mapping[str, ValueT]:
    """Copy a mapping into an immutable container owned by the domain record."""
    return cast(Mapping[str, ValueT], MappingProxyType(dict(values)))


class AttentionStatus(StrEnum):
    """Lifecycle states for deduplicated work requiring agent attention."""

    OPEN = "open"
    PENDING_APPROVAL = "pending_approval"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    """Lifecycle states for a plan-bound approval request."""

    PENDING = "pending"
    REROUTED = "rerouted"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ToolInvocationStatus(StrEnum):
    """Durable state of one idempotent effectful-tool attempt."""

    PENDING = "pending"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATED = "compensated"


class WorkflowStatus(StrEnum):
    """Durable state of a declared workflow instance."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"


class ScheduledTaskStatus(StrEnum):
    """Lifecycle state of a durable scheduled task."""

    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, kw_only=True)
class Money:
    """A non-negative monetary amount with a normalized ISO-like currency code."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("Money amount must be non-negative")

        currency = self.currency.upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("Money currency must use three uppercase letters")

        object.__setattr__(self, "currency", currency)


@dataclass(frozen=True, slots=True, kw_only=True)
class DateRange:
    """An inclusive business-date range used by schedule and availability rules."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("Date range end must not precede its start")

    @property
    def days(self) -> int:
        """Return the calendar-day distance between the range endpoints."""
        return (self.end - self.start).days


@dataclass(frozen=True, slots=True, kw_only=True)
class ActorContext:
    """Immutable identity and permission facts passed to every provider and tool."""

    user_id: UserId
    role: str
    scopes: frozenset[Scope]
    plant_ids: frozenset[PlantId]
    backup_approver_id: UserId | None
    approval_limits: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        normalized_limits: dict[str, Decimal] = {}
        for currency, limit in self.approval_limits.items():
            normalized = Money(amount=limit, currency=currency)
            normalized_limits[normalized.currency] = normalized.amount
        object.__setattr__(self, "approval_limits", _freeze_mapping(normalized_limits))

    def approval_limit_for(self, currency: str) -> Decimal | None:
        """Return the actor's approval ceiling for a normalized currency code."""
        return self.approval_limits.get(currency.upper())


@dataclass(frozen=True, slots=True, kw_only=True)
class Evidence:
    """A versioned, time-stamped source fact allowed into an agent context bundle."""

    evidence_id: EvidenceId
    source: str
    record_type: str
    record_id: str
    source_version: int
    observed_at: datetime
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))


@dataclass(frozen=True, slots=True, kw_only=True)
class AttentionItem:
    """One deduplicated business risk requiring a controlled agent response."""

    attention_id: AttentionId
    scenario: str
    cause: str
    dedupe_key: str
    status: AttentionStatus
    created_at: datetime
    source_versions: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_versions", _freeze_mapping(self.source_versions))


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    """An immutable proposed intent bound to actor, policy, and source versions."""

    plan_id: PlanId
    attention_id: AttentionId
    actor_id: UserId
    intent: str
    workflow_name: str | None
    workflow_version: int | None
    parameters: Mapping[str, object]
    source_versions: Mapping[str, int]
    policy_version: str
    plan_hash: str
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _freeze_mapping(self.parameters))
        object.__setattr__(self, "source_versions", _freeze_mapping(self.source_versions))


@dataclass(frozen=True, slots=True, kw_only=True)
class Approval:
    """A decision record that is permanently bound to one immutable plan hash."""

    approval_id: ApprovalId
    plan_id: PlanId
    plan_hash: str
    requester_id: UserId
    approver_id: UserId
    status: ApprovalStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowState:
    """Durable progress for a versioned, declared workflow definition."""

    workflow_id: WorkflowId
    plan_id: PlanId
    definition_name: str
    definition_version: int
    status: WorkflowStatus
    current_step: int
    started_at: datetime | None
    completed_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolInvocation:
    """One externally visible tool action with a stable idempotency key."""

    invocation_id: ToolInvocationId
    workflow_id: WorkflowId
    tool_name: str
    idempotency_key: str
    status: ToolInvocationStatus
    parameters: Mapping[str, object]
    result: Mapping[str, object] | None
    attempt_count: int
    started_at: datetime | None
    completed_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _freeze_mapping(self.parameters))
        if self.result is not None:
            object.__setattr__(self, "result", _freeze_mapping(self.result))


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduledTask:
    """A durable, idempotent task for time-driven business follow-up."""

    task_id: ScheduledTaskId
    task_type: str
    due_at: datetime
    status: ScheduledTaskStatus
    idempotency_key: str
    payload: Mapping[str, object]
    attempt_count: int
    lease_expires_at: datetime | None
    completed_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEvent:
    """An append-only sanitized record of a material agent-system event."""

    event_id: AuditEventId
    occurred_at: datetime
    event_type: str
    run_id: RunId
    actor_id: UserId | None
    attention_id: AttentionId | None
    workflow_id: WorkflowId | None
    evidence_ids: tuple[EvidenceId, ...]
    payload: Mapping[str, object]
    policy_version: str | None
    plan_hash: str | None
    idempotency_key: str | None
    failure_category: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))
