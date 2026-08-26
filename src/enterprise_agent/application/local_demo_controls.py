"""Strict-local-target clock control for the optional verification UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


class LocalDemoClockControlDisabledError(RuntimeError):
    """Raised when an unconfigured UI import attempts the optional clock write."""


class LocalDemoClockControlUnavailableError(RuntimeError):
    """Raised when the enabled local control cannot reach its durable demo-clock dependency."""


class DemoClockAdvancePort(Protocol):
    """The only mutation port needed by the local UI's fixed demo-time action."""

    def advance(self, duration: timedelta) -> datetime:
        """Move persisted demo time forward by a validated positive duration."""
        ...


@dataclass(frozen=True, slots=True)
class DemoClockControlAvailability:
    """Safe presentation state for the one optional demo-only control."""

    can_advance: bool


@dataclass(frozen=True, slots=True)
class DemoClockAdvanceResult:
    """Safe receipt for a completed fixed-duration local-demo clock write."""

    current_at: str


class LocalDemoClockControlPort(Protocol):
    """Browser-facing contract that excludes reset, seed, workflow, and recovery operations."""

    def availability(self) -> DemoClockControlAvailability:
        """State whether strict local-demo composition permits time advancement."""
        ...

    def advance_one_day(self) -> DemoClockAdvanceResult:
        """Advance only one deterministic demo day and return its safe timestamp receipt."""
        ...


@dataclass(slots=True)
class LocalDemoClockControlService:
    """Permit a fixed one-day clock advance after composition validates the local demo target."""

    clock: DemoClockAdvancePort

    def availability(self) -> DemoClockControlAvailability:
        """Expose successful strict-local composition without leaking local settings."""
        return DemoClockControlAvailability(can_advance=True)

    def advance_one_day(self) -> DemoClockAdvanceResult:
        """Advance only persisted demo time, never a workflow, plan, provider, or business record."""
        try:
            current_at = self.clock.advance(timedelta(days=1))
        except RuntimeError as error:
            raise LocalDemoClockControlUnavailableError(
                "local demo clock is unavailable"
            ) from error
        return DemoClockAdvanceResult(current_at=current_at.isoformat())


@dataclass(frozen=True, slots=True)
class UnconfiguredLocalDemoClockControlService:
    """Fail closed unless composition has validated the strict local demo database target."""

    def availability(self) -> DemoClockControlAvailability:
        """Render a clear locked state without opening a database connection."""
        return DemoClockControlAvailability(can_advance=False)

    def advance_one_day(self) -> DemoClockAdvanceResult:
        """Refuse all clock writes while the local demo control is unconfigured."""
        raise LocalDemoClockControlDisabledError("demo clock control is disabled")
