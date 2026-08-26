"""Durable local-demo clock contracts."""

from __future__ import annotations

import subprocess

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics when the clock contract fails."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.critical
@pytest.mark.integration
def test_seeded_postgres_clock_persists_advance_and_resets_to_the_demo_start(
    disposable_database: str,
) -> None:
    """Clock time survives a new adapter and reset/seed restores its deterministic start."""
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
    command = (
        "from datetime import timedelta\n"
        "from os import environ\n"
        "from enterprise_agent.adapters import PostgresDemoClock\n"
        "from enterprise_agent.ports import ClockPort\n"
        "from enterprise_agent.seed import DEMO_CLOCK_START, DEMO_TUESDAY, reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "clock = PostgresDemoClock(database_url)\n"
        "assert isinstance(clock, ClockPort)\n"
        "assert clock.now() == DEMO_CLOCK_START\n"
        "assert clock.advance(timedelta(hours=24)) == DEMO_TUESDAY\n"
        "assert PostgresDemoClock(database_url).now() == DEMO_TUESDAY\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "assert PostgresDemoClock(database_url).now() == DEMO_CLOCK_START\n"
    )
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "python",
        "-c",
        command,
    )
