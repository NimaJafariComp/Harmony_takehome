"""Contracts for the loopback UI's separately guarded live A/B/C proposal launcher."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from enterprise_agent.application.live_demo import LiveDemoCase, LiveDemoCaseId, LiveDemoResult
from enterprise_agent.config import ProviderConfiguration
from enterprise_agent.domain import AttentionId, PlanId, RunId, ScheduledTaskId, WorkflowId
from enterprise_agent.ports import LLMGenerationStatus
from enterprise_agent.review_provenance import (
    GateStatus,
    PlannerMode,
    PlannerProvenance,
    SchemaValidation,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@dataclass
class RecordingRunner:
    """Return a sanitized live receipt while recording the only allowed composition inputs."""

    calls: list[tuple[str, ProviderConfiguration, str, bool]] = field(default_factory=list)

    def __call__(
        self,
        database_url: str,
        *,
        configuration: ProviderConfiguration,
        case_id: str,
        allow_test_database: bool,
    ) -> LiveDemoResult:
        self.calls.append((database_url, configuration, case_id, allow_test_database))
        return _result()


def _result() -> LiveDemoResult:
    """Build the smallest real-shaped safe runner result the browser may receive."""
    return LiveDemoResult(
        case=LiveDemoCase(
            case_id=LiveDemoCaseId.SCENARIO_A_REROUTE,
            scenario="scenario_a",
            title="Scenario A — supplier reroute proposal",
            response_schema="scenario_a_recommendation:v1",
        ),
        run_id=RunId("live-demo:scenario-a-reroute"),
        attention_id=AttentionId("00000000-0000-0000-0000-000000000901"),
        provider="openai",
        profile="openai",
        model="gpt-5.6-luna",
        planner_status=LLMGenerationStatus.SUCCEEDED,
        outcome="ENTER_WORKFLOW",
        provenance=PlannerProvenance(
            mode=PlannerMode.LIVE,
            provider="openai",
            profile="openai",
            model="gpt-5.6-luna",
            schema_validation=SchemaValidation.PASSED,
            gate_status=GateStatus.PENDING_APPROVAL,
        ),
        plan_id=PlanId("00000000-0000-0000-0000-000000000902"),
        approval_id="00000000-0000-0000-0000-000000000903",
        workflow_id=WorkflowId("00000000-0000-0000-0000-000000000904"),
        escalation_task_id=ScheduledTaskId("00000000-0000-0000-0000-000000000905"),
        usage=None,
    )


def test_local_live_demo_accepts_only_configured_profile_and_fixed_case_after_target_recheck() -> (
    None
):
    """The local UI cannot select an arbitrary profile, case, database, or test-target bypass."""
    from enterprise_agent.application.local_live_demo import LocalLiveDemoService

    configuration = ProviderConfiguration(
        profile="openai", model="gpt-5.6-luna", api_key="secret-openai"
    )
    runner = RecordingRunner()
    required_targets: list[str] = []
    service = LocalLiveDemoService(
        database_url="postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent",
        configurations=(configuration,),
        runner=runner,
        require_local_target=required_targets.append,
    )

    availability = service.availability()
    receipt = service.run(profile_id="openai", case_id="scenario-a-reroute")

    assert [(item.profile, item.model) for item in availability.profiles] == [
        ("openai", "gpt-5.6-luna")
    ]
    assert [item.case_id for item in availability.cases] == [
        "scenario-a-reroute",
        "scenario-b-quality-hold",
        "scenario-c-supplier-risk",
    ]
    assert required_targets == [
        "postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent"
    ]
    assert runner.calls == [
        (
            "postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent",
            configuration,
            "scenario-a-reroute",
            False,
        )
    ]
    assert receipt.provenance.mode_label == "LIVE"
    assert receipt.provenance.gate_label == "Passed to pending approval"
    assert receipt.approval_id == "00000000-0000-0000-0000-000000000903"
    assert "secret-openai" not in repr(receipt)


def test_local_live_demo_refuses_unknown_selection_or_unsafe_target_before_the_runner() -> None:
    """Bad form values and remote targets cannot reset data or call a provider."""
    from enterprise_agent.application.local_live_demo import (
        LocalLiveDemoDisabledError,
        LocalLiveDemoSelectionError,
        LocalLiveDemoService,
    )

    runner = RecordingRunner()
    service = LocalLiveDemoService(
        database_url="postgresql+psycopg://operator:operator@remote:5432/production",
        configurations=(
            ProviderConfiguration(profile="openai", model="gpt-5.6-luna", api_key="secret-openai"),
        ),
        runner=runner,
        require_local_target=lambda _url: (_ for _ in ()).throw(ValueError("unsafe target")),
    )

    with pytest.raises(LocalLiveDemoSelectionError, match="unknown configured profile"):
        service.run(profile_id="claude", case_id="scenario-a-reroute")
    with pytest.raises(LocalLiveDemoSelectionError, match="unknown fixed live-demo case"):
        service.run(profile_id="openai", case_id="arbitrary-case")
    with pytest.raises(LocalLiveDemoDisabledError, match="live local demo is unavailable"):
        service.run(profile_id="openai", case_id="scenario-a-reroute")

    assert runner.calls == []


def test_local_live_demo_composition_requires_the_strict_target_and_a_complete_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production composition root fails closed for incomplete credentials and non-demo databases."""
    from enterprise_agent import local_review_composition
    from enterprise_agent.application.local_live_demo import (
        LocalLiveDemoService,
        UnconfiguredLocalLiveDemoService,
    )

    database_url = "postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent"
    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {
            "DATABASE_URL": database_url,
            "OPENAI_API_KEY": "secret-openai",
            "OPENAI_MODEL": "gpt-5.6-luna",
        },
    )
    safe = local_review_composition.create_local_live_demo_service()

    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {
            "DATABASE_URL": "postgresql+psycopg://operator:operator@remote:5432/production",
            "OPENAI_API_KEY": "secret-openai",
            "OPENAI_MODEL": "gpt-5.6-luna",
        },
    )
    unsafe = local_review_composition.create_local_live_demo_service()

    assert isinstance(safe, LocalLiveDemoService)
    assert isinstance(unsafe, UnconfiguredLocalLiveDemoService)
