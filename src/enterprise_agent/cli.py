"""Command-line interface for the enterprise agent harness."""

import sys
from datetime import timedelta
from os import environ
from typing import cast

import typer

from enterprise_agent import __version__
from enterprise_agent.adapters import (
    DemoClockNotInitializedError,
    PostgresAuditAdapter,
    PostgresDemoClock,
)
from enterprise_agent.application import AuditExplainer, AuditExplanationError
from enterprise_agent.config import (
    ConfigurationError,
    ProviderConfiguration,
    load_provider_settings,
    load_settings,
    normalize_llm_profile,
)
from enterprise_agent.domain import RunId
from enterprise_agent.llm_setup import (
    CuratedModel,
    LLMSetupSelection,
    curated_models_for,
    default_env_path,
    load_local_environment,
    save_llm_profile,
    verify_credential,
)
from enterprise_agent.seed import (
    SeedSafetyError,
    _require_local_demo_database,
    reset_database,
    seed_database,
)

app = typer.Typer(
    help="Operate the enterprise agent harness.",
    no_args_is_help=True,
)
clock_app = typer.Typer(help="Inspect and advance deterministic local-demo time.")
audit_app = typer.Typer(help="Reconstruct read-only operator stories from the audit ledger.")
app.add_typer(clock_app, name="clock")
app.add_typer(audit_app, name="audit")


@app.callback()
def harness() -> None:
    """Enterprise agent harness commands."""


@app.command()
def version() -> None:
    """Print the installed harness version."""
    typer.echo(f"enterprise-agent {__version__}")


@app.command(name="config-check")
def config_check() -> None:
    """Validate required runtime configuration without displaying credentials."""
    try:
        configuration = load_settings(_runtime_environment())
    except (ConfigurationError, ValueError) as error:
        typer.echo(f"configuration: invalid ({error})", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(configuration.safe_summary())


@app.command(name="llm-setup")
def llm_setup() -> None:
    """Interactively save one selected LLM profile locally without displaying its API key."""
    if not _is_interactive_terminal():
        typer.echo(
            "configuration: setup refused (an interactive terminal is required; no key was requested)",
            err=True,
        )
        raise typer.Exit(code=1)
    _interactive_llm_setup()


@app.command()
def run() -> None:
    """Bootstrap one configured LLM profile; full scenario execution is introduced with the M8 demo."""
    configuration = _provider_configuration_or_setup(action="run")
    typer.echo(
        "run: LLM profile ready "
        f"(profile: {configuration.profile}, model: {configuration.model}); "
        "scenario execution is not available until M8."
    )


@app.command()
def reset() -> None:
    """Remove data from the strictly limited local demo database."""
    database_url = _database_url()
    try:
        reset_database(database_url)
    except SeedSafetyError as error:
        typer.echo(f"database: reset refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    typer.echo("database: reset")


@app.command()
def seed() -> None:
    """Insert the fixed dataset into the strictly limited local demo database."""
    database_url = _database_url()
    try:
        seed_database(database_url)
    except SeedSafetyError as error:
        typer.echo(f"database: seed refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    typer.echo("database: seeded")


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
        typer.echo(f"clock: advance refused ({error})", err=True)
        raise typer.Exit(code=1) from error

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
        typer.echo(f"audit: explain refused ({error})", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(explanation.render())


def _database_url(*, action: str = "reset") -> str:
    """Read only the database setting needed by local reset and seed commands."""
    database_url = _runtime_environment().get("DATABASE_URL", "").strip()
    if not database_url:
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
    typer.echo("Select an LLM provider: openai, claude, or openrouter.")
    selected_profile = cast(str, typer.prompt("Provider"))
    try:
        profile = normalize_llm_profile(selected_profile)
        catalog = curated_models_for(profile)
    except ValueError as error:
        typer.echo(f"configuration: setup refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    api_key = cast(str, typer.prompt(f"{profile.title()} API key", hide_input=True))
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
    try:
        save_llm_profile(default_env_path(), selection)
    except (OSError, ValueError) as error:
        typer.echo(f"configuration: setup refused ({error})", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(f"configuration: saved profile {profile} with model {model}")
    if not verified:
        typer.echo("configuration: key saved without live verification")
    return ProviderConfiguration(profile=profile, model=model, api_key=api_key)


def _prompt_model_choice(catalog: tuple[CuratedModel, ...]) -> str:
    """Display only curated adapter-reviewed models plus an explicit custom-ID selection."""
    typer.echo("Available models:")
    for index, model in enumerate(catalog, start=1):
        marker = " (recommended)" if model.recommended else ""
        typer.echo(f"  {index}. {model.label}: {model.model_id}{marker}")
    custom_index = len(catalog) + 1
    typer.echo(f"  {custom_index}. Enter a custom model ID")
    selection = cast(str, typer.prompt("Model choice", default="1")).strip().lower()
    if selection == "custom" or selection == str(custom_index):
        return cast(str, typer.prompt("Custom model ID"))
    try:
        selected_index = int(selection)
    except ValueError as error:
        raise typer.BadParameter("choose a listed model number or custom") from error
    if not 1 <= selected_index <= len(catalog):
        raise typer.BadParameter("choose a listed model number or custom")
    return catalog[selected_index - 1].model_id


def _is_interactive_terminal() -> bool:
    """Prevent hidden prompts from blocking scripts, CI, or redirected terminal use."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def main() -> None:
    """Run the CLI entry point installed by the package."""
    app()
