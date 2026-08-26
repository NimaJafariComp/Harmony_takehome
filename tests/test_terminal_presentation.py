"""Terminal presentation component tests."""

from datetime import UTC, datetime
from io import StringIO

import pytest
from rich.console import Console

from enterprise_agent.presentation.terminal import (
    ApprovalSummary,
    AuditTimelineEntry,
    BoundedProgress,
    ConfirmationSummary,
    EvidenceDisposition,
    EvidenceSummary,
    StatusSummary,
    TerminalPresenter,
    TerminalState,
    TerminalTheme,
    WorkflowSummary,
)

pytestmark = pytest.mark.unit


def _presenter(*, width: int = 88) -> tuple[TerminalPresenter, StringIO]:
    """Create an injected, deterministic console for semantic output assertions."""
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=width)
    return TerminalPresenter(console=console, theme=TerminalTheme()), output


def test_presenter_renders_a_header_and_pending_status_with_next_action() -> None:
    """An operator can understand a pending result without relying on color or spacing."""
    presenter, output = _presenter()

    presenter.render_header(title="Supplier risk review", subtitle="Deterministic Scenario C")
    presenter.render_status(
        StatusSummary(
            state=TerminalState.PENDING_APPROVAL,
            summary="Plan awaits Dana's approval",
            next_action="Review approval 00000000-0000-0000-0000-000000000802",
        )
    )

    rendered = output.getvalue()
    assert "Supplier risk review" in rendered
    assert "Deterministic Scenario C" in rendered
    assert "Pending approval" in rendered
    assert "Plan awaits Dana's approval" in rendered
    assert "Review approval 00000000-0000-0000-0000-000000000802" in rendered


def test_presenter_keeps_operational_ids_and_evidence_dispositions_copyable() -> None:
    """Approval, workflow, evidence, and audit views retain safe operator-facing facts."""
    presenter, output = _presenter(width=54)
    approval_id = "00000000-0000-0000-0000-000000000802"
    workflow_id = "00000000-0000-0000-0000-000000000803"

    presenter.render_approvals(
        (
            ApprovalSummary(
                approval_id=approval_id,
                plan_id="00000000-0000-0000-0000-000000000801",
                requester="Purchasing agent",
                approver="Dana",
                decision_state="pending",
                expires_at="2026-08-27T17:00:00+00:00",
            ),
        )
    )
    presenter.render_workflows(
        (
            WorkflowSummary(
                workflow_id=workflow_id,
                status="awaiting_approval",
                current_step="approval",
                idempotency_key_prefix="hold-notify-801",
                recovery_state="not_required",
            ),
        )
    )
    presenter.render_evidence(
        (
            EvidenceSummary(
                evidence_id="email-ship-001",
                source="supplier email",
                summary="Shipment will miss Tuesday receipt",
                disposition=EvidenceDisposition.INCLUDED,
            ),
            EvidenceSummary(
                evidence_id="email-ship-000",
                source="supplier email",
                summary="Superseded by a newer shipment update",
                disposition=EvidenceDisposition.EXCLUDED,
            ),
        )
    )
    presenter.render_audit_timeline(
        (
            AuditTimelineEntry(
                occurred_at=datetime(2026, 8, 26, 12, tzinfo=UTC),
                event="approval.requested",
                summary="Dana must approve before the PO hold can execute",
            ),
        )
    )

    rendered = output.getvalue()
    assert approval_id in rendered
    assert workflow_id in rendered
    assert "email-ship-001" in rendered
    assert "included" in rendered
    assert "email-ship-000" in rendered
    assert "excluded" in rendered
    assert "approval.requested" in rendered
    assert "2026-08-26T12:00:00+00:00" in rendered


def test_presenter_renders_bounded_progress_and_rejects_invalid_bounds() -> None:
    """Local progress always exposes an honest finite completion range."""
    presenter, output = _presenter()

    presenter.render_progress(
        BoundedProgress(label="Staging deterministic review", completed=2, total=3)
    )

    rendered = output.getvalue()
    assert "Staging deterministic review" in rendered
    assert "2/3" in rendered

    with pytest.raises(ValueError, match="total must be positive"):
        BoundedProgress(label="Invalid", completed=0, total=0)
    with pytest.raises(ValueError, match="cannot exceed total"):
        BoundedProgress(label="Invalid", completed=4, total=3)


def test_presenter_renders_a_consequential_action_receipt() -> None:
    """Interactive commands can name the effect and freshness consequence before confirmation."""
    presenter, output = _presenter()

    presenter.render_confirmation(
        ConfirmationSummary(
            action="Stage Scenario C review",
            target="the local synthetic supplier-risk scenario",
            effect="Creates one pending approval and workflow; it does not hold a purchase order.",
            freshness="Execution will revalidate the plan's evidence after approval.",
            write_consequence="Writes only to the local demo database.",
            confirmation_word="stage",
        )
    )

    rendered = output.getvalue()
    assert "Action" in rendered
    assert "Stage Scenario C review" in rendered
    assert "the local synthetic supplier-risk scenario" in rendered
    assert "does not hold a purchase order" in rendered
    assert "revalidate the plan's evidence" in rendered
    assert "Type stage to continue or cancel to stop" in rendered
