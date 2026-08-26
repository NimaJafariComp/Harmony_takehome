"""Scenario C translation into the shared approval-gated bounded-tool control plane."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from enterprise_agent.application.approvals import PlanApprovalService
from enterprise_agent.application.planning import (
    HoldAndNotifyRecommendation,
    ManualReviewRecommendation,
)
from enterprise_agent.application.scenario_c_context import ScenarioCContextBundle
from enterprise_agent.application.scenario_c_control import (
    ScenarioCControlRejectedError,
    ScenarioCControlService,
)
from enterprise_agent.application.tools import NotifyProductionInput, PlacePurchaseOrderHoldInput
from enterprise_agent.application.workflow_state import WorkflowStateService
from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    Evidence,
    EvidenceId,
    PlantId,
    Scope,
    UserId,
)

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
RecommendationFactory = Callable[[ScenarioCContextBundle], HoldAndNotifyRecommendation]
SourceVersionFactory = Callable[[ScenarioCContextBundle], Mapping[str, int]]
DANA = ActorContext(
    user_id=UserId("00000000-0000-0000-0000-000000000001"),
    role="purchasing_manager",
    scopes=frozenset({Scope("erp:po:hold"), Scope("production:notify")}),
    plant_ids=frozenset({PlantId("PLANT-CHI")}),
    backup_approver_id=None,
    approval_limits={"USD": Decimal(10000)},
)


def _evidence(
    *, record_type: str, record_id: str, source_version: int, payload: dict[str, object]
) -> Evidence:
    """Create one versioned provider-owned fact for the Scenario C controller contract."""
    return Evidence(
        evidence_id=EvidenceId(f"erp:{record_type}:{record_id}"),
        source="erp" if record_type != "supplier_risk_bulletin" else "knowledge",
        record_type=record_type,
        record_id=record_id,
        source_version=source_version,
        observed_at=NOW,
        payload=payload,
    )


def _context() -> ScenarioCContextBundle:
    """Build exactly one fresh, authorized supplier-risk context with matching material IDs."""
    bulletin = _evidence(
        record_type="supplier_risk_bulletin",
        record_id="bulletin-c",
        source_version=2,
        payload={"status": "active", "supplier_id": "supplier-w", "plant_id": "PLANT-CHI"},
    )
    purchase_order = _evidence(
        record_type="purchase_order",
        record_id="po-c-9001-w",
        source_version=4,
        payload={
            "status": "open",
            "supplier_id": "supplier-w",
            "part_id": "part-c",
            "plant_id": "PLANT-CHI",
        },
    )
    production_order = _evidence(
        record_type="production_order",
        record_id="production-c-9001",
        source_version=3,
        payload={"status": "scheduled", "part_id": "part-c", "plant_id": "PLANT-CHI"},
    )
    source_versions = {
        str(bulletin.evidence_id): bulletin.source_version,
        str(purchase_order.evidence_id): purchase_order.source_version,
        str(production_order.evidence_id): production_order.source_version,
    }
    return ScenarioCContextBundle(
        actor=DANA,
        attention=AttentionItem(
            attention_id=AttentionId("attention-supplier-risk"),
            scenario="scenario_c",
            cause="supplier_risk",
            dedupe_key="scenario_c:supplier_risk:v1:control-test",
            status=AttentionStatus.OPEN,
            created_at=NOW,
            source_versions=source_versions,
        ),
        trigger=MagicMock(),
        bulletin=bulletin,
        purchase_order=purchase_order,
        production_order=production_order,
    )


def _recommendation(context: ScenarioCContextBundle) -> HoldAndNotifyRecommendation:
    """Return the sole coherent hold-and-notify action permitted for this fresh context."""
    return HoldAndNotifyRecommendation(
        outcome="HOLD_AND_NOTIFY",
        hold_purchase_order=PlacePurchaseOrderHoldInput(
            purchase_order_id=context.purchase_order.record_id,
            production_order_id=context.production_order.record_id,
            expected_purchase_order_version=context.purchase_order.source_version,
        ),
        notify_production=NotifyProductionInput(
            production_order_id=context.production_order.record_id,
            message="Supplier-risk bulletin requires a temporary purchase-order hold and review.",
        ),
        rationale="The active supplier-risk bulletin affects this open purchase order.",
    )


def _control() -> tuple[ScenarioCControlService, MagicMock, MagicMock]:
    """Wire the real shared application services to isolated persistence spies."""
    approval_store = MagicMock()
    workflow_store = MagicMock()
    return (
        ScenarioCControlService(
            approvals=PlanApprovalService(approval_store),
            workflow_state=WorkflowStateService(workflow_store),
        ),
        approval_store,
        workflow_store,
    )


@pytest.mark.critical
def test_supplier_risk_control_stages_one_immutable_hold_then_notify_plan() -> None:
    """A fresh typed recommendation becomes the common two-step approval-gated workflow."""
    context = _context()
    control, approval_store, workflow_store = _control()

    result = control.request_pending(
        context=context,
        recommendation=_recommendation(context),
        current_source_versions=context.source_versions,
        policy_version="scenario_c_policy:v1",
        requested_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )

    plan, approval = approval_store.create_pending.call_args.args
    staged = workflow_store.create.call_args.args[0]
    assert result.pending is not None and result.workflow is not None
    assert plan.actor_id == DANA.user_id
    assert plan.approver_id == DANA.user_id
    assert plan.intent == "bounded_tool_plan"
    assert approval.plan_hash == plan.plan_hash
    assert [step.tool_name for step in staged.steps] == [
        "place_purchase_order_hold",
        "notify_production",
    ]
    assert staged.steps[0].input["tool_input"] == {
        "purchase_order_id": "po-c-9001-w",
        "production_order_id": "production-c-9001",
        "expected_purchase_order_version": 4,
    }


@pytest.mark.critical
@pytest.mark.parametrize(
    ("recommendation", "current_source_versions", "error_match"),
    [
        (
            lambda context: _recommendation(context).model_copy(
                update={
                    "hold_purchase_order": PlacePurchaseOrderHoldInput(
                        purchase_order_id="po-unrelated",
                        production_order_id=context.production_order.record_id,
                        expected_purchase_order_version=context.purchase_order.source_version,
                    )
                },
            ),
            lambda context: context.source_versions,
            "does not target the current purchase order",
        ),
        (
            lambda context: _recommendation(context).model_copy(
                update={
                    "hold_purchase_order": PlacePurchaseOrderHoldInput(
                        purchase_order_id=context.purchase_order.record_id,
                        production_order_id=context.production_order.record_id,
                        expected_purchase_order_version=context.purchase_order.source_version + 1,
                    )
                },
            ),
            lambda context: context.source_versions,
            "does not bind the current purchase-order version",
        ),
        (
            _recommendation,
            lambda context: {**context.source_versions, str(context.purchase_order.evidence_id): 5},
            "stale_source_evidence",
        ),
    ],
    ids=("unrelated_purchase_order", "stale_embedded_po_version", "stale_context"),
)
def test_supplier_risk_control_rejects_unbound_or_stale_hold_before_any_persistence(
    recommendation: RecommendationFactory,
    current_source_versions: SourceVersionFactory,
    error_match: str,
) -> None:
    """The model cannot redirect a hold or revive stale supplier-risk evidence into a write plan."""
    context = _context()
    control, approval_store, workflow_store = _control()

    with pytest.raises(ScenarioCControlRejectedError, match=error_match):
        control.request_pending(
            context=context,
            recommendation=recommendation(context),
            current_source_versions=current_source_versions(context),
            policy_version="scenario_c_policy:v1",
            requested_at=NOW,
            expires_at=NOW + timedelta(hours=4),
        )

    approval_store.create_pending.assert_not_called()
    workflow_store.create.assert_not_called()


def test_manual_supplier_risk_recommendation_creates_no_approval_or_workflow() -> None:
    """Manual review is a deliberately non-writing Scenario C outcome."""
    from enterprise_agent.application.scenario_c_control import ScenarioCControlService

    approvals = MagicMock()
    workflow_state = MagicMock()
    result = ScenarioCControlService(
        approvals=approvals, workflow_state=workflow_state
    ).request_pending(
        context=MagicMock(),
        recommendation=ManualReviewRecommendation(
            outcome="MANUAL_REVIEW",
            reason="The supplier bulletin and ERP facts conflict.",
        ),
        current_source_versions={},
        policy_version="scenario_c_policy:v1",
        requested_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )

    assert result.pending is None
    assert result.workflow is None
    approvals.request_pending_plan.assert_not_called()
    workflow_state.stage_bounded_tool_plan.assert_not_called()


@pytest.mark.critical
def test_supplier_risk_control_denies_an_actor_without_the_hold_scope_before_persistence() -> None:
    """A planner cannot use the notification scope to smuggle an unauthorized purchase-order hold."""
    context = replace(
        _context(),
        actor=replace(DANA, scopes=frozenset({Scope("production:notify")})),
    )
    control, approval_store, workflow_store = _control()

    with pytest.raises(ScenarioCControlRejectedError, match="missing_required_scope"):
        control.request_pending(
            context=context,
            recommendation=_recommendation(context),
            current_source_versions=context.source_versions,
            policy_version="scenario_c_policy:v1",
            requested_at=NOW,
            expires_at=NOW + timedelta(hours=4),
        )

    approval_store.create_pending.assert_not_called()
    workflow_store.create.assert_not_called()
