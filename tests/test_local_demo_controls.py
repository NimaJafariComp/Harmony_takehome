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


def test_demo_clock_control_advances_exactly_one_day_only_when_explicitly_enabled() -> None:
    """The UI has one predictable demo-time action, separate from workflows and the demo engine."""
    from enterprise_agent.application.local_demo_controls import LocalDemoClockControlService

    clock = RecordingDemoClock()
    control = LocalDemoClockControlService(clock=clock, demo_mode_enabled=True)

    result = control.advance_one_day()

    assert control.availability().can_advance
    assert result.current_at == (NOW + timedelta(days=1)).isoformat()
    assert clock.advances == [timedelta(days=1)]


def test_demo_clock_control_fails_closed_when_demo_mode_is_not_explicitly_enabled() -> None:
    """A missing or false setting cannot be turned into a clock mutation by a UI caller."""
    from enterprise_agent.application.local_demo_controls import (
        LocalDemoClockControlDisabledError,
        LocalDemoClockControlService,
    )

    clock = RecordingDemoClock()
    control = LocalDemoClockControlService(clock=clock, demo_mode_enabled=False)

    assert not control.availability().can_advance
    with pytest.raises(LocalDemoClockControlDisabledError):
        control.advance_one_day()
    assert clock.advances is None


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
            demo_mode_enabled=True,
        ).advance_one_day()


def test_demo_clock_control_composition_requires_true_setting_and_uses_only_the_local_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local UI cannot enable clock writes from a provider profile, missing value, or other setting."""
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
    disabled = local_review_composition.create_local_demo_clock_control_service()

    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {"DATABASE_URL": database_url, "DEMO_MODE": "true"},
    )
    enabled = local_review_composition.create_local_demo_clock_control_service()

    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {
            "DATABASE_URL": "postgresql+psycopg://operator:operator@remote:5432/production",
            "DEMO_MODE": "true",
        },
    )
    unsafe_target = local_review_composition.create_local_demo_clock_control_service()

    assert isinstance(disabled, UnconfiguredLocalDemoClockControlService)
    assert isinstance(enabled, LocalDemoClockControlService)
    assert isinstance(unsafe_target, UnconfiguredLocalDemoClockControlService)
    assert constructed_urls == [database_url]
