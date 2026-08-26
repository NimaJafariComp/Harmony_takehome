"""Browser-facing boundary for one guarded live-provider proposal over fixed synthetic data."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from enterprise_agent.application.live_demo import (
    LiveDemoExecutionError,
    LiveDemoResult,
    live_demo_cases,
    run_live_demo,
    select_live_demo_case,
)
from enterprise_agent.config import ProviderConfiguration
from enterprise_agent.ports import LLMUsage
from enterprise_agent.review_provenance import PlannerProvenance
from enterprise_agent.seed import SeedSafetyError, _require_local_demo_database


class LocalLiveDemoSelectionError(ValueError):
    """Raised before any provider call or local reset for an unconfigured profile or arbitrary case."""


class LocalLiveDemoDisabledError(RuntimeError):
    """Raised when an unsafe or unconfigured browser surface attempts a live local reset."""


class LocalLiveDemoUnavailableError(RuntimeError):
    """Raised when the guarded local runner cannot safely prepare a selected proposal."""


@dataclass(frozen=True, slots=True)
class LiveDemoProfile:
    """One configured profile safe to display without its credential."""

    profile: str
    model: str


@dataclass(frozen=True, slots=True)
class LiveDemoCaseOption:
    """One fixed local Scenario A, B, or C story eligible for a live proposal."""

    case_id: str
    scenario: str
    title: str


@dataclass(frozen=True, slots=True)
class LocalLiveDemoAvailability:
    """Safe form choices for the distinct local live-demo action."""

    can_run: bool
    profiles: tuple[LiveDemoProfile, ...]
    cases: tuple[LiveDemoCaseOption, ...]


@dataclass(frozen=True, slots=True)
class LocalLiveDemoReceipt:
    """Sanitized local-demo result with durable review IDs but no credential, prompt, or provider payload."""

    case_id: str
    case_title: str
    profile: str
    model: str
    planner_status: str
    outcome: str | None
    provenance: PlannerProvenance
    run_id: str
    attention_id: str
    plan_id: str | None
    approval_id: str | None
    workflow_id: str | None
    escalation_task_id: str | None
    usage: LLMUsage | None


class LocalLiveDemoPort(Protocol):
    """Browser contract that permits only fixed selected live proposals on a strict local target."""

    def availability(self) -> LocalLiveDemoAvailability:
        """List safe profile and fixed-case labels without contacting a provider or database."""
        ...

    def run(self, *, profile_id: str, case_id: str) -> LocalLiveDemoReceipt:
        """Reset and seed the guarded target, then stage at most one approval-gated proposal."""
        ...


def _require_strict_local_demo_target(database_url: str) -> None:
    """Reuse the only permitted target check immediately before any live local reset."""
    _require_local_demo_database(database_url, allow_test_database=False)


@dataclass(slots=True)
class LocalLiveDemoService:
    """Invoke the existing guarded A/B/C runner only after browser selection and target revalidation."""

    database_url: str
    configurations: tuple[ProviderConfiguration, ...]
    runner: Callable[..., LiveDemoResult] = run_live_demo
    require_local_target: Callable[[str], None] = _require_strict_local_demo_target

    def __post_init__(self) -> None:
        """Keep profile selection unambiguous and reject an incomplete composition early."""
        profile_ids = tuple(item.profile for item in self.configurations)
        if not profile_ids or len(set(profile_ids)) != len(profile_ids):
            raise ValueError("local live demo requires one configuration per profile")

    def availability(self) -> LocalLiveDemoAvailability:
        """Expose only configured profile/model labels and the fixed shared A/B/C catalogue."""
        return LocalLiveDemoAvailability(
            can_run=True,
            profiles=tuple(
                LiveDemoProfile(profile=item.profile, model=item.model)
                for item in self.configurations
            ),
            cases=tuple(
                LiveDemoCaseOption(
                    case_id=str(item.case_id), scenario=item.scenario, title=item.title
                )
                for item in live_demo_cases()
            ),
        )

    def run(self, *, profile_id: str, case_id: str) -> LocalLiveDemoReceipt:
        """Validate fixed selections and target before the runner can reset data or call one provider."""
        configuration = next(
            (item for item in self.configurations if item.profile == profile_id.strip().lower()),
            None,
        )
        if configuration is None:
            raise LocalLiveDemoSelectionError("unknown configured profile")
        try:
            case = select_live_demo_case(case_id)
        except ValueError as error:
            raise LocalLiveDemoSelectionError("unknown fixed live-demo case") from error
        try:
            self.require_local_target(self.database_url)
        except (SeedSafetyError, ValueError) as error:
            raise LocalLiveDemoDisabledError("live local demo is unavailable") from error
        try:
            result = self.runner(
                self.database_url,
                configuration=configuration,
                case_id=str(case.case_id),
                allow_test_database=False,
            )
        except (LiveDemoExecutionError, SeedSafetyError, ValueError) as error:
            raise LocalLiveDemoUnavailableError("live local demo could not be prepared") from error
        return _receipt(result)


@dataclass(frozen=True, slots=True)
class UnconfiguredLocalLiveDemoService:
    """Fail closed when local configuration cannot prove a strict synthetic target and profile."""

    def availability(self) -> LocalLiveDemoAvailability:
        """Hide selectable values while keeping the action's safety explanation visible."""
        return LocalLiveDemoAvailability(can_run=False, profiles=(), cases=())

    def run(self, *, profile_id: str, case_id: str) -> LocalLiveDemoReceipt:
        """Reject a submitted live-demo form without reading a key or database target."""
        del profile_id, case_id
        raise LocalLiveDemoDisabledError("live local demo is disabled")


def _receipt(result: LiveDemoResult) -> LocalLiveDemoReceipt:
    """Copy only explicitly review-safe scalar fields from the already-sanitized runner receipt."""
    return LocalLiveDemoReceipt(
        case_id=str(result.case.case_id),
        case_title=result.case.title,
        profile=result.profile,
        model=result.model,
        planner_status=result.planner_status.value,
        outcome=result.outcome,
        provenance=result.provenance,
        run_id=str(result.run_id),
        attention_id=str(result.attention_id),
        plan_id=str(result.plan_id) if result.plan_id is not None else None,
        approval_id=result.approval_id,
        workflow_id=str(result.workflow_id) if result.workflow_id is not None else None,
        escalation_task_id=(
            str(result.escalation_task_id) if result.escalation_task_id is not None else None
        ),
        usage=result.usage,
    )
