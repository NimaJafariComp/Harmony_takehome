"""Safe read-only projection of the shared deterministic demo catalogue for local review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LocalDemoCase:
    """One non-executable guided-demo story rendered by the optional local UI."""

    case_id: str
    title: str
    execution_mode: str
    phase: str
    outcome: str
    next_safe_action: str


class LocalDemoCataloguePort(Protocol):
    """Read shared demo stories without exposing its reset, seed, or staging operations."""

    def cases(self) -> tuple[LocalDemoCase, ...]:
        """Return the fixed deterministic catalogue in its shared presentation order."""
        ...


class LocalDemoCatalogueService:
    """Project the existing CLI catalogue lazily so importing the web app never starts a demo."""

    def cases(self) -> tuple[LocalDemoCase, ...]:
        """Reuse the single guided-demo definition without invoking reset, seed, or staging code."""
        from enterprise_agent.application.guided_demo import guided_demo_cases

        return tuple(
            LocalDemoCase(
                case_id=case.case_id,
                title=case.title,
                execution_mode=case.execution_mode.value,
                phase=case.phase,
                outcome=case.outcome,
                next_safe_action=case.next_safe_action,
            )
            for case in guided_demo_cases()
        )
