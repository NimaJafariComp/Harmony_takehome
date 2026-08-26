"""Command-line interface for the enterprise agent harness."""

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from os import environ
from typing import Annotated, NoReturn, cast

import typer
from rich.console import Console
from sqlalchemy.exc import SQLAlchemyError
from typer._click.globals import get_current_context

from enterprise_agent import __version__
from enterprise_agent.adapters import (
    DemoClockNotInitializedError,
    PostgresAuditAdapter,
    PostgresDemoClock,
    PostgresOperatorStatusAdapter,
)
from enterprise_agent.application import AuditExplainer, AuditExplanationError
from enterprise_agent.application.guided_demo import (
    DemoCaseSelectionError,
    DemoExecutionMode,
    GuidedDemoCase,
    GuidedDemoRun,
    guided_demo_cases,
    run_guided_demo,
    select_guided_demo_cases,
)
from enterprise_agent.application.live_demo import (
    LiveDemoCase,
    LiveDemoExecutionError,
    LiveDemoResult,
    LiveDemoSelectionError,
    live_demo_cases,
    run_live_demo,
    select_live_demo_case,
)
from enterprise_agent.application.llm_evaluation import (
    EvaluationCaseSelectionError,
    LLMEvaluationCase,
    LLMEvaluationReport,
    evaluate_cases,
    evaluation_cases,
    live_evaluation_provenance,
    select_evaluation_cases,
)
from enterprise_agent.application.operator_status import (
    OperatorStatusSnapshot,
    operator_status_data,
)
from enterprise_agent.application.scenario_c_demo import (
    ScenarioCDeterministicRunError,
    stage_scenario_c_pending,
)
from enterprise_agent.config import (
    PROFILE_ENVIRONMENT_PREFIXES,
    ConfigurationError,
    ProviderConfiguration,
    load_provider_profile,
    load_provider_settings,
    load_settings,
    normalize_llm_profile,
)
from enterprise_agent.domain import RunId
from enterprise_agent.llm_setup import (
    CuratedModel,
    LLMSetupSelection,
    ModelDiscoveryError,
    default_env_path,
    discover_compatible_models,
    load_local_environment,
    save_llm_profile,
    verify_credential,
)
from enterprise_agent.llm_usage import LLMUsageSummary, summarize_llm_usage
from enterprise_agent.presentation import (
    ApprovalSummary,
    CommandGuideEntry,
    ConfirmationSummary,
    DemoCatalogueEntry,
    EvaluationCatalogueEntry,
    EvidenceDisposition,
    EvidenceSummary,
    MenuEntry,
    StatusSummary,
    StoryBrief,
    TerminalError,
    TerminalPresenter,
    TerminalResult,
    TerminalState,
    TerminalTheme,
    WorkflowSummary,
)
from enterprise_agent.review_provenance import GateStatus, PlannerProvenance
from enterprise_agent.seed import (
    SeedSafetyError,
    _require_local_demo_database,
    reset_database,
    seed_database,
)
from enterprise_agent.smoke import create_no_write_adapter, run_smoke

app = typer.Typer(
    help="Operate the enterprise agent harness.",
    no_args_is_help=False,
    epilog=(
        "Interactive home: enterprise-agent\n"
        "Command directory: enterprise-agent guide\n"
        "Demo cases: enterprise-agent demo --list\n"
        "Shell completion: enterprise-agent --install-completion"
    ),
)
clock_app = typer.Typer(help="Inspect and advance deterministic local-demo time.")
audit_app = typer.Typer(help="Reconstruct read-only operator stories from the audit ledger.")
app.add_typer(clock_app, name="clock")
app.add_typer(audit_app, name="audit")

_CANCELLATION_WORD = "cancel"


class OutputMode(StrEnum):
    """The additive command-output surfaces supported by the terminal contract."""

    TEXT = "text"
    JSON = "json"


@dataclass(frozen=True)
class OutputOptions:
    """Presentation preferences selected once at the Typer root command boundary."""

    mode: OutputMode = OutputMode.TEXT
    no_color: bool = False


class InteractiveFlowCancelled(Exception):
    """Raised when an operator explicitly cancels before a command creates a durable write."""


@app.callback(invoke_without_command=True)
def harness(
    context: typer.Context,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Render human output without ANSI color or style codes."),
    ] = False,
    output: Annotated[
        str,
        typer.Option("--output", help="Output format: text (default) or json."),
    ] = OutputMode.TEXT.value,
) -> None:
    """Enterprise agent harness commands."""
    try:
        mode = OutputMode(output.strip().lower())
    except ValueError as error:
        raise typer.BadParameter(
            "must be either 'text' or 'json'", param_hint="--output"
        ) from error
    context.obj = OutputOptions(mode=mode, no_color=no_color)
    if context.invoked_subcommand is not None:
        return
    if _uses_json_output():
        guide()
        return
    if _is_interactive_terminal():
        _run_application_shell()
        return
    guide()


@app.command()
def version() -> None:
    """Print the installed harness version."""
    if _emit_json_result(
        TerminalResult(
            state=TerminalState.SUCCEEDED,
            summary="Installed harness version.",
            data={"version": __version__},
        )
    ):
        return
    typer.echo(f"enterprise-agent {__version__}")


@app.command(name="config-check")
def config_check() -> None:
    """Validate required runtime configuration without displaying credentials."""
    try:
        configuration = load_settings(_runtime_environment())
    except (ConfigurationError, ValueError) as error:
        if _emit_json_result(_configuration_refusal(action="config check", error=error)):
            raise typer.Exit(code=1) from error
        typer.echo(f"configuration: invalid ({error})", err=True)
        raise typer.Exit(code=1) from error

    if _emit_json_result(
        TerminalResult(
            state=TerminalState.SUCCEEDED,
            summary="Runtime configuration is valid.",
            data={
                "profile": configuration.provider.profile,
                "model": configuration.provider.model,
                "database_configured": True,
            },
            next_actions=("enterprise-agent guide",),
        )
    ):
        return
    typer.echo(configuration.safe_summary())


@app.command()
def guide() -> None:
    """Show the shortest safe path to demo, inspect state, and enable shell completion."""
    entries = _guide_entries()
    if _emit_json_result(
        TerminalResult(
            state=TerminalState.SUCCEEDED,
            summary="Reviewer command guide.",
            data={
                "commands": [
                    {"command": entry.command, "purpose": entry.purpose} for entry in entries
                ],
                "shell_completion": "enterprise-agent --install-completion",
            },
            next_actions=("enterprise-agent demo --list",),
        )
    ):
        return
    _terminal_presenter().render_command_guide(
        title="Reviewer guide",
        entries=entries,
        completion_command="enterprise-agent --install-completion",
    )


def _run_application_shell() -> None:
    """Route a TTY through safe modes while direct commands remain scriptable."""
    entries = (
        MenuEntry(
            key="1",
            title="Guided company demo",
            description="Run one deterministic Scenario A, B, or C story.",
            boundary="Local synthetic data; no live provider or automatic effect.",
        ),
        MenuEntry(
            key="2",
            title="Guarded live local demo",
            description="Stage one live provider proposal for human approval.",
            boundary="Resets only local synthetic data; provider request and local review records require confirmation.",
        ),
        MenuEntry(
            key="3",
            title="Normal operator mode",
            description="Inspect status, audit, LLM usage, and safe provider checks.",
            boundary="Reads do not prompt; every write keeps its existing confirmation.",
        ),
        MenuEntry(
            key="4",
            title="Configure an LLM profile",
            description="Save one selected local provider and reviewed model.",
            boundary="The API key stays hidden and is saved only after confirmation.",
        ),
    )
    while True:
        presenter = _terminal_presenter()
        presenter.clear_screen()
        presenter.render_app_shell(
            title="Enterprise agent",
            subtitle="Purchasing and quality control plane · keyboard-only local operator shell",
            entries=entries,
            prompt="Choose 1–4. Enter q to quit.",
        )
        try:
            choice = _prompt_with_cancellation("Mode").strip().lower()
        except InteractiveFlowCancelled:
            _exit_cancelled("shell: cancelled; no data was written")
        if choice == "q":
            typer.echo("shell: closed; no data was written")
            return
        if choice == "1":
            _run_demo_shell()
            continue
        if choice == "2":
            _run_live_demo_shell()
            continue
        if choice == "3":
            _run_operator_shell()
            continue
        if choice == "4":
            _run_shell_command(llm_setup)
            continue
        typer.echo("Choose 1–4 or q.")


def _run_demo_shell() -> None:
    """Let a reviewer select one short local story before the existing write confirmation."""
    cases = guided_demo_cases()
    entries = (
        DemoCatalogueEntry(
            key="1",
            title="Safety tour",
            summary="Runs the short cross-scenario proof set in its fixed order.",
            mode="Local reset, seed, and guarded pending-plan stages",
        ),
        *tuple(
            DemoCatalogueEntry(
                key=str(index),
                title=item.title,
                summary=_demo_catalogue_summary(item),
                mode=_demo_execution_mode_label(item.execution_mode),
            )
            for index, item in enumerate(cases, start=2)
        ),
    )
    while True:
        presenter = _terminal_presenter()
        presenter.clear_screen()
        presenter.render_demo_catalogue(
            entries=entries,
            prompt="Choose a case number. Enter b to return to Home.",
        )
        _render_guided_story_briefs(presenter)
        try:
            choice = _prompt_with_cancellation("Demo case").strip().lower()
        except InteractiveFlowCancelled:
            _exit_cancelled("shell: cancelled; no data was written")
        if choice == "b":
            return
        if choice == "1":
            _run_shell_command(lambda: demo(case=[_SAFETY_TOUR_LABEL]))
            return
        try:
            selected = cases[int(choice) - 2]
        except (IndexError, ValueError):
            typer.echo("Choose a listed case number or b to return.")
            continue

        def run_selected_case(case_id: str = selected.case_id) -> None:
            demo(case=[case_id])

        _run_shell_command(run_selected_case)
        return


