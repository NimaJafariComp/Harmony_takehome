"""Scenario B orchestration over the shared approval and bounded-tool workflow controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from enterprise_agent.application.approvals import PendingPlanApproval, PlanApprovalService
from enterprise_agent.application.bounded_tool_plan import (
    BoundedToolCall,
    BoundedToolPlanGate,
    build_bounded_tool_plan,
)
from enterprise_agent.application.gate import GateStatus
from enterprise_agent.application.planning import (
    FlagShortageToPurchasingRecommendation,
    ManualReviewRecommendation,
    ReallocateAndNotifyRecommendation,
    ScenarioBRecommendation,
)
from enterprise_agent.application.quality_context import ScenarioBContextBundle
from enterprise_agent.application.tools import ToolName
from enterprise_agent.application.workflow_state import WorkflowStateService
from enterprise_agent.domain import RunId, WorkflowId, WorkflowStateSnapshot


class ScenarioBControlRejectedError(PermissionError):
    """Raised when Scenario B cannot enter the common approval-gated write control plane."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ScenarioBControlResult:
    """The pending control records for an executable recommendation, or a manual-review outcome."""

    pending: PendingPlanApproval | None
    workflow: WorkflowStateSnapshot | None


class ScenarioBControlService:
    """Translate only typed Scenario B recommendations into one shared bounded-tool plan."""

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
        context: ScenarioBContextBundle,
        recommendation: ScenarioBRecommendation,
        current_source_versions: Mapping[str, int],
        policy_version: str,
        requested_at: datetime,
        expires_at: datetime,
        run_id: RunId | None = None,
        workflow_id: WorkflowId | None = None,
    ) -> ScenarioBControlResult:
        """Persist and stage only a fresh, authorized, approval-gated selected-tool plan."""
        _require_safe_reallocation_coverage(context, recommendation)
        tool_calls = tool_calls_for_scenario_b_recommendation(recommendation)
        if not tool_calls:
            return ScenarioBControlResult(pending=None, workflow=None)
        decision = self._gate.evaluate(
            context.actor,
            tool_calls,
            source_versions=context.source_versions,
            current_source_versions=current_source_versions,
        )
        if decision.status is not GateStatus.PENDING_APPROVAL or not decision.approval_required:
            reasons = ", ".join(reason.value for reason in decision.denial_reasons) or "policy"
            raise ScenarioBControlRejectedError(f"Scenario B control denied: {reasons}")
        plan = build_bounded_tool_plan(
            attention_id=context.attention.attention_id,
            actor_id=context.actor.user_id,
            approver_id=context.production_supervisor_id,
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
        return ScenarioBControlResult(pending=pending, workflow=workflow)


def tool_calls_for_scenario_b_recommendation(
    recommendation: ScenarioBRecommendation,
) -> tuple[BoundedToolCall, ...]:
    """Map each permissible Scenario B outcome to its exact registered ordered tool calls."""
    if isinstance(recommendation, ReallocateAndNotifyRecommendation):
        return (
            BoundedToolCall(
                tool_name=ToolName.REALLOCATE_LOT,
                input=recommendation.reallocate_lot,
            ),
            BoundedToolCall(
                tool_name=ToolName.NOTIFY_PRODUCTION,
                input=recommendation.notify_production,
            ),
        )
    if isinstance(recommendation, FlagShortageToPurchasingRecommendation):
        return (
            BoundedToolCall(
                tool_name=ToolName.FLAG_SHORTAGE_TO_PURCHASING,
                input=recommendation.shortage,
            ),
        )
    if isinstance(recommendation, ManualReviewRecommendation):
        return ()
    raise ScenarioBControlRejectedError("Scenario B recommendation is not recognized")


def _require_safe_reallocation_coverage(
    context: ScenarioBContextBundle,
    recommendation: ScenarioBRecommendation,
) -> None:
    """Reject a claimed full-cover transfer unless one current released lot exactly covers the hold."""
    if not isinstance(recommendation, ReallocateAndNotifyRecommendation):
        return
    if len(context.alternative_lots) != 1:
        raise ScenarioBControlRejectedError(
            "Scenario B reallocation is ambiguous without an explicit lot-selection policy"
        )

    alternative = context.alternative_lots[0]
    reallocation = recommendation.reallocate_lot
    if alternative.record_id != reallocation.quality_lot_id:
        raise ScenarioBControlRejectedError(
            "Scenario B reallocation selects a lot outside the current authorized alternatives"
        )
    if reallocation.to_production_order_id != context.production_impact.record_id:
        raise ScenarioBControlRejectedError(
            "Scenario B reallocation does not target the current production impact"
        )

    required_quantity = _decimal_payload(
        context.production_allocation.payload, "allocated_quantity"
    )
    lot_quantity = _decimal_payload(alternative.payload, "quantity")
    allocated_quantity = _decimal_payload(
        alternative.payload, "allocated_quantity", default=Decimal()
    )
    available_quantity = (
        lot_quantity - allocated_quantity
        if lot_quantity is not None and allocated_quantity is not None
        else None
    )
    if (
        required_quantity is None
        or available_quantity is None
        or available_quantity < required_quantity
        or reallocation.quantity != required_quantity
    ):
        raise ScenarioBControlRejectedError(
            "Scenario B reallocation does not fully cover current production impact"
        )


def _decimal_payload(
    payload: Mapping[str, object], name: str, *, default: Decimal | None = None
) -> Decimal | None:
    """Read one finite non-negative quantity without treating malformed evidence as safe capacity."""
    value = payload.get(name, default)
    if value is None:
        return None
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return quantity if quantity.is_finite() and quantity >= 0 else None
