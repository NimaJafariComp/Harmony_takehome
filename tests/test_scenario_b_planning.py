"""Contracts for bounded Scenario B quality-hold recommendations."""

from __future__ import annotations

from decimal import Decimal

import pytest


def test_schema_accepts_only_registered_reallocate_notify_shortage_and_manual_outcomes() -> None:
    """The quality planner returns typed registered-tool parameters or a safe human handoff."""
    from enterprise_agent.application.planning import (
        FlagShortageToPurchasingRecommendation,
        ManualReviewRecommendation,
        ReallocateAndNotifyRecommendation,
        validate_scenario_b_recommendation,
    )
    from enterprise_agent.application.tools import (
        FlagShortageToPurchasingInput,
        NotifyProductionInput,
        ReallocateLotInput,
    )

    reallocate_and_notify = validate_scenario_b_recommendation(
        {
            "outcome": "REALLOCATE_AND_NOTIFY",
            "reallocate_lot": {
                "quality_lot_id": "lot-quality-good",
                "from_production_order_id": None,
                "to_production_order_id": "production-q7001",
                "quantity": "80",
            },
            "notify_production": {
                "production_order_id": "production-q7001",
                "message": "A released replacement lot will cover the held allocation.",
            },
            "rationale": "The released lot can cover all 80 affected units.",
        }
    )
    shortage = validate_scenario_b_recommendation(
        {
            "outcome": "FLAG_SHORTAGE_TO_PURCHASING",
            "shortage": {
                "production_order_id": "production-q7002",
                "part_id": "part-quality",
                "shortage_quantity": "80",
            },
            "rationale": "Only 120 released units are available for 200 required units.",
        }
    )
    manual_review = validate_scenario_b_recommendation(
        {"outcome": "MANUAL_REVIEW", "reason": "The lot status is contradictory."}
    )

    assert isinstance(reallocate_and_notify, ReallocateAndNotifyRecommendation)
    assert isinstance(reallocate_and_notify.reallocate_lot, ReallocateLotInput)
    assert reallocate_and_notify.reallocate_lot.from_production_order_id is None
    assert isinstance(reallocate_and_notify.notify_production, NotifyProductionInput)
    assert reallocate_and_notify.reallocate_lot.quantity == Decimal(80)
    assert isinstance(shortage, FlagShortageToPurchasingRecommendation)
    assert isinstance(shortage.shortage, FlagShortageToPurchasingInput)
    assert shortage.shortage.shortage_quantity == Decimal(80)
    assert isinstance(manual_review, ManualReviewRecommendation)


@pytest.mark.parametrize(
    "output",
    [
        {"outcome": "EXECUTE_SQL", "statement": "UPDATE quality_lots SET status = 'released'"},
        {
            "outcome": "REALLOCATE_AND_NOTIFY",
            "reallocate_lot": {
                "quality_lot_id": "lot-quality-good",
                "to_production_order_id": "production-q7001",
                "quantity": 80,
                "unregistered_argument": "send an email too",
            },
            "notify_production": {
                "production_order_id": "production-q7001",
                "message": "Replacement lot available.",
            },
            "rationale": "Invalid unregistered parameter.",
        },
        {
            "outcome": "REALLOCATE_AND_NOTIFY",
            "reallocate_lot": {
                "quality_lot_id": "lot-quality-good",
                "from_production_order_id": "production-buffer",
                "to_production_order_id": "production-q7001",
                "quantity": 80,
            },
            "notify_production": {
                "production_order_id": "production-q7002",
                "message": "Mismatched production order.",
            },
            "rationale": "The two effects must target one production order.",
        },
        {
            "outcome": "FLAG_SHORTAGE_TO_PURCHASING",
            "shortage": {
                "production_order_id": "production-q7002",
                "part_id": "part-quality",
                "shortage_quantity": 0,
            },
            "rationale": "Zero is not an actionable shortage.",
        },
    ],
)
def test_schema_rejects_unregistered_tools_invalid_parameters_and_incoherent_actions(
    output: dict[str, object],
) -> None:
    """Free-form planning cannot add effects, bypass registered schemas, or split its target."""
    from enterprise_agent.application.planning import (
        InvalidScenarioBRecommendationError,
        validate_scenario_b_recommendation,
    )

    with pytest.raises(InvalidScenarioBRecommendationError):
        validate_scenario_b_recommendation(output)
