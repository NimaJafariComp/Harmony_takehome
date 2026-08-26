"""Durable Tuesday receipt checking and causal Scenario A follow-up creation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

from enterprise_agent.domain import (
    AttentionId,
    AttentionItem,
    AttentionRegistration,
    AttentionStatus,
    Evidence,
    RunId,
    ScheduledTask,
    ScheduledTaskStatus,
    UserId,
)
from enterprise_agent.ports import ErpPort, EvidenceQuery, IdentityPort

ARRIVAL_CHECK_TASK_TYPE = "arrival_check"


class ArrivalCheckOutcome(StrEnum):
    """The bounded consequences of processing one safely claimed Tuesday task."""

    RESOLVED = "resolved"
    REOPENED = "reopened"
    NOT_ACTIONABLE = "not_actionable"
    MISSING_ATTENTION = "missing_attention"


@dataclass(frozen=True, slots=True, kw_only=True)
class ArrivalCheckResult:
    """One immutable receipt-check decision and its resulting attention state, if any."""

    outcome: ArrivalCheckOutcome
    attention: AttentionItem | None
    followup: AttentionItem | None


class ArrivalCheckError(ValueError):
    """Raised when a claimed arrival task or its scoped ERP evidence is not safe to process."""


class _ArrivalCheckAttentionPort(Protocol):
    """Keep the worker dependent only on attention lifecycle and causal-follow-up operations."""

    def load(self, attention_id: AttentionId) -> AttentionItem | None:
        """Return the current original attention item, if it still exists."""
        ...

    def transition(
        self,
        attention: AttentionItem,
        target: AttentionStatus,
        run_id: RunId,
        occurred_at: datetime,
    ) -> AttentionItem:
        """Atomically advance one current attention lifecycle state."""
        ...

    def register_arrival_followup(
        self,
        *,
        original_attention: AttentionItem,
        purchase_order: Evidence,
        run_id: RunId,
        detected_at: datetime,
    ) -> AttentionRegistration:
        """Create or return the exact deduplicated missing-receipt follow-up attention item."""
        ...


class TuesdayArrivalCheckService:
    """Resolve a delivered replacement PO or open a distinct missing-receipt attention loop."""

    def __init__(
        self,
        *,
        erp: ErpPort,
        identity: IdentityPort,
        attention: _ArrivalCheckAttentionPort,
    ) -> None:
        """Use only current authorized ERP evidence and durable attention operations."""
        self._erp = erp
        self._identity = identity
        self._attention = attention

    def handle_claimed_task(
        self,
        task: ScheduledTask,
        *,
        checked_at: datetime,
        run_id: RunId,
    ) -> ArrivalCheckResult:
        """Process one due leased task without inferring a receipt from an expected delivery date."""
        _validate_claimed_arrival_task(task, checked_at)
        purchase_order_id, original_attention_id, actor_id = _task_binding(task)
        original_attention = self._attention.load(original_attention_id)
        if original_attention is None:
            return ArrivalCheckResult(
                outcome=ArrivalCheckOutcome.MISSING_ATTENTION,
                attention=None,
                followup=None,
            )
        if original_attention.status in {AttentionStatus.RESOLVED, AttentionStatus.CANCELLED}:
            return ArrivalCheckResult(
                outcome=ArrivalCheckOutcome.NOT_ACTIONABLE,
                attention=original_attention,
                followup=None,
            )

        actor = self._identity.actor_for(actor_id)
        purchase_order = _current_purchase_order(
            self._erp.query(
                actor,
                EvidenceQuery(
                    record_types=frozenset({"purchase_order"}),
                    record_ids=frozenset({purchase_order_id}),
                ),
            ),
            purchase_order_id,
        )
        if _has_full_receipt(purchase_order):
            resolved = self._attention.transition(
                original_attention,
                AttentionStatus.RESOLVED,
                run_id,
                checked_at,
            )
            return ArrivalCheckResult(
                outcome=ArrivalCheckOutcome.RESOLVED,
                attention=resolved,
                followup=None,
            )

        registration = self._attention.register_arrival_followup(
            original_attention=original_attention,
            purchase_order=purchase_order,
            run_id=run_id,
            detected_at=checked_at,
        )
        return ArrivalCheckResult(
            outcome=ArrivalCheckOutcome.REOPENED,
            attention=original_attention,
            followup=registration.attention,
        )


def _validate_claimed_arrival_task(task: ScheduledTask, checked_at: datetime) -> None:
    """Require exact task type, durable claim, live lease, due time, and unambiguous timestamps."""
    _require_timezone(checked_at, name="check time")
    _require_timezone(task.due_at, name="task due time")
    if task.task_type != ARRIVAL_CHECK_TASK_TYPE:
        raise ArrivalCheckError("scheduled task is not an arrival check")
    if task.status is not ScheduledTaskStatus.CLAIMED or task.lease_expires_at is None:
        raise ArrivalCheckError("arrival check task must hold a scheduler lease")
    if task.due_at > checked_at:
        raise ArrivalCheckError("arrival check task is not due")
    if task.lease_expires_at <= checked_at:
        raise ArrivalCheckError("arrival check task lease has expired")


def _task_binding(task: ScheduledTask) -> tuple[str, AttentionId, UserId]:
    """Extract the immutable causal bindings required to process one arrival check safely."""
    purchase_order_id = _required_payload_text(task, "purchase_order_id")
    original_attention_id = AttentionId(_required_payload_text(task, "original_attention_id"))
    actor_id = UserId(_required_payload_text(task, "actor_id"))
    return purchase_order_id, original_attention_id, actor_id


def _required_payload_text(task: ScheduledTask, name: str) -> str:
    """Read a nonblank causal task payload field without accepting invented defaults."""
    value = task.payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ArrivalCheckError(f"arrival check task lacks {name}")
    return value


def _current_purchase_order(evidence: Sequence[Evidence], purchase_order_id: str) -> Evidence:
    """Require one exact authorized current PO record rather than guessing from other ERP facts."""
    matches = tuple(
        item
        for item in evidence
        if item.record_type == "purchase_order" and item.record_id == purchase_order_id
    )
    if len(matches) != 1:
        raise ArrivalCheckError("arrival check requires one current purchase-order evidence record")
    return matches[0]


def _has_full_receipt(purchase_order: Evidence) -> bool:
    """Treat only a nonzero full received quantity as explicit arrival confirmation."""
    ordered_quantity = _required_nonnegative_decimal(purchase_order, "ordered_quantity")
    received_quantity = _required_nonnegative_decimal(purchase_order, "received_quantity")
    if ordered_quantity == Decimal():
        raise ArrivalCheckError("arrival check purchase order has zero ordered quantity")
    return received_quantity >= ordered_quantity


def _required_nonnegative_decimal(purchase_order: Evidence, name: str) -> Decimal:
    """Parse receipt quantities strictly so malformed ERP evidence cannot resolve an attention item."""
    try:
        value = Decimal(str(purchase_order.payload[name]))
    except (KeyError, InvalidOperation, ValueError) as error:
        raise ArrivalCheckError(f"arrival check purchase order has invalid {name}") from error
    if not value.is_finite() or value < Decimal():
        raise ArrivalCheckError(f"arrival check purchase order has invalid {name}")
    return value


def _require_timezone(value: datetime, *, name: str) -> None:
    """Reject naive business timestamps before durable time policy can be silently reinterpreted."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArrivalCheckError(f"{name} must include a timezone")
