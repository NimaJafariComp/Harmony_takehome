"""Contracts for the explicit local-only guided demo launcher."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from enterprise_agent.application.guided_demo import GuidedDemoRun

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@dataclass
class RecordingRunner:
    """Capture a requested guided run without resetting a database in a unit test."""

    calls: list[tuple[str, tuple[str, ...], bool]] = field(default_factory=list)

    def __call__(
        self,
        database_url: str,
        *,
        case_ids: tuple[str, ...],
        allow_test_database: bool,
    ) -> GuidedDemoRun:
        self.calls.append((database_url, case_ids, allow_test_database))
        return _guided_run()


def _guided_run() -> GuidedDemoRun:
    """Build the smallest real-shaped deterministic result needed by the projection contract."""
    from enterprise_agent.application.guided_demo import (
        DemoIdentifier,
        GuidedDemoCaseResult,
        GuidedDemoRun,
        guided_demo_cases,
    )

    case = next(item for item in guided_demo_cases() if item.case_id == "scenario-a-reroute-bait")
    return GuidedDemoRun(
        results=(
            GuidedDemoCaseResult(
                case=case,
                identifiers=(DemoIdentifier(label="Run", value="demo-scenario-a-reroute"),),
            ),
        )
    )


def test_guided_demo_launcher_accepts_only_matching_seeded_personas_before_the_reset() -> None:
    """Dana owns purchasing stories, Quinn owns quality stories, and mixed/arbitrary selections fail."""
    from enterprise_agent.application.local_guided_demo import (
        GuidedDemoSelectionError,
        LocalGuidedDemoService,
    )

    runner = RecordingRunner()
    service = LocalGuidedDemoService(
        database_url="postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent",
        runner=runner,
        require_local_target=lambda _url: None,
    )

    receipt = service.run(
        persona_id="dana-buyer",
        case_ids=("scenario-a-reroute-bait", "scenario-c-pending-review"),
    )

    assert [persona.persona_id for persona in service.availability().personas] == [
        "dana-buyer",
        "quinn-quality-manager",
    ]
    assert receipt.persona_label == "Dana Buyer"
    assert runner.calls == [
        (
            "postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent",
            ("scenario-a-reroute-bait", "scenario-c-pending-review"),
            False,
        )
    ]

    with pytest.raises(GuidedDemoSelectionError, match="matching seeded persona"):
        service.run(
            persona_id="dana-buyer",
            case_ids=("scenario-b-capacity",),
        )
    with pytest.raises(GuidedDemoSelectionError, match="same persona"):
        service.run(
            persona_id="quinn-quality-manager",
            case_ids=("scenario-a-reroute-bait", "scenario-b-capacity"),
        )
    with pytest.raises(GuidedDemoSelectionError, match="unknown guided-demo persona"):
        service.run(persona_id="arbitrary-user", case_ids=("scenario-a-reroute-bait",))
    with pytest.raises(GuidedDemoSelectionError, match="select at least one"):
        service.run(persona_id="dana-buyer", case_ids=())

    assert len(runner.calls) == 1


def test_guided_demo_launcher_refuses_an_unsafe_target_without_invoking_the_runner() -> None:
    """A service rechecks its local-only target before the reset/seed runner can execute."""
    from enterprise_agent.application.local_guided_demo import (
        LocalGuidedDemoDisabledError,
        LocalGuidedDemoService,
    )

    runner = RecordingRunner()
    service = LocalGuidedDemoService(
        database_url="postgresql+psycopg://operator:operator@remote:5432/production",
        runner=runner,
        require_local_target=lambda _url: (_ for _ in ()).throw(ValueError("unsafe target")),
    )

    with pytest.raises(LocalGuidedDemoDisabledError):
        service.run(persona_id="dana-buyer", case_ids=("scenario-a-reroute-bait",))

    assert runner.calls == []


def test_local_guided_demo_composition_only_exposes_the_strict_synthetic_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional UI cannot compose a reset/stage launcher for an arbitrary configured database."""
    from enterprise_agent import local_review_composition
    from enterprise_agent.application.local_guided_demo import (
        LocalGuidedDemoService,
        UnconfiguredLocalGuidedDemoService,
    )

    database_url = "postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent"
    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {"DATABASE_URL": database_url},
    )
    safe = local_review_composition.create_local_guided_demo_service()

    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {
            "DATABASE_URL": "postgresql+psycopg://operator:operator@remote:5432/production"
        },
    )
    unsafe = local_review_composition.create_local_guided_demo_service()

    assert isinstance(safe, LocalGuidedDemoService)
    assert isinstance(unsafe, UnconfiguredLocalGuidedDemoService)
