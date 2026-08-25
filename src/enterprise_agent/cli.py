"""Command-line interface for the enterprise agent harness."""

import typer

from enterprise_agent import __version__

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


def main() -> None:
    """Run the CLI entry point installed by the package."""
    app()