def _run_operator_shell() -> None:
    """Expose reviewed read/check routes without inventing a second operational API."""
    entries = (
        MenuEntry(
            key="1",
            title="Control-plane status",
            description="Read pending approvals, workflow state, and recovery status.",
            boundary="Read-only local database query; no provider request.",
        ),
        MenuEntry(
            key="2",
            title="Audit explanation",
            description="Reconstruct one durable run from the append-only ledger.",
            boundary="Read-only; requires a displayed run ID.",
        ),
        MenuEntry(
            key="3",
            title="LLM usage",
            description="Read scalar token and cost totals from the immutable ledger.",
            boundary="Read-only; no provider request.",
        ),
        MenuEntry(
            key="4",
            title="LLM profile setup",
            description="Choose a provider and adapter-reviewed model for this machine.",
            boundary="Hidden key input; save remains explicitly confirmed.",
        ),
        MenuEntry(
            key="5",
            title="Provider smoke check",
            description="Run one fixed no-business-data provider probe.",
            boundary="An explicit provider request; no business-system write.",
        ),
        MenuEntry(
            key="6",
            title="Live-evaluation catalogue",
            description="Inspect the fixed synthetic LLM evaluation cases.",
            boundary="Listing only; no provider request or write.",
        ),
    )
    while True:
        presenter = _terminal_presenter()
        presenter.clear_screen()
        presenter.render_app_shell(
            title="Normal operator mode",
            subtitle="Use existing safe commands through a concise keyboard menu",
            entries=entries,
            prompt="Choose 1–6. Enter b to return to Home.",
        )
        try:
            choice = _prompt_with_cancellation("Operator action").strip().lower()
        except InteractiveFlowCancelled:
            _exit_cancelled("shell: cancelled; no data was written")
        if choice == "b":
            return
        if choice == "1":
            _run_shell_command(status)
            continue
        if choice == "2":
            try:
                run_id = _prompt_with_cancellation("Audit run ID")
            except InteractiveFlowCancelled:
                typer.echo("audit: cancelled; no ledger query was made")
                continue

            def explain_selected_run(selected_run_id: str = run_id) -> None:
                audit_explain(selected_run_id)

            _run_shell_command(explain_selected_run)
            continue
        if choice == "3":
            _run_shell_command(llm_usage)
            continue
        if choice == "4":
            _run_shell_command(llm_setup)
            continue
        if choice == "5":
            _run_shell_command(llm_smoke)
            continue
        if choice == "6":
            _run_live_evaluation_shell()
            continue
        typer.echo("Choose 1–6 or b to return.")


def _run_live_evaluation_shell() -> None:
    """Turn the fixed no-write evaluation catalogue into an explicit keyboard action."""
    _emit_evaluation_catalog()
    try:
        profile = normalize_llm_profile(
            _prompt_with_cancellation(
                "Provider", default=_runtime_environment().get("LLM_PROFILE", "openai")
            )
        )
        selected_case = _prompt_with_cancellation("Case ID (or all)").strip()
    except InteractiveFlowCancelled:
        typer.echo("live evaluation: cancelled; no provider request was made")
        return
    except ValueError:
        typer.echo(
            "live evaluation: choose openai, claude, or openrouter; no provider request was made"
        )
        return

    include_all = selected_case.lower() == "all"
    if not include_all:
        try:
            select_evaluation_cases((selected_case,), include_all=False)
        except EvaluationCaseSelectionError:
            typer.echo(
                "live evaluation: choose a listed case ID or all; no provider request was made"
            )
            return

    request_count = len(evaluation_cases()) if include_all else 1
    if not typer.confirm(
        f"Send {request_count} fixed synthetic no-write request(s) to {profile}?", default=False
    ):
        typer.echo("live evaluation: cancelled; no provider request was made")
        return

    _run_shell_command(
        lambda: llm_evaluate(
            profile=profile,
            case=None if include_all else [selected_case],
            all_cases=include_all,
            execute=True,
        )
    )


def _run_live_demo_shell() -> None:
    """Start one fixed approval-gated live demo from the terminal shell."""
    _render_live_demo_catalogue()
    try:
        profile = normalize_llm_profile(
            _prompt_with_cancellation(
                "Provider", default=_runtime_environment().get("LLM_PROFILE", "openai")
            )
        )
        case_id = _prompt_with_cancellation("Live demo case ID").strip()
        select_live_demo_case(case_id)
    except InteractiveFlowCancelled:
        typer.echo("live demo: cancelled; no provider request or local write was made")
        return
    except ValueError:
        typer.echo("live demo: choose openai, claude, or openrouter; no provider request was made")
        return
    except LiveDemoSelectionError:
        typer.echo(
            "live demo: choose a listed case ID; no provider request or local write was made"
        )
        return

    _run_shell_command(lambda: live_demo(profile=profile, case=case_id))


def _run_shell_command(command: Callable[[], None]) -> None:
    """Let an operator read a command result before the next menu replaces it."""
    try:
        command()
    except typer.Exit:
        pass
    typer.prompt("Press Enter to return to the menu", default="", show_default=False)


@app.command()
def status() -> None:
    """Read pending approvals and workflow recovery state without prompting or writing."""
    try:
        snapshot = PostgresOperatorStatusAdapter(_database_url(action="status")).read_status()
    except SQLAlchemyError as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.FAILED,
                summary="Local operator status is unavailable.",
                data={},
                next_actions=("Run make demo first, then retry enterprise-agent status.",),
                error=TerminalError(
                    code="database_unavailable",
                    message="The local database is unavailable.",
                ),
            )
        ):
            raise typer.Exit(code=1) from error
        typer.echo(
            "status: unavailable (the local database is unavailable; run make demo first)",
            err=True,
        )
        raise typer.Exit(code=1) from error
    if _uses_json_output():
        _emit_json_result(_operator_status_result(snapshot))
        return
    _render_operator_status(snapshot)


@app.command(name="llm-setup")
def llm_setup() -> None:
    """Interactively save one selected LLM profile locally without displaying its API key."""
    if _uses_json_output():
        _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="LLM setup requires interactive text output.",
                data={},
                next_actions=("Run enterprise-agent llm-setup in an interactive terminal.",),
                error=TerminalError(
                    code="interactive_input_required",
                    message="No API key was requested or saved.",
                ),
            )
        )
        raise typer.Exit(code=1)
    if not _is_interactive_terminal():
        typer.echo(
            "configuration: setup refused (an interactive terminal is required; no key was requested)",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        _interactive_llm_setup()
    except InteractiveFlowCancelled:
        _exit_cancelled("configuration: cancelled; no settings were saved")


@app.command()
def run() -> None:
    """Bootstrap one configured LLM profile and direct the operator to explicit safe next steps."""
    if _uses_json_output():
        try:
            configuration = load_provider_settings(_runtime_environment())
        except (ConfigurationError, ValueError) as error:
            _emit_json_result(_configuration_refusal(action="run", error=error))
            raise typer.Exit(code=1) from error
        _emit_json_result(
            TerminalResult(
                state=TerminalState.SUCCEEDED,
                summary="LLM profile is ready.",
                data={"profile": configuration.profile, "model": configuration.model},
                next_actions=("enterprise-agent demo --list", "enterprise-agent llm-smoke"),
            )
        )
        return
    try:
        configuration = _provider_configuration_or_setup(action="run")
    except InteractiveFlowCancelled:
        _exit_cancelled("configuration: cancelled; no settings were saved")
    typer.echo(
        "run: LLM profile ready "
        f"(profile: {configuration.profile}, model: {configuration.model}); "
        "next: enterprise-agent demo --list for the local safety tour or enterprise-agent llm-smoke "
        "for a no-business-data provider check."
    )


@app.command(name="llm-smoke")
def llm_smoke() -> None:
    """Run one deliberate fixed-input provider probe without any business-system data or writes."""
    try:
        configuration = load_provider_settings(_runtime_environment())
    except (ConfigurationError, ValueError) as error:
        if _emit_json_result(_configuration_refusal(action="llm smoke", error=error)):
            raise typer.Exit(code=1) from error
        typer.echo(f"configuration: llm smoke refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    try:
        result = run_smoke(configuration)
    except ValueError as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="LLM smoke probe was refused.",
                data={"profile": configuration.profile, "model": configuration.model},
                next_actions=(
                    "Review the configured profile, then retry enterprise-agent llm-smoke.",
                ),
                error=TerminalError(
                    code="smoke_refused", message="The fixed probe could not start safely."
                ),
            )
        ):
            raise typer.Exit(code=1) from error
        typer.echo(f"llm-smoke: refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    message = (
        f"llm-smoke: {result.status.value} "
        f"(profile: {configuration.profile}, model: {configuration.model}; no business data was sent)"
    )
    if _uses_json_output():
        _emit_json_result(
            TerminalResult(
                state=(TerminalState.SUCCEEDED if result.is_success else TerminalState.FAILED),
                summary="LLM smoke probe completed."
                if result.is_success
                else "LLM smoke probe failed.",
                data={
                    "profile": configuration.profile,
                    "model": configuration.model,
                    "status": result.status.value,
                    "business_data_sent": False,
                },
                next_actions=("enterprise-agent llm-setup",) if not result.is_success else (),
                error=(
                    None
                    if result.is_success
                    else TerminalError(
                        code="smoke_failed",
                        message="The fixed no-business-data probe did not complete successfully.",
                    )
                ),
            )
        )
        if not result.is_success:
            raise typer.Exit(code=1)
        return
    if not result.is_success:
        typer.echo(message, err=True)
        raise typer.Exit(code=1)
    typer.echo(message)


