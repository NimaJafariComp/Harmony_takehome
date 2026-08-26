"""Safe, read-only operator summaries for the local terminal control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from enterprise_agent.domain import WorkflowStatus


class RecoveryState(StrEnum):
    """Operator-facing recovery categories that deliberately omit internal failure details."""

    APPROVAL_REQUIRED = "approval_required"
    IN_PROGRESS = "in_progress"
    RECLAIMABLE = "reclaimable"
    RECOVERY_REQUIRED = "recovery_required"
    COMPENSATING = "compensating"
    NOT_REQUIRED = "not_required"

    @property
    def label(self) -> str:
        """Render a readable state without asking the presentation layer to infer semantics."""
        return self.replace("_", " ")


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingApprovalStatus:
    """A copyable approval summary with an optional audit-only continuation path."""

    approval_id: str
    plan_id: str
    requester: str
    approver: str
    decision_state: str
    expires_at: str
    audit_run_id: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowStatusSummary:
    """A safe workflow state summary without plan inputs, tool results, or error bodies."""

    workflow_id: str
    status: str
    current_step: str
    idempotency_key_prefix: str
    recovery_state: RecoveryState


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorStatusSnapshot:
    """The complete read-only state needed by the terminal overview command."""

    pending_approvals: tuple[PendingApprovalStatus, ...]
    workflows: tuple[WorkflowStatusSummary, ...]


def recovery_state_for(
    status: WorkflowStatus,
    *,
    lease_expires_at: datetime | None,
    now: datetime | None,
) -> RecoveryState:
    """Classify recovery needs from durable state and persisted demo time, never wall-clock time."""
    if status is WorkflowStatus.PENDING:
        return RecoveryState.APPROVAL_REQUIRED
    if status is WorkflowStatus.FAILED:
        return RecoveryState.RECOVERY_REQUIRED
    if status is WorkflowStatus.COMPENSATING:
        return RecoveryState.COMPENSATING
    if status is WorkflowStatus.RUNNING:
        if lease_expires_at is None:
            return RecoveryState.RECOVERY_REQUIRED
        if now is not None and lease_expires_at <= now:
            return RecoveryState.RECLAIMABLE
        return RecoveryState.IN_PROGRESS
    return RecoveryState.NOT_REQUIRED
