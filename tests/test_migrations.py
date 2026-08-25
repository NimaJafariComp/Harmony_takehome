"""Integration contract for the database migration runner."""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.integration


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


def test_baseline_migration_applies_to_a_clean_compose_database(
    disposable_database: str,
) -> None:
    """The private migration runner must upgrade an empty database to baseline."""

    compose(
        "--profile",
        "tools",
        "run",
        "--build",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "alembic",
        "upgrade",
        "head",
    )

    version = compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "alembic",
        "current",
    )
    assert "(head)" in version.stdout
