"""CLI contract tests."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.domain import (
    ApprovalId,
    AttentionId,
    AuditEvent,
    AuditEventId,
    RunId,
    WorkflowId,
)
from enterprise_agent.seed import SeedSafetyError

pytestmark = pytest.mark.unit


def test_version_command_reports_package_version() -> None:
    """The CLI exposes the package version for diagnostics."""
    result = CliRunner().invoke(cli.app, ["version"])

    assert result.exit_code == 0
    assert result.stdout == "enterprise-agent 0.1.0\n"


def test_main_invokes_cli_application(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installed console entry point invokes the Typer application."""
    invoked: list[bool] = []

    def fake_app() -> None:
        invoked.append(True)

    monkeypatch.setattr(cli, "app", fake_app)

    cli.main()

    assert invoked == [True]


def test_reset_and_seed_commands_need_only_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local data commands do not require any selected LLM profile or credential."""
    called_urls: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "reset_database", called_urls.append)
    monkeypatch.setattr(cli, "seed_database", called_urls.append)

    runner = CliRunner()
    reset_result = runner.invoke(cli.app, ["reset"])
    seed_result = runner.invoke(cli.app, ["seed"])

    assert reset_result.exit_code == 0
    assert reset_result.stdout == "database: reset\n"
    assert seed_result.exit_code == 0
    assert seed_result.stdout == "database: seeded\n"
    assert called_urls == [
        "postgresql+psycopg://agent:agent@db:5432/enterprise_agent",
        "postgresql+psycopg://agent:agent@db:5432/enterprise_agent",
    ]


@pytest.mark.parametrize("command", ("reset", "seed"))
def test_interactive_local_data_cancellation_creates_no_database_write(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """Reset and seed require explicit confirmation only in a human terminal."""
    called_urls: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr("typer.prompt", lambda *_args, **_kwargs: "cancel")
    monkeypatch.setattr(cli, "reset_database", called_urls.append)
    monkeypatch.setattr(cli, "seed_database", called_urls.append)

    result = CliRunner().invoke(cli.app, [command])

    assert result.exit_code == 130
    assert "local synthetic demo database" in result.stdout
    assert "cancelled" in result.stdout
    assert called_urls == []


def test_reset_command_reports_guard_failures_and_missing_database_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The CLI preserves reset safety errors and rejects absent database configuration."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/not_demo")

    def reject(_: str) -> None:
        raise SeedSafetyError("reset is restricted to the local demo database")

    monkeypatch.setattr(cli, "reset_database", reject)
    rejected_result = runner.invoke(cli.app, ["reset"])
    monkeypatch.delenv("DATABASE_URL")
    missing_result = runner.invoke(cli.app, ["seed"])

    assert rejected_result.exit_code == 1
    assert "database: reset refused" in rejected_result.stderr
    assert missing_result.exit_code == 1
    assert "DATABASE_URL is required" in missing_result.stderr


def test_clock_advance_command_uses_the_local_persisted_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators advance deterministic demo time by a positive whole number of hours."""
    created_urls: list[str] = []
    advanced_by: list[timedelta] = []

    class RecordingClock:
        """Capture CLI interactions without requiring a PostgreSQL service."""

        def __init__(self, database_url: str) -> None:
            created_urls.append(database_url)

        def advance(self, duration: timedelta) -> datetime:
            advanced_by.append(duration)
            return datetime(2026, 8, 25, 9, tzinfo=UTC)

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "PostgresDemoClock", RecordingClock, raising=False)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["clock", "advance", "--hours", "24"])
    invalid = runner.invoke(cli.app, ["clock", "advance", "--hours", "0"])

    assert result.exit_code == 0
    assert result.stdout == "clock: advanced to 2026-08-25T09:00:00+00:00\n"
    assert created_urls == ["postgresql+psycopg://agent:agent@db:5432/enterprise_agent"]
    assert advanced_by == [timedelta(hours=24)]
    assert invalid.exit_code == 2


