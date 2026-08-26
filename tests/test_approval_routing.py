"""End-of-day backup approval-routing contracts."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from enterprise_agent.application.approval_routing import (
    ApprovalRoutingOutcome,
    ApprovalRoutingService,
)
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
    ScheduledTaskId,
    ScheduledTaskStatus,
    Scope,
    UserId,
)
from enterprise_agent.ports import EvidenceQuery

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
END_OF_DAY = datetime(2026, 8, 24, 17, tzinfo=UTC)
DANA = UserId("00000000-0000-0000-0000-000000000001")
AVERY = UserId("00000000-0000-0000-0000-000000000002")


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
    reroute_returns_none: bool = False

    def create_pending(self, immutable_plan: Plan, approval: Approval) -> None:
        """Satisfy the full approval port while preserving one test binding."""
        self.immutable_plan = immutable_plan
        self.approval = approval

    def load(self, approval_id: ApprovalId) -> tuple[Plan, Approval] | None:
        if approval_id != self.approval.approval_id:
            return None
        return (self.immutable_plan, self.approval)

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        """Return the one stored binding only for its exact immutable plan identity."""
        return (
            self.load(self.approval.approval_id) if plan_id == self.immutable_plan.plan_id else None
        )

    def approve(
        self,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decider_id: UserId,
        decided_at: datetime,
    ) -> Approval | None:
        """Provide the terminal approval port method without broadening router test behavior."""
        return None

    def reject(
        self,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decider_id: UserId,
        decided_at: datetime,
    ) -> Approval | None:
        """Provide the terminal rejection port method without broadening router test behavior."""
        return None

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
            self.reroute_returns_none
            or approval_id != self.approval.approval_id
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

    def claim_due(self, now: datetime, limit: int) -> tuple[ScheduledTask, ...]:
        """Provide the unused queue-read port method for structural contract compatibility."""
        return ()

    def mark_succeeded(self, task_id: ScheduledTaskId, completed_at: datetime) -> None:
        """Provide the unused completion port method for structural contract compatibility."""


def routing_service(
    *,
    events: tuple[Evidence, ...] = (out_of_office(),),
    original_approver: ActorContext | None = None,
    backup_approver: ActorContext | None = None,
) -> tuple[ApprovalRoutingService, MemoryRoutingStore, RecordingCalendar, RecordingScheduler]:
    """Build the service with Dana, an authorized Avery, and one isolated approval record."""
    stored_plan = plan()
    store = MemoryRoutingStore(stored_plan, pending_approval())
    dana = original_approver or actor(
        DANA,
        backup_approver_id=AVERY,
        scopes=frozenset({Scope("calendar:read")}),
    )
    avery = backup_approver or actor(
        AVERY,
        scopes=frozenset({Scope("approval:decide")}),
        approval_limits={"USD": Decimal(50000)},
    )
    identity = RecordingIdentity({DANA: dana, AVERY: avery})
    calendar = RecordingCalendar(events)
    scheduler = RecordingScheduler()
    return ApprovalRoutingService(store, identity, calendar, scheduler), store, calendar, scheduler


@pytest.mark.critical
def test_end_of_day_routing_schedules_once_and_reroutes_only_for_next_day_absence() -> None:
    """A pending approval reaches its designated capable backup without changing plan hash input."""
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
    ("events", "approval_status", "expected_outcome"),
    [
        ((), None, "ORIGINAL_APPROVER_AVAILABLE"),
        ((out_of_office(),), ApprovalStatus.APPROVED, "NOT_PENDING"),
    ],
)
def test_routing_leaves_answered_or_available_approvals_with_the_original_approver(
    events: tuple[Evidence, ...],
    approval_status: ApprovalStatus | None,
    expected_outcome: str,
) -> None:
    """Calendar availability and an already answered request must both fail closed without rerouting."""
    router, store, _, _ = routing_service(events=events)
    if approval_status is not None:
        store.approval = replace(store.approval, status=approval_status)
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


def test_routing_fails_closed_for_missing_stale_or_expired_approval_bindings() -> None:
    """A scheduled task never mutates a deleted, altered, or expired approval record."""
    router, store, _, _ = routing_service()
    task = router.schedule_escalation(store.approval)
    claimed_task = replace(
        task,
        status=ScheduledTaskStatus.CLAIMED,
        lease_expires_at=END_OF_DAY + timedelta(minutes=5),
    )

    store.approval = replace(
        store.approval,
        approval_id=ApprovalId("00000000-0000-0000-0000-000000000899"),
    )
    assert (
        router.handle_claimed_task(claimed_task, routed_at=END_OF_DAY).outcome
        is ApprovalRoutingOutcome.MISSING_APPROVAL
    )

    router, store, _, _ = routing_service()
    task = router.schedule_escalation(store.approval)
    claimed_task = replace(
        task,
        status=ScheduledTaskStatus.CLAIMED,
        lease_expires_at=END_OF_DAY + timedelta(minutes=5),
    )
    store.immutable_plan = replace(store.immutable_plan, plan_hash="sha256:stale")
    assert (
        router.handle_claimed_task(claimed_task, routed_at=END_OF_DAY).outcome
        is ApprovalRoutingOutcome.STALE_TASK
    )

    router, store, _, _ = routing_service()
    task = router.schedule_escalation(store.approval)
    claimed_task = replace(
        task,
        status=ScheduledTaskStatus.CLAIMED,
        lease_expires_at=END_OF_DAY + timedelta(minutes=5),
    )
    store.approval = replace(store.approval, expires_at=END_OF_DAY)
    assert (
        router.handle_claimed_task(claimed_task, routed_at=END_OF_DAY).outcome
        is ApprovalRoutingOutcome.EXPIRED
    )


def test_routing_requires_a_designated_authorized_and_capable_backup() -> None:
    """An absence cannot broaden decision authority to an arbitrary or under-authorized user."""
    without_backup = actor(DANA, scopes=frozenset({Scope("calendar:read")}))
    router, store, _, _ = routing_service(original_approver=without_backup)
    task = router.schedule_escalation(store.approval)
    assert (
        router.handle_claimed_task(
            replace(
                task,
                status=ScheduledTaskStatus.CLAIMED,
                lease_expires_at=END_OF_DAY + timedelta(minutes=5),
            ),
            routed_at=END_OF_DAY,
        ).outcome
        is ApprovalRoutingOutcome.NO_BACKUP
    )

    incapable_backup = actor(AVERY, approval_limits={"USD": Decimal(50000)})
    router, store, _, _ = routing_service(backup_approver=incapable_backup)
    task = router.schedule_escalation(store.approval)
    assert (
        router.handle_claimed_task(
            replace(
                task,
                status=ScheduledTaskStatus.CLAIMED,
                lease_expires_at=END_OF_DAY + timedelta(minutes=5),
            ),
            routed_at=END_OF_DAY,
        ).outcome
        is ApprovalRoutingOutcome.BACKUP_NOT_AUTHORIZED
    )

    over_limit_backup = actor(
        AVERY,
        scopes=frozenset({Scope("approval:decide")}),
        approval_limits={"USD": Decimal(1079)},
    )
    router, store, _, _ = routing_service(backup_approver=over_limit_backup)
    task = router.schedule_escalation(store.approval)
    assert (
        router.handle_claimed_task(
            replace(
                task,
                status=ScheduledTaskStatus.CLAIMED,
                lease_expires_at=END_OF_DAY + timedelta(minutes=5),
            ),
            routed_at=END_OF_DAY,
        ).outcome
        is ApprovalRoutingOutcome.BACKUP_NOT_AUTHORIZED
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"estimated_value_amount": "1080", "estimated_value_currency": Decimal(1)},
        {"estimated_value_amount": "not-a-decimal", "estimated_value_currency": "USD"},
    ],
)
def test_routing_denies_malformed_persisted_value_authority_data(
    parameters: dict[str, object],
) -> None:
    """Unexpected persisted value fields fail closed before a backup approver is assigned."""
    router, store, _, _ = routing_service()
    store.immutable_plan = replace(store.immutable_plan, parameters=parameters)
    task = router.schedule_escalation(store.approval)

    result = router.handle_claimed_task(
        replace(
            task,
            status=ScheduledTaskStatus.CLAIMED,
            lease_expires_at=END_OF_DAY + timedelta(minutes=5),
        ),
        routed_at=END_OF_DAY,
    )

    assert result.outcome is ApprovalRoutingOutcome.BACKUP_NOT_AUTHORIZED
    assert store.reroute_calls == 0


def test_routing_fails_closed_when_calendar_evidence_is_not_next_day_out_of_office() -> None:
    """Only a matching absence event, rather than any calendar record, authorizes rerouting."""
    non_absence = replace(out_of_office(), payload={"event_type": "meeting"})
    router, store, _, _ = routing_service(events=(non_absence,))
    task = router.schedule_escalation(store.approval)

    result = router.handle_claimed_task(
        replace(
            task,
            status=ScheduledTaskStatus.CLAIMED,
            lease_expires_at=END_OF_DAY + timedelta(minutes=5),
        ),
        routed_at=END_OF_DAY,
    )

    assert result.outcome is ApprovalRoutingOutcome.ORIGINAL_APPROVER_AVAILABLE
    assert store.reroute_calls == 0


def test_routing_reports_a_compare_and_swap_race_without_reassigning_the_approval() -> None:
    """A worker that loses the durable reroute update does not claim a successful escalation."""
    router, store, _, _ = routing_service()
    store.reroute_returns_none = True
    task = router.schedule_escalation(store.approval)

    result = router.handle_claimed_task(
        replace(
            task,
            status=ScheduledTaskStatus.CLAIMED,
            lease_expires_at=END_OF_DAY + timedelta(minutes=5),
        ),
        routed_at=END_OF_DAY,
    )

    assert result.outcome is ApprovalRoutingOutcome.RACE_LOST
    assert store.approval.approver_id == DANA


def test_routing_rejects_invalid_task_state_payload_and_business_timestamps() -> None:
    """The route handler accepts only a due, leased, fully bound escalation task."""
    from enterprise_agent.application.approval_routing import ApprovalRoutingError

    router, store, _, _ = routing_service()
    task = router.schedule_escalation(store.approval)
    live_lease = END_OF_DAY + timedelta(minutes=5)

    with pytest.raises(ApprovalRoutingError, match="pending approval"):
        router.schedule_escalation(replace(store.approval, status=ApprovalStatus.APPROVED))
    with pytest.raises(ApprovalRoutingError, match="request time"):
        router.schedule_escalation(replace(store.approval, requested_at=NOW.replace(tzinfo=None)))
    with pytest.raises(ApprovalRoutingError, match="not an approval escalation"):
        router.handle_claimed_task(
            replace(task, task_type="arrival_check", status=ScheduledTaskStatus.CLAIMED),
            routed_at=END_OF_DAY,
        )
    with pytest.raises(ApprovalRoutingError, match="must hold a scheduler lease"):
        router.handle_claimed_task(task, routed_at=END_OF_DAY)
    with pytest.raises(ApprovalRoutingError, match="not due"):
        router.handle_claimed_task(
            replace(
                task,
                due_at=END_OF_DAY + timedelta(seconds=1),
                status=ScheduledTaskStatus.CLAIMED,
                lease_expires_at=live_lease,
            ),
            routed_at=END_OF_DAY,
        )
    with pytest.raises(ApprovalRoutingError, match="lease has expired"):
        router.handle_claimed_task(
            replace(task, status=ScheduledTaskStatus.CLAIMED, lease_expires_at=END_OF_DAY),
            routed_at=END_OF_DAY,
        )
    with pytest.raises(ApprovalRoutingError, match="lacks plan_hash"):
        router.handle_claimed_task(
            replace(
                task,
                status=ScheduledTaskStatus.CLAIMED,
                lease_expires_at=live_lease,
                payload={
                    "approval_id": str(store.approval.approval_id),
                    "original_approver_id": str(DANA),
                },
            ),
            routed_at=END_OF_DAY,
        )
    with pytest.raises(ApprovalRoutingError, match="routing time"):
        router.handle_claimed_task(
            replace(task, status=ScheduledTaskStatus.CLAIMED, lease_expires_at=live_lease),
            routed_at=END_OF_DAY.replace(tzinfo=None),
        )


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and retain diagnostics if it fails."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.critical
@pytest.mark.integration
def test_postgres_approval_creation_schedules_and_routes_to_avery_at_end_of_day(
    disposable_database: str,
) -> None:
    """The real database retains one hash-bound plan while durable routing changes active approver."""
    compose(
        "--profile",
        "tools",
        "run",
        "--build",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "alembic",
        "upgrade",
        "head",
    )
    command = (
        "from datetime import timedelta\n"
        "from decimal import Decimal\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresCalendarAdapter,\n"
        "    PostgresDemoClock,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter,\n"
        "    PostgresPlanApprovalAdapter,\n"
        "    PostgresSchedulerAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approval_routing import (\n"
        "    ApprovalRoutingOutcome,\n"
        "    ApprovalRoutingService,\n"
        ")\n"
        "from enterprise_agent.application.approvals import (\n"
        "    PlanNotApprovableError,\n"
        "    ScenarioAApprovalService,\n"
        ")\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.planning import EnterWorkflowRecommendation\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "clock = PostgresDemoClock(database_url)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "dana = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "avery = identity.actor_for(UserId('00000000-0000-0000-0000-000000000002'))\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "detection = StockoutDetector(erp, PostgresAttentionAdapter(database_url), clock).detect(dana, RunId('run-approval-routing'))[0]\n"
        "context = ScenarioAContextAssembler(identity, erp, PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=dana.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id='00000000-0000-0000-0000-000000000202', quantity=Decimal(60), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='Approved alternate meets the production date.')\n"
        "approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "scheduler = PostgresSchedulerAdapter(database_url, clock)\n"
        "router = ApprovalRoutingService(approvals, identity, PostgresCalendarAdapter(database_url), scheduler)\n"
        "service = ScenarioAApprovalService(approvals, escalation_scheduler=router)\n"
        "now = clock.now()\n"
        "pending = service.request_pending(context, recommendation, current_source_versions=context.source_versions, policy_version='scenario_a_policy:v1', requested_at=now, expires_at=now + timedelta(days=2))\n"
        "assert scheduler.claim_due(now, limit=1) == ()\n"
        "at_end_of_day = clock.advance(timedelta(hours=8))\n"
        "claimed = scheduler.claim_due(at_end_of_day, limit=1)\n"
        "assert len(claimed) == 1\n"
        "routed = router.handle_claimed_task(claimed[0], routed_at=at_end_of_day)\n"
        "assert routed.outcome is ApprovalRoutingOutcome.REROUTED\n"
        "assert routed.approval is not None and routed.approval.approver_id == avery.user_id\n"
        "scheduler.mark_succeeded(claimed[0].task_id, at_end_of_day)\n"
        "plan, active_approval = approvals.load(pending.approval.approval_id) or (None, None)\n"
        "assert plan is not None and active_approval is not None\n"
        "assert plan.approver_id == dana.user_id and plan.plan_hash == pending.plan.plan_hash\n"
        "assert active_approval.status is ApprovalStatus.REROUTED and active_approval.plan_hash == plan.plan_hash\n"
        "try:\n"
        "    service.approve(approval_id=active_approval.approval_id, expected_plan_hash=plan.plan_hash, decider_id=dana.user_id, current_source_versions=context.source_versions, decided_at=at_end_of_day)\n"
        "except PlanNotApprovableError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('Dana must not retain decision authority after rerouting')\n"
        "approved = service.approve(approval_id=active_approval.approval_id, expected_plan_hash=plan.plan_hash, decider_id=avery.user_id, current_source_versions=context.source_versions, decided_at=at_end_of_day)\n"
        "assert approved.status is ApprovalStatus.APPROVED and approved.approver_id == avery.user_id\n"
        "with create_engine(database_url).connect() as connection:\n"
        "    task = connection.execute(text(\"SELECT status, payload->>'approval_id', payload->>'original_approver_id' FROM scheduled_tasks\")).one()\n"
        "assert task == ('succeeded', str(pending.approval.approval_id), str(dana.user_id))\n"
    )
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "python",
        "-c",
        command,
    )
