"""Scenario C orchestration over the shared approval and bounded-tool workflow controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from enterprise_agent.application.approvals import PendingPlanApproval, PlanApprovalService
from enterprise_agent.application.bounded_tool_plan import (
    BoundedToolCall,
    BoundedToolPlanGate,
    build_bounded_tool_plan,
)
from enterprise_agent.application.gate import GateStatus
from enterprise_agent.application.planning import (
    HoldAndNotifyRecommendation,
    ManualReviewRecommendation,
    ScenarioCRecommendation,
)
from enterprise_agent.application.scenario_c_context import ScenarioCContextBundle
from enterprise_agent.application.tools import ToolName
from enterprise_agent.application.workflow_state import WorkflowStateService
from enterprise_agent.domain import RunId, WorkflowId, WorkflowStateSnapshot


class ScenarioCControlRejectedError(PermissionError):
    """Raised when Scenario C cannot enter the common approval-gated write control plane."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ScenarioCControlResult:
    """The pending control records for an executable recommendation, or a manual-review outcome."""

    pending: PendingPlanApproval | None
    workflow: WorkflowStateSnapshot | None


class ScenarioCControlService:
    """Translate only typed Scenario C recommendations into one shared bounded-tool plan."""

    def __init__(
        self,
        *,
        approvals: PlanApprovalService,
        workflow_state: WorkflowStateService,
        gate: BoundedToolPlanGate | None = None,
    ) -> None:
        """Use existing approval persistence and workflow state services, not scenario-owned writes."""
        self._approvals = approvals
        self._workflow_state = workflow_state
        self._gate = gate or BoundedToolPlanGate()

    def request_pending(
        self,
        *,
        context: ScenarioCContextBundle,
        recommendation: ScenarioCRecommendation,
        current_source_versions: Mapping[str, int],
        policy_version: str,
        requested_at: datetime,
        expires_at: datetime,
        run_id: RunId | None = None,
        workflow_id: WorkflowId | None = None,
    ) -> ScenarioCControlResult:
        """Persist and stage only a fresh, authorized, approval-gated selected-tool plan."""
        tool_calls = tool_calls_for_scenario_c_recommendation(recommendation)
        if not tool_calls:
            return ScenarioCControlResult(pending=None, workflow=None)
        _require_current_hold_targets(context, recommendation)
        decision = self._gate.evaluate(
            context.actor,
            tool_calls,
            source_versions=context.source_versions,
            current_source_versions=current_source_versions,
        )
        if decision.status is not GateStatus.PENDING_APPROVAL or not decision.approval_required:
            reasons = ", ".join(reason.value for reason in decision.denial_reasons) or "policy"
            raise ScenarioCControlRejectedError(f"Scenario C control denied: {reasons}")
        plan = build_bounded_tool_plan(
            attention_id=context.attention.attention_id,
            actor_id=context.actor.user_id,
            approver_id=context.actor.user_id,
            tool_calls=tool_calls,
            source_versions=context.source_versions,
            policy_version=policy_version,
            created_at=requested_at,
            expires_at=expires_at,
        )
        pending = self._approvals.request_pending_plan(
            plan,
            decision,
            requested_at=requested_at,
            expires_at=expires_at,
            evidence_ids=tuple(item.evidence_id for item in context.evidence),
            planner_outcome=recommendation.outcome,
            run_id=run_id,
        )
        workflow = self._workflow_state.stage_bounded_tool_plan(
            pending.plan,
            created_at=requested_at,
            workflow_id=workflow_id,
            audit_run_id=run_id,
        )
        return ScenarioCControlResult(pending=pending, workflow=workflow)


def tool_calls_for_scenario_c_recommendation(
    recommendation: ScenarioCRecommendation,
) -> tuple[BoundedToolCall, ...]:
    """Map each permissible Scenario C outcome to its exact registered ordered tool calls."""
    if isinstance(recommendation, HoldAndNotifyRecommendation):
        return (
            BoundedToolCall(
                tool_name=ToolName.PLACE_PURCHASE_ORDER_HOLD,
                input=recommendation.hold_purchase_order,
            ),
            BoundedToolCall(
                tool_name=ToolName.NOTIFY_PRODUCTION,
                input=recommendation.notify_production,
            ),
        )
    if isinstance(recommendation, ManualReviewRecommendation):
        return ()
    raise ScenarioCControlRejectedError("Scenario C recommendation is not recognized")


def _require_current_hold_targets(
    context: ScenarioCContextBundle,
    recommendation: ScenarioCRecommendation,
) -> None:
    """Bind the only writable request to the exact PO, production demand, and PO version reviewed."""
    if not isinstance(recommendation, HoldAndNotifyRecommendation):
        return
    hold = recommendation.hold_purchase_order
    if hold.purchase_order_id != context.purchase_order.record_id:
        raise ScenarioCControlRejectedError(
            "Scenario C hold does not target the current purchase order"
        )
    if hold.production_order_id != context.production_order.record_id:
        raise ScenarioCControlRejectedError(
            "Scenario C hold does not target the current production order"
        )
    if hold.expected_purchase_order_version != context.purchase_order.source_version:
        raise ScenarioCControlRejectedError(
            "Scenario C hold does not bind the current purchase-order version"
        )
    if recommendation.notify_production.production_order_id != context.production_order.record_id:
        raise ScenarioCControlRejectedError(
            "Scenario C notification does not target the current production order"
        )