@app.command(name="llm-evaluate")
def llm_evaluate(
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Configured provider profile to evaluate: openai, claude, or openrouter.",
        ),
    ] = None,
    case: Annotated[
        list[str] | None,
        typer.Option(
            "--case", "-c", help="Fixed synthetic evaluation case ID; repeat to choose several."
        ),
    ] = None,
    all_cases: Annotated[
        bool,
        typer.Option("--all", help="Evaluate the full fixed thirteen-case synthetic pack."),
    ] = False,
    list_cases: Annotated[
        bool,
        typer.Option(
            "--list",
            help="List the fixed evaluation cases without loading configuration or calling a provider.",
        ),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute", help="Explicitly authorize this no-write live-provider evaluation request."
        ),
    ] = False,
) -> None:
    """Manually score fixed synthetic cases through one named provider without any business-system write."""
    if list_cases:
        if profile is not None or case or all_cases or execute:
            _evaluation_refusal(
                summary="Evaluation listing cannot be combined with provider selection or execution.",
                code="invalid_arguments",
                message="Run enterprise-agent llm-evaluate --list by itself.",
                exit_code=2,
            )
        _emit_evaluation_catalog()
        return
    if profile is None:
        _evaluation_refusal(
            summary="Manual LLM evaluation requires an explicit provider profile.",
            code="explicit_profile_required",
            message="--profile is required; no provider was called.",
            exit_code=1,
        )
    if not execute:
        _evaluation_refusal(
            summary="Manual LLM evaluation requires explicit execution.",
            code="explicit_execution_required",
            message="--execute is required; no provider was called.",
            exit_code=1,
        )
    try:
        selected_cases = select_evaluation_cases(tuple(case or ()), include_all=all_cases)
    except EvaluationCaseSelectionError as error:
        _evaluation_refusal(
            summary="Manual LLM evaluation case selection was refused.",
            code="invalid_evaluation_selection",
            message="Select one named case or the full fixed pack.",
            exit_code=2,
            error=error,
        )
    try:
        configuration = load_provider_profile(profile, _runtime_environment())
    except (ConfigurationError, ValueError) as error:
        if _emit_json_result(_configuration_refusal(action="LLM evaluation", error=error)):
            raise typer.Exit(code=1) from error
        typer.echo(
            "configuration: LLM evaluation refused (configured profile is unavailable)", err=True
        )
        raise typer.Exit(code=1) from error

    report = evaluate_cases(selected_cases, create_no_write_adapter(configuration))
    result = _llm_evaluation_result(configuration, report)
    if _emit_json_result(result):
        if not report.passed:
            raise typer.Exit(code=1)
        return
    _render_llm_evaluation(
        configuration,
        report,
        case_titles={item.case_id: item.title for item in selected_cases},
    )
    if not report.passed:
        raise typer.Exit(code=1)


@app.command(name="llm-usage")
def llm_usage() -> None:
    """Summarize safe provider token and cost facts from the append-only audit ledger."""
    try:
        events = PostgresAuditAdapter(_database_url(action="llm usage")).llm_usage_events()
    except SQLAlchemyError as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.FAILED,
                summary="LLM usage ledger is unavailable.",
                data={},
                next_actions=("Run make demo first, then retry enterprise-agent llm-usage.",),
                error=TerminalError(
                    code="database_unavailable",
                    message="The local database is unavailable.",
                ),
            )
        ):
            raise typer.Exit(code=1) from error
        _terminal_presenter().render_status(
            StatusSummary(
                state=TerminalState.FAILED,
                summary="LLM usage ledger is unavailable.",
                next_action="Start the terminal UI with make tui, then choose LLM usage again.",
            )
        )
        raise typer.Exit(code=1) from error
    summary = summarize_llm_usage(events)
    if _uses_json_output():
        _emit_json_result(_llm_usage_result(summary))
        return
    _render_llm_usage_summary(summary)


