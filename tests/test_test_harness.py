"""Smoke checks for the reusable test harness."""

from __future__ import annotations

import subprocess

from enterprise_agent import cli


def test_cli_module_imports() -> None:
    """The CLI remains importable before any application workflow is exercised."""
    assert cli.app is not None


def test_disposable_database_is_available_to_the_application_runner(
    disposable_database: str,
) -> None:
    """Integration tests receive an isolated database reachable from the app service."""
    command = "\n".join(
        [
            "from sqlalchemy import create_engine, text",
            f"engine = create_engine({disposable_database!r})",
            "with engine.connect() as connection:",
            "    print(connection.scalar(text('SELECT current_database()')))",
        ]
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "--profile",
            "tools",
            "run",
            "--rm",
            "app",
            "python",
            "-c",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == disposable_database.rsplit("/", maxsplit=1)[1]
