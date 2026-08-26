"""Semantic CLI usability contracts without brittle visual snapshots."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.application.operator_status import (
    OperatorStatusSnapshot,
    PendingApprovalStatus,
    RecoveryState,
    WorkflowStatusSummary,
)
from enterprise_agent.config import ProviderConfiguration
from enterprise_agent.ports import LLMGenerationResult
from enterprise_agent.presentation import TerminalPresenter, TerminalTheme

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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read path must not prompt")
        ),
    )


def _presenter_for(force_terminal: bool) -> TerminalPresenter:
    """Model a TTY or piped stream while retaining the CLI's selected no-color preference."""
    return TerminalPresenter(
        console=Console(
            file=sys.stdout,
            force_terminal=force_terminal,
            color_system="standard" if force_terminal else None,
            no_color=cli._output_options().no_color,
        ),
        theme=TerminalTheme(),
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
                    "command": "enterprise-agent live-demo --list",
                    "purpose": (
                        "Inspect the guarded one-provider local Scenario A, B, and C catalogue "
                        "before opting in."
                    ),
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
                {
                    "command": "enterprise-agent llm-evaluate --list",
                    "purpose": (
                        "Inspect the optional synthetic no-write live-LLM evaluation pack before opting in."
                    ),
                },
            ],
            "shell_completion": "enterprise-agent --install-completion",
        },
        "next_actions": ["enterprise-agent demo --list"],
        "error": None,
    }


def test_default_noninteractive_entry_is_a_compact_guide_without_a_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pipes retain safe command discovery rather than attempting to start an interactive shell."""
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        "typer.prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("non-TTY must not prompt")),
    )

    result = CliRunner().invoke(cli.app, [])

    assert result.exit_code == 0
    assert "Reviewer guide" in result.stdout
    assert "enterprise-agent demo --list" in result.stdout
    assert "enterprise-agent status" in result.stdout


def test_default_json_entry_remains_one_command_directory_envelope() -> None:
    """A script that omits a subcommand still receives stable JSON and no menu decoration."""
    result = CliRunner().invoke(cli.app, ["--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "succeeded"
    assert payload["summary"] == "Reviewer command guide."
    assert payload["data"]["commands"][0]["command"] == "enterprise-agent demo --list"


def test_default_tty_entry_starts_the_operator_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installed command itself is the primary interactive interface, not a dev wrapper."""
    launched: list[bool] = []
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli, "_run_application_shell", lambda: launched.append(True))

    result = CliRunner().invoke(cli.app, [])

    assert result.exit_code == 0
    assert launched == [True]


def test_application_shell_routes_keyboard_choices_to_demo_and_operator_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY navigation is an app shell that delegates to existing safe command flows."""
    choices = iter(("1", "2", "q"))
    routed: list[str] = []
    monkeypatch.setattr(cli, "_prompt_with_cancellation", lambda *_args, **_kwargs: next(choices))
    monkeypatch.setattr(cli, "_run_demo_shell", lambda: routed.append("demo"))
    monkeypatch.setattr(cli, "_run_operator_shell", lambda: routed.append("operator"))

    cli._run_application_shell()

    assert routed == ["demo", "operator"]


def test_shell_command_waits_for_an_operator_to_read_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed result remains visible until the operator explicitly returns to its menu."""
    prompts: list[tuple[object, dict[str, object]]] = []
    completed: list[bool] = []

    def dismiss(*args: object, **kwargs: object) -> str:
        prompts.append((args, kwargs))
        return ""

    monkeypatch.setattr(
        "typer.prompt",
        dismiss,
    )

    cli._run_shell_command(lambda: completed.append(True))

    assert completed == [True]
    assert prompts == [
        (("Press Enter to return to the menu",), {"default": "", "show_default": False})
    ]


