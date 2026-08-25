"""Integration contract for the database migration runner."""

from __future__ import annotations

import subprocess


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and expose useful diagnostics on failure."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


def reset_database() -> None:
    """Return the running Compose database to a clean state for this test."""
    compose("up", "-d", "--wait", "db")
    compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "enterprise_agent",
        "-d",
        "postgres",
        "-c",
        "DROP DATABASE IF EXISTS enterprise_agent WITH (FORCE)",
    )
    compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "enterprise_agent",
        "-d",
        "postgres",
        "-c",
        "CREATE DATABASE enterprise_agent",
    )


def test_baseline_migration_applies_to_a_clean_compose_database() -> None:
    """The private migration runner must upgrade an empty database to baseline."""
    reset_database()

    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "app",
        "alembic",
        "upgrade",
        "head",
    )

    version = compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-U",
        "enterprise_agent",
        "-d",
        "enterprise_agent",
        "-At",
        "-c",
        "SELECT version_num FROM alembic_version",
    )
    assert version.stdout.strip() == "20260825_0001"
