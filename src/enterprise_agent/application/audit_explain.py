"""Read-only, human-readable Scenario A reconstruction from audit events alone."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from enterprise_agent.domain import AuditEvent, RunId
from enterprise_agent.ports import AuditPort


class AuditExplanationError(ValueError):
    """Raised when an audit history cannot safely support a truthful reconstruction."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditExplanation:
    """One immutable chronological narrative derived without any live-system lookup."""

    run_id: RunId
    lines: tuple[str, ...]

    @property
    def event_count(self) -> int:
        """Return the number of ledger events represented by this explanation."""
        return len(self.lines)

    def render(self) -> str:
        """Format an operator-facing explanation without adding non-audit-derived claims."""
        heading = f"Audit explanation for run {self.run_id} ({self.event_count} events)"
        return "\n".join((heading, *self.lines))


class AuditExplainer:
    """Turn a single run's append-only ledger into a safe chronological Scenario A story."""

    def __init__(self, audit: AuditPort) -> None:
        """Depend only on the audit port so explanation cannot query live business state."""
        self._audit = audit

    def explain(self, run_id: RunId) -> AuditExplanation:
        """Read and render one self-contained run ledger or fail instead of inventing missing facts."""
        events = tuple(self._audit.events_for_run(run_id))
        if not events:
            raise AuditExplanationError(f"no audit events exist for run {run_id}")
        if any(event.run_id != run_id for event in events):
            raise AuditExplanationError("audit ledger returned events for a different run")
        ordered_events = tuple(
            sorted(events, key=lambda event: (event.occurred_at, str(event.event_id)))
        )
        lines = tuple(_render_event(event) for event in ordered_events)
        return AuditExplanation(run_id=run_id, lines=lines)


def _render_event(event: AuditEvent) -> str:
    """Render one supported material event using only its explicit safe narrative fields."""
    _require_timezone(event.occurred_at)
    description = _describe(event)
    return f"{event.occurred_at.isoformat()} | {description}"


def _describe(event: AuditEvent) -> str:
    """Map the controlled audit vocabulary to concise prose without rendering arbitrary payloads."""
    payload = event.payload
    match event.event_type:
        case "attention.detected":
            return (
                "Detected stockout risk for part "
                f"{_text(payload, 'part_id')} affecting production order "
                f"{_text(payload, 'production_order_id')}."
            )
        case "attention.deduplicated":
            return "Deduplicated a repeated stockout detection without creating duplicate work."
        case "context.gathered":
            return f"Gathered {_count(payload, 'evidence_count')} authorized evidence records for planning."
        case "evidence.observed":
            return f"Recorded {_count(payload, 'evidence_count')} evidence references."
        case "llm.completed":
            return (
                f"LLM provider {_text(payload, 'provider')} completed "
                f"{_text(payload, 'response_schema')} using {_text(payload, 'model')} "
                f"with status {_text(payload, 'status')}."
            )
        case "planner.recommended":
            return (
                f"Planner recommended {_text(payload, 'outcome')} using "
                f"{_text(payload, 'workflow_name')}."
            )
        case "gate.allowed":
            return f"Gate allowed the proposed action at estimated value {_text(payload, 'estimated_value')}."
        case "gate.denied":
            return f"Gate denied the proposed action: {_text(payload, 'reason')}."
        case "approval.requested":
            return f"Requested approval from {_text(payload, 'approver_id')}."
        case "approval.rerouted":
            return f"Rerouted approval to backup approver {_text(payload, 'approver_id')}."
        case "approval.approved" | "approval.rejected":
            decision = event.event_type.rsplit(".", maxsplit=1)[1]
            return f"Approval was {decision} by {_text(payload, 'approver_id')}."
        case "workflow.started":
            return f"Started workflow {_text(payload, 'workflow_name')}."
        case "workflow.step_started":
            return f"Workflow started {_text(payload, 'step_name')}."
        case "workflow.step_completed":
            return f"Workflow completed {_text(payload, 'step_name')}: {_text(payload, 'result')}."
        case "workflow.failed":
            return f"Workflow failed: {_text(payload, 'failure_category')}."
        case "tool.started":
            return f"Started tool {_text(payload, 'tool_name')}."
        case "tool.succeeded":
            return _tool_succeeded(payload)
        case "tool.failed":
            return (
                f"Tool {_text(payload, 'tool_name')} failed: {_text(payload, 'failure_category')}."
            )
        case "compensation.started":
            return f"Started compensation for {_text(payload, 'tool_name')}."
        case "compensation.completed":
            return f"Completed compensation for {_text(payload, 'tool_name')}."
        case "schedule.created":
            return f"Scheduled {_text(payload, 'task_type')} for {_text(payload, 'due_at')}."
        case "schedule.fired":
            return f"Fired scheduled {_text(payload, 'task_type')} work."
        case "followup.resolved":
            return (
                "Confirmed receipt for replacement PO "
                f"{_text(payload, 'purchase_order_id')} and resolved the follow-up."
            )
        case "followup.reopened":
            return (
                "Receipt is still missing for replacement PO "
                f"{_text(payload, 'purchase_order_id')}; reopened the follow-up."
            )
        case "attention.status_changed":
            return (
                f"Changed attention status from {_text(payload, 'from_status')} to "
                f"{_text(payload, 'to_status')}."
            )
        case _:
            raise AuditExplanationError(f"unsupported audit event type: {event.event_type}")


def _tool_succeeded(payload: Mapping[str, object]) -> str:
    """Describe the bounded Scenario A replacement effect without exposing arbitrary tool output."""
    tool_name = _text(payload, "tool_name")
    if tool_name == "create_replacement_po":
        return "Created replacement PO using create_replacement_po."
    return f"Tool {tool_name} succeeded."


def _text(payload: Mapping[str, object], name: str) -> str:
    """Return one printable scalar field or an explicit unknown marker without traversing raw data."""
    value = payload.get(name)
    if isinstance(value, str) and value.strip():
        return value.replace("\n", " ").replace("\r", " ")[:160]
    if isinstance(value, int | float):
        return str(value)
    return "unknown"


def _count(payload: Mapping[str, object], name: str) -> str:
    """Read a non-negative event count without interpreting arbitrary payload objects as numbers."""
    value = payload.get(name)
    return str(value) if isinstance(value, int) and value >= 0 else "unknown"


def _require_timezone(value: datetime) -> None:
    """Reject ambiguous persisted chronology rather than silently treating it as local wall time."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise AuditExplanationError("audit event time must include a timezone")
