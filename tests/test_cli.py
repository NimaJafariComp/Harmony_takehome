"""CLI contract tests."""

from typer.testing import CliRunner

from enterprise_agent.cli import app


def test_version_command_reports_package_version() -> None:
    """The CLI exposes the package version for diagnostics."""
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout == "enterprise-agent 0.1.0\n"