@app.command()
def reset() -> None:
    """Remove data from the strictly limited local demo database."""
    if _uses_json_output():
        _refuse_json_write(command="reset", summary="Reset requires interactive text confirmation.")
    database_url = _database_url()
    try:
        _confirm_local_write(
            ConfirmationSummary(
                action="Reset local demo data",
                target="the local synthetic demo database",
                effect="Removes only synthetic harness records and resets deterministic time.",
                freshness="Any pending plan or workflow will no longer be available after reset.",
                write_consequence="Deletes local demo records only; no provider request is made.",
                confirmation_word="reset",
            )
        )
    except InteractiveFlowCancelled:
        _exit_cancelled("operation: cancelled; no data was written")
    try:
        reset_database(database_url)
    except SeedSafetyError as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="Reset was refused by the local-demo safety guard.",
                data={},
                next_actions=("Check DATABASE_URL, then retry enterprise-agent reset.",),
                error=TerminalError(
                    code="local_demo_guard", message="The requested database is not allowed."
                ),
            )
        ):
            raise typer.Exit(code=1) from error
        typer.echo(f"database: reset refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    typer.echo("database: reset")


@app.command()
def seed() -> None:
    """Insert the fixed dataset into the strictly limited local demo database."""
    if _uses_json_output():
        _refuse_json_write(command="seed", summary="Seed requires interactive text confirmation.")
    database_url = _database_url()
    try:
        _confirm_local_write(
            ConfirmationSummary(
                action="Seed local demo data",
                target="the local synthetic demo database",
                effect="Inserts the fixed, deterministic harness dataset.",
                freshness="The seed resets demo time and establishes the known evidence baseline.",
                write_consequence="Writes local synthetic records only; no provider request is made.",
                confirmation_word="seed",
            )
        )
    except InteractiveFlowCancelled:
        _exit_cancelled("operation: cancelled; no data was written")
    try:
        seed_database(database_url)
    except SeedSafetyError as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="Seed was refused by the local-demo safety guard.",
                data={},
                next_actions=("Check DATABASE_URL, then retry enterprise-agent seed.",),
                error=TerminalError(
                    code="local_demo_guard", message="The requested database is not allowed."
                ),
            )
        ):
            raise typer.Exit(code=1) from error
        typer.echo(f"database: seed refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    typer.echo("database: seeded")


@app.command(name="scenario-c")
def scenario_c() -> None:
    """Stage the fixed local supplier-risk scenario for human review without any automatic write."""
    if _uses_json_output():
        _refuse_json_write(
            command="scenario-c",
            summary="Scenario C staging requires interactive text confirmation.",
        )
    database_url = _database_url(action="scenario-c")
    try:
        _require_local_demo_database(database_url, allow_test_database=False)
    except (SeedSafetyError, ValueError) as error:
        typer.echo(f"scenario-c: refused ({error})", err=True)
        raise typer.Exit(code=1) from error
    try:
        _confirm_local_write(
            ConfirmationSummary(
                action="Stage Scenario C review",
                target="the local synthetic supplier-risk scenario",
                effect="Creates one pending approval and workflow; it does not hold a purchase order.",
                freshness="Execution revalidates the approved plan's evidence before any hold.",
                write_consequence="Writes only the local demo attention, plan, approval, and workflow.",
                confirmation_word="stage",
            )
        )
    except InteractiveFlowCancelled:
        _exit_cancelled("operation: cancelled; no data was written")
    try:
        staged = stage_scenario_c_pending(database_url, run_id=RunId("run-scenario-c-cli"))
    except (ScenarioCDeterministicRunError, SeedSafetyError, ValueError) as error:
        typer.echo(f"scenario-c: refused ({error})", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        "scenario-c: pending approval\n"
        f"run: {staged.run_id}\n"
        f"attention: {staged.attention_id}\n"
        f"approval: {staged.approval_id}\n"
        f"workflow: {staged.workflow_id}\n"
        "next: review and approve this exact plan before it can execute"
    )


@app.command(name="live-demo")
def live_demo(
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Configured provider profile: openai, claude, or openrouter.",
        ),
    ] = None,
    case: Annotated[
        str | None,
        typer.Option("--case", help="Fixed synthetic Scenario A, B, or C live-demo case ID."),
    ] = None,
    list_cases: Annotated[
        bool,
        typer.Option(
            "--list", help="List fixed local cases without reading configuration or a provider."
        ),
    ] = False,
) -> None:
    """Ask one selected provider for a proposal over fixed local data, then stage review only."""
    if list_cases:
        if profile is not None or case is not None:
            _live_demo_refusal(
                summary="Live-demo listing cannot be combined with provider or case selection.",
                code="invalid_arguments",
                message="Run enterprise-agent live-demo --list by itself.",
                exit_code=2,
            )
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.SUCCEEDED,
                summary="Guarded local live-demo catalogue.",
                data={"cases": [_live_demo_case_data(item) for item in live_demo_cases()]},
                next_actions=(
                    "Run enterprise-agent live-demo --profile PROVIDER --case CASE_ID in an interactive terminal.",
                ),
            )
        ):
            return
        _render_live_demo_catalogue()
        return

    if profile is None:
        _live_demo_refusal(
            summary="Guarded live demo requires an explicit provider profile.",
            code="explicit_profile_required",
            message="--profile is required; no provider was called and no data was reset.",
            exit_code=1,
        )
    if case is None:
        _live_demo_refusal(
            summary="Guarded live demo requires one fixed scenario case.",
            code="explicit_case_required",
            message="--case is required; no provider was called and no data was reset.",
            exit_code=1,
        )
    if _uses_json_output():
        _refuse_json_write(
            command="live-demo",
            summary="Live demo requires interactive text confirmation.",
        )
    if not _is_interactive_terminal():
        typer.echo(
            "live-demo: refused (an interactive terminal is required for explicit confirmation; "
            "no provider was called and no data was reset)",
            err=True,
        )
        raise typer.Exit(code=1)

    assert profile is not None and case is not None
    try:
        selected_case = select_live_demo_case(case)
        configuration = load_provider_profile(profile, _runtime_environment())
        database_url = _database_url(action="live-demo")
        _require_local_demo_database(database_url, allow_test_database=False)
    except LiveDemoSelectionError as error:
        _live_demo_refusal(
            summary="Guarded live-demo case selection was refused.",
            code="invalid_live_demo_selection",
            message="Choose one case displayed by enterprise-agent live-demo --list.",
            exit_code=2,
            error=error,
        )
    except (ConfigurationError, ValueError) as error:
        if _emit_json_result(_configuration_refusal(action="live demo", error=error)):
            raise typer.Exit(code=1) from error
        typer.echo(
            "configuration: live demo refused (configured provider profile is unavailable)",
            err=True,
        )
        raise typer.Exit(code=1) from error
    except SeedSafetyError as error:
        typer.echo(f"live-demo: refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    try:
        _confirm_local_write(
            ConfirmationSummary(
                action=f"Run live {selected_case.title}",
                target="the local synthetic demo database and one selected provider",
                effect=(
                    "Resets and seeds fixed local data, sends one bounded proposal request, and stages "
                    "at most one human approval."
                ),
                freshness=(
                    "The proposal is schema-validated and deterministically gated; any later workflow "
                    "execution rechecks freshness."
                ),
                write_consequence=(
                    "Calls the selected provider and writes only local synthetic attention, plan, approval, "
                    "workflow, schedule, and audit records. No business tool effect is executed."
                ),
                confirmation_word="live",
            )
        )
    except InteractiveFlowCancelled:
        _exit_cancelled("live-demo: cancelled; no provider was called and no data was written")

    try:
        result = run_live_demo(
            database_url,
            configuration=configuration,
            case_id=selected_case.case_id,
        )
    except (LiveDemoExecutionError, SeedSafetyError, ValueError) as error:
        typer.echo(f"live-demo: refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    _render_live_demo(result)


@app.command()
def demo(
    case: Annotated[
        list[str] | None,
        typer.Option(
            "--case",
            "-c",
            help="Guided case ID; repeat for several cases. Defaults to safety-tour.",
        ),
    ] = None,
    all_cases: Annotated[
        bool,
        typer.Option("--all", help="Run every guided deterministic case."),
    ] = False,
    list_cases: Annotated[
        bool,
        typer.Option("--list", help="List cases without a database read or write."),
    ] = False,
    unattended: Annotated[
        bool,
        typer.Option(
            "--unattended",
            help="Explicitly skip the local-demo confirmation for scripts and CI.",
        ),
    ] = False,
) -> None:
    """Reset, seed, and present selected local-only safety stories without a live LLM call."""
    if list_cases:
        if case or all_cases or unattended:
            if _emit_json_result(
                TerminalResult(
                    state=TerminalState.REFUSED,
                    summary="Guided demo list cannot be combined with selection or unattended mode.",
                    data={},
                    next_actions=("Run enterprise-agent demo --list by itself.",),
                    error=TerminalError(
                        code="invalid_arguments",
                        message="The requested demo options cannot be combined.",
                    ),
                )
            ):
                raise typer.Exit(code=2)
            typer.echo("demo: --list cannot be combined with selection or --unattended", err=True)
            raise typer.Exit(code=2)
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.SUCCEEDED,
                summary="Guided deterministic demo catalogue.",
                data={"cases": [_guided_demo_case_data(item) for item in guided_demo_cases()]},
                next_actions=("enterprise-agent demo --unattended",),
            )
        ):
            return
        _render_guided_demo_catalog()
        return
    try:
        selected = select_guided_demo_cases(tuple(case or ()), include_all=all_cases)
    except DemoCaseSelectionError as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="Guided demo selection was refused.",
                data={},
                next_actions=("Run enterprise-agent demo --list to choose a valid case.",),
                error=TerminalError(
                    code="invalid_demo_selection",
                    message="The requested guided-demo case selection is not valid.",
                ),
            )
        ):
            raise typer.Exit(code=2) from error
        typer.echo(f"demo: refused ({error})", err=True)
        raise typer.Exit(code=2) from error

    if _uses_json_output() and not unattended:
        _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="Guided demo requires --unattended when --output json is selected.",
                data={},
                next_actions=("Run enterprise-agent demo interactively or add --unattended.",),
                error=TerminalError(
                    code="interactive_confirmation_required",
                    message="No local demo data was reset or staged.",
                ),
            )
        )
        raise typer.Exit(code=1)

    database_url = _database_url(action="guided demo")
    if not unattended:
        try:
            _confirm_local_write(
                ConfirmationSummary(
                    action="Run guided deterministic demo",
                    target="the local synthetic demo database",
                    effect=(
                        "Resets and seeds the fixed data, then stages only the selected local "
                        "pending plans."
                    ),
                    freshness=(
                        "The reset establishes the known evidence baseline; all staged plans remain "
                        "freshness-gated before effects."
                    ),
                    write_consequence=(
                        "Writes only local synthetic data. No live provider, credential, or business "
                        "system is called."
                    ),
                    confirmation_word="demo",
                )
            )
        except InteractiveFlowCancelled:
            _exit_cancelled("demo: cancelled; no data was written")

    try:
        result = run_guided_demo(database_url, case_ids=tuple(item.case_id for item in selected))
    except (SeedSafetyError, ValueError) as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="Guided demo was refused by the local-demo safety guard.",
                data={},
                next_actions=("Check DATABASE_URL, then retry enterprise-agent demo.",),
                error=TerminalError(
                    code="local_demo_guard",
                    message="The requested local demo operation cannot run against this database.",
                ),
            )
        ):
            raise typer.Exit(code=1) from error
        typer.echo(f"demo: refused ({error})", err=True)
        raise typer.Exit(code=1) from error
    if _uses_json_output():
        _emit_json_result(_guided_demo_result(result))
        return
    _render_guided_demo(result)


def _guide_entries() -> tuple[CommandGuideEntry, ...]:
    """Keep text and JSON command discovery on one safe, copyable directory."""
    return (
        CommandGuideEntry(
            command="enterprise-agent demo --list",
            purpose="See selectable local deterministic safety cases before resetting demo data.",
        ),
        CommandGuideEntry(
            command="make demo",
            purpose="Run the unattended local safety tour with no LLM provider call.",
        ),
        CommandGuideEntry(
            command="enterprise-agent live-demo --list",
            purpose="Inspect the guarded one-provider local Scenario A, B, and C catalogue before opting in.",
        ),
        CommandGuideEntry(
            command="enterprise-agent status",
            purpose="Inspect pending approvals plus workflow and recovery state.",
        ),
        CommandGuideEntry(
            command="enterprise-agent audit explain RUN_ID",
            purpose="Reconstruct one run from the append-only audit ledger.",
        ),
        CommandGuideEntry(
            command="enterprise-agent llm-usage",
            purpose="Read recorded token and cost totals without a provider request.",
        ),
        CommandGuideEntry(
            command="enterprise-agent llm-evaluate --list",
            purpose="Inspect the optional synthetic no-write live-LLM evaluation pack before opting in.",
        ),
    )


def _live_demo_case_data(item: LiveDemoCase) -> dict[str, str]:
    """Project the fixed live-demo catalogue without exposing a provider prompt or seed payload."""
    return {
        "case_id": str(item.case_id),
        "scenario": item.scenario,
        "title": item.title,
        "response_schema": item.response_schema,
    }


