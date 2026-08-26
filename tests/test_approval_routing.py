"""End-of-day backup approval-routing contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import pytest

from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionId,
    DateRange,
    Evidence,
    EvidenceId,
    Plan,
    PlanId,
    ScheduledTask,
    ScheduledTaskStatus,
    Scope,
    UserId,
)
from enterprise_agent.ports import EvidenceQuery

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
END_OF_DAY = datetime(2026, 8, 24, 17, tzinfo=UTC)
DANA = UserId("00000000-0000-0000-0000-000000000001")
AVERY = UserId("00000000-0000-0000-0000-000000000002")


class RoutingResult(Protocol):
    """Subset of the routing result asserted without importing missing production code."""

    outcome: object
    approval: Approval | None


class ApprovalRouter(Protocol):
    """Subset of the new routing service contract exercised by these policy tests."""

    def schedule_escalation(self, approval: Approval) -> ScheduledTask:
        """Create the durable same-day escalation check for one pending approval."""
        ...

    def handle_claimed_task(self, task: ScheduledTask, *, routed_at: datetime) -> RoutingResult:
        """Evaluate one safely claimed end-of-day escalation task."""
        ...


def plan() -> Plan:
    """Build the immutable approved intent whose active approver may later be rerouted."""
    return Plan(
        plan_id=PlanId("00000000-0000-0000-0000-000000000701"),
        attention_id=AttentionId("00000000-0000-0000-0000-000000000601"),
        actor_id=DANA,
        approver_id=DANA,
        intent="enter_workflow",
        workflow_name="po_reroute",
        workflow_version=1,
        parameters={
            "estimated_value_amount": "1080",
            "estimated_value_currency": "USD",
            "supplier_id": "supplier-z",
        },
        source_versions={"erp:purchase_order:po-4812-y": 2},
        policy_version="scenario_a_policy:v1",
        plan_hash="sha256:immutable-routing-test",
        created_at=NOW,
        expires_at=NOW + timedelta(days=2),
    )


def pending_approval() -> Approval:
    """Build Dana's pending approval request for one immutable plan."""
    immutable_plan = plan()
    return Approval(
        approval_id=ApprovalId("00000000-0000-0000-0000-000000000801"),
        plan_id=immutable_plan.plan_id,
        plan_hash=immutable_plan.plan_hash,
        requester_id=DANA,
        approver_id=DANA,
        status=ApprovalStatus.PENDING,
        requested_at=NOW,
        expires_at=immutable_plan.expires_at,
    )


def actor(
    user_id: UserId,
    *,
    backup_approver_id: UserId | None = None,
    scopes: frozenset[Scope] = frozenset(),
    approval_limits: dict[str, Decimal] | None = None,
) -> ActorContext:
    """Build one identity response for routing-policy tests."""
    return ActorContext(
        user_id=user_id,
        role="purchasing_manager",
        scopes=scopes,
        plant_ids=frozenset(),
        backup_approver_id=backup_approver_id,
        approval_limits={} if approval_limits is None else approval_limits,
    )


def out_of_office() -> Evidence:
    """Return Dana's following-day absence as scoped calendar evidence."""
    return Evidence(
        evidence_id=EvidenceId("calendar:event:dana-ooo"),
        source="calendar",
        record_type="calendar_event",
        record_id="dana-ooo",
        source_version=1,
        observed_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
        payload={"event_type": "out_of_office"},
    )


@dataclass
class MemoryRoutingStore:
    """Model the single approval-row compare-and-swap needed by the routing service."""

    immutable_plan: Plan
    approval: Approval
    reroute_calls: int = 0

    def load(self, approval_id: ApprovalId) -> tuple[Plan, Approval] | None:
        if approval_id != self.approval.approval_id:
            return None
        return (self.immutable_plan, self.approval)

    def reroute(
        self,
        approval_id: ApprovalId,
        *,
        expected_plan_hash: str,
        original_approver_id: UserId,
        backup_approver_id: UserId,
        routed_at: datetime,
    ) -> Approval | None:
        self.reroute_calls += 1
        if (
            approval_id != self.approval.approval_id
            or self.approval.status is not ApprovalStatus.PENDING
            or self.approval.plan_hash != expected_plan_hash
            or self.approval.approver_id != original_approver_id
            or self.approval.expires_at <= routed_at
        ):
            return None
        self.approval = replace(
            self.approval,
            approver_id=backup_approver_id,
            status=ApprovalStatus.REROUTED,
        )
        return self.approval


@dataclass
class RecordingIdentity:
    """Return exact current identity records without granting extra lookup behavior."""

    actors: dict[UserId, ActorContext]

    def actor_for(self, user_id: UserId) -> ActorContext:
        return self.actors[user_id]


