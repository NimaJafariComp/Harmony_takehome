"""Contracts for the one explicitly demo-gated clock mutation in the local UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@dataclass
class RecordingDemoClock:
    """Record clock writes without exposing a database or business-system mutation."""

    current_at: datetime = NOW
    advances: list[timedelta] | None = None

    def advance(self, duration: timedelta) -> datetime:
        if self.advances is None:
            self.advances = []
        self.advances.append(duration)
        self.current_at += duration
        return self.current_at


def test_composed_demo_clock_control_advances_exactly_one_day() -> None:
    """The local-target composition has one predictable demo-time action without a second setting."""
    from enterprise_agent.application.local_demo_controls import LocalDemoClockControlService

    clock = RecordingDemoClock()
    control = LocalDemoClockControlService(clock=clock)

    result = control.advance_one_day()

    assert control.availability().can_advance
    assert result.current_at == (NOW + timedelta(days=1)).isoformat()
    assert clock.advances == [timedelta(days=1)]


def test_unconfigured_demo_clock_control_fails_closed() -> None:
    """Only composition may expose the clock; an unconfigured imported app cannot advance it."""
    from enterprise_agent.application.local_demo_controls import (
        LocalDemoClockControlDisabledError,
        UnconfiguredLocalDemoClockControlService,
    )

    control = UnconfiguredLocalDemoClockControlService()

    assert not control.availability().can_advance
    with pytest.raises(LocalDemoClockControlDisabledError):
        control.advance_one_day()


def test_demo_clock_control_reports_an_unavailable_local_clock_without_falling_back() -> None:
    """An enabled setting never substitutes wall-clock time or a successful-looking receipt on failure."""
    from enterprise_agent.application.local_demo_controls import (
        LocalDemoClockControlService,
        LocalDemoClockControlUnavailableError,
    )

    class UnavailableClock:
        """Model a local database that has not been reset and seeded."""

        def advance(self, duration: timedelta) -> datetime:
            del duration
            raise RuntimeError("clock is not initialized")

    with pytest.raises(LocalDemoClockControlUnavailableError):
        LocalDemoClockControlService(
            clock=UnavailableClock(),
        ).advance_one_day()


def test_demo_clock_control_composition_requires_only_the_strict_local_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local UI never needs DEMO_MODE, but it still cannot target a remote database."""
    from enterprise_agent import local_review_composition
    from enterprise_agent.application.local_demo_controls import (
        LocalDemoClockControlService,
        UnconfiguredLocalDemoClockControlService,
    )

    database_url = "postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent"
    constructed_urls: list[str] = []

    class StubClock:
        """Capture only the local database destination for the durable demo-clock adapter."""

        def __init__(self, configured_database_url: str) -> None:
            constructed_urls.append(configured_database_url)

    monkeypatch.setattr(local_review_composition, "PostgresDemoClock", StubClock)
    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {"DATABASE_URL": database_url, "DEMO_MODE": "false"},
    )
    with_legacy_false = local_review_composition.create_local_demo_clock_control_service()

    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {"DATABASE_URL": database_url},
    )
    without_setting = local_review_composition.create_local_demo_clock_control_service()

    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {
            "DATABASE_URL": "postgresql+psycopg://operator:operator@remote:5432/production",
        },
    )
    unsafe_target = local_review_composition.create_local_demo_clock_control_service()

    assert isinstance(with_legacy_false, LocalDemoClockControlService)
    assert isinstance(without_setting, LocalDemoClockControlService)
    assert isinstance(unsafe_target, UnconfiguredLocalDemoClockControlService)
    assert constructed_urls == [database_url, database_url]
