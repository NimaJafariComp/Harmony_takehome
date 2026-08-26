"""Actor-scoped, read-only models for the optional local evidence-review UI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from enterprise_agent.application.audit_explain import AuditExplainer
from enterprise_agent.application.operator_status import (
    OperatorStatusSnapshot,
    operator_status_data,
    recovery_state_for,
)
from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalId,
    AttentionId,
    AttentionItem,
    AuditEvent,
    Plan,
    PlanId,
    RunId,
    UserId,
    WorkflowId,
    WorkflowStateSnapshot,
)

ReviewPayload = Mapping[str, object]


class LocalReviewError(RuntimeError):
    """Base error for deliberately non-mutating local review operations."""


class LocalReviewResourceNotFoundError(LocalReviewError):
    """Raised when the selected local review resource has no durable record."""


class LocalReviewAccessDeniedError(LocalReviewError):
    """Raised when a record belongs outside the selected local demo actor's view."""


class LocalReviewUnavailableError(LocalReviewError):
    """Raised when the optional UI has no local demo reader configured."""


class LocalReviewReadPort(Protocol):
    """The small presentation contract consumed by FastAPI without persistence knowledge."""

    def status(self) -> ReviewPayload:
        """Return the CLI-compatible actor-scoped control-plane summary."""
        ...

    def attention(self, attention_id: str) -> ReviewPayload:
        """Return one authorized attention record and its safe evidence references."""
        ...

    def approval(self, approval_id: str) -> ReviewPayload:
        """Return one authorized immutable plan/approval summary."""
        ...

    def workflow(self, workflow_id: str) -> ReviewPayload:
        """Return one authorized workflow/recovery projection."""
        ...

    def audit(self, run_id: str) -> ReviewPayload:
        """Return one fully authorized ledger explanation."""
        ...

    def demo_clock(self) -> ReviewPayload:
        """Return the persisted local demo time without advancing it."""
        ...


class OperatorStatusReadPort(Protocol):
    """Read the terminal-compatible status model for one immutable actor identity."""

    def read_status_for_actor(self, actor_id: UserId) -> OperatorStatusSnapshot:
        """Return only pending approvals and workflows visible to this actor."""
        ...


class AttentionReadPort(Protocol):
    """Load an immutable attention record without transitioning its lifecycle."""

    def load(self, attention_id: AttentionId) -> AttentionItem | None:
        """Return one attention item, if it exists."""
        ...


class PlanApprovalReadPort(Protocol):
    """Read immutable plan/approval bindings without deciding or rerouting them."""

    def load(self, approval_id: ApprovalId) -> tuple[Plan, Approval] | None:
        """Return one approval and its bound plan, if it exists."""
        ...

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        """Return the one approval binding for an existing plan, if it exists."""
        ...


class WorkflowStateReadPort(Protocol):
    """Load one durable workflow snapshot without claiming or resuming it."""

    def load(self, workflow_id: WorkflowId) -> WorkflowStateSnapshot | None:
        """Return one workflow and its ordered stored step records, if present."""
        ...


class AuditReadPort(Protocol):
    """Read one chronological audit run without appending to the ledger."""

    def events_for_run(self, run_id: RunId) -> Sequence[AuditEvent]:
        """Return one immutable run ledger in its durable order."""
        ...


class DemoClockReadPort(Protocol):
    """Read the deterministic demo clock without advancing it."""

    def now(self) -> datetime:
        """Return the durable local-demo timestamp."""
        ...


class IdentityReadPort(Protocol):
    """Confirm the selected actor still exists in the local company fixture."""

    def actor_for(self, user_id: UserId) -> ActorContext:
        """Return the scoped actor context for one durable identity."""
        ...


class AttentionAccessPort(Protocol):
    """Answer whether a selected actor has a plan-bound right to inspect one attention item."""

    def can_view_attention(self, actor_id: UserId, attention_id: AttentionId) -> bool:
        """Return an actor-scoped authorization answer without exposing another plan."""
        ...


