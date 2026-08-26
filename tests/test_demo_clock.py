"""Durable local-demo clock contracts."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
TUESDAY = datetime(2026, 8, 25, 9, tzinfo=UTC)


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


@pytest.mark.unit
def test_postgres_demo_clock_reads_and_advances_the_persisted_business_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clock reads one durable value and advances it atomically without wall time."""
    from enterprise_agent.adapters import clock
    from enterprise_agent.ports import ClockPort

    engine = MagicMock()
    reader = engine.connect.return_value.__enter__.return_value
    reader.execute.return_value.scalar_one_or_none.return_value = NOW
    transaction = engine.begin.return_value.__enter__.return_value
    transaction.execute.return_value.scalar_one_or_none.return_value = TUESDAY
    monkeypatch.setattr(clock, "create_engine", lambda _: engine)

    adapter = clock.PostgresDemoClock("postgresql+psycopg://ignored")

    assert isinstance(adapter, ClockPort)
    assert adapter.now() == NOW
    assert adapter.advance(timedelta(hours=24)) == TUESDAY
    assert transaction.execute.call_args.args[1] == {"advance_seconds": 86_400}


@pytest.mark.unit
@pytest.mark.parametrize(
    "duration",
    [timedelta(), timedelta(seconds=-1), timedelta(microseconds=1)],
)
def test_postgres_demo_clock_rejects_non_monotonic_or_subsecond_advances(
    monkeypatch: pytest.MonkeyPatch,
    duration: timedelta,
) -> None:
    """Invalid clock movement is rejected before it can issue a database mutation."""
    from enterprise_agent.adapters import clock

    engine = MagicMock()
    monkeypatch.setattr(clock, "create_engine", lambda _: engine)
    adapter = clock.PostgresDemoClock("postgresql+psycopg://ignored")

    with pytest.raises(ValueError, match="positive whole number"):
        adapter.advance(duration)

    engine.begin.assert_not_called()


@pytest.mark.unit
def test_postgres_demo_clock_requires_seeded_state_for_reads_and_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty database cannot silently substitute wall-clock time for demo time."""
    from enterprise_agent.adapters import clock

    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one_or_none.return_value = None
    engine.begin.return_value.__enter__.return_value.execute.return_value.scalar_one_or_none.return_value = None
    monkeypatch.setattr(clock, "create_engine", lambda _: engine)
    adapter = clock.PostgresDemoClock("postgresql+psycopg://ignored")

    with pytest.raises(clock.DemoClockNotInitializedError, match="run reset and seed"):
        adapter.now()
    with pytest.raises(clock.DemoClockNotInitializedError, match="run reset and seed"):
        adapter.advance(timedelta(hours=1))


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
