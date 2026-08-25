"""CLI contract tests."""

import pytest
from typer.testing import CliRunner

from enterprise_agent import cli

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