def test_live_evaluation_shell_runs_one_explicitly_confirmed_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyboard shell forwards one deliberate provider/case choice to the safe evaluator."""
    prompts = iter(("claude", "a-hostile-email"))
    invoked: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "_emit_evaluation_catalog", lambda: None)
    monkeypatch.setattr(cli, "_prompt_with_cancellation", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr("typer.confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cli, "_run_shell_command", lambda command: command())
    monkeypatch.setattr(
        cli,
        "llm_evaluate",
        lambda **kwargs: invoked.append(kwargs),
    )

    cli._run_live_evaluation_shell()

    assert invoked == [
        {
            "profile": "claude",
            "case": ["a-hostile-email"],
            "all_cases": False,
            "execute": True,
        }
    ]


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
        "Read-only command: enterprise-agent audit explain run-terminal-usability",
        "Recovery: reclaimable",
    ):
        assert text in no_color_result.stdout


def test_tty_and_piped_status_keep_the_same_operational_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Color and table decoration may differ, but a reviewer never loses the control-plane facts."""
    _install_status_reader(monkeypatch)
    runner = CliRunner()
    monkeypatch.setattr(cli, "_terminal_presenter", lambda: _presenter_for(True))
    tty_result = runner.invoke(cli.app, ["status"])
    monkeypatch.setattr(cli, "_terminal_presenter", lambda: _presenter_for(False))
    piped_result = runner.invoke(cli.app, ["status"])

    assert tty_result.exit_code == 0
    assert piped_result.exit_code == 0
    assert _ANSI_ESCAPE.search(tty_result.stdout)
    assert not _ANSI_ESCAPE.search(piped_result.stdout)
    tty_text = _ANSI_ESCAPE.sub("", tty_result.stdout)
    for text in (
        "Pending approval",
        "00000000-0000-0000-0000-000000000802",
        "00000000-0000-0000-0000-000000000803",
        "Read-only command: enterprise-agent audit explain run-terminal-usability",
        "Recovery: reclaimable",
    ):
        assert text in tty_text
        assert text in piped_result.stdout


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


def test_json_setup_refuses_sensitive_interaction_without_prompting_or_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """JSON output cannot turn a secret-collecting setup flow into an unattended prompt."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(
        "typer.prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("JSON setup must not prompt")
        ),
    )

    result = CliRunner().invoke(cli.app, ["--output", "json", "llm-setup"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "status": "refused",
        "summary": "LLM setup requires interactive text output.",
        "data": {},
        "next_actions": ["Run enterprise-agent llm-setup in an interactive terminal."],
        "error": {
            "code": "interactive_input_required",
            "message": "No API key was requested or saved.",
        },
    }
    assert not (tmp_path / ".env").exists()


def test_json_mode_envelopes_static_and_configured_read_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The root output option has one meaning for ordinary diagnostics as well as operator paths."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setenv("LLM_PROFILE", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "terminal-usability-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    runner = CliRunner()

    version_result = runner.invoke(cli.app, ["--output", "json", "version"])
    config_result = runner.invoke(cli.app, ["--output", "json", "config-check"])

    assert version_result.exit_code == 0
    assert json.loads(version_result.stdout) == {
        "schema_version": 1,
        "status": "succeeded",
        "summary": "Installed harness version.",
        "data": {"version": "0.1.0"},
        "next_actions": [],
        "error": None,
    }
    assert config_result.exit_code == 0
    assert json.loads(config_result.stdout) == {
        "schema_version": 1,
        "status": "succeeded",
        "summary": "Runtime configuration is valid.",
        "data": {
            "profile": "openai",
            "model": "gpt-5.6-luna",
            "database_configured": True,
        },
        "next_actions": ["enterprise-agent guide"],
        "error": None,
    }


@pytest.mark.parametrize(
    ("command", "summary"),
    (
        ("reset", "Reset requires interactive text confirmation."),
        ("seed", "Seed requires interactive text confirmation."),
        ("scenario-c", "Scenario C staging requires interactive text confirmation."),
    ),
)
def test_json_confirmation_protected_writes_refuse_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    summary: str,
) -> None:
    """A serialized response never skips the interactive receipt required for a local write."""
    monkeypatch.setattr(
        "typer.prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("JSON write must not prompt")
        ),
    )

    result = CliRunner().invoke(cli.app, ["--output", "json", command])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "status": "refused",
        "summary": summary,
        "data": {},
        "next_actions": [f"Run enterprise-agent {command} in an interactive terminal."],
        "error": {
            "code": "interactive_confirmation_required",
            "message": "No local data was written.",
        },
    }


def test_json_operational_reads_and_demo_catalogue_keep_safe_structured_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured reads and the demo catalogue use the same envelope without exposing secret data."""
    configuration = ProviderConfiguration(
        profile="openai", model="gpt-5.6-luna", api_key="json-smoke-secret"
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "load_provider_settings", lambda _environment: configuration)
    monkeypatch.setattr(
        cli,
        "run_smoke",
        lambda _configuration: LLMGenerationResult.succeeded(
            provider="openai",
            model="gpt-5.6-luna",
            output={"outcome": "MANUAL_REVIEW", "reason": "safe fixture"},
        ),
    )

    class Audit:
        """Return no metering events, proving the zero-state remains structured and safe."""

        def __init__(self, _: str) -> None:
            pass

        def llm_usage_events(self) -> tuple[object, ...]:
            return ()

    monkeypatch.setattr(cli, "PostgresAuditAdapter", Audit)
    runner = CliRunner()

    run_result = runner.invoke(cli.app, ["--output", "json", "run"])
    smoke_result = runner.invoke(cli.app, ["--output", "json", "llm-smoke"])
    usage_result = runner.invoke(cli.app, ["--output", "json", "llm-usage"])
    demo_list_result = runner.invoke(cli.app, ["--output", "json", "demo", "--list"])
    demo_error_result = runner.invoke(cli.app, ["--output", "json", "demo", "--case", "not-a-case"])

    assert json.loads(run_result.stdout)["data"] == {
        "profile": "openai",
        "model": "gpt-5.6-luna",
    }
    smoke_payload = json.loads(smoke_result.stdout)
    assert smoke_payload["status"] == "succeeded"
    assert smoke_payload["data"]["business_data_sent"] is False
    assert "json-smoke-secret" not in smoke_result.output
    assert json.loads(usage_result.stdout)["data"] == {"lines": [], "total_cost_usd": "0"}
    assert json.loads(demo_list_result.stdout)["data"]["cases"][0]["case_id"] == (
        "scenario-a-reroute-bait"
    )
    assert demo_error_result.exit_code == 2
    assert json.loads(demo_error_result.stdout)["error"]["code"] == "invalid_demo_selection"


