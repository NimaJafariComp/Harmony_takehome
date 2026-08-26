"""CLI contract tests."""

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.domain import AuditEvent, AuditEventId, RunId
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


def test_reset_command_reports_guard_failures_and_missing_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI preserves reset safety errors and rejects absent database configuration."""
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
) -> None:
    """Noninteractive audit reconstruction names the missing required setting instead of prompting."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = CliRunner().invoke(cli.app, ["audit", "explain", "run-cli-audit"])

    assert result.exit_code == 1
    assert "database: audit explain refused (DATABASE_URL is required)" in result.stderr
