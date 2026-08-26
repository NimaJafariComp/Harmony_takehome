"""Contracts for constrained Scenario C supplier-risk recommendations."""

from __future__ import annotations

import pytest


def test_schema_accepts_only_manual_review_or_coherent_hold_and_notify() -> None:
    """A supplier-risk planner can request only the registered bound hold and production notice."""
    from enterprise_agent.application.planning import (
        HoldAndNotifyRecommendation,
        ManualReviewRecommendation,
        json_schema_for_recommendation,
        validate_recommendation,
        validate_scenario_c_recommendation,
    )
    from enterprise_agent.application.tools import (
        NotifyProductionInput,
        PlacePurchaseOrderHoldInput,
    )

    hold_and_notify = validate_scenario_c_recommendation(
        {
            "outcome": "HOLD_AND_NOTIFY",
            "hold_purchase_order": {
                "purchase_order_id": "po-c-9001-w",
                "production_order_id": "production-c-9001",
                "expected_purchase_order_version": 1,
            },
            "notify_production": {
                "production_order_id": "production-c-9001",
                "message": "Supplier-risk bulletin requires a temporary PO hold and review.",
            },
            "rationale": "The current supplier bulletin affects the open PO before production starts.",
        }
    )
    manual_review = validate_scenario_c_recommendation(
        {"outcome": "MANUAL_REVIEW", "reason": "The bulletin and ERP facts conflict."}
    )

    assert isinstance(hold_and_notify, HoldAndNotifyRecommendation)
    assert isinstance(hold_and_notify.hold_purchase_order, PlacePurchaseOrderHoldInput)
    assert isinstance(hold_and_notify.notify_production, NotifyProductionInput)
    assert isinstance(manual_review, ManualReviewRecommendation)
    assert (
        validate_recommendation(
            "scenario_c_recommendation:v1",
            hold_and_notify.model_dump(mode="json"),
        )
        == hold_and_notify
    )
    assert "HOLD_AND_NOTIFY" in str(json_schema_for_recommendation("scenario_c_recommendation:v1"))


@pytest.mark.parametrize(
    "output",
    [
        {"outcome": "NO_ACTION", "rationale": "Not available for a supplier-risk alert."},
        {"outcome": "CANCEL_SUPPLIER", "supplier_id": "supplier-w"},
        {
            "outcome": "HOLD_AND_NOTIFY",
            "hold_purchase_order": {
                "purchase_order_id": "po-c-9001-w",
                "production_order_id": "production-c-9001",
                "expected_purchase_order_version": 1,
                "freeform_effect": "cancel every PO",
            },
            "notify_production": {
                "production_order_id": "production-c-9001",
                "message": "Supplier-risk bulletin requires a temporary PO hold and review.",
            },
            "rationale": "Unreviewed parameters are forbidden.",
        },
        {
            "outcome": "HOLD_AND_NOTIFY",
            "hold_purchase_order": {
                "purchase_order_id": "po-c-9001-w",
                "production_order_id": "production-c-9001",
                "expected_purchase_order_version": 0,
            },
            "notify_production": {
                "production_order_id": "production-c-9001",
                "message": "A stale version must not become a hold.",
            },
            "rationale": "Source versions must be positive.",
        },
        {
            "outcome": "HOLD_AND_NOTIFY",
            "hold_purchase_order": {
                "purchase_order_id": "po-c-9001-w",
                "production_order_id": "production-c-9001",
                "expected_purchase_order_version": 1,
            },
            "notify_production": {
                "production_order_id": "production-other",
                "message": "The notice cannot target another production order.",
            },
            "rationale": "The two effects must keep one causal production target.",
        },
    ],
)
def test_schema_rejects_unreviewed_stale_or_incoherent_supplier_risk_actions(
    output: dict[str, object],
) -> None:
    """Structured output cannot introduce arbitrary actions, stale versions, or split targets."""
    from enterprise_agent.application.planning import (
        InvalidScenarioCRecommendationError,
        validate_scenario_c_recommendation,
    )

    with pytest.raises(InvalidScenarioCRecommendationError):
        validate_scenario_c_recommendation(output)
