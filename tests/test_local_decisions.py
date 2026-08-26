"""Approval-decision contracts for the optional local verification UI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Never

import pytest

from enterprise_agent.application.approvals import recompute_plan_hash
from enterprise_agent.domain import (
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionId,
    AuditEvent,
    Plan,
    PlanId,
    RunId,
    UserId,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
ACTOR_ID = UserId("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UserId("00000000-0000-0000-0000-000000000002")
APPROVAL_ID = ApprovalId("00000000-0000-0000-0000-000000000030")
PLAN_ID = PlanId("00000000-0000-0000-0000-000000000020")
RUN_ID = RunId("run-local-decision")


@dataclass
class MemoryApprovalStore:
    """Persist one mutable approval status while retaining the immutable plan binding."""

    binding: tuple[Plan, Approval]
    approve_calls: int = 0
    reject_calls: int = 0

    def create_pending(self, plan: Plan, approval: Approval) -> Never:
        raise AssertionError("the local decision service must not create a plan")

    def load(self, approval_id: ApprovalId) -> tuple[Plan, Approval] | None:
        return self.binding if approval_id == APPROVAL_ID else None

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        return self.binding if plan_id == PLAN_ID else None

    def approve(
        self,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decider_id: UserId,
        decided_at: datetime,
    ) -> Approval | None:
        self.approve_calls += 1
        plan, approval = self.binding
        if (
            approval_id != approval.approval_id
            or expected_plan_hash != plan.plan_hash
            or decider_id != approval.approver_id
            or approval.status not in {ApprovalStatus.PENDING, ApprovalStatus.REROUTED}
        ):
            return None
        approved = replace(approval, status=ApprovalStatus.APPROVED, decided_at=decided_at)
        self.binding = (plan, approved)
        return approved

    def reject(
        self,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decider_id: UserId,
        decided_at: datetime,
    ) -> Approval | None:
        self.reject_calls += 1
        plan, approval = self.binding
        if (
            approval_id != approval.approval_id
            or expected_plan_hash != plan.plan_hash
            or decider_id != approval.approver_id
            or approval.status not in {ApprovalStatus.PENDING, ApprovalStatus.REROUTED}
        ):
            return None
        rejected = replace(approval, status=ApprovalStatus.REJECTED, decided_at=decided_at)
        self.binding = (plan, rejected)
        return rejected

    def reroute(self, *args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise AssertionError("the local decision service must not reroute approvals")


@dataclass
class FixedClock:
    """Return the local deterministic business time without permitting wall-clock fallback."""

    current_at: datetime = NOW

    def now(self) -> datetime:
        return self.current_at


@dataclass
class RecordingAudit:
    """Retain exactly the event written by the shared approval service."""

    events: list[AuditEvent]

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        return tuple(event for event in self.events if event.run_id == run_id)

    def latest_run_for_plan(self, plan_id: PlanId) -> RunId | None:
        return RUN_ID if plan_id == PLAN_ID else None


@dataclass
class FixedFreshness:
    """Supply a deterministic current-version result without provider or database access."""

    current_versions: dict[str, int]

    def current_source_versions(self, plan: Plan) -> dict[str, int]:
        assert plan.plan_id == PLAN_ID
        return self.current_versions


def _binding(*, approver_id: UserId = ACTOR_ID) -> tuple[Plan, Approval]:
    """Build one current immutable plan and approval whose hash is never provided by the caller."""
    plan = Plan(
        plan_id=PLAN_ID,
        attention_id=AttentionId("00000000-0000-0000-0000-000000000010"),
        actor_id=ACTOR_ID,
        approver_id=approver_id,
        intent="enter_workflow",
        workflow_name="po_reroute",
        workflow_version=1,
        parameters={"supplier_id": "supplier-z"},
        source_versions={"erp:inventory:part-x": 4},
        policy_version="scenario_a_policy:v1",
        plan_hash="",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )
    plan = replace(plan, plan_hash=recompute_plan_hash(plan))
    return plan, Approval(
        approval_id=APPROVAL_ID,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        requester_id=ACTOR_ID,
        approver_id=approver_id,
        status=ApprovalStatus.PENDING,
        requested_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )


def _service(
    *,
    actor_id: UserId = ACTOR_ID,
    current_versions: dict[str, int] | None = None,
    approver_id: UserId = ACTOR_ID,
):
    """Compose the new local decision boundary only from in-memory ports."""
    from enterprise_agent.application.local_decisions import LocalApprovalDecisionService

    store = MemoryApprovalStore(binding=_binding(approver_id=approver_id))
    audit = RecordingAudit(events=[])
    service = LocalApprovalDecisionService(
        actor_id=actor_id,
        approvals=store,
        freshness=FixedFreshness(
            current_versions=current_versions or {"erp:inventory:part-x": 4}
        ),
        clock=FixedClock(),
        audit=audit,
        audit_runs=audit,
    )
    return service, store, audit


def test_current_approver_can_approve_only_the_server_loaded_hash_and_audit_run() -> None:
    """An approval page may advance one current plan without receiving a client-controlled hash."""
    from enterprise_agent.application.local_decisions import ApprovalDecision

    service, store, audit = _service()

    result = service.decide(approval_id=str(APPROVAL_ID), decision=ApprovalDecision.APPROVE)

    assert result.decision_state == "approved"
    assert result.audit_run_id == str(RUN_ID)
    assert store.approve_calls == 1
    assert store.reject_calls == 0
    assert store.binding[1].status is ApprovalStatus.APPROVED
    assert [event.event_type for event in audit.events] == ["approval.approved"]


def test_stale_source_versions_cannot_advance_an_approval() -> None:
    """A stale evidence snapshot stops before the shared approval compare-and-swap write."""
    from enterprise_agent.application.local_decisions import (
        ApprovalDecision,
        LocalApprovalDecisionStaleError,
    )

    service, store, audit = _service(current_versions={"erp:inventory:part-x": 5})

    with pytest.raises(LocalApprovalDecisionStaleError):
        service.decide(approval_id=str(APPROVAL_ID), decision=ApprovalDecision.APPROVE)

    assert store.approve_calls == 0
    assert store.binding[1].status is ApprovalStatus.PENDING
    assert audit.events == []


def test_non_approver_can_review_but_cannot_submit_a_decision() -> None:
    """Viewing a plan is intentionally broader than issuing its approval decision."""
    from enterprise_agent.application.local_decisions import (
        ApprovalDecision,
        LocalApprovalDecisionAccessDeniedError,
    )

    service, store, _ = _service(actor_id=OTHER_ACTOR_ID)

    availability = service.availability(str(APPROVAL_ID))
    assert not availability.can_decide
    with pytest.raises(LocalApprovalDecisionAccessDeniedError):
        service.decide(approval_id=str(APPROVAL_ID), decision=ApprovalDecision.REJECT)
    assert store.approve_calls == 0
    assert store.reject_calls == 0