def _render_live_demo_catalogue() -> None:
    """Show fixed live-eligible local stories before configuration, reset, or provider composition."""
    presenter = _terminal_presenter()
    presenter.render_header(
        title="Guarded live local demo",
        subtitle=(
            "Fixed synthetic data · one selected provider proposal · schema validation and approval "
            "gate remain mandatory"
        ),
    )
    presenter.render_status(
        StatusSummary(
            state=TerminalState.SUCCEEDED,
            summary="Listing is local and does not load credentials, reset data, or call a provider.",
            next_action=(
                "Run enterprise-agent live-demo --profile PROVIDER --case CASE_ID in an interactive terminal."
            ),
        )
    )
    presenter.render_text_table(
        title="Fixed live-demo cases",
        columns=("Case", "Scenario", "Purpose"),
        rows=tuple((str(item.case_id), item.scenario, item.title) for item in live_demo_cases()),
    )
    for item in live_demo_cases():
        presenter.render_story_brief(
            StoryBrief(item.scenario, item.story, item.safety_rule, item.facts)
        )


def _live_demo_terminal_state(result: LiveDemoResult) -> TerminalState:
    """Map an already-safe control result to the narrow terminal status vocabulary."""
    match result.provenance.gate_status:
        case GateStatus.PENDING_APPROVAL:
            return TerminalState.PENDING_APPROVAL
        case GateStatus.NOT_INVOKED_MANUAL_REVIEW:
            return TerminalState.MANUAL_REVIEW
        case GateStatus.NOT_INVOKED_NO_ACTION:
            return TerminalState.SUCCEEDED
        case GateStatus.NOT_INVOKED_PLANNER_FAILURE:
            return TerminalState.FAILED
        case GateStatus.DENIED:
            return TerminalState.REFUSED
        case _:
            return TerminalState.REFUSED


def _live_demo_next_action(result: LiveDemoResult) -> str:
    """State the one safe follow-up without implying that a provider proposal is executable."""
    match result.provenance.gate_status:
        case GateStatus.PENDING_APPROVAL:
            return f"Inspect enterprise-agent audit explain {result.run_id}, then decide the displayed approval."
        case GateStatus.NOT_INVOKED_MANUAL_REVIEW:
            return "No plan was created; resolve the ambiguity through the designated human review process."
        case GateStatus.NOT_INVOKED_NO_ACTION:
            return "No plan was created because the validated proposal selected no action."
        case GateStatus.NOT_INVOKED_PLANNER_FAILURE:
            return "No plan was created; inspect the safe provider status and retry only after correction."
        case GateStatus.DENIED:
            return "No plan was created because deterministic policy denied the validated proposal."
        case _:
            return "No plan was created."


def _render_live_demo(result: LiveDemoResult) -> None:
    """Render only sanitized result identifiers and metering, never a prompt or provider-owned text."""
    presenter = _terminal_presenter()
    presenter.render_header(
        title="Guarded live local demo",
        subtitle=(
            "LIVE provider proposal over fixed synthetic data · no business tool effect is executed "
            "without a separate approval and freshness check"
        ),
    )
    presenter.render_demo_case(
        state=_live_demo_terminal_state(result),
        title=result.case.title,
        phase="Provider proposal → schema validation → deterministic policy → approval-gated stage",
        provenance=result.provenance,
        mode=(
            "The selected provider made one bounded proposal. It cannot execute a business tool or "
            "bypass the existing approval and workflow controls."
        ),
        outcome=result.outcome or "No usable canonical recommendation was produced.",
        next_action=_live_demo_next_action(result),
        validation_category=result.validation_category,
    )
    presenter.render_story_brief(
        StoryBrief(
            result.case.scenario, result.case.story, result.case.safety_rule, result.case.facts
        )
    )
    presenter.render_text_table(
        title="Live-demo control records",
        columns=(
            "Provider status",
            "Run ID",
            "Attention ID",
            "Plan ID",
            "Approval ID",
            "Workflow ID",
            "Escalation task ID",
        ),
        rows=(
            (
                result.planner_status.value,
                str(result.run_id),
                str(result.attention_id),
                str(result.plan_id or "not created"),
                str(result.approval_id or "not created"),
                str(result.workflow_id or "not created"),
                str(result.escalation_task_id or "not created"),
            ),
        ),
    )
    if result.usage is not None:
        presenter.render_text_table(
            title="Selected-provider request metering",
            columns=("Input tokens", "Output tokens", "Total tokens", "Cost (USD)", "Cost source"),
            rows=(
                (
                    str(result.usage.input_tokens),
                    str(result.usage.output_tokens),
                    str(result.usage.total_tokens),
                    str(result.usage.cost_usd)
                    if result.usage.cost_usd is not None
                    else "unavailable",
                    result.usage.cost_source.value,
                ),
            ),
        )


def _guided_demo_case_data(item: GuidedDemoCase) -> dict[str, object]:
    """Project a guided case to safe scalar fields for the JSON catalogue and result envelope."""
    return {
        "case_id": item.case_id,
        "title": item.title,
        "execution_mode": item.execution_mode.value,
        "planner": item.planner_provenance.to_data(),
        "phase": item.phase,
        "outcome": item.outcome,
        "next_action": item.next_safe_action,
    }


def _guided_demo_catalogue_entries() -> tuple[DemoCatalogueEntry, ...]:
    """Project only concise case labels into the read-only terminal catalogue."""
    return tuple(
        DemoCatalogueEntry(
            key=item.case_id,
            title=item.title,
            summary=_demo_catalogue_summary(item),
            mode=_demo_execution_mode_label(item.execution_mode),
        )
        for item in guided_demo_cases()
    )


def _demo_catalogue_summary(item: GuidedDemoCase) -> str:
    """Keep the catalogue scannable while the selected-case panel retains the full result."""
    summaries = {
        "scenario-a-reroute-bait": "Rejects the cheaper, faster supplier because it is unapproved.",
        "scenario-a-crash-recovery": "Restarts after a replacement-PO crash without creating a duplicate.",
        "scenario-a-current-evidence": "Uses the newest update and treats hostile email text as evidence only.",
        "scenario-a-tuesday-follow-up": "Closes on full receipt or creates one freshness-bound follow-up.",
        "scenario-b-capacity": "Does not reallocate insufficient or already committed quality capacity.",
        "scenario-c-pending-review": "Stages a supplier-risk review; no hold or notification occurs yet.",
    }
    return summaries[item.case_id]


def _demo_execution_mode_label(mode: DemoExecutionMode) -> str:
    """Name whether a catalogue option stages guarded state or explains a fixed fixture."""
    if mode is DemoExecutionMode.STAGE_PENDING:
        return "Stages a local pending plan"
    return "Fixture walkthrough (no workflow effect)"


def _guided_demo_result(result: GuidedDemoRun) -> TerminalResult:
    """Serialize only selected demo facts, never a provider payload or tool result."""
    results: list[dict[str, object]] = []
    has_pending_approval = False
    for item in result.results:
        case_data = _guided_demo_case_data(item.case)
        case_data["identifiers"] = [
            {
                "label": identifier.label,
                "value": identifier.value,
                "disposition": (
                    EvidenceDisposition.INCLUDED.value
                    if identifier.included
                    else EvidenceDisposition.EXCLUDED.value
                ),
            }
            for identifier in item.identifiers
        ]
        if item.scenario_a_pending is not None:
            has_pending_approval = True
            case_data["pending_approval"] = {
                "run_id": str(item.scenario_a_pending.run_id),
                "attention_id": str(item.scenario_a_pending.attention_id),
                "plan_id": str(item.scenario_a_pending.plan_id),
                "approval_id": str(item.scenario_a_pending.approval_id),
                "expires_at": item.scenario_a_pending.approval_expires_at,
            }
        if item.scenario_c_pending is not None:
            has_pending_approval = True
            case_data["pending_approval"] = {
                "run_id": str(item.scenario_c_pending.run_id),
                "attention_id": str(item.scenario_c_pending.attention_id),
                "approval_id": str(item.scenario_c_pending.approval_id),
                "workflow_id": str(item.scenario_c_pending.workflow_id),
            }
        results.append(case_data)
    return TerminalResult(
        state=(TerminalState.PENDING_APPROVAL if has_pending_approval else TerminalState.SUCCEEDED),
        summary="Guided deterministic demo completed with local synthetic data only.",
        data={"cases": results},
        next_actions=tuple(item.case.next_safe_action for item in result.results),
    )


def _render_guided_demo_catalog() -> None:
    """Print the safe catalogue before an operator chooses whether to reset local data."""
    entries = (
        DemoCatalogueEntry(
            key=_SAFETY_TOUR_LABEL,
            title="Safety tour",
            summary="Runs the short cross-scenario proof set in its fixed order.",
            mode="Local reset, seed, and guarded pending-plan stages",
        ),
        *_guided_demo_catalogue_entries(),
    )
    presenter = _terminal_presenter()
    presenter.render_demo_catalogue(
        entries=entries,
        prompt="Run enterprise-agent demo --case CASE_ID, or enterprise-agent demo for the safety tour.",
    )
    _render_guided_story_briefs(presenter)


def _render_guided_story_briefs(presenter: TerminalPresenter) -> None:
    """Show the deterministic case facts before a reviewer resets the local synthetic database."""
    for case in guided_demo_cases():
        presenter.render_story_brief(
            StoryBrief(
                case.title.split(" — ", maxsplit=1)[0],
                case.outcome,
                case.next_safe_action,
                tuple((identifier.label, identifier.value) for identifier in case.identifiers),
            )
        )


