"""Explicitly demo-gated local clock control for the optional verification UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


class LocalDemoClockControlDisabledError(RuntimeError):
    """Raised when a caller attempts the optional clock write without explicit demo mode."""


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
        """State whether an explicit local demo setting permits time advancement."""
        ...

    def advance_one_day(self) -> DemoClockAdvanceResult:
        """Advance only one deterministic demo day and return its safe timestamp receipt."""
        ...


@dataclass(slots=True)
class LocalDemoClockControlService:
    """Permit a fixed one-day clock advance only after composition explicitly enables demo mode."""

    clock: DemoClockAdvancePort
    demo_mode_enabled: bool

    def availability(self) -> DemoClockControlAvailability:
        """Expose the immutable configuration decision without leaking local settings."""
        return DemoClockControlAvailability(can_advance=self.demo_mode_enabled)

    def advance_one_day(self) -> DemoClockAdvanceResult:
        """Advance only persisted demo time, never a workflow, plan, provider, or business record."""
        if not self.demo_mode_enabled:
            raise LocalDemoClockControlDisabledError("demo clock control is disabled")
        try:
            current_at = self.clock.advance(timedelta(days=1))
        except RuntimeError as error:
            raise LocalDemoClockControlUnavailableError(
                "local demo clock is unavailable"
            ) from error
        return DemoClockAdvanceResult(current_at=current_at.isoformat())


@dataclass(frozen=True, slots=True)
class UnconfiguredLocalDemoClockControlService:
    """Fail closed when local configuration has not explicitly enabled mutable demo time."""

    def availability(self) -> DemoClockControlAvailability:
        """Render a clear locked state without opening a database connection."""
        return DemoClockControlAvailability(can_advance=False)

    def advance_one_day(self) -> DemoClockAdvanceResult:
        """Refuse all clock writes while the local demo control is unconfigured."""
        raise LocalDemoClockControlDisabledError("demo clock control is disabled")
