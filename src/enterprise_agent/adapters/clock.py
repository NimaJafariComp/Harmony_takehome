"""Durable PostgreSQL clock for deterministic local-demo time."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

SELECT_CURRENT_TIME = text("SELECT current_at FROM demo_clock WHERE id = 1")
ADVANCE_CURRENT_TIME = text("""
    UPDATE demo_clock
    SET current_at = current_at + (:advance_seconds * INTERVAL '1 second'),
        updated_at = current_at + (:advance_seconds * INTERVAL '1 second')
    WHERE id = 1
    RETURNING current_at
""")


class DemoClockNotInitializedError(RuntimeError):
    """Raised when an operator has not reset and seeded the local demo database."""


class PostgresDemoClock:
    """Read and monotonically advance the one persisted local-demo business clock."""

    def __init__(self, database_url: str) -> None:
        """Connect this clock adapter to one PostgreSQL database."""
        self._engine: Engine = create_engine(database_url)

    def now(self) -> datetime:
        """Return the current persisted business time without consulting wall-clock time."""
        with self._engine.connect() as connection:
            current_at = connection.execute(SELECT_CURRENT_TIME).scalar_one_or_none()
        if current_at is None:
            raise DemoClockNotInitializedError("demo clock is not initialized; run reset and seed")
        return cast(datetime, current_at)

    def advance(self, duration: timedelta) -> datetime:
        """Advance by a positive whole-second duration and return the newly persisted time."""
        seconds = int(duration.total_seconds())
        if seconds <= 0 or duration != timedelta(seconds=seconds):
            raise ValueError("demo clock advance must be a positive whole number of seconds")
        with self._engine.begin() as connection:
            current_at = connection.execute(
                ADVANCE_CURRENT_TIME,
                {"advance_seconds": seconds},
            ).scalar_one_or_none()
        if current_at is None:
            raise DemoClockNotInitializedError("demo clock is not initialized; run reset and seed")
        return cast(datetime, current_at)