def _render_guided_demo(result: GuidedDemoRun) -> None:
    """Render only command-owned, safe summaries from the deterministic local runner."""
    presenter = _terminal_presenter()
    presenter.render_header(
        title="Guided deterministic demo",
        subtitle=(
            "Local synthetic data only · Planner: FAKE / DETERMINISTIC · no live provider or external business write"
        ),
    )
    for item in result.results:
        state = (
            TerminalState.PENDING_APPROVAL
            if item.case.execution_mode is DemoExecutionMode.STAGE_PENDING
            else TerminalState.SUCCEEDED
        )
        presenter.render_demo_case(
            state=state,
            title=item.case.title,
            phase=item.case.phase,
            provenance=item.case.planner_provenance,
            mode=(
                "Local pending-plan stage; no workflow effect is executed."
                if item.case.execution_mode is DemoExecutionMode.STAGE_PENDING
                else "Fixed acceptance-case walkthrough; no workflow effect is staged."
            ),
            outcome=item.case.outcome,
            next_action=item.case.next_safe_action,
        )
        presenter.render_story_brief(
            StoryBrief(
                item.case.title.split(" — ", maxsplit=1)[0],
                item.case.outcome,
                item.case.next_safe_action,
                tuple((identifier.label, identifier.value) for identifier in item.identifiers),
            )
        )
        presenter.render_evidence(
            tuple(
                EvidenceSummary(
                    evidence_id=identifier.value,
                    source="guided demo",
                    summary=identifier.label,
                    disposition=(
                        EvidenceDisposition.INCLUDED
                        if identifier.included
                        else EvidenceDisposition.EXCLUDED
                    ),
                )
                for identifier in item.identifiers
            )
        )
        if item.scenario_a_pending is not None:
            scenario_a_pending = item.scenario_a_pending
            presenter.render_approvals(
                (
                    ApprovalSummary(
                        approval_id=str(scenario_a_pending.approval_id),
                        plan_id=str(scenario_a_pending.plan_id),
                        requester="Dana Buyer",
                        approver="Dana Buyer",
                        decision_state="pending",
                        expires_at=scenario_a_pending.approval_expires_at,
                    ),
                )
            )
        if item.scenario_c_pending is not None:
            scenario_c_pending = item.scenario_c_pending
            presenter.render_workflows(
                (
                    WorkflowSummary(
                        workflow_id=str(scenario_c_pending.workflow_id),
                        status="pending_approval",
                        current_step="awaiting approval",
                        idempotency_key_prefix="not started",
                        recovery_state="freshness rechecked before effects",
                    ),
                )
            )


def _render_operator_status(snapshot: OperatorStatusSnapshot) -> None:
    """Render the safe local read model without issuing additional database or provider calls."""
    presenter = _terminal_presenter()
    presenter.render_header(
        title="Local operator status",
        subtitle="Read-only control-plane summary · no provider request or write",
    )
    if not snapshot.pending_approvals and not snapshot.workflows:
        presenter.render_status(
            StatusSummary(
                state=TerminalState.SUCCEEDED,
                summary="No pending approvals or workflow instances are recorded.",
                next_action="Run enterprise-agent demo --list to choose a local safety case.",
            )
        )
        return

    state = (
        TerminalState.PENDING_APPROVAL if snapshot.pending_approvals else TerminalState.IN_PROGRESS
    )
    presenter.render_status(
        StatusSummary(
            state=state,
            summary=(
                f"{len(snapshot.pending_approvals)} pending approval(s), "
                f"{len(snapshot.workflows)} workflow instance(s)."
            ),
            next_action="Use the full IDs below to inspect audit history or resume an authorized path.",
        )
    )
    if snapshot.pending_approvals:
        presenter.render_approvals(
            tuple(
                ApprovalSummary(
                    approval_id=item.approval_id,
                    plan_id=item.plan_id,
                    requester=item.requester,
                    approver=item.approver,
                    decision_state=item.decision_state,
                    expires_at=item.expires_at,
                )
                for item in snapshot.pending_approvals
            )
        )
        audit_actions = tuple(
            (
                item.audit_run_id,
                f"enterprise-agent audit explain {item.audit_run_id}",
            )
            for item in snapshot.pending_approvals
            if item.audit_run_id is not None
        )
        if audit_actions:
            presenter.render_text_table(
                title="Audit actions",
                columns=("Run ID", "Read-only command"),
                rows=audit_actions,
            )
    if snapshot.workflows:
        presenter.render_workflows(
            tuple(
                WorkflowSummary(
                    workflow_id=item.workflow_id,
                    status=item.status,
                    current_step=item.current_step,
                    idempotency_key_prefix=item.idempotency_key_prefix,
                    recovery_state=item.recovery_state.label,
                )
                for item in snapshot.workflows
            )
        )
    presenter.render_text_table(
        title="Next safe action",
        columns=("Read-only command",),
        rows=(("enterprise-agent llm-usage",),),
    )


def _operator_status_result(snapshot: OperatorStatusSnapshot) -> TerminalResult:
    """Project the read model to stable, safe scalar JSON without a second database query."""
    data = operator_status_data(snapshot)
    approvals = data["pending_approvals"]
    workflows = data["workflows"]
    audit_actions = tuple(
        dict.fromkeys(
            f"enterprise-agent audit explain {item.audit_run_id}"
            for item in snapshot.pending_approvals
            if item.audit_run_id is not None
        )
    )
    if not approvals and not workflows:
        return TerminalResult(
            state=TerminalState.SUCCEEDED,
            summary="No pending approvals or workflow instances are recorded.",
            data=data,
            next_actions=("enterprise-agent demo --list",),
        )
    return TerminalResult(
        state=(TerminalState.PENDING_APPROVAL if approvals else TerminalState.IN_PROGRESS),
        summary=f"{len(approvals)} pending approval(s), {len(workflows)} workflow instance(s).",
        data=data,
        next_actions=(*audit_actions, "enterprise-agent llm-usage"),
    )


_SAFETY_TOUR_LABEL = "safety-tour"


@clock_app.command("advance")
def clock_advance(
    hours: int = typer.Option(..., min=1, help="Positive whole number of demo hours to advance."),
) -> None:
    """Advance the persisted local-demo clock without reading or writing wall-clock time."""
    database_url = _database_url()
    try:
        _require_local_demo_database(database_url, allow_test_database=False)
        advanced_to = PostgresDemoClock(database_url).advance(timedelta(hours=hours))
    except (DemoClockNotInitializedError, SeedSafetyError, ValueError) as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="Demo-clock advance was refused.",
                data={"hours": hours},
                next_actions=("Run make demo first, then retry the clock advance.",),
                error=TerminalError(
                    code="local_demo_guard",
                    message="The requested clock operation cannot run against this local state.",
                ),
            )
        ):
            raise typer.Exit(code=1) from error
        typer.echo(f"clock: advance refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    if _emit_json_result(
        TerminalResult(
            state=TerminalState.SUCCEEDED,
            summary="Deterministic demo clock advanced.",
            data={"hours": hours, "current_at": advanced_to.isoformat()},
            next_actions=("enterprise-agent status",),
        )
    ):
        return
    typer.echo(f"clock: advanced to {advanced_to.isoformat()}")


@audit_app.command("explain")
def audit_explain(
    run_id: str = typer.Argument(..., help="Run ID to reconstruct from audit events."),
) -> None:
    """Print a chronological Scenario A story using only the selected run's audit ledger."""
    try:
        explanation = AuditExplainer(
            PostgresAuditAdapter(_database_url(action="audit explain"))
        ).explain(RunId(run_id))
    except AuditExplanationError as error:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary="Audit explanation was refused.",
                data={"run_id": run_id},
                next_actions=("Use enterprise-agent status to find a recorded audit run ID.",),
                error=TerminalError(
                    code="audit_run_unavailable",
                    message="The requested audit run could not be reconstructed.",
                ),
            )
        ):
            raise typer.Exit(code=1) from error
        typer.echo(f"audit: explain refused ({error})", err=True)
        raise typer.Exit(code=1) from error
    if _emit_json_result(
        TerminalResult(
            state=TerminalState.SUCCEEDED,
            summary="Audit explanation reconstructed from the immutable ledger.",
            data={"run_id": run_id, "explanation": explanation.render()},
            next_actions=("enterprise-agent status",),
        )
    ):
        return
    typer.echo(explanation.render())


def _database_url(*, action: str = "reset") -> str:
    """Read only the database setting needed by local reset and seed commands."""
    database_url = _runtime_environment().get("DATABASE_URL", "").strip()
    if not database_url:
        if _emit_json_result(
            TerminalResult(
                state=TerminalState.REFUSED,
                summary=f"{action.capitalize()} requires DATABASE_URL.",
                data={},
                next_actions=(f"Set DATABASE_URL, then run enterprise-agent {action}.",),
                error=TerminalError(
                    code="missing_configuration",
                    message="DATABASE_URL is required.",
                ),
            )
        ):
            raise typer.Exit(code=1)
        typer.echo(f"database: {action} refused (DATABASE_URL is required)", err=True)
        raise typer.Exit(code=1)
    return database_url


def _runtime_environment() -> dict[str, str]:
    """Load the local ignored profile file while keeping explicit process variables authoritative."""
    return load_local_environment(default_env_path(), environ)


