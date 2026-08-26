"""Provider-neutral interfaces for external systems and control-plane services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, Self, runtime_checkable

from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalId,
    AttentionItem,
    AttentionRegistration,
    AttentionStatus,
    AttentionTrigger,
    AuditEvent,
    DateRange,
    Evidence,
    Plan,
    PlanId,
    RunId,
    ScheduledTask,
    ScheduledTaskId,
    ToolCompensation,
    ToolInvocation,
    UserId,
    WorkflowId,
    WorkflowStateSnapshot,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceQuery:
    """Narrow read request supplied to a scoped ERP, mail, or calendar provider."""

    record_types: frozenset[str]
    record_ids: frozenset[str] = frozenset()
    date_range: DateRange | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMMessage:
    """One classified prompt message prepared by application code."""

    role: str
    content: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptEnvelope:
    """Authorized context sent to one selected structured-output LLM adapter."""

    run_id: RunId
    actor: ActorContext
    attention: AttentionItem
    evidence: tuple[Evidence, ...]
    messages: tuple[LLMMessage, ...]
    purpose: str
    response_schema: str


class LLMGenerationStatus(StrEnum):
    """The complete safe outcome vocabulary returned by every configured LLM adapter."""

    SUCCEEDED = "succeeded"
    INVALID_RESPONSE = "invalid_response"
    TIMEOUT = "timeout"
    PROVIDER_FAILURE = "provider_failure"
    REFUSAL = "refusal"


class LLMCostSource(StrEnum):
    """Whether a cost is reported by the selected provider or estimated from a reviewed public rate."""

    ESTIMATED = "estimated"
    PROVIDER_REPORTED = "provider_reported"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMUsage:
    """Normalized safe metering returned by a selected provider without raw provider payload retention."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: Decimal | None
    cost_source: LLMCostSource

    def __post_init__(self) -> None:
        if not isinstance(self.cost_source, LLMCostSource):
            raise TypeError("LLM cost source must be a supported value")
        for value, name in (
            (self.input_tokens, "input tokens"),
            (self.cached_input_tokens, "cached input tokens"),
            (self.output_tokens, "output tokens"),
            (self.total_tokens, "total tokens"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"LLM {name} must be a non-negative integer")
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("LLM total tokens cannot be less than input plus output tokens")
        if self.cost_source is LLMCostSource.UNAVAILABLE:
            if self.cost_usd is not None:
                raise ValueError("unavailable LLM cost must be absent")
            return
        if (
            self.cost_usd is None
            or not isinstance(self.cost_usd, Decimal)
            or not self.cost_usd.is_finite()
            or self.cost_usd < 0
        ):
            raise ValueError("known LLM cost must be a non-negative finite decimal")


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMGenerationResult:
    """One provider-neutral structured success or sanitized failure that cannot become prompt input."""

    provider: str
    model: str
    status: LLMGenerationStatus = LLMGenerationStatus.SUCCEEDED
    output: Mapping[str, object] | None = None
    usage: LLMUsage | None = None

    def __post_init__(self) -> None:
        provider = self.provider.strip()
        model = self.model.strip()
        if not provider:
            raise ValueError("LLM provider is required")
        if not model:
            raise ValueError("LLM model is required")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        if self.status is LLMGenerationStatus.SUCCEEDED:
            if self.output is None or not isinstance(self.output, Mapping):
                raise ValueError("successful LLM result requires structured output")
            object.__setattr__(self, "output", MappingProxyType(dict(self.output)))
        elif self.output is not None:
            raise ValueError("failed LLM result must not include output")

    @classmethod
    def succeeded(
        cls,
        *,
        provider: str,
        model: str,
        output: Mapping[str, object],
        usage: LLMUsage | None = None,
    ) -> Self:
        """Normalize one transport-validated structured provider response."""
        return cls(
            provider=provider,
            model=model,
            status=LLMGenerationStatus.SUCCEEDED,
            output=output,
            usage=usage,
        )

    @classmethod
    def failed(
        cls,
        *,
        provider: str,
        model: str,
        status: LLMGenerationStatus,
        usage: LLMUsage | None = None,
    ) -> Self:
        """Normalize a safe failure category without retaining provider error text or payloads."""
        if status is LLMGenerationStatus.SUCCEEDED:
            raise ValueError("failed LLM result requires a failure status")
        return cls(provider=provider, model=model, status=status, usage=usage)

    @property
    def is_success(self) -> bool:
        """Return whether this result contains structured output eligible for schema validation."""
        return self.status is LLMGenerationStatus.SUCCEEDED

    def require_output(self) -> Mapping[str, object]:
        """Return structured output or refuse to pass a provider failure into application planning."""
        if not self.is_success or self.output is None:
            raise ValueError(f"LLM result has no structured output: {self.status.value}")
        return self.output


# Compatibility name retained while callers migrate from the success-only pre-M7 contract.
StructuredLLMResponse = LLMGenerationResult


@runtime_checkable
class ErpPort(Protocol):
    """Read scoped ERP facts without exposing storage or transport details."""

    def query(self, actor: ActorContext, query: EvidenceQuery) -> Sequence[Evidence]:
        """Return only ERP records visible to the supplied actor."""
        ...


@runtime_checkable
class QualityPort(Protocol):
    """Read quality-owned lot, allocation, and production-impact evidence without ERP access."""

    def query(self, actor: ActorContext, query: EvidenceQuery) -> Sequence[Evidence]:
        """Return only quality records visible to the supplied actor."""
        ...


@runtime_checkable
class MailPort(Protocol):
    """Read scoped mail facts without exposing mailbox implementation details."""

    def query(self, actor: ActorContext, query: EvidenceQuery) -> Sequence[Evidence]:
        """Return only messages visible to the supplied actor."""
        ...


@runtime_checkable
class CalendarPort(Protocol):
    """Read scoped calendar facts without exposing provider implementation details."""

    def query(self, actor: ActorContext, query: EvidenceQuery) -> Sequence[Evidence]:
        """Return only availability records visible to the supplied actor."""
        ...


@runtime_checkable
class IdentityPort(Protocol):
    """Resolve an immutable actor context before any scoped provider read."""

    def actor_for(self, user_id: UserId) -> ActorContext:
        """Return the current identity, scopes, plants, backup, and approval limits."""
        ...


@runtime_checkable
class AttentionPort(Protocol):
    """Create and advance durable, deduplicated business-attention items."""

    def register(self, trigger: AttentionTrigger, run_id: RunId) -> AttentionRegistration:
        """Persist one detector attempt and return its unique attention item."""
        ...

    def transition(
        self,
        attention: AttentionItem,
        target: AttentionStatus,
        run_id: RunId,
        occurred_at: datetime,
    ) -> AttentionItem:
        """Advance one attention item only from its known current state."""
        ...


@runtime_checkable
class PlanApprovalPort(Protocol):
    """Persist immutable plans and atomically advance only their bound approval records."""

    def create_pending(self, plan: Plan, approval: Approval) -> None:
        """Store one immutable plan and its pending approval in the same transaction."""
        ...

    def load(self, approval_id: ApprovalId) -> tuple[Plan, Approval] | None:
        """Load the exact plan and approval pair needed for a pre-decision validation."""
        ...

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        """Load the only plan/approval binding that controls a workflow instance."""
        ...

    def approve(
        self,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decider_id: UserId,
        decided_at: datetime,
    ) -> Approval | None:
        """Atomically approve one active unexpired request assigned to its deciding approver."""
        ...

    def reject(
        self,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decider_id: UserId,
        decided_at: datetime,
    ) -> Approval | None:
        """Atomically reject one active unexpired request assigned to its deciding approver."""
        ...

    def reroute(
        self,
        approval_id: ApprovalId,
        *,
        expected_plan_hash: str,
        original_approver_id: UserId,
        backup_approver_id: UserId,
        routed_at: datetime,
    ) -> Approval | None:
        """Atomically transfer only an unanswered request to its designated backup approver."""
        ...


@runtime_checkable
class WorkflowStatePort(Protocol):
    """Persist and retrieve the complete durable state of one declared workflow instance."""

    def create(self, snapshot: WorkflowStateSnapshot) -> None:
        """Store one plan-bound workflow and every declared initial step atomically."""
        ...

    def load(self, workflow_id: WorkflowId) -> WorkflowStateSnapshot | None:
        """Load one workflow and its ordered steps for a later claim or transition."""
        ...

    def claim(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Atomically claim one pending, expired, or owner-renewed runnable workflow."""
        ...

    def complete_guard_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Atomically complete exactly the currently declared next read-only guard step."""
        ...

    def start_tool_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        idempotency_key: str,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Commit the exact next external step as started before its effect may be invoked."""
        ...

    def complete_tool_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        idempotency_key: str,
        result: Mapping[str, object],
        finish_workflow: bool,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Atomically retain a tool result and advance only its still-current workflow cursor."""
        ...

    def fail_tool_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        idempotency_key: str,
        error: str,
        failed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Atomically record one terminal tool error while retaining the lease for compensation."""
        ...

    def begin_compensation(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Atomically move one failed workflow into its bounded compensation lifecycle."""
        ...

    def start_compensation_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Mark one completed external step as compensating before its reverse effect is invoked."""
        ...

    def complete_compensation_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        result: Mapping[str, object],
        finish_workflow: bool,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Retain one reverse result and close the workflow only after its final compensation."""
        ...


@runtime_checkable
class ToolExecutionPort(Protocol):
    """Invoke one already-started, idempotent external-style tool action."""

    def execute(self, actor: ActorContext, invocation: ToolInvocation) -> Mapping[str, object]:
        """Execute or return the durable result for this exact stable idempotency key."""
        ...


@runtime_checkable
class ToolCompensationPort(Protocol):
    """Invoke one independently idempotent reverse action for an existing external effect."""

    def compensate(
        self, actor: ActorContext, compensation: ToolCompensation
    ) -> Mapping[str, object]:
        """Compensate only the bound original effect or return its durable compensation result."""
        ...


@runtime_checkable
class ClockPort(Protocol):
    """Provide the application clock independently of wall-clock time."""

    def now(self) -> datetime:
        """Return the current business time."""
        ...


@runtime_checkable
class AuditPort(Protocol):
    """Append and read the durable, append-only audit ledger."""

    def append(self, event: AuditEvent) -> None:
        """Persist one material event without mutating prior events."""
        ...

    def events_for_run(self, run_id: RunId) -> Sequence[AuditEvent]:
        """Return run events for an audit-only reconstruction."""
        ...


@runtime_checkable
class SchedulerPort(Protocol):
    """Persist and drive idempotent time-based work without a concrete queue dependency."""

    def schedule(self, task: ScheduledTask) -> None:
        """Store one durable task using its idempotency key."""
        ...

    def claim_due(self, now: datetime, limit: int) -> Sequence[ScheduledTask]:
        """Claim up to ``limit`` due tasks for one worker pass."""
        ...

    def mark_succeeded(self, task_id: ScheduledTaskId, completed_at: datetime) -> None:
        """Record completion after a task's effect has succeeded."""
        ...


@runtime_checkable
class LLMPort(Protocol):
    """Generate one validated-transport structured response from an authorized prompt."""

    def generate(self, prompt: PromptEnvelope) -> LLMGenerationResult:
        """Call only the selected provider and normalize success or a safe failure without fallback."""
        ...