@dataclass(frozen=True, slots=True)
class _RecordedAuditRun:
    """Supply an already-authorized immutable event sequence to the existing audit renderer."""

    run_id: RunId
    events: tuple[AuditEvent, ...]

    def append(self, event: AuditEvent) -> None:
        """Refuse mutation because UI audit rendering can only consume its supplied sequence."""
        raise AssertionError(f"local review must not append {event.event_type}")

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        """Return only the previously authorized run, never a caller-selected replacement."""
        return self.events if run_id == self.run_id else ()


@dataclass(slots=True)
class LocalReviewReadService:
    """Compose existing safe read services into actor-scoped presentation records."""

    actor_id: UserId
    operator_status: OperatorStatusReadPort
    attention_store: AttentionReadPort
    approvals: PlanApprovalReadPort
    workflows: WorkflowStateReadPort
    audit_ledger: AuditReadPort
    demo_clock_reader: DemoClockReadPort
    identity: IdentityReadPort
    attention_access: AttentionAccessPort

    def status(self) -> ReviewPayload:
        """Return exactly the safe scalar status representation already used by the CLI."""
        self._require_selected_actor()
        return operator_status_data(self.operator_status.read_status_for_actor(self.actor_id))

    def attention(self, attention_id: str) -> ReviewPayload:
        """Return an actor-authorized attention item and versioned evidence references only."""
        self._require_selected_actor()
        parsed_id = _attention_id_or_not_found(attention_id)
        attention = self.attention_store.load(parsed_id)
        if attention is None:
            raise LocalReviewResourceNotFoundError("attention is unavailable")
        if not self.attention_access.can_view_attention(self.actor_id, parsed_id):
            raise LocalReviewAccessDeniedError("attention belongs to another actor")
        return {
            "attention_id": str(attention.attention_id),
            "scenario": attention.scenario,
            "cause": attention.cause,
            "status": attention.status.value,
            "created_at": attention.created_at.isoformat(),
            "resolved_at": None
            if attention.resolved_at is None
            else attention.resolved_at.isoformat(),
            "evidence": [
                {"evidence_id": evidence_id, "source_version": source_version}
                for evidence_id, source_version in sorted(attention.source_versions.items())
            ],
        }

    def approval(self, approval_id: str) -> ReviewPayload:
        """Return an immutable plan-bound approval only when its actor membership is authorized."""
        self._require_selected_actor()
        parsed_id = _approval_id_or_not_found(approval_id)
        binding = self.approvals.load(parsed_id)
        if binding is None:
            raise LocalReviewResourceNotFoundError("approval is unavailable")
        plan, approval = binding
        self._require_plan_access(plan, approval)
        return _approval_data(plan, approval)

    def workflow(self, workflow_id: str) -> ReviewPayload:
        """Return safe recovery state and declared step progress without inputs, errors, or results."""
        self._require_selected_actor()
        parsed_id = _workflow_id_or_not_found(workflow_id)
        snapshot = self.workflows.load(parsed_id)
        if snapshot is None:
            raise LocalReviewResourceNotFoundError("workflow is unavailable")
        binding = self.approvals.load_for_plan(snapshot.workflow.plan_id)
        if binding is None:
            raise LocalReviewResourceNotFoundError("workflow plan is unavailable")
        plan, approval = binding
        self._require_plan_access(plan, approval)
        now = self._demo_now()
        workflow = snapshot.workflow
        return {
            "workflow_id": str(workflow.workflow_id),
            "plan_id": str(workflow.plan_id),
            "definition_name": workflow.definition_name,
            "definition_version": workflow.definition_version,
            "status": workflow.status.value,
            "current_step": workflow.current_step,
            "recovery_state": recovery_state_for(
                workflow.status,
                lease_expires_at=workflow.lease_expires_at,
                now=now,
            ).value,
            "created_at": workflow.created_at.isoformat(),
            "updated_at": workflow.updated_at.isoformat(),
            "steps": [
                {
                    "step_index": step.step_index,
                    "step_name": step.step_name,
                    "tool_name": step.tool_name,
                    "status": step.status.value,
                    "attempt_count": step.attempt_count,
                    "idempotency_key_prefix": _idempotency_key_prefix(step.idempotency_key),
                }
                for step in snapshot.steps
            ],
        }

    def audit(self, run_id: str) -> ReviewPayload:
        """Explain one whole run only after every event is authorized for the selected actor."""
        self._require_selected_actor()
        parsed_id = RunId(run_id.strip())
        if not parsed_id:
            raise LocalReviewResourceNotFoundError("audit run is unavailable")
        events = tuple(self.audit_ledger.events_for_run(parsed_id))
        if not events:
            raise LocalReviewResourceNotFoundError("audit run is unavailable")
        plan_bindings: dict[PlanId, tuple[Plan, Approval] | None] = {}
        workflow_bindings: dict[WorkflowId, tuple[Plan, Approval] | None] = {}
        attention_access: dict[AttentionId, bool] = {}
        for event in events:
            if not self._can_view_audit_event(
                event,
                plan_bindings=plan_bindings,
                workflow_bindings=workflow_bindings,
                attention_access=attention_access,
            ):
                raise LocalReviewAccessDeniedError("audit run contains another actor's evidence")
        explanation = AuditExplainer(_RecordedAuditRun(run_id=parsed_id, events=events)).explain(
            parsed_id
        )
        return {
            "run_id": str(parsed_id),
            "event_count": explanation.event_count,
            "explanation": explanation.render(),
        }

    def demo_clock(self) -> ReviewPayload:
        """Return the durable demo time without exposing a UI mutation control in this milestone."""
        self._require_selected_actor()
        return {"current_at": self._demo_now().isoformat()}

    def _require_selected_actor(self) -> None:
        """Fail closed if the configured local demo actor no longer resolves to a scoped identity."""
        try:
            self.identity.actor_for(self.actor_id)
        except LookupError as error:
            raise LocalReviewUnavailableError("selected local demo actor is unavailable") from error

    def _require_plan_access(self, plan: Plan, approval: Approval) -> None:
        """Allow review only to the plan actor, requester, or currently assigned approver."""
        if not _actor_can_view_plan(self.actor_id, plan, approval):
            raise LocalReviewAccessDeniedError("plan belongs to another actor")

    def _can_view_audit_event(
        self,
        event: AuditEvent,
        *,
        plan_bindings: dict[PlanId, tuple[Plan, Approval] | None],
        workflow_bindings: dict[WorkflowId, tuple[Plan, Approval] | None],
        attention_access: dict[AttentionId, bool],
    ) -> bool:
        """Require every rendered event to have an actor, plan, workflow, or attention authorization path."""
        resource_authorizations: list[bool] = []
        if event.plan_id is not None:
            if event.plan_id not in plan_bindings:
                plan_bindings[event.plan_id] = self.approvals.load_for_plan(event.plan_id)
            binding = plan_bindings[event.plan_id]
            resource_authorizations.append(
                binding is not None and _actor_can_view_plan(self.actor_id, *binding)
            )
        if event.workflow_id is not None:
            binding = workflow_bindings.get(event.workflow_id)
            if binding is None:
                workflow = self.workflows.load(event.workflow_id)
                binding = (
                    None
                    if workflow is None
                    else self.approvals.load_for_plan(workflow.workflow.plan_id)
                )
                workflow_bindings[event.workflow_id] = binding
            resource_authorizations.append(
                binding is not None and _actor_can_view_plan(self.actor_id, *binding)
            )
        if event.attention_id is not None:
            if event.attention_id not in attention_access:
                attention_access[event.attention_id] = self.attention_access.can_view_attention(
                    self.actor_id,
                    event.attention_id,
                )
            allowed = attention_access[event.attention_id]
            resource_authorizations.append(allowed)
        if resource_authorizations:
            return any(resource_authorizations)
        return event.actor_id == self.actor_id

    def _demo_now(self) -> datetime:
        """Translate a missing local clock into an intentionally generic optional-UI availability result."""
        try:
            return self.demo_clock_reader.now()
        except RuntimeError as error:
            raise LocalReviewUnavailableError("local demo clock is unavailable") from error