def test_json_status_clock_and_audit_paths_cover_safe_success_and_error_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator reads and clock actions keep their state and failures structured without raw internals."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:agent@db:5432/enterprise_agent")
    monkeypatch.setattr(cli, "_require_local_demo_database", lambda *_args, **_kwargs: None)

    class Clock:
        """Return a fixed persisted demo time without connecting to PostgreSQL."""

        def __init__(self, _: str) -> None:
            pass

        def advance(self, _duration: object) -> datetime:
            return datetime(2026, 8, 26, 12, tzinfo=UTC)

    class Audit:
        """Supply only the audit object required by the isolated explanation boundary."""

        def __init__(self, _: str) -> None:
            pass

    class Explanation:
        """Represent a safe ledger-only explanation result."""

        def render(self) -> str:
            return "Audit explanation for run run-terminal-json (1 events)"

    class Explainer:
        """Avoid a database while preserving the CLI's audit-explanation boundary."""

        def __init__(self, _: object) -> None:
            pass

        def explain(self, _run_id: object) -> Explanation:
            return Explanation()

    monkeypatch.setattr(cli, "PostgresDemoClock", Clock)
    monkeypatch.setattr(cli, "PostgresAuditAdapter", Audit)
    monkeypatch.setattr(cli, "AuditExplainer", Explainer)
    runner = CliRunner()

    clock_result = runner.invoke(cli.app, ["--output", "json", "clock", "advance", "--hours", "2"])
    audit_result = runner.invoke(
        cli.app, ["--output", "json", "audit", "explain", "run-terminal-json"]
    )

    assert json.loads(clock_result.stdout)["data"]["current_at"] == "2026-08-26T12:00:00+00:00"
    assert json.loads(audit_result.stdout)["data"] == {
        "run_id": "run-terminal-json",
        "explanation": "Audit explanation for run run-terminal-json (1 events)",
    }

    class UnavailableStatus:
        """Raise a sanitized database boundary failure."""

        def __init__(self, _: str) -> None:
            pass

        def read_status(self) -> OperatorStatusSnapshot:
            raise SQLAlchemyError("connection detail must not be exposed")

    monkeypatch.setattr(cli, "PostgresOperatorStatusAdapter", UnavailableStatus)
    status_result = runner.invoke(cli.app, ["--output", "json", "status"])

    assert status_result.exit_code == 1
    assert json.loads(status_result.stdout)["error"] == {
        "code": "database_unavailable",
        "message": "The local database is unavailable.",
    }