def _provider_configuration_or_setup(*, action: str) -> ProviderConfiguration:
    """Load one valid local profile or start interactive setup only for the run bootstrap path."""
    try:
        return load_provider_settings(_runtime_environment())
    except (ConfigurationError, ValueError) as error:
        if not _is_interactive_terminal():
            typer.echo(
                "configuration: "
                f"{action} refused ({error}; run 'enterprise-agent llm-setup' in an interactive terminal)",
                err=True,
            )
            raise typer.Exit(code=1) from error
    return _interactive_llm_setup()


def _interactive_llm_setup() -> ProviderConfiguration:
    """Collect, optionally verify, and locally save one profile without echoing its secret key."""
    _terminal_presenter().render_header(
        title="LLM setup",
        subtitle="Choose one local provider profile. Type cancel at any prompt to leave without writing.",
    )
    _terminal_presenter().render_text_table(
        title="Provider profiles",
        columns=("Key", "Provider", "Use"),
        rows=(
            ("openai", "OpenAI", "Reviewed structured-output adapter"),
            ("claude", "Claude", "Reviewed structured-output adapter"),
            ("openrouter", "OpenRouter", "Reviewed JSON-only adapter"),
        ),
    )
    selected_profile = _prompt_with_cancellation("Provider")
    try:
        profile = normalize_llm_profile(selected_profile)
    except ValueError as error:
        typer.echo(f"configuration: setup refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(
        "Your API key input is hidden. It is never displayed or logged; it is saved only if you "
        "confirm this local profile."
    )
    api_key = _prompt_with_cancellation(f"{profile.title()} API key", hide_input=True)
    try:
        catalog = discover_compatible_models(profile, api_key)
    except (ModelDiscoveryError, ValueError) as error:
        typer.echo(f"configuration: setup refused ({error})", err=True)
        raise typer.Exit(code=1) from error
    verified = False
    if typer.confirm("Verify this key with a no-generation request now?", default=False):
        try:
            verified = verify_credential(profile, api_key)
        except ValueError as error:
            typer.echo(f"configuration: setup refused ({error})", err=True)
            raise typer.Exit(code=1) from error
        if not verified:
            typer.echo(
                "configuration: credential verification failed; settings were not saved",
                err=True,
            )
            raise typer.Exit(code=1)

    model = _prompt_model_choice(catalog)
    selection = LLMSetupSelection(profile=profile, api_key=api_key, model=model)
    _confirm_local_write(
        ConfirmationSummary(
            action="Save local LLM profile",
            target=f"{default_env_path()} with owner-only permissions",
            effect=f"Selects the {profile} provider profile and chosen model.",
            freshness="Suggested models are adapter-reviewed and visible to this key; custom IDs are explicit.",
            write_consequence="Writes the selected profile, key, and model while preserving other profiles.",
            confirmation_word="save",
        )
    )
    try:
        save_llm_profile(default_env_path(), selection)
    except (OSError, ValueError) as error:
        typer.echo(f"configuration: setup refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    prefix = PROFILE_ENVIRONMENT_PREFIXES[profile]
    environ.update(
        {
            "LLM_PROFILE": profile,
            f"{prefix}_API_KEY": api_key,
            f"{prefix}_MODEL": model,
        }
    )

    typer.echo(f"configuration: saved profile {profile} with model {model}")
    if not verified:
        typer.echo("configuration: key saved without live verification")
    return ProviderConfiguration(profile=profile, model=model, api_key=api_key)


def _prompt_model_choice(catalog: tuple[CuratedModel, ...]) -> str:
    """Display only curated adapter-reviewed models plus an explicit custom-ID selection."""
    _terminal_presenter().render_text_table(
        title="Adapter-compatible models visible to this key",
        columns=("Key", "Model", "Recommendation"),
        rows=tuple(
            (
                str(index),
                f"{model.label}: {model.model_id}",
                "Recommended" if model.recommended else "Reviewed option",
            )
            for index, model in enumerate(catalog, start=1)
        ),
    )
    custom_index = len(catalog) + 1
    _terminal_presenter().render_text_table(
        title="Custom model",
        columns=("Key", "Choice", "Boundary"),
        rows=(
            (
                str(custom_index),
                "Enter a custom model ID",
                "Explicit only; it is not promoted as a reviewed suggestion.",
            ),
        ),
    )
    selection = _prompt_with_cancellation("Model choice", default="1").strip().lower()
    if selection == "custom" or selection == str(custom_index):
        return _prompt_with_cancellation("Custom model ID")
    try:
        selected_index = int(selection)
    except ValueError as error:
        raise typer.BadParameter("choose a listed model number or custom") from error
    if not 1 <= selected_index <= len(catalog):
        raise typer.BadParameter("choose a listed model number or custom")
    return catalog[selected_index - 1].model_id


def _terminal_presenter() -> TerminalPresenter:
    """Create one terminal-bound presentation adapter only after command semantics are known."""
    return TerminalPresenter(
        console=Console(file=sys.stdout, no_color=_output_options().no_color),
        theme=TerminalTheme(),
    )


def _output_options() -> OutputOptions:
    """Read root CLI presentation options without coupling commands to Click context internals."""
    context = get_current_context(silent=True)
    if context is not None and isinstance(context.obj, OutputOptions):
        return context.obj
    return OutputOptions()


def _uses_json_output() -> bool:
    """Keep command semantics separate from the decision to serialize the final result."""
    return _output_options().mode is OutputMode.JSON


def _configuration_refusal(*, action: str, error: Exception) -> TerminalResult:
    """Return safe configuration guidance without serializing an exception or a credential value."""
    del error
    return TerminalResult(
        state=TerminalState.REFUSED,
        summary=f"{action.capitalize()} requires a valid local LLM configuration.",
        data={},
        next_actions=("Run enterprise-agent llm-setup in an interactive terminal.",),
        error=TerminalError(
            code="missing_configuration",
            message="A required local profile setting is missing or invalid.",
        ),
    )


def _llm_usage_result(summary: LLMUsageSummary) -> TerminalResult:
    """Expose only normalized, immutable scalar metering in the standard result envelope."""
    return TerminalResult(
        state=TerminalState.SUCCEEDED,
        summary="LLM usage reconstructed from the immutable audit ledger.",
        data={
            "lines": [
                {
                    "provider": line.provider,
                    "model": line.model,
                    "request_count": line.request_count,
                    "input_tokens": line.input_tokens,
                    "cached_input_tokens": line.cached_input_tokens,
                    "output_tokens": line.output_tokens,
                    "total_tokens": line.total_tokens,
                    "cost_usd": str(line.cost_usd),
                    "estimated_cost_usd": str(line.estimated_cost_usd),
                    "provider_reported_cost_usd": str(line.provider_reported_cost_usd),
                    "estimated_request_count": line.estimated_request_count,
                    "provider_reported_request_count": line.provider_reported_request_count,
                    "unknown_cost_request_count": line.unknown_cost_request_count,
                    "unmetered_request_count": line.unmetered_request_count,
                }
                for line in summary.lines
            ],
            "total_cost_usd": str(summary.total_cost_usd),
        },
        next_actions=("enterprise-agent status",),
    )


def _render_llm_usage_summary(summary: LLMUsageSummary) -> None:
    """Render immutable metering as a compact table instead of a wrapped text report."""
    presenter = _terminal_presenter()
    presenter.render_header(
        title="LLM usage",
        subtitle="Immutable audit ledger · provider billing remains authoritative",
    )
    if not summary.lines:
        presenter.render_status(
            StatusSummary(
                state=TerminalState.SUCCEEDED,
                summary="No LLM calls have been recorded.",
                next_action="Use Normal operator mode to run an explicit no-business-data smoke check.",
            )
        )
        return
    presenter.render_text_table(
        title="Usage by configured model",
        columns=("Provider", "Model", "Requests", "Tokens", "Known cost (USD)"),
        rows=tuple(
            (
                item.provider,
                item.model,
                str(item.request_count),
                f"{item.total_tokens} total ({item.input_tokens} input / {item.output_tokens} output)",
                f"${item.cost_usd}",
            )
            for item in summary.lines
        ),
    )
    presenter.render_status(
        StatusSummary(
            state=TerminalState.SUCCEEDED,
            summary=(
                f"Known total: ${summary.total_cost_usd}. Estimates use the reviewed public rate card; "
                "provider billing is authoritative."
            ),
            next_action="Use enterprise-agent status to inspect the associated local control-plane state.",
        )
    )


def _evaluation_refusal(
    *,
    summary: str,
    code: str,
    message: str,
    exit_code: int,
    error: Exception | None = None,
) -> NoReturn:
    """Refuse an evaluation before adapter composition and never serialize configuration or provider errors."""
    result = TerminalResult(
        state=TerminalState.REFUSED,
        summary=summary,
        data={},
        next_actions=("Run enterprise-agent llm-evaluate --list to inspect fixed safe cases.",),
        error=TerminalError(code=code, message=message),
    )
    if _emit_json_result(result):
        raise typer.Exit(code=exit_code) from error
    typer.echo(f"llm-evaluate: refused ({message})", err=True)
    raise typer.Exit(code=exit_code) from error


def _live_demo_refusal(
    *,
    summary: str,
    code: str,
    message: str,
    exit_code: int,
    error: Exception | None = None,
) -> NoReturn:
    """Refuse a live-demo request before loading a key, resetting data, or composing an adapter."""
    result = TerminalResult(
        state=TerminalState.REFUSED,
        summary=summary,
        data={},
        next_actions=("Run enterprise-agent live-demo --list to inspect fixed local cases.",),
        error=TerminalError(code=code, message=message),
    )
    if _emit_json_result(result):
        raise typer.Exit(code=exit_code) from error
    typer.echo(f"live-demo: refused ({message})", err=True)
    raise typer.Exit(code=exit_code) from error


def _emit_evaluation_catalog() -> None:
    """List the fixed synthetic cases without loading profile settings or constructing a live adapter."""
    cases = evaluation_cases()
    if _emit_json_result(
        TerminalResult(
            state=TerminalState.SUCCEEDED,
            summary="Manual no-write LLM evaluation catalogue.",
            data={
                "cases": [
                    {
                        "case_id": item.case_id,
                        "scenario": item.scenario,
                        "title": item.title,
                        "expected_outcomes": sorted(item.expected_outcomes),
                    }
                    for item in cases
                ]
            },
            next_actions=(
                "Run enterprise-agent llm-evaluate --profile PROFILE --case CASE_ID --execute.",
            ),
        )
    ):
        return
    presenter = _terminal_presenter()
    presenter.render_header(
        title="Manual live-LLM evaluation",
        subtitle="Fixed synthetic inputs only · no workflow, ERP, mail, audit, or business-system write",
    )
    presenter.render_status(
        StatusSummary(
            state=TerminalState.SUCCEEDED,
            summary="Listing is local and makes no provider request.",
            next_action="Use --profile PROFILE --case CASE_ID --execute for one deliberate request.",
        )
    )
    presenter.render_evaluation_catalogue(
        tuple(
            EvaluationCatalogueEntry(
                case_id=item.case_id,
                scenario=item.scenario,
                expected_outcomes=", ".join(sorted(item.expected_outcomes)),
                story=item.title,
                safety_rule=_evaluation_safety_rule(item),
                facts=_evaluation_story_facts(item.facts),
            )
            for item in cases
        )
    )


def _evaluation_safety_rule(item: LLMEvaluationCase) -> str:
    """State the public policy check without exposing the evaluation prompt."""
    if item.tests_prompt_injection_resistance:
        return "Treat instruction-like evidence as data only; require human review."
    if item.tests_newest_evidence:
        return "Use the newest authorized evidence, not a superseded update."
    if item.tests_manual_review_under_ambiguity:
        return "Ambiguous, stale, or unauthorized evidence requires human review."
    return item.title


def _evaluation_story_facts(facts: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """Make the fixed synthetic facts readable without showing a provider prompt or response."""
    return tuple(
        (key.replace("_", " ").capitalize(), _evaluation_fact_value(value))
        for key, value in facts.items()
    )


def _evaluation_fact_value(value: object) -> str:
    """Format one owned synthetic fact compactly for a Rich story card."""
    if isinstance(value, Mapping):
        return ", ".join(
            f"{key.replace('_', ' ')}={_evaluation_fact_value(child)}"
            for key, child in value.items()
        )
    if isinstance(value, list):
        return ", ".join(_evaluation_fact_value(item) for item in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _llm_evaluation_result(
    configuration: ProviderConfiguration,
    report: LLMEvaluationReport,
) -> TerminalResult:
    """Wrap the scalar-only manual scorecard in the universal command JSON envelope."""
    provenance = _live_evaluation_provenance(configuration, report)
    data = {
        "profile": configuration.profile,
        "model": configuration.model,
        "planner": provenance.to_data(),
    } | report.to_data()
    if report.passed:
        return TerminalResult(
            state=TerminalState.SUCCEEDED,
            summary=(
                f"Manual LLM evaluation passed {report.passed_case_count}/{len(report.observations)} "
                "synthetic cases."
            ),
            data=data,
            next_actions=(
                (
                    "Save this JSON transcript if you need to retain the manual scorecard; "
                    "no ledger entry was written."
                ),
            ),
        )
    return TerminalResult(
        state=TerminalState.FAILED,
        summary=(
            f"Manual LLM evaluation passed {report.passed_case_count}/{len(report.observations)} "
            "synthetic cases."
        ),
        data=data,
        next_actions=(
            "Review the scalar scorecard; no workflow or business-system write was attempted.",
        ),
        error=TerminalError(
            code="evaluation_checks_failed",
            message="At least one synthetic evaluation criterion did not pass.",
        ),
    )


def _render_llm_evaluation(
    configuration: ProviderConfiguration,
    report: LLMEvaluationReport,
    *,
    case_titles: dict[str, str],
) -> None:
    """Render scorecards with reviewed case rules, never a provider response or rationale."""
    presenter = _terminal_presenter()
    provenance = _live_evaluation_provenance(configuration, report)
    presenter.render_header(
        title="Manual live-LLM evaluation",
        subtitle=(
            f"Planner: {provenance.mode_label} · Provider: {provenance.provider_label} · "
            f"Profile: {provenance.profile_label} · Model: {provenance.model} · fixed synthetic inputs · "
            "no business-system write"
        ),
    )
    presenter.render_planner_provenance(provenance)
    presenter.render_status(
        StatusSummary(
            state=TerminalState.SUCCEEDED if report.passed else TerminalState.FAILED,
            summary=(
                f"{report.passed_case_count}/{len(report.observations)} cases and "
                f"{report.passed_check_count}/{report.check_count} checks passed."
            ),
            next_action=(
                "Review the scalar scorecard; no workflow or business-system write was attempted."
                if not report.passed
                else "The selected synthetic evaluation pack is complete."
            ),
        )
    )
    presenter.render_text_table(
        title="Synthetic evaluation scorecard",
        columns=("Case", "Safety rule", "Expected", "Observed", "Provider status", "Checks"),
        rows=tuple(
            (
                observation.case_id,
                case_titles[observation.case_id],
                ", ".join(observation.expected_outcomes),
                observation.observed_outcome or "no structured outcome",
                observation.status.value,
                ", ".join(f"{name}={state.value}" for name, state in observation.checks.items())
                + (
                    "; mismatched fields: " + ", ".join(observation.reference_mismatches)
                    if observation.reference_mismatches
                    else ""
                ),
            )
            for observation in report.observations
        ),
    )

    usage = report.usage
    presenter.render_text_table(
        title="Request metering",
        columns=("Requests", "Metered", "Tokens", "Cost (USD)"),
        rows=(
            (
                str(usage.request_count),
                f"{usage.metered_request_count} metered / {usage.unmetered_request_count} unavailable",
                f"{usage.total_tokens} total ({usage.input_tokens} input / {usage.output_tokens} output)",
                str(usage.total_cost_usd),
            ),
        ),
    )


def _live_evaluation_provenance(
    configuration: ProviderConfiguration,
    report: LLMEvaluationReport,
) -> PlannerProvenance:
    """Keep CLI text and JSON on the exact same safe live-evaluation provenance contract."""
    return live_evaluation_provenance(
        profile=configuration.profile,
        model=configuration.model,
        report=report,
    )


def _refuse_json_write(*, command: str, summary: str) -> NoReturn:
    """Keep confirmation-protected local writes explicit instead of silently skipping their receipt."""
    _emit_json_result(
        TerminalResult(
            state=TerminalState.REFUSED,
            summary=summary,
            data={},
            next_actions=(f"Run enterprise-agent {command} in an interactive terminal.",),
            error=TerminalError(
                code="interactive_confirmation_required",
                message="No local data was written.",
            ),
        )
    )
    raise typer.Exit(code=1)


def _emit_json_result(result: TerminalResult) -> bool:
    """Write exactly one schema-versioned JSON object when the operator selected JSON output."""
    if not _uses_json_output():
        return False
    typer.echo(result.render_json())
    return True


def _confirm_local_write(summary: ConfirmationSummary) -> None:
    """Require an explicit keyboard confirmation only when an interactive terminal is available."""
    if not _is_interactive_terminal():
        return
    _terminal_presenter().render_confirmation(summary)
    expected = summary.confirmation_word.lower()
    while True:
        response = _prompt_with_cancellation("Confirmation").strip().lower()
        if response == expected:
            return
        typer.echo(
            f"Enter {summary.confirmation_word} to continue or {_CANCELLATION_WORD} to stop."
        )


def _prompt_with_cancellation(
    message: str,
    *,
    default: str | None = None,
    hide_input: bool = False,
) -> str:
    """Collect one terminal value while reserving an explicit cancellation key for safe exit."""
    value = cast(str, typer.prompt(message, default=default, hide_input=hide_input))
    if value.strip().lower() == _CANCELLATION_WORD:
        raise InteractiveFlowCancelled
    return value


def _exit_cancelled(message: str) -> NoReturn:
    """Return the documented cancellation exit code after guaranteeing no local write occurred."""
    typer.echo(message)
    raise typer.Exit(code=130)


def _is_interactive_terminal() -> bool:
    """Prevent hidden prompts from blocking scripts, CI, or redirected terminal use."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def main() -> None:
    """Run the CLI entry point installed by the package."""
    app()
