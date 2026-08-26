"""Contracts for bounded Scenario B quality-hold recommendations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

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


@pytest.mark.critical
def test_bounded_tool_plan_revalidates_its_catalog_schema_freshness_and_write_scopes() -> None:
    """A persisted selected-tool plan cannot gain a tool, stale evidence, or a missing write scope."""
    from enterprise_agent.application.bounded_tool_plan import (
        BoundedToolCall,
        BoundedToolPlanError,
        BoundedToolPlanGate,
        bounded_tool_calls_from_plan,
        build_bounded_tool_plan,
    )
    from enterprise_agent.application.gate import GateDenialReason, GateStatus
    from enterprise_agent.application.tools import (
        NotifyProductionInput,
        ReallocateLotInput,
        ToolName,
    )
    from enterprise_agent.domain import ActorContext, AttentionId, Scope, UserId

    now = datetime(2026, 8, 25, 9, tzinfo=UTC)
    tool_calls = (
        BoundedToolCall(
            tool_name=ToolName.REALLOCATE_LOT,
            input=ReallocateLotInput(
                quality_lot_id="lot-good",
                to_production_order_id="production-q7001",
                quantity=Decimal(80),
            ),
        ),
        BoundedToolCall(
            tool_name=ToolName.NOTIFY_PRODUCTION,
            input=NotifyProductionInput(
                production_order_id="production-q7001",
                message="Released lot covers the held allocation.",
            ),
        ),
    )
    source_versions = {"quality:quality_lot:lot-held": 3}
    plan = build_bounded_tool_plan(
        attention_id=AttentionId("attention-quality-held"),
        actor_id=UserId("quality-user"),
        approver_id=UserId("supervisor-user"),
        tool_calls=tool_calls,
        source_versions=source_versions,
        policy_version="scenario_b_policy:v1",
        created_at=now,
        expires_at=now + timedelta(hours=4),
    )
    actor = ActorContext(
        user_id=plan.actor_id,
        role="quality_manager",
        scopes=frozenset({Scope("erp:lot:write"), Scope("production:notify")}),
        plant_ids=frozenset(),
        backup_approver_id=None,
        approval_limits={},
    )

    assert bounded_tool_calls_from_plan(plan) == tool_calls
    allowed = BoundedToolPlanGate().evaluate(
        actor,
        tool_calls,
        source_versions=source_versions,
        current_source_versions=source_versions,
    )
    denied = BoundedToolPlanGate().evaluate(
        replace(actor, scopes=frozenset()),
        tool_calls,
        source_versions=source_versions,
        current_source_versions={"quality:quality_lot:lot-held": 4},
    )
    assert allowed.status is GateStatus.PENDING_APPROVAL
    assert denied.status is GateStatus.DENIED
    assert denied.denial_reasons == (
        GateDenialReason.STALE_SOURCE_EVIDENCE,
        GateDenialReason.MISSING_REQUIRED_SCOPE,
    )

    with pytest.raises(BoundedToolPlanError, match="input schema"):
        BoundedToolCall(tool_name=ToolName.REALLOCATE_LOT, input=tool_calls[1].input)
    with pytest.raises(BoundedToolPlanError, match="at least one"):
        BoundedToolPlanGate().evaluate(
            actor,
            (),
            source_versions=source_versions,
            current_source_versions=source_versions,
        )
    with pytest.raises(BoundedToolPlanError, match="source versions"):
        build_bounded_tool_plan(
            attention_id=plan.attention_id,
            actor_id=plan.actor_id,
            approver_id=plan.approver_id,
            tool_calls=tool_calls,
            source_versions={},
            policy_version=plan.policy_version,
            created_at=now,
            expires_at=now + timedelta(hours=4),
        )
    with pytest.raises(BoundedToolPlanError, match="call is invalid"):
        bounded_tool_calls_from_plan(
            replace(plan, parameters={"tool_calls": [{"tool_name": "execute_sql", "input": {}}]})
        )


def test_manual_quality_recommendation_creates_no_approval_or_workflow() -> None:
    """Manual review remains an auditable non-writing planner outcome, not an empty executable plan."""
    from enterprise_agent.application.planning import ManualReviewRecommendation
    from enterprise_agent.application.scenario_b_control import ScenarioBControlService

    approvals = MagicMock()
    workflow_state = MagicMock()
    result = ScenarioBControlService(
        approvals=approvals,
        workflow_state=workflow_state,
    ).request_pending(
        context=MagicMock(),
        recommendation=ManualReviewRecommendation(
            outcome="MANUAL_REVIEW",
            reason="The quality facts conflict.",
        ),
        current_source_versions={},
        policy_version="scenario_b_policy:v1",
        requested_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
        expires_at=datetime(2026, 8, 25, 13, tzinfo=UTC),
    )

    assert result.pending is None
    assert result.workflow is None
    approvals.request_pending_plan.assert_not_called()
    workflow_state.stage_bounded_tool_plan.assert_not_called()
