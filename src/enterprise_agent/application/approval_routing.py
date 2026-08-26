"""Deterministic end-of-day routing of unanswered approvals to an authorized backup."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from enterprise_agent.domain import (
    Approval,
    ApprovalId,
    ApprovalStatus,
    DateRange,
    Evidence,
    Plan,
    RunId,
    ScheduledTask,
    ScheduledTaskId,
    ScheduledTaskStatus,
    Scope,
    UserId,
)
from enterprise_agent.ports import (
    AuditPort,
    CalendarPort,
    EvidenceQuery,
    IdentityPort,
    PlanApprovalPort,
    SchedulerPort,
)

from .audit_trail import append_material_audit_event

APPROVAL_ESCALATION_TASK_TYPE = "approval_escalation"
END_OF_BUSINESS_DAY = time(hour=17)
_APPROVAL_DECIDE_SCOPE = Scope("approval:decide")


class ApprovalRoutingOutcome(StrEnum):
    """The complete bounded result vocabulary for one claimed escalation task."""

    REROUTED = "rerouted"
    NOT_PENDING = "not_pending"
    EXPIRED = "expired"
    STALE_TASK = "stale_task"
    MISSING_APPROVAL = "missing_approval"
    NO_BACKUP = "no_backup"
    BACKUP_NOT_AUTHORIZED = "backup_not_authorized"
    ORIGINAL_APPROVER_AVAILABLE = "original_approver_available"
    RACE_LOST = "race_lost"


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRoutingResult:
    """One immutable routing decision and the approval changed by its final compare-and-swap."""

    outcome: ApprovalRoutingOutcome
    approval: Approval | None


class ApprovalRoutingError(ValueError):
    """Raised when a claimed task is not a valid, due approval-escalation request."""


class ApprovalRoutingService:
    """Schedule and process only the approved, calendar-evidenced backup-routing policy."""

    def __init__(
        self,
        approvals: PlanApprovalPort,
        identity: IdentityPort,
        calendar: CalendarPort,
        scheduler: SchedulerPort,
        *,
        audit: AuditPort | None = None,
    ) -> None:
        """Depend only on durable state, current identities, scoped availability, and scheduling ports."""
        self._approvals = approvals
        self._identity = identity
        self._calendar = calendar
        self._scheduler = scheduler
        self._audit = audit

    def schedule_escalation(
        self, approval: Approval, *, run_id: RunId | None = None
    ) -> ScheduledTask:
        """Persist the deterministic same-day check paired with an unanswered approval request."""
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalRoutingError("only a pending approval may receive an escalation task")
        _require_timezone(approval.requested_at, name="approval request time")
        payload: dict[str, object] = {
            "approval_id": str(approval.approval_id),
            "original_approver_id": str(approval.approver_id),
            "plan_hash": approval.plan_hash,
        }
        if run_id is not None:
            payload["audit_run_id"] = str(run_id)
        task = ScheduledTask(
            task_id=ScheduledTaskId(
                str(uuid5(NAMESPACE_URL, f"approval-escalation:v1:{approval.approval_id}"))
            ),
            task_type=APPROVAL_ESCALATION_TASK_TYPE,
            due_at=_end_of_business_day(approval.requested_at),
            status=ScheduledTaskStatus.PENDING,
            idempotency_key=f"approval-escalation:v1:{approval.approval_id}",
            payload=payload,
            attempt_count=0,
            lease_expires_at=None,
            completed_at=None,
        )
        self._scheduler.schedule(task)
        if self._audit is not None and run_id is not None:
            append_material_audit_event(
                self._audit,
                event_type="schedule.created",
                run_id=run_id,
                occurred_at=approval.requested_at + timedelta(microseconds=6),
                actor_id=approval.requester_id,
                plan_id=approval.plan_id,
                payload={
                    "task_type": task.task_type,
                    "due_at": task.due_at.isoformat(),
                },
                idempotency_key=task.idempotency_key,
                plan_hash=approval.plan_hash,
            )
        return task

    def handle_claimed_task(
        self, task: ScheduledTask, *, routed_at: datetime
    ) -> ApprovalRoutingResult:
        """Reroute a due claimed task only when current policy and its persisted facts all agree."""
        _validate_claimed_escalation(task, routed_at)
        approval_id, original_approver_id, plan_hash = _task_binding(task)
        audit_run_id = _audit_run_id(task)
        if self._audit is not None and audit_run_id is not None:
            append_material_audit_event(
                self._audit,
                event_type="schedule.fired",
                run_id=audit_run_id,
                occurred_at=routed_at,
                actor_id=original_approver_id,
                payload={"task_type": task.task_type},
                idempotency_key=task.idempotency_key,
                plan_hash=plan_hash,
            )
        record = self._approvals.load(approval_id)
        if record is None:
            return ApprovalRoutingResult(
                outcome=ApprovalRoutingOutcome.MISSING_APPROVAL,
                approval=None,
            )
        plan, approval = record
        if approval.status is not ApprovalStatus.PENDING:
            return ApprovalRoutingResult(outcome=ApprovalRoutingOutcome.NOT_PENDING, approval=None)
        if (
            approval.plan_hash != plan_hash
            or plan.plan_hash != plan_hash
            or approval.approver_id != original_approver_id
            or plan.approver_id != original_approver_id
        ):
            return ApprovalRoutingResult(outcome=ApprovalRoutingOutcome.STALE_TASK, approval=None)
        if approval.expires_at <= routed_at or plan.expires_at <= routed_at:
            return ApprovalRoutingResult(outcome=ApprovalRoutingOutcome.EXPIRED, approval=None)

        original_approver = self._identity.actor_for(original_approver_id)
        backup_approver_id = original_approver.backup_approver_id
        if backup_approver_id is None:
            return ApprovalRoutingResult(outcome=ApprovalRoutingOutcome.NO_BACKUP, approval=None)
        backup_approver = self._identity.actor_for(backup_approver_id)
        if not _can_decide(backup_approver.approval_limits, backup_approver.scopes, plan):
            return ApprovalRoutingResult(
                outcome=ApprovalRoutingOutcome.BACKUP_NOT_AUTHORIZED,
                approval=None,
            )

        next_day = task.due_at.date() + timedelta(days=1)
        calendar_events = self._calendar.query(
            original_approver,
            EvidenceQuery(
                record_types=frozenset({"calendar_event"}),
                date_range=DateRange(start=next_day, end=next_day),
            ),
        )
        if not any(_is_out_of_office(event, next_day) for event in calendar_events):
            return ApprovalRoutingResult(
                outcome=ApprovalRoutingOutcome.ORIGINAL_APPROVER_AVAILABLE,
                approval=None,
            )

        routed = self._approvals.reroute(
            approval_id,
            expected_plan_hash=plan_hash,
            original_approver_id=original_approver_id,
            backup_approver_id=backup_approver_id,
            routed_at=routed_at,
        )
        if routed is None:
            return ApprovalRoutingResult(outcome=ApprovalRoutingOutcome.RACE_LOST, approval=None)
        if self._audit is not None and audit_run_id is not None:
            append_material_audit_event(
                self._audit,
                event_type="approval.rerouted",
                run_id=audit_run_id,
                occurred_at=routed_at + timedelta(microseconds=1),
                actor_id=backup_approver_id,
                plan_id=plan.plan_id,
                payload={"approver_id": str(backup_approver_id)},
                plan_hash=plan.plan_hash,
            )
        return ApprovalRoutingResult(outcome=ApprovalRoutingOutcome.REROUTED, approval=routed)


def _end_of_business_day(requested_at: datetime) -> datetime:
    """Return the fixed local-demo end-of-day moment on the approval's calendar date."""
    return requested_at.replace(
        hour=END_OF_BUSINESS_DAY.hour,
        minute=END_OF_BUSINESS_DAY.minute,
        second=0,
        microsecond=0,
    )


