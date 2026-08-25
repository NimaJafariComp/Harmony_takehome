"""Command-line interface for the enterprise agent harness."""

from os import environ

import typer

from enterprise_agent import __version__
from enterprise_agent.config import ConfigurationError, load_settings
from enterprise_agent.seed import SeedSafetyError, reset_database, seed_database

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


def _database_url() -> str:
    """Read only the database setting needed by local reset and seed commands."""
    database_url = environ.get("DATABASE_URL", "").strip()
    if not database_url:
        typer.echo("database: reset refused (DATABASE_URL is required)", err=True)
        raise typer.Exit(code=1)
    return database_url


def main() -> None:
    """Run the CLI entry point installed by the package."""
    app()