@dataclass(frozen=True, slots=True)
class UnconfiguredLocalReviewService:
    """Fail closed for imported ASGI apps that were not started through the local composition root."""

    def status(self) -> ReviewPayload:
        """Reject an unconfigured local UI status request."""
        return self._unavailable()

    def attention(self, attention_id: str) -> ReviewPayload:
        """Reject an unconfigured attention request without inspecting its ID."""
        return self._unavailable()

    def approval(self, approval_id: str) -> ReviewPayload:
        """Reject an unconfigured approval request without inspecting its ID."""
        return self._unavailable()

    def workflow(self, workflow_id: str) -> ReviewPayload:
        """Reject an unconfigured workflow request without inspecting its ID."""
        return self._unavailable()

    def audit(self, run_id: str) -> ReviewPayload:
        """Reject an unconfigured audit request without inspecting its ID."""
        return self._unavailable()

    def demo_clock(self) -> ReviewPayload:
        """Reject an unconfigured demo-clock request."""
        return self._unavailable()

    @staticmethod
    def _unavailable() -> ReviewPayload:
        """Raise one stable error that never contains configuration or credential detail."""
        raise LocalReviewUnavailableError("local review data is not configured")


def _actor_can_view_plan(actor_id: UserId, plan: Plan, approval: Approval) -> bool:
    """Treat plan actor, requester, and current approver as the only legitimate review participants."""
    return actor_id in {
        plan.actor_id,
        plan.approver_id,
        approval.requester_id,
        approval.approver_id,
    }


