"""Explicit, strict-local launcher for deterministic company-demo stories."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from enterprise_agent.application.guided_demo import (
    DemoCaseSelectionError,
    GuidedDemoRun,
    run_guided_demo,
    select_guided_demo_cases,
)
from enterprise_agent.seed import SeedSafetyError, _require_local_demo_database


class GuidedDemoSelectionError(ValueError):
    """Raised before any reset when the browser submits an invalid persona or case selection."""


class LocalGuidedDemoDisabledError(RuntimeError):
    """Raised when an unconfigured or unsafe target is asked to reset and stage a demo."""


class LocalGuidedDemoUnavailableError(RuntimeError):
    """Raised when the strict local deterministic runner cannot complete safely."""


@dataclass(frozen=True, slots=True)
class GuidedDemoPersona:
    """One deliberately fixed seeded persona compatible with a subset of demo stories."""

    persona_id: str
    label: str
    role: str
    case_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GuidedDemoAvailability:
    """Safe presentation state for the optional local reset-and-stage operation."""

    can_run: bool
    personas: tuple[GuidedDemoPersona, ...]


@dataclass(frozen=True, slots=True)
class GuidedDemoReceiptCase:
    """One selected story plus only the durable review identifiers it safely creates."""

    case_id: str
    title: str
    execution_mode: str
    outcome: str
    next_safe_action: str
    run_id: str | None
    approval_id: str | None
    workflow_id: str | None


@dataclass(frozen=True, slots=True)
class GuidedDemoReceipt:
    """Sanitized summary returned after one explicit deterministic guided-demo run."""

    persona_label: str
    cases: tuple[GuidedDemoReceiptCase, ...]


class LocalGuidedDemoPort(Protocol):
    """Browser-facing operation that does not expose a database URL or seeded actor identifier."""

    def availability(self) -> GuidedDemoAvailability:
        """Describe whether a strict local demo target is available and who can run each story."""
        ...

    def run(self, *, persona_id: str, case_ids: tuple[str, ...]) -> GuidedDemoReceipt:
        """Reset/seed the guarded local target, then stage only the explicitly selected stories."""
        ...


_PERSONAS: tuple[GuidedDemoPersona, ...] = (
    GuidedDemoPersona(
        persona_id="dana-buyer",
        label="Dana Buyer",
        role="Purchasing",
        case_ids=(
            "scenario-a-reroute-bait",
            "scenario-a-crash-recovery",
            "scenario-a-current-evidence",
            "scenario-a-tuesday-follow-up",
            "scenario-c-pending-review",
        ),
    ),
    GuidedDemoPersona(
        persona_id="quinn-quality-manager",
        label="Quinn Quality Manager",
        role="Quality",
        case_ids=("scenario-b-capacity",),
    ),
)
_PERSONAS_BY_ID = {persona.persona_id: persona for persona in _PERSONAS}
_PERSONA_BY_CASE_ID = {
    case_id: persona.persona_id for persona in _PERSONAS for case_id in persona.case_ids
}


def _require_strict_local_demo_target(database_url: str) -> None:
    """Reuse the single reset guard before the UI can call the deterministic runner."""
    _require_local_demo_database(database_url, allow_test_database=False)


@dataclass(slots=True)
class LocalGuidedDemoService:
    """Run deterministic stories only after validating the exact local target and persona mapping."""

    database_url: str
    runner: Callable[..., GuidedDemoRun] = run_guided_demo
    require_local_target: Callable[[str], None] = _require_strict_local_demo_target

    def availability(self) -> GuidedDemoAvailability:
        """Expose fixed safe persona choices without rendering user IDs or attempting a write."""
        return GuidedDemoAvailability(can_run=True, personas=_PERSONAS)

    def run(self, *, persona_id: str, case_ids: tuple[str, ...]) -> GuidedDemoReceipt:
        """Validate the selection and strict target immediately before reset/seed/staging occurs."""
        persona, selected_case_ids = _validated_selection(persona_id=persona_id, case_ids=case_ids)
        try:
            self.require_local_target(self.database_url)
        except (SeedSafetyError, ValueError) as error:
            raise LocalGuidedDemoDisabledError("guided demo is unavailable") from error
        try:
            run = self.runner(
                self.database_url,
                case_ids=selected_case_ids,
                allow_test_database=False,
            )
        except ValueError as error:
            raise LocalGuidedDemoUnavailableError("guided demo could not be prepared") from error
        return _receipt(persona=persona, run=run)


@dataclass(frozen=True, slots=True)
class UnconfiguredLocalGuidedDemoService:
    """Fail closed unless the composition root has validated the strict local demo target."""

    def availability(self) -> GuidedDemoAvailability:
        """Keep cards discoverable while preventing the reset/stage form from appearing."""
        return GuidedDemoAvailability(can_run=False, personas=())

    def run(self, *, persona_id: str, case_ids: tuple[str, ...]) -> GuidedDemoReceipt:
        """Refuse a browser action when no strict local target was composed."""
        del persona_id, case_ids
        raise LocalGuidedDemoDisabledError("guided demo launcher is disabled")


def _validated_selection(
    *, persona_id: str, case_ids: tuple[str, ...]
) -> tuple[GuidedDemoPersona, tuple[str, ...]]:
    """Reject empty, duplicate, cross-persona, and arbitrary-user selections before any reset."""
    normalized_persona_id = persona_id.strip().lower()
    persona = _PERSONAS_BY_ID.get(normalized_persona_id)
    if persona is None:
        raise GuidedDemoSelectionError("unknown guided-demo persona")
    if not case_ids:
        raise GuidedDemoSelectionError("select at least one guided demo case")
    try:
        cases = select_guided_demo_cases(case_ids)
    except DemoCaseSelectionError as error:
        raise GuidedDemoSelectionError(str(error)) from error
    selected_case_ids = tuple(case.case_id for case in cases)
    selected_personas = {_PERSONA_BY_CASE_ID.get(case_id) for case_id in selected_case_ids}
    if None in selected_personas:
        raise GuidedDemoSelectionError("guided demo case has no seeded persona")
    if len(selected_personas) != 1:
        raise GuidedDemoSelectionError("selected cases must belong to the same persona")
    if selected_personas != {persona.persona_id}:
        raise GuidedDemoSelectionError("selected cases require their matching seeded persona")
    return persona, selected_case_ids


def _receipt(*, persona: GuidedDemoPersona, run: GuidedDemoRun) -> GuidedDemoReceipt:
    """Project runner results to review links without exposing actor IDs, prompts, or planner internals."""
    cases: list[GuidedDemoReceiptCase] = []
    for result in run.results:
        scenario_a = result.scenario_a_pending
        scenario_c = result.scenario_c_pending
        pending = scenario_a if scenario_a is not None else scenario_c
        cases.append(
            GuidedDemoReceiptCase(
                case_id=result.case.case_id,
                title=result.case.title,
                execution_mode=result.case.execution_mode.value,
                outcome=result.case.outcome,
                next_safe_action=result.case.next_safe_action,
                run_id=str(pending.run_id) if pending is not None else None,
                approval_id=str(pending.approval_id) if pending is not None else None,
                workflow_id=str(scenario_c.workflow_id) if scenario_c is not None else None,
            )
        )
    return GuidedDemoReceipt(persona_label=persona.label, cases=tuple(cases))