def _validate_claimed_escalation(task: ScheduledTask, routed_at: datetime) -> None:
    """Reject arbitrary, unclaimed, premature, or timestamp-ambiguous scheduler input."""
    _require_timezone(routed_at, name="routing time")
    if task.task_type != APPROVAL_ESCALATION_TASK_TYPE:
        raise ApprovalRoutingError("scheduled task is not an approval escalation")
    if task.status is not ScheduledTaskStatus.CLAIMED or task.lease_expires_at is None:
        raise ApprovalRoutingError("approval escalation task must hold a scheduler lease")
    if task.due_at > routed_at:
        raise ApprovalRoutingError("approval escalation task is not due")
    if task.lease_expires_at <= routed_at:
        raise ApprovalRoutingError("approval escalation task lease has expired")


def _task_binding(task: ScheduledTask) -> tuple[ApprovalId, UserId, str]:
    """Extract exact immutable approval binding fields without accepting arbitrary task payloads."""
    approval_id = _required_payload_text(task, "approval_id")
    original_approver_id = _required_payload_text(task, "original_approver_id")
    plan_hash = _required_payload_text(task, "plan_hash")
    return (ApprovalId(approval_id), UserId(original_approver_id), plan_hash)


def _required_payload_text(task: ScheduledTask, name: str) -> str:
    """Read one nonblank task payload value before it can direct an approval mutation."""
    value = task.payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ApprovalRoutingError(f"approval escalation task lacks {name}")
    return value


def _audit_run_id(task: ScheduledTask) -> RunId | None:
    """Read an optional durable audit correlation without weakening task business bindings."""
    value = task.payload.get("audit_run_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApprovalRoutingError("approval escalation task has an invalid audit_run_id")
    return RunId(value)


def _can_decide(
    approval_limits: Mapping[str, Decimal], scopes: frozenset[Scope], plan: Plan
) -> bool:
    """Require explicit approval authority and enough current currency capacity for this plan value."""
    if _APPROVAL_DECIDE_SCOPE not in scopes:
        return False
    amount_value = plan.parameters.get("estimated_value_amount")
    currency_value = plan.parameters.get("estimated_value_currency")
    if not isinstance(currency_value, str):
        return False
    try:
        amount = Decimal(str(amount_value))
    except (InvalidOperation, ValueError):
        return False
    normalized_currency = currency_value.upper()
    limit = approval_limits.get(normalized_currency)
    return isinstance(limit, Decimal) and Decimal(0) <= amount <= limit


def _is_out_of_office(event: Evidence, day: date) -> bool:
    """Recognize only scoped calendar absence evidence that covers the exact following calendar day."""
    if event.record_type != "calendar_event" or event.payload.get("event_type") != "out_of_office":
        return False
    ends_at = event.payload.get("ends_at")
    end_date = ends_at.date() if isinstance(ends_at, datetime) else event.observed_at.date()
    return event.observed_at.date() <= day <= end_date


def _require_timezone(value: datetime, *, name: str) -> None:
    """Reject naive business timestamps before date policy can be silently interpreted differently."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalRoutingError(f"{name} must include a timezone")
