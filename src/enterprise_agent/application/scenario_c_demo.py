"""Deterministic, approval-only Scenario C staging for the local seeded demonstration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from enterprise_agent.adapters import (
    PostgresAttentionAdapter,
    PostgresAuditAdapter,
    PostgresDemoClock,
    PostgresErpAdapter,
    PostgresIdentityAdapter,
    PostgresKnowledgeAdapter,
    PostgresPlanApprovalAdapter,
    PostgresWorkflowStateAdapter,
)
from enterprise_agent.application.approvals import PlanApprovalService
from enterprise_agent.application.planning import HoldAndNotifyRecommendation
from enterprise_agent.application.scenario_c_context import ScenarioCContextAssembler
from enterprise_agent.application.scenario_c_control import (
    ScenarioCControlRejectedError,
    ScenarioCControlService,
)
from enterprise_agent.application.supplier_risk import SupplierRiskDetector
from enterprise_agent.application.tools import NotifyProductionInput, PlacePurchaseOrderHoldInput
from enterprise_agent.application.workflow_state import WorkflowStateService
from enterprise_agent.domain import ApprovalId, AttentionId, RunId, UserId, WorkflowId
from enterprise_agent.seed import ID_DANA


class ScenarioCDeterministicRunError(ValueError):
    """Raised when the fixed local Scenario C facts cannot safely stage one pending decision."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ScenarioCPendingRun:
    """Only the identifiers an operator needs to review a newly staged Scenario C plan."""

    run_id: RunId
    attention_id: AttentionId
    approval_id: ApprovalId
    workflow_id: WorkflowId


def stage_scenario_c_pending(
    database_url: str,
    *,
    run_id: RunId,
) -> ScenarioCPendingRun:
    """Stage the one seeded supplier-risk plan without approving or executing any external effect."""
    clock = PostgresDemoClock(database_url)
    identity = PostgresIdentityAdapter(database_url)
    actor = identity.actor_for(UserId(str(ID_DANA)))
    knowledge = PostgresKnowledgeAdapter(database_url)
    erp = PostgresErpAdapter(database_url)
    attention_store = PostgresAttentionAdapter(database_url)
    detections = SupplierRiskDetector(knowledge, erp, attention_store, clock).detect(actor, run_id)
    if len(detections) != 1:
        raise ScenarioCDeterministicRunError(
            "expected exactly one current seeded supplier-risk attention item"
        )
    detection = detections[0]
    if not detection.registration.created:
        raise ScenarioCDeterministicRunError(
            "the seeded supplier-risk attention item already exists; reset and seed before rerunning"
        )
    context = ScenarioCContextAssembler(identity, knowledge, erp).assemble(
        user_id=actor.user_id,
        attention=detection.registration.attention,
        trigger=detection.risk.trigger,
    )
    recommendation = HoldAndNotifyRecommendation(
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
        rationale="The current seeded supplier-risk bulletin affects this open purchase order.",
    )
    audit = PostgresAuditAdapter(database_url)
    requested_at = clock.now()
    try:
        pending = ScenarioCControlService(
            approvals=PlanApprovalService(PostgresPlanApprovalAdapter(database_url), audit=audit),
            workflow_state=WorkflowStateService(PostgresWorkflowStateAdapter(database_url)),
        ).request_pending(
            context=context,
            recommendation=recommendation,
            current_source_versions=context.source_versions,
            policy_version="scenario_c_policy:v1",
            requested_at=requested_at,
            expires_at=requested_at + _APPROVAL_WINDOW,
            run_id=run_id,
        )
    except ScenarioCControlRejectedError as error:
        raise ScenarioCDeterministicRunError(
            "the seeded supplier-risk control was denied"
        ) from error
    if pending.pending is None or pending.workflow is None:
        raise ScenarioCDeterministicRunError(
            "the seeded supplier-risk recommendation was not executable"
        )
    return ScenarioCPendingRun(
        run_id=run_id,
        attention_id=context.attention.attention_id,
        approval_id=pending.pending.approval.approval_id,
        workflow_id=pending.workflow.workflow.workflow_id,
    )


_APPROVAL_WINDOW = timedelta(hours=4)
