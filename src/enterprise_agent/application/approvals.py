"""Immutable Scenario A plan construction and compare-and-swap human approval control."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from enterprise_agent.application.context import AuthorizedContextBundle
from enterprise_agent.application.gate import GateDecision, GateStatus, ScenarioAGate
from enterprise_agent.application.planning import (
    EnterWorkflowRecommendation,
    ScenarioARecommendation,
)
from enterprise_agent.domain import Approval, ApprovalId, ApprovalStatus, Plan, PlanId, UserId
from enterprise_agent.ports import PlanApprovalPort


class PlanNotApprovableError(ValueError):
    """Raised when an approval is not bound to the exact still-valid plan evidence."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingPlanApproval:
    """The immutable proposed plan and its only associated pending human approval."""

    plan: Plan
    approval: Approval
    gate_decision: GateDecision


class _ScenarioAGate(Protocol):
    """Capture the gate dependency without coupling plan persistence to a concrete class."""

    def evaluate(
        self,
        context: AuthorizedContextBundle,
        recommendation: ScenarioARecommendation,
        *,
        current_source_versions: Mapping[str, int],
    ) -> GateDecision:
        """Return the policy decision for the exact context snapshot about to be persisted."""
        ...


class _ApprovalEscalationScheduler(Protocol):
    """Schedule one deterministic end-of-day check after a pending approval is persisted."""

    def schedule_escalation(self, approval: Approval) -> object:
        """Persist the replay-safe escalation task for this exact approval request."""
        ...


class ScenarioAApprovalService:
    """Persist a write intent only after a fresh gate result requires human approval."""

    def __init__(
        self,
        store: PlanApprovalPort,
        *,
        gate: _ScenarioAGate | None = None,
        escalation_scheduler: _ApprovalEscalationScheduler | None = None,
    ) -> None:
        """Depend on a transactional persistence port and the deterministic Scenario A gate."""
        self._store = store
        self._gate = gate or ScenarioAGate()
        self._escalation_scheduler = escalation_scheduler

    def request_pending(
        self,
        context: AuthorizedContextBundle,
        recommendation: ScenarioARecommendation,
        *,
        current_source_versions: Mapping[str, int],
        policy_version: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> PendingPlanApproval:
        """Recheck policy, then atomically persist a pending approval for one immutable intent."""
        if expires_at <= requested_at:
            raise PlanNotApprovableError("plan expiry must be after the approval request time")

        decision = self._gate.evaluate(
            context,
            recommendation,
            current_source_versions=current_source_versions,
        )
        if (
            decision.status is not GateStatus.PENDING_APPROVAL
            or not decision.approval_required
            or not isinstance(recommendation, EnterWorkflowRecommendation)
            or decision.estimated_value is None
        ):
            raise PlanNotApprovableError("only a gate result pending approval may create a plan")

        approver_id = context.actor.user_id
        parameters = {
            "supplier_id": recommendation.supplier_id,
            "quantity": str(recommendation.quantity),
            "original_purchase_order_id": recommendation.original_purchase_order_id,
            "production_order_id": recommendation.production_order_id,
            "estimated_value_amount": str(decision.estimated_value.amount),
            "estimated_value_currency": decision.estimated_value.currency,
        }
        plan = Plan(
            plan_id=PlanId(str(uuid4())),
            attention_id=context.attention.attention_id,
            actor_id=context.actor.user_id,
            approver_id=approver_id,
            intent="enter_workflow",
            workflow_name=recommendation.workflow_name,
            workflow_version=recommendation.workflow_version,
            parameters=parameters,
            source_versions=dict(context.source_versions),
            policy_version=policy_version,
            plan_hash="",
            created_at=requested_at,
            expires_at=expires_at,
        )
        plan = replace(plan, plan_hash=recompute_plan_hash(plan))
        approval = Approval(
            approval_id=ApprovalId(str(uuid4())),
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            requester_id=context.actor.user_id,
            approver_id=approver_id,
            status=ApprovalStatus.PENDING,
            requested_at=requested_at,
            expires_at=expires_at,
        )
        self._store.create_pending(plan, approval)
        if self._escalation_scheduler is not None:
            self._escalation_scheduler.schedule_escalation(approval)
        return PendingPlanApproval(plan=plan, approval=approval, gate_decision=decision)

    def approve(
        self,
        *,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decider_id: UserId,
        current_source_versions: Mapping[str, int],
        decided_at: datetime,
    ) -> Approval:
        """Approve only one unchanged, current plan before any later executor can use it."""
        record = self._store.load(approval_id)
        if record is None:
            raise PlanNotApprovableError("approval does not exist")
        plan, approval = record
        if approval.status not in {ApprovalStatus.PENDING, ApprovalStatus.REROUTED}:
            raise PlanNotApprovableError("approval is no longer pending or rerouted")
        if decider_id != approval.approver_id:
            raise PlanNotApprovableError("approval decision actor is not the current approver")
        if decided_at >= plan.expires_at or decided_at >= approval.expires_at:
            raise PlanNotApprovableError("plan approval has expired")
        if expected_plan_hash != plan.plan_hash or approval.plan_hash != plan.plan_hash:
            raise PlanNotApprovableError("approval does not match the expected plan hash")
        if recompute_plan_hash(plan) != plan.plan_hash:
            raise PlanNotApprovableError(
                "persisted plan hash does not match immutable plan content"
            )
        if dict(current_source_versions) != dict(plan.source_versions):
            raise PlanNotApprovableError("plan source evidence is stale")

        approved = self._store.approve(approval_id, expected_plan_hash, decider_id, decided_at)
        if approved is None:
            raise PlanNotApprovableError("approval could not be atomically advanced")
        return approved

    def reject(
        self,
        *,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decider_id: UserId,
        decided_at: datetime,
    ) -> Approval:
        """Let only the current approver reject the same still-valid immutable plan binding."""
        record = self._store.load(approval_id)
        if record is None:
            raise PlanNotApprovableError("approval does not exist")
        plan, approval = record
        if approval.status not in {ApprovalStatus.PENDING, ApprovalStatus.REROUTED}:
            raise PlanNotApprovableError("approval is no longer pending or rerouted")
        if decider_id != approval.approver_id:
            raise PlanNotApprovableError("approval decision actor is not the current approver")
        if decided_at >= plan.expires_at or decided_at >= approval.expires_at:
            raise PlanNotApprovableError("plan approval has expired")
        if expected_plan_hash != plan.plan_hash or approval.plan_hash != plan.plan_hash:
            raise PlanNotApprovableError("approval does not match the expected plan hash")
        if recompute_plan_hash(plan) != plan.plan_hash:
            raise PlanNotApprovableError(
                "persisted plan hash does not match immutable plan content"
            )
        rejected = self._store.reject(approval_id, expected_plan_hash, decider_id, decided_at)
        if rejected is None:
            raise PlanNotApprovableError("approval could not be atomically advanced")
        return rejected


def recompute_plan_hash(plan: Plan) -> str:
    """Canonicalize every approval-relevant intent field into one deterministic SHA-256 binding."""
    payload = {
        "attention_id": str(plan.attention_id),
        "actor_id": str(plan.actor_id),
        "approver_id": str(plan.approver_id),
        "intent": plan.intent,
        "workflow_name": plan.workflow_name,
        "workflow_version": plan.workflow_version,
        "parameters": dict(plan.parameters),
        "source_versions": dict(plan.source_versions),
        "policy_version": plan.policy_version,
        "expires_at": plan.expires_at.isoformat(),
    }
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as error:
        raise PlanNotApprovableError("plan contains non-canonical hash material") from error
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
