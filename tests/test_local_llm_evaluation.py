"""Contracts for the explicit no-write live-LLM lane in local Demo mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from enterprise_agent.config import ProviderConfiguration
from enterprise_agent.ports import (
    LLMCostSource,
    LLMGenerationResult,
    LLMPort,
    LLMUsage,
    PromptEnvelope,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@dataclass
class RecordingLLM:
    """Return a fixed safe structured response for one synthetic evaluation prompt."""

    prompts: list[PromptEnvelope] = field(default_factory=list)

    def generate(self, prompt: PromptEnvelope) -> LLMGenerationResult:
        self.prompts.append(prompt)
        return LLMGenerationResult.succeeded(
            provider="openai",
            model="gpt-5.6-luna",
            output={
                "outcome": "ENTER_WORKFLOW",
                "workflow_name": "po_reroute",
                "workflow_version": 1,
                "supplier_id": "EVAL-SUP-Z",
                "quantity": "90",
                "original_purchase_order_id": "EVAL-PO-A2",
                "production_order_id": "EVAL-PROD-A2",
                "rationale": "Approved alternate meets the production deadline safely.",
            },
            usage=LLMUsage(
                input_tokens=120,
                cached_input_tokens=0,
                output_tokens=30,
                total_tokens=150,
                cost_usd=Decimal("0.000060"),
                cost_source=LLMCostSource.ESTIMATED,
            ),
        )


def test_local_llm_evaluation_lists_only_configured_profiles_and_returns_a_scalar_no_write_scorecard() -> (
    None
):
    """The browser chooses one configured profile and one fixed case; no credential or raw output escapes."""
    from enterprise_agent.application.local_llm_evaluation import LocalLLMEvaluationService

    llm = RecordingLLM()
    received_configurations: list[ProviderConfiguration] = []

    def adapter_factory(configuration: ProviderConfiguration) -> LLMPort:
        received_configurations.append(configuration)
        return llm

    service = LocalLLMEvaluationService(
        configurations=(
            ProviderConfiguration(profile="openai", model="gpt-5.6-luna", api_key="secret-openai"),
        ),
        adapter_factory=adapter_factory,
    )

    availability = service.availability()
    receipt = service.evaluate(profile_id="openai", case_id="a-unapproved-bait")

    assert [(profile.profile, profile.model) for profile in availability.profiles] == [
        ("openai", "gpt-5.6-luna")
    ]
    assert len(availability.cases) == 10
    assert receipt.profile == "openai"
    assert receipt.model == "gpt-5.6-luna"
    assert receipt.report.passed
    assert receipt.report.usage.total_tokens == 150
    assert receipt.report.usage.total_cost_usd == Decimal("0.000060")
    assert received_configurations == [
        ProviderConfiguration(profile="openai", model="gpt-5.6-luna", api_key="secret-openai")
    ]
    assert llm.prompts[0].purpose == "manual_synthetic_llm_evaluation"
    assert llm.prompts[0].actor.scopes == frozenset()
    assert "secret-openai" not in repr(receipt)
    assert "rationale" not in repr(receipt)


def test_local_llm_evaluation_refuses_unknown_profile_or_case_before_a_provider_call() -> None:
    """Neither arbitrary profiles nor unbounded case IDs can spend a configured provider's budget."""
    from enterprise_agent.application.local_llm_evaluation import (
        LocalLLMEvaluationSelectionError,
        LocalLLMEvaluationService,
    )

    llm = RecordingLLM()
    service = LocalLLMEvaluationService(
        configurations=(
            ProviderConfiguration(profile="openai", model="gpt-5.6-luna", api_key="secret-openai"),
        ),
        adapter_factory=lambda _configuration: llm,
    )

    with pytest.raises(LocalLLMEvaluationSelectionError, match="unknown configured profile"):
        service.evaluate(profile_id="claude", case_id="a-unapproved-bait")
    with pytest.raises(LocalLLMEvaluationSelectionError, match="unknown evaluation case"):
        service.evaluate(profile_id="openai", case_id="not-a-case")

    assert llm.prompts == []


def test_local_llm_evaluation_composition_does_not_expose_missing_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only complete locally configured profiles appear; no setting is invented for an account."""
    from enterprise_agent import local_review_composition
    from enterprise_agent.application.local_llm_evaluation import (
        LocalLLMEvaluationService,
        UnconfiguredLocalLLMEvaluationService,
    )

    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {
            "OPENAI_API_KEY": "secret-openai",
            "OPENAI_MODEL": "gpt-5.6-luna",
        },
    )
    configured = local_review_composition.create_local_llm_evaluation_service()

    monkeypatch.setattr(local_review_composition, "load_local_environment", lambda _path: {})
    missing = local_review_composition.create_local_llm_evaluation_service()

    assert isinstance(configured, LocalLLMEvaluationService)
    assert [profile.profile for profile in configured.availability().profiles] == ["openai"]
    assert isinstance(missing, UnconfiguredLocalLLMEvaluationService)