def test_audit_explain_command_renders_a_read_only_run_story(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can request reconstruction by run ID without loading any live business provider."""
    created_urls: list[str] = []
    run_id = RunId("run-cli-audit")
    event = AuditEvent(
        event_id=AuditEventId("event-cli-audit"),
        occurred_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
        event_type="attention.detected",
        run_id=run_id,
        actor_id=None,
        attention_id=None,
        workflow_id=None,
        plan_id=None,
        evidence_ids=(),
        payload={"part_id": "part-101", "production_order_id": "order-301"},
        policy_version=None,
        plan_hash=None,
        idempotency_key=None,
        failure_category=None,
    )

    class RecordingAudit:
        """Return only the audit event that the CLI explainer may read."""

        def __init__(self, database_url: str) -> None:
            created_urls.append(database_url)

        def events_for_run(self, requested_run_id: RunId) -> tuple[AuditEvent, ...]:
            assert requested_run_id == run_id
            return (event,)

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "PostgresAuditAdapter", RecordingAudit, raising=False)

    result = CliRunner().invoke(cli.app, ["audit", "explain", str(run_id)])

    assert result.exit_code == 0
    assert "Audit explanation for run run-cli-audit (1 events)" in result.stdout
    assert "Detected stockout risk for part part-101" in result.stdout
    assert created_urls == ["postgresql+psycopg://agent:agent@db:5432/enterprise_agent"]


def test_audit_explain_command_fails_clearly_without_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Noninteractive audit reconstruction names the missing required setting instead of prompting."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = CliRunner().invoke(cli.app, ["audit", "explain", "run-cli-audit"])

    assert result.exit_code == 1
    assert "database: audit explain refused (DATABASE_URL is required)" in result.stderr


def test_scenario_c_command_stages_a_pending_human_review_without_an_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deterministic optional scenario creates only a reviewed pending plan, never an auto-hold."""
    staged_urls: list[str] = []
    expected_run_id = RunId("run-scenario-c-cli")

    class StagedScenarioC:
        """Return only the safe, operator-visible identifiers from a deterministic pending run."""

        run_id = expected_run_id
        attention_id = AttentionId("00000000-0000-0000-0000-000000000801")
        approval_id = ApprovalId("00000000-0000-0000-0000-000000000802")
        workflow_id = WorkflowId("00000000-0000-0000-0000-000000000803")

    def stage(database_url: str, *, run_id: RunId) -> StagedScenarioC:
        staged_urls.append(database_url)
        assert run_id == expected_run_id
        return StagedScenarioC()

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "stage_scenario_c_pending", stage, raising=False)

    result = CliRunner().invoke(cli.app, ["scenario-c"])

    assert result.exit_code == 0
    assert result.stdout == (
        "scenario-c: pending approval\n"
        "run: run-scenario-c-cli\n"
        "attention: 00000000-0000-0000-0000-000000000801\n"
        "approval: 00000000-0000-0000-0000-000000000802\n"
        "workflow: 00000000-0000-0000-0000-000000000803\n"
        "next: review and approve this exact plan before it can execute\n"
    )
    assert staged_urls == ["postgresql+psycopg://agent:agent@db:5432/enterprise_agent"]


def test_demo_list_is_read_only_and_discovers_the_guided_cases() -> None:
    """Operators can inspect the deterministic tour without a database, key, prompt, or write."""
    result = CliRunner().invoke(cli.app, ["demo", "--list"])

    assert result.exit_code == 0
    assert "Guided company demo" in result.stdout
    assert "scenario-a-reroute-bait" in result.stdout
    assert "scenario-c-pending-review" in result.stdout
    assert "no live provider" in result.stdout


