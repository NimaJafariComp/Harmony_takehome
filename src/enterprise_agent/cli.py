"""Command-line interface for the enterprise agent harness."""

import typer

from enterprise_agent import __version__
from enterprise_agent.config import ConfigurationError, load_settings

app = typer.Typer(
    help="Operate the enterprise agent harness.",
    no_args_is_help=True,
)


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
        configuration = load_settings()
    except ConfigurationError as error:
        typer.echo(f"configuration: invalid ({error})", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(configuration.safe_summary())


def main() -> None:
    """Run the CLI entry point installed by the package."""
    app()
