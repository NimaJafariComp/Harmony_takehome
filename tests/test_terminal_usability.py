"""Semantic CLI usability contracts without brittle visual snapshots."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.application.operator_status import (
    OperatorStatusSnapshot,
    PendingApprovalStatus,
    RecoveryState,
    WorkflowStatusSummary,
)

pytestmark = pytest.mark.unit

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _snapshot() -> OperatorStatusSnapshot:
    """Return copyable, safe control-plane facts without requiring a database service."""
    return OperatorStatusSnapshot(
        pending_approvals=(
            PendingApprovalStatus(
                approval_id="00000000-0000-0000-0000-000000000802",
                plan_id="00000000-0000-0000-0000-000000000801",
                requester="Dana Buyer",
                approver="Avery Backup",
                decision_state="rerouted",
                expires_at="2026-08-27T17:00:00+00:00",
                audit_run_id="run-terminal-usability",
            ),
        ),
        workflows=(
            WorkflowStatusSummary(
                workflow_id="00000000-0000-0000-0000-000000000803",
                status="running",
                current_step="create replacement purchase order",
                idempotency_key_prefix="replacement-po-803",
                recovery_state=RecoveryState.RECLAIMABLE,
            ),
        ),
    )


def _install_status_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep status-output contracts independent of PostgreSQL and prompt behavior."""

    class Reader:
        """Return a controlled read-model snapshot only."""

        def __init__(self, _: str) -> None:
            pass

        def read_status(self) -> OperatorStatusSnapshot:
            return _snapshot()

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "PostgresOperatorStatusAdapter", Reader)
    monkeypatch.setattr(
        "typer.prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("read path must not prompt")),
    )


def test_guide_json_is_one_stable_envelope_without_human_decoration() -> None:
    """Automation can discover commands from one parseable, versioned stdout object."""
    result = CliRunner().invoke(cli.app, ["--output", "json", "guide"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "schema_version": 1,
        "status": "succeeded",
        "summary": "Reviewer command guide.",
        "data": {
            "commands": [
                {
                    "command": "enterprise-agent demo --list",
                    "purpose": "See selectable local deterministic safety cases before resetting demo data.",
                },
                {
                    "command": "make demo",
                    "purpose": "Run the unattended local safety tour with no LLM provider call.",
                },
                {
                    "command": "enterprise-agent status",
                    "purpose": "Inspect pending approvals plus workflow and recovery state.",
                },
                {
                    "command": "enterprise-agent audit explain RUN_ID",
                    "purpose": "Reconstruct one run from the append-only audit ledger.",
                },
                {
                    "command": "enterprise-agent llm-usage",
                    "purpose": "Read recorded token and cost totals without a provider request.",
                },
            ],
            "shell_completion": "enterprise-agent --install-completion",
        },
        "next_actions": ["enterprise-agent demo --list"],
        "error": None,
    }


def test_status_json_and_no_color_preserve_semantics_and_copyable_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY formatting choices never remove status, recovery, audit, or durable identifiers."""
    _install_status_reader(monkeypatch)
    runner = CliRunner()

    json_result = runner.invoke(cli.app, ["--output", "json", "status"])
    no_color_result = runner.invoke(cli.app, ["--no-color", "status"], color=True)

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["status"] == "pending_approval"
    assert payload["data"]["pending_approvals"][0]["approval_id"] == (
        "00000000-0000-0000-0000-000000000802"
    )
    assert payload["data"]["pending_approvals"][0]["audit_run_id"] == "run-terminal-usability"
    assert payload["data"]["workflows"][0]["workflow_id"] == (
        "00000000-0000-0000-0000-000000000803"
    )
    assert payload["data"]["workflows"][0]["recovery_state"] == "reclaimable"
    assert payload["next_actions"] == [
        "enterprise-agent audit explain run-terminal-usability",
        "enterprise-agent llm-usage",
    ]
    assert no_color_result.exit_code == 0
    assert not _ANSI_ESCAPE.search(no_color_result.stdout)
    for text in (
        "Pending approval",
        "00000000-0000-0000-0000-000000000802",
        "00000000-0000-0000-0000-000000000803",
        "Audit: enterprise-agent audit explain run-terminal-usability",
        "Recovery: reclaimable",
    ):
        assert text in no_color_result.stdout


def test_json_demo_requires_explicit_unattended_mode_before_local_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Machine-readable output never silently bypasses the demo's explicit local-write consent."""
    calls: list[object] = []
    monkeypatch.setattr(cli, "run_guided_demo", lambda *_args, **_kwargs: calls.append(object()))

    result = CliRunner().invoke(cli.app, ["--output", "json", "demo"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "status": "refused",
        "summary": "Guided demo requires --unattended when --output json is selected.",
        "data": {},
        "next_actions": ["Run enterprise-agent demo interactively or add --unattended."],
        "error": {
            "code": "interactive_confirmation_required",
            "message": "No local demo data was reset or staged.",
        },
    }
    assert calls == []


def test_json_missing_database_error_is_actionable_and_has_no_plain_text_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A script receives one safe machine-readable error instead of a prompt or traceback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = CliRunner().invoke(cli.app, ["--output", "json", "status"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "status": "refused",
        "summary": "Status requires DATABASE_URL.",
        "data": {},
        "next_actions": ["Set DATABASE_URL, then run enterprise-agent status."],
        "error": {"code": "missing_configuration", "message": "DATABASE_URL is required."},
    }
    assert result.stderr == ""