def _approval_data(plan: Plan, approval: Approval) -> ReviewPayload:
    """Project the durable approval binding without immutable parameters or a full plan hash."""
    return {
        "approval_id": str(approval.approval_id),
        "plan_id": str(plan.plan_id),
        "attention_id": str(plan.attention_id),
        "requester_id": str(approval.requester_id),
        "approver_id": str(approval.approver_id),
        "decision_state": approval.status.value,
        "requested_at": approval.requested_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "decided_at": None if approval.decided_at is None else approval.decided_at.isoformat(),
        "intent": plan.intent,
        "workflow_name": plan.workflow_name,
        "workflow_version": plan.workflow_version,
        "policy_version": plan.policy_version,
        "source_versions": dict(sorted(plan.source_versions.items())),
    }


def _attention_id_or_not_found(value: str) -> AttentionId:
    """Validate an opaque attention identifier before a PostgreSQL UUID cast can reject it noisily."""
    return AttentionId(_uuid_or_not_found(value))


def _approval_id_or_not_found(value: str) -> ApprovalId:
    """Validate an opaque approval identifier before a PostgreSQL UUID cast can reject it noisily."""
    return ApprovalId(_uuid_or_not_found(value))


def _workflow_id_or_not_found(value: str) -> WorkflowId:
    """Validate an opaque workflow identifier before a PostgreSQL UUID cast can reject it noisily."""
    return WorkflowId(_uuid_or_not_found(value))


def _uuid_or_not_found(value: str) -> str:
    """Normalize a UUID-shaped path segment or return the public missing-resource outcome."""
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise LocalReviewResourceNotFoundError("review resource is unavailable") from error


def _idempotency_key_prefix(value: str | None) -> str:
    """Expose a short correlation marker while withholding full tool inputs and workflow payloads."""
    return "not started" if not value else value[:24]