def test_demo_unattended_starts_only_the_selected_local_case_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit unattended mode supports CI while never loading a provider profile or prompting."""
    called: list[tuple[str, tuple[str, ...]]] = []
    rendered: list[object] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(
        "typer.prompt", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no prompt"))
    )

    def run(database_url: str, *, case_ids: tuple[str, ...]) -> object:
        called.append((database_url, case_ids))
        return object()

    monkeypatch.setattr(cli, "run_guided_demo", run, raising=False)
    monkeypatch.setattr(cli, "_render_guided_demo", rendered.append, raising=False)

    result = CliRunner().invoke(
        cli.app,
        ["demo", "--case", "scenario-a-reroute-bait", "--unattended"],
    )

    assert result.exit_code == 0
    assert called == [
        ("postgresql+psycopg://agent:agent@db:5432/enterprise_agent", ("scenario-a-reroute-bait",))
    ]
    assert len(rendered) == 1


def test_guide_command_exposes_examples_and_shell_completion_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewer can discover the safe commands before configuring or resetting anything."""
    monkeypatch.setattr(
        cli,
        "PostgresOperatorStatusAdapter",
        lambda *_args: (_ for _ in ()).throw(AssertionError("guide must not read a database")),
        raising=False,
    )

    result = CliRunner().invoke(cli.app, ["guide"])

    assert result.exit_code == 0
    assert "Reviewer guide" in result.stdout
    assert "enterprise-agent demo --list" in result.stdout
    assert "enterprise-agent status" in result.stdout
    assert "enterprise-agent audit explain RUN_ID" in result.stdout
    assert "enterprise-agent --install-completion" in result.stdout


def test_status_command_is_read_only_and_renders_the_adapter_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The overview delegates to the operator read adapter and does not prompt or load LLM setup."""
    created_urls: list[str] = []
    rendered: list[object] = []
    snapshot = object()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(
        "typer.prompt", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no prompt"))
    )

    class RecordingReader:
        """Capture the query boundary without a PostgreSQL process."""

        def __init__(self, database_url: str) -> None:
            created_urls.append(database_url)

        def read_status(self) -> object:
            return snapshot

    monkeypatch.setattr(cli, "PostgresOperatorStatusAdapter", RecordingReader, raising=False)
    monkeypatch.setattr(cli, "_render_operator_status", rendered.append, raising=False)

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert created_urls == ["postgresql+psycopg://agent:agent@db:5432/enterprise_agent"]
    assert rendered == [snapshot]


def test_interactive_scenario_c_cancellation_creates_no_pending_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyboard cancellation happens before the deterministic scenario can make a durable write."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli, "_require_local_demo_database", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "stage_scenario_c_pending",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not stage")),
    )
    monkeypatch.setattr("typer.prompt", lambda *_args, **_kwargs: "cancel")

    result = CliRunner().invoke(cli.app, ["scenario-c"])

    assert result.exit_code == 130
    assert "Stage Scenario C review" in result.stdout
    assert "does not hold" in result.stdout
    assert "purchase order" in result.stdout
    assert "cancelled" in result.stdout


def test_llm_usage_command_renders_only_grouped_immutable_metering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator-facing usage report reads its safe totals from the ledger without a provider request."""
    event = AuditEvent(
        event_id=AuditEventId("event-cli-usage"),
        occurred_at=datetime(2026, 8, 26, 12, tzinfo=UTC),
        event_type="llm.completed",
        run_id=RunId("run-cli-usage"),
        actor_id=None,
        attention_id=None,
        workflow_id=None,
        plan_id=None,
        evidence_ids=(),
        payload={
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "status": "succeeded",
            "input_tokens": 1000,
            "cached_input_tokens": 200,
            "output_tokens": 500,
            "total_tokens": 1500,
            "cost_usd": "0.000764",
            "cost_source": "estimated",
        },
        policy_version=None,
        plan_hash=None,
        idempotency_key=None,
        failure_category=None,
    )

    class RecordingAudit:
        """Return the ledger event without exposing any mutable or provider-facing operation."""

        def __init__(self, database_url: str) -> None:
            assert database_url.endswith("enterprise_agent")

        def llm_usage_events(self) -> tuple[AuditEvent, ...]:
            return (event,)

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "PostgresAuditAdapter", RecordingAudit, raising=False)

    result = CliRunner().invoke(cli.app, ["llm-usage"])

    assert result.exit_code == 0
    assert "Immutable audit ledger" in result.stdout
    assert "Provider: openai" in result.stdout
    assert "Model: gpt-5.6-luna" in result.stdout
    assert "Requests: 1" in result.stdout
    assert "1500 total (1000 input / 500 output)" in result.stdout
    assert "Known cost (USD): $0.000764" in result.stdout
