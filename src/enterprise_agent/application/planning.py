"""Bounded Scenario A/B recommendations and a deterministic LLM port for tests."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from enterprise_agent.application.tools import (
    FlagShortageToPurchasingInput,
    NotifyProductionInput,
    ReallocateLotInput,
)
from enterprise_agent.ports import PromptEnvelope, StructuredLLMResponse


class InvalidScenarioARecommendationError(ValueError):
    """Raised when a structured model response is outside the fixed Scenario A contract."""


class InvalidScenarioBRecommendationError(ValueError):
    """Raised when a structured model response is outside the bounded Scenario B contract."""


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveQuantity = Annotated[Decimal, Field(gt=0)]


class _RecommendationModel(BaseModel):
    """Shared strict model configuration for recommendations that can influence later gates."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class NoActionRecommendation(_RecommendationModel):
    """Record that current authorized evidence warrants no proposed action."""

    outcome: Literal["NO_ACTION"]
    rationale: NonBlankString


class ManualReviewRecommendation(_RecommendationModel):
    """Record that a human must resolve insufficient or conflicting evidence."""

    outcome: Literal["MANUAL_REVIEW"]
    reason: NonBlankString


class EnterWorkflowRecommendation(_RecommendationModel):
    """Request the one fixed Scenario A workflow using only structured, gateable parameters."""

    outcome: Literal["ENTER_WORKFLOW"]
    workflow_name: Literal["po_reroute"]
    workflow_version: Literal[1]
    supplier_id: NonBlankString
    quantity: PositiveQuantity
    original_purchase_order_id: NonBlankString
    production_order_id: NonBlankString
    rationale: NonBlankString


ScenarioARecommendation = Annotated[
    NoActionRecommendation | ManualReviewRecommendation | EnterWorkflowRecommendation,
    Field(discriminator="outcome"),
]
_SCENARIO_A_RECOMMENDATION_ADAPTER: TypeAdapter[ScenarioARecommendation] = TypeAdapter(
    ScenarioARecommendation
)


def validate_scenario_a_recommendation(
    output: Mapping[str, object],
) -> ScenarioARecommendation:
    """Accept only the three declared Scenario A outcomes without leaking raw provider output."""
    try:
        return _SCENARIO_A_RECOMMENDATION_ADAPTER.validate_python(output)
    except ValidationError as error:
        raise InvalidScenarioARecommendationError("invalid Scenario A recommendation") from error


class ReallocateAndNotifyRecommendation(_RecommendationModel):
    """Propose the only two registered effects that can resolve a covered quality hold."""

    outcome: Literal["REALLOCATE_AND_NOTIFY"]
    reallocate_lot: ReallocateLotInput
    notify_production: NotifyProductionInput
    rationale: NonBlankString

    @model_validator(mode="after")
    def _require_single_production_target(self) -> ReallocateAndNotifyRecommendation:
        if self.reallocate_lot.to_production_order_id != self.notify_production.production_order_id:
            raise ValueError("reallocation and notification must target the same production order")
        return self


class FlagShortageToPurchasingRecommendation(_RecommendationModel):
    """Propose the registered purchasing escalation when no released lot can cover demand."""

    outcome: Literal["FLAG_SHORTAGE_TO_PURCHASING"]
    shortage: FlagShortageToPurchasingInput
    rationale: NonBlankString


ScenarioBRecommendation = Annotated[
    ManualReviewRecommendation
    | ReallocateAndNotifyRecommendation
    | FlagShortageToPurchasingRecommendation,
    Field(discriminator="outcome"),
]
_SCENARIO_B_RECOMMENDATION_ADAPTER: TypeAdapter[ScenarioBRecommendation] = TypeAdapter(
    ScenarioBRecommendation
)


def validate_scenario_b_recommendation(
    output: Mapping[str, object],
) -> ScenarioBRecommendation:
    """Accept only registered Scenario B tool parameters or a safe manual-review handoff."""
    try:
        return _SCENARIO_B_RECOMMENDATION_ADAPTER.validate_python(output)
    except ValidationError as error:
        raise InvalidScenarioBRecommendationError("invalid Scenario B recommendation") from error


AnyScenarioRecommendation = (
    NoActionRecommendation
    | ManualReviewRecommendation
    | EnterWorkflowRecommendation
    | ReallocateAndNotifyRecommendation
    | FlagShortageToPurchasingRecommendation
)


class FakeLLMPort:
    """Return deterministic, scenario-keyed structured recommendations without a network dependency."""

    def __init__(self, recommendations: Mapping[str, AnyScenarioRecommendation]) -> None:
        """Copy immutable test configuration so later caller mutation cannot change fake behavior."""
        self._recommendations: Mapping[str, AnyScenarioRecommendation] = MappingProxyType(
            dict(recommendations)
        )

    def generate(self, prompt: PromptEnvelope) -> StructuredLLMResponse:
        """Return configured output or a safe manual-review recommendation for unknown scenarios."""
        scenario_key = f"{prompt.attention.scenario}:{prompt.attention.cause}"
        recommendation = self._recommendations.get(scenario_key)
        if recommendation is None:
            recommendation = ManualReviewRecommendation(
                outcome="MANUAL_REVIEW",
                reason=f"No fake recommendation configured for {scenario_key}.",
            )
        return StructuredLLMResponse(
            provider="fake",
            model="deterministic-fake-v1",
            output=recommendation.model_dump(mode="json"),
        )