@dataclass
class RecordingCalendar:
    """Return selected absence evidence and retain the policy-owned query."""

    events: tuple[Evidence, ...]
    queries: list[tuple[ActorContext, EvidenceQuery]]

    def __init__(self, events: tuple[Evidence, ...]) -> None:
        self.events = events
        self.queries = []

    def query(self, principal: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        self.queries.append((principal, query))
        return self.events


@dataclass
class RecordingScheduler:
    """Retain the deterministic durable escalation task without implementing a queue."""

    tasks: list[ScheduledTask]

    def __init__(self) -> None:
        self.tasks = []

    def schedule(self, task: ScheduledTask) -> None:
        self.tasks.append(task)


def routing_service(
    *, events: tuple[Evidence, ...] = (out_of_office(),)
) -> tuple[ApprovalRouter, MemoryRoutingStore, RecordingCalendar, RecordingScheduler]:
    """Build the service with Dana, an authorized Avery, and one isolated approval record."""
    from enterprise_agent.application.approval_routing import ApprovalRoutingService

    stored_plan = plan()
    store = MemoryRoutingStore(stored_plan, pending_approval())
    identity = RecordingIdentity(
        {
            DANA: actor(
                DANA,
                backup_approver_id=AVERY,
                scopes=frozenset({Scope("calendar:read")}),
            ),
            AVERY: actor(
                AVERY,
                scopes=frozenset({Scope("approval:decide")}),
                approval_limits={"USD": Decimal("50000")},
            ),
        }
    )
    calendar = RecordingCalendar(events)
    scheduler = RecordingScheduler()
    return ApprovalRoutingService(store, identity, calendar, scheduler), store, calendar, scheduler


@pytest.mark.critical
def test_end_of_day_routing_schedules_once_and_reroutes_only_for_next_day_absence() -> None:
    """A pending approval reaches its designated capable backup without changing plan hash input."""
    from enterprise_agent.application.approval_routing import ApprovalRoutingOutcome

    router, store, calendar, scheduler = routing_service()
    task = router.schedule_escalation(store.approval)
    result = router.handle_claimed_task(
        replace(
            task,
            status=ScheduledTaskStatus.CLAIMED,
            lease_expires_at=END_OF_DAY + timedelta(minutes=5),
        ),
        routed_at=END_OF_DAY,
    )

    assert task.task_type == "approval_escalation"
    assert task.due_at == END_OF_DAY
    assert task.payload == {
        "approval_id": str(store.approval.approval_id),
        "original_approver_id": str(DANA),
        "plan_hash": plan().plan_hash,
    }
    assert scheduler.tasks == [task]
    assert result.outcome is ApprovalRoutingOutcome.REROUTED
    assert result.approval is not None
    assert result.approval.approver_id == AVERY
    assert result.approval.plan_hash == plan().plan_hash
    assert store.immutable_plan.approver_id == DANA
    assert calendar.queries == [
        (
            actor(DANA, backup_approver_id=AVERY, scopes=frozenset({Scope("calendar:read")})),
            EvidenceQuery(
                record_types=frozenset({"calendar_event"}),
                date_range=DateRange(start=date(2026, 8, 25), end=date(2026, 8, 25)),
            ),
        )
    ]


@pytest.mark.parametrize(
    ("events", "approval_update", "expected_outcome"),
    [
        ((), None, "ORIGINAL_APPROVER_AVAILABLE"),
        ((out_of_office(),), {"status": ApprovalStatus.APPROVED}, "NOT_PENDING"),
    ],
)
def test_routing_leaves_answered_or_available_approvals_with_the_original_approver(
    events: tuple[Evidence, ...],
    approval_update: dict[str, ApprovalStatus] | None,
    expected_outcome: str,
) -> None:
    """Calendar availability and an already answered request must both fail closed without rerouting."""
    from enterprise_agent.application.approval_routing import ApprovalRoutingOutcome

    router, store, _, _ = routing_service(events=events)
    if approval_update is not None:
        store.approval = replace(store.approval, **approval_update)
    task = router.schedule_escalation(pending_approval())
    result = router.handle_claimed_task(
        replace(
            task,
            status=ScheduledTaskStatus.CLAIMED,
            lease_expires_at=END_OF_DAY + timedelta(minutes=5),
        ),
        routed_at=END_OF_DAY,
    )

    assert result.outcome is getattr(ApprovalRoutingOutcome, expected_outcome)
    assert result.approval is None
    assert store.approval.approver_id == DANA
    assert store.reroute_calls == 0
