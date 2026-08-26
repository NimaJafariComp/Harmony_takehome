"""Terminal status contracts for explicit audit run identifiers."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

import pytest
from rich.console import Console

from enterprise_agent import cli
from enterprise_agent.application.operator_status import (
    OperatorStatusSnapshot,
    PendingApprovalStatus,
)
from enterprise_agent.presentation import TerminalPresenter, TerminalTheme

pytestmark = pytest.mark.unit


def test_status_labels_each_audit_explain_target_as_a_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit command arguments cannot be mistaken for approval or workflow UUIDs."""
    output = StringIO()
    presenter = TerminalPresenter(
        console=Console(file=output, force_terminal=False, color_system=None, width=120),
        theme=TerminalTheme(),
    )
    monkeypatch.setattr(cli, "_terminal_presenter", lambda: presenter)
    snapshot = OperatorStatusSnapshot(
        pending_approvals=(
            _approval("demo-scenario-a-reroute"),
            _approval("demo-scenario-c-pending"),
        ),
        workflows=(),
    )

    cli._render_operator_status(snapshot)

    rendered = output.getvalue()
    assert "Run ID" in rendered
    assert "demo-scenario-a-reroute" in rendered
    assert "demo-scenario-c-pending" in rendered
    assert "enterprise-agent audit explain demo-scenario-a-reroute" in rendered
    assert "enterprise-agent audit explain demo-scenario-c-pending" in rendered


def _approval(run_id: str) -> PendingApprovalStatus:
    """Create a controlled status row that isolates the audit-command label contract."""
    return PendingApprovalStatus(
        approval_id=f"approval-{run_id}",
        plan_id=f"plan-{run_id}",
        requester="Dana Buyer",
        approver="Dana Buyer",
        decision_state="pending",
        expires_at=datetime(2026, 8, 27, 13, tzinfo=UTC).isoformat(),
        audit_run_id=run_id,
    )
