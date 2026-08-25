"""Safety contracts for claiming and advancing the declared Scenario A workflow."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionId,
    Plan,
    PlanId,
    Scope,
    UserId,
    WorkflowId,
    WorkflowStateSnapshot,
    WorkflowStatus,
    WorkflowStepStatus,
)

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)
WORKFLOW_ID = WorkflowId("00000000-0000-0000-0000-000000000901")
ALL_SCENARIO_A_WRITE_SCOPES = frozenset(
    {
        Scope("erp:po:create"),
        Scope("erp:po:cancel"),
        Scope("production:notify"),
        Scope("scheduler:write"),
    }
)


def approved_plan() -> Plan:
    """Build one hash-valid approved intent whose state can be safely staged."""
    from enterprise_agent.application.approvals import recompute_plan_hash

    plan = Plan(
        plan_id=PlanId("00000000-0000-0000-0000-000000000701"),
        attention_id=AttentionId("00000000-0000-0000-0000-000000000601"),
        actor_id=UserId("00000000-0000-0000-0000-000000000001"),
        approver_id=UserId("00000000-0000-0000-0000-000000000001"),
        intent="enter_workflow",
        workflow_name="po_reroute",
        workflow_version=1,
        parameters={
            "supplier_id": "supplier-z",
            "quantity": "60",
            "original_purchase_order_id": "po-4812-y",
            "production_order_id": "production-4812",
        },
        source_versions={"erp:purchase_order:po-4812-y": 2},
        policy_version="scenario_a_policy:v1",
        plan_hash="",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )
    return replace(plan, plan_hash=recompute_plan_hash(plan))


def approval_for(plan: Plan, *, status: ApprovalStatus = ApprovalStatus.APPROVED) -> Approval:
    """Return the persisted human decision associated with the immutable test plan."""
    return Approval(
        approval_id=ApprovalId("00000000-0000-0000-0000-000000000801"),
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        requester_id=plan.actor_id,
        approver_id=plan.approver_id,
        status=status,
        requested_at=NOW,
        expires_at=plan.expires_at,
        decided_at=NOW + timedelta(minutes=1) if status is ApprovalStatus.APPROVED else None,
    )


def actor_with(scopes: frozenset[Scope]) -> ActorContext:
    """Return the current identity used by the executor's write-scope revalidation."""
    return ActorContext(
        user_id=UserId("00000000-0000-0000-0000-000000000001"),
        role="purchasing_manager",
        scopes=scopes,
        plant_ids=frozenset(),
        backup_approver_id=None,
        approval_limits={},
    )


@dataclass
class MemoryWorkflowStore:
    """Small stateful fake that models atomic claim and declared guard transitions."""

    snapshots: dict[WorkflowId, WorkflowStateSnapshot]
    claim_calls: list[tuple[WorkflowId, str]]
    complete_guard_calls: list[tuple[WorkflowId, str, int]]
    lose_next_guard_transition: bool

    def __init__(self) -> None:
        self.snapshots = {}
        self.claim_calls = []
        self.complete_guard_calls = []
        self.lose_next_guard_transition = False

    def create(self, snapshot: WorkflowStateSnapshot) -> None:
        self.snapshots[snapshot.workflow.workflow_id] = snapshot

    def load(self, workflow_id: WorkflowId) -> WorkflowStateSnapshot | None:
        return self.snapshots.get(workflow_id)

    def claim(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        self.claim_calls.append((workflow_id, worker_id))
        snapshot = self.snapshots.get(workflow_id)
        if snapshot is None:
            return None
        workflow = snapshot.workflow
        runnable = workflow.status is WorkflowStatus.PENDING or (
            workflow.status is WorkflowStatus.RUNNING
            and (
                workflow.lease_owner == worker_id
                or workflow.lease_expires_at is not None
                and workflow.lease_expires_at <= claimed_at
            )
        )
        if not runnable:
            return None
        claimed = replace(
            workflow,
            status=WorkflowStatus.RUNNING,
            started_at=workflow.started_at or claimed_at,
            lease_owner=worker_id,
            lease_expires_at=lease_expires_at,
            updated_at=claimed_at,
        )
        updated = replace(snapshot, workflow=claimed)
        self.snapshots[workflow_id] = updated
        return updated

    def complete_guard_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        self.complete_guard_calls.append((workflow_id, worker_id, expected_step_index))
        snapshot = self.snapshots.get(workflow_id)
        if snapshot is None:
            return None
        if self.lose_next_guard_transition:
            return None
        workflow = snapshot.workflow
        if (
            workflow.status is not WorkflowStatus.RUNNING
            or workflow.lease_owner != worker_id
            or workflow.lease_expires_at is None
            or workflow.lease_expires_at <= completed_at
            or workflow.current_step != expected_step_index - 1
        ):
            return None
        step = snapshot.steps[expected_step_index - 1]
        if step.tool_name is not None or step.status is not WorkflowStepStatus.PENDING:
            return None
        completed_step = replace(
            step,
            status=WorkflowStepStatus.SUCCEEDED,
            attempt_count=step.attempt_count + 1,
            started_at=step.started_at or completed_at,
            completed_at=completed_at,
            result={"guard": "confirmed"},
            updated_at=completed_at,
        )
        steps = list(snapshot.steps)
        steps[expected_step_index - 1] = completed_step
        updated = WorkflowStateSnapshot(
            workflow=replace(workflow, current_step=expected_step_index, updated_at=completed_at),
            steps=tuple(steps),
        )
        self.snapshots[workflow_id] = updated
        return updated


@dataclass
class MemoryApprovals:
    """Return one authoritative plan/approval binding by the workflow's plan ID."""

    record: tuple[Plan, Approval]

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        return self.record if self.record[0].plan_id == plan_id else None


@dataclass
class MemoryIdentity:
    """Resolve the actor at execution time rather than trusting staged identity state."""

    actor: ActorContext

    def actor_for(self, user_id: UserId) -> ActorContext:
        return self.actor


class MissingPlanApprovalBinding:
    """Model a durable workflow whose approval record has been removed or is unavailable."""

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        return None


def executor_setup(
    *,
    approval: Approval | None = None,
    actor: ActorContext | None = None,
    snapshot: WorkflowStateSnapshot | None = None,
) -> tuple[Any, MemoryWorkflowStore, Plan, Approval]:
    """Stage a workflow and construct its executor with only controlled dependencies."""
    from enterprise_agent.application.workflow_executor import ScenarioAWorkflowExecutor
    from enterprise_agent.application.workflow_state import WorkflowStateService

    plan = approved_plan()
    stored_approval = approval or approval_for(plan)
    store = MemoryWorkflowStore()
    if snapshot is None:
        WorkflowStateService(store).stage(
            plan,
            created_at=NOW,
            workflow_id=WORKFLOW_ID,
        )
    else:
        store.create(snapshot)
    executor = ScenarioAWorkflowExecutor(
        workflow_store=store,
        approvals=MemoryApprovals((plan, stored_approval)),
        identity=MemoryIdentity(actor or actor_with(ALL_SCENARIO_A_WRITE_SCOPES)),
    )
    return executor, store, plan, stored_approval


@pytest.mark.critical
@pytest.mark.parametrize(
    ("approval_status", "source_versions", "scopes", "message"),
    [
        (
            ApprovalStatus.PENDING,
            {"erp:purchase_order:po-4812-y": 2},
            ALL_SCENARIO_A_WRITE_SCOPES,
            "approved",
        ),
        (
            ApprovalStatus.APPROVED,
            {"erp:purchase_order:po-4812-y": 3},
            ALL_SCENARIO_A_WRITE_SCOPES,
            "source evidence",
        ),
        (
            ApprovalStatus.APPROVED,
            {"erp:purchase_order:po-4812-y": 2},
            ALL_SCENARIO_A_WRITE_SCOPES - {Scope("erp:po:create")},
            "write scope",
        ),
    ],
)
def test_executor_fails_closed_before_claim_on_approval_freshness_or_scope_loss(
    approval_status: ApprovalStatus,
    source_versions: dict[str, int],
    scopes: frozenset[Scope],
    message: str,
) -> None:
    """No lease or later effect is possible until every mutable authorization fact revalidates."""
    from enterprise_agent.application.workflow_executor import WorkflowExecutionRejectedError

    plan = approved_plan()
    executor, store, _, _ = executor_setup(
        approval=approval_for(plan, status=approval_status),
        actor=actor_with(scopes),
    )

    with pytest.raises(WorkflowExecutionRejectedError, match=message):
        executor.claim(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=7),
            current_source_versions=source_versions,
        )

    assert store.claim_calls == []
    assert store.complete_guard_calls == []


@pytest.mark.critical
def test_executor_claims_once_then_advances_only_the_declared_read_only_guards() -> None:
    """A lease can advance the exact next guard, but M4.4 cannot skip into an external effect."""
    from enterprise_agent.application.workflow_executor import (
        ScenarioAWorkflowExecutor,
        WorkflowExternalStepPendingError,
    )

    executor, store, plan, _ = executor_setup()
    assert isinstance(executor, ScenarioAWorkflowExecutor)
    claimed = executor.claim(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=2),
        lease_expires_at=NOW + timedelta(minutes=7),
        current_source_versions=plan.source_versions,
    )
    first_guard = executor.advance_next_guard(
        claimed,
        worker_id="worker-a",
        completed_at=NOW + timedelta(minutes=3),
    )
    second_guard = executor.advance_next_guard(
        first_guard,
        worker_id="worker-a",
        completed_at=NOW + timedelta(minutes=4),
    )

    assert second_guard.workflow.status is WorkflowStatus.RUNNING
    assert second_guard.workflow.current_step == 2
    assert [step.status for step in second_guard.steps] == [
        WorkflowStepStatus.SUCCEEDED,
        WorkflowStepStatus.SUCCEEDED,
        WorkflowStepStatus.PENDING,
        WorkflowStepStatus.PENDING,
        WorkflowStepStatus.PENDING,
        WorkflowStepStatus.PENDING,
    ]
    assert store.complete_guard_calls == [
        (WORKFLOW_ID, "worker-a", 1),
        (WORKFLOW_ID, "worker-a", 2),
    ]

    with pytest.raises(WorkflowExternalStepPendingError, match="external tool"):
        executor.advance_next_guard(
            second_guard,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=5),
        )

    assert store.complete_guard_calls == [
        (WORKFLOW_ID, "worker-a", 1),
        (WORKFLOW_ID, "worker-a", 2),
    ]


def test_executor_reclaims_only_an_expired_lease_or_renews_its_own_lease() -> None:
    """A second worker cannot steal a live workflow, while its owner can revalidate and renew it."""
    from enterprise_agent.application.workflow_executor import WorkflowClaimLostError

    executor, store, plan, _ = executor_setup()
    claimed = executor.claim(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=2),
        lease_expires_at=NOW + timedelta(minutes=7),
        current_source_versions=plan.source_versions,
    )
    renewed = executor.claim(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=3),
        lease_expires_at=NOW + timedelta(minutes=8),
        current_source_versions=plan.source_versions,
    )

    assert renewed.workflow.lease_owner == "worker-a"
    assert renewed.workflow.lease_expires_at == NOW + timedelta(minutes=8)
    assert claimed.workflow.started_at == renewed.workflow.started_at
    with pytest.raises(WorkflowClaimLostError, match="claim"):
        executor.claim(
            WORKFLOW_ID,
            worker_id="worker-b",
            now=NOW + timedelta(minutes=4),
            lease_expires_at=NOW + timedelta(minutes=9),
            current_source_versions=plan.source_versions,
        )
    assert store.claim_calls == [
        (WORKFLOW_ID, "worker-a"),
        (WORKFLOW_ID, "worker-a"),
        (WORKFLOW_ID, "worker-b"),
    ]


def test_executor_rejects_tampered_workflow_state_before_claiming_it() -> None:
    """The executor trusts neither a stored workflow declaration nor staged step membership."""
    from enterprise_agent.application.workflow_executor import WorkflowExecutionRejectedError
    from enterprise_agent.application.workflow_state import WorkflowStateService

    plan = approved_plan()
    staging_store = MemoryWorkflowStore()
    staged = WorkflowStateService(staging_store).stage(
        plan, created_at=NOW, workflow_id=WORKFLOW_ID
    )
    tampered = replace(staged, workflow=replace(staged.workflow, definition_version=999))
    executor, store, _, _ = executor_setup(snapshot=tampered)

    with pytest.raises(WorkflowExecutionRejectedError, match="declaration"):
        executor.claim(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=7),
            current_source_versions=plan.source_versions,
        )

    assert store.claim_calls == []


@pytest.mark.parametrize(
    ("worker_id", "lease_offset", "message"),
    [
        ("", 5, "worker identity"),
        ("worker-a", 0, "lease expiry"),
    ],
)
def test_executor_rejects_invalid_claim_inputs_before_loading_state(
    worker_id: str, lease_offset: int, message: str
) -> None:
    """Malformed worker/lease requests cannot obtain access to persisted workflow state."""
    from enterprise_agent.application.workflow_executor import WorkflowExecutionRejectedError

    executor, store, plan, _ = executor_setup()

    with pytest.raises(WorkflowExecutionRejectedError, match=message):
        executor.claim(
            WORKFLOW_ID,
            worker_id=worker_id,
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=2 + lease_offset),
            current_source_versions=plan.source_versions,
        )

    assert store.claim_calls == []


def test_executor_rejects_missing_storage_binding_corrupted_hash_and_identity_mismatch() -> None:
    """Every runtime dependency must still agree with the immutable plan before a claim."""
    from enterprise_agent.application.workflow_executor import WorkflowExecutionRejectedError

    executor, store, plan, approval = executor_setup()
    store.snapshots.clear()
    with pytest.raises(WorkflowExecutionRejectedError, match="does not exist"):
        executor.claim(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=7),
            current_source_versions=plan.source_versions,
        )

    executor, _, plan, _ = executor_setup()
    executor._approvals = MissingPlanApprovalBinding()
    with pytest.raises(WorkflowExecutionRejectedError, match="no approval binding"):
        executor.claim(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=7),
            current_source_versions=plan.source_versions,
        )

    executor, _, plan, approval = executor_setup()
    corrupted_plan = replace(plan, plan_hash="sha256:corrupted")
    executor._approvals = MemoryApprovals(
        (corrupted_plan, replace(approval, plan_hash=corrupted_plan.plan_hash))
    )
    with pytest.raises(WorkflowExecutionRejectedError, match="binding"):
        executor.claim(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=7),
            current_source_versions=plan.source_versions,
        )

    executor, _, plan, _ = executor_setup()
    executor._identity = MemoryIdentity(
        replace(actor_with(ALL_SCENARIO_A_WRITE_SCOPES), user_id=UserId("different-actor"))
    )
    with pytest.raises(WorkflowExecutionRejectedError, match="actor identity"):
        executor.claim(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=7),
            current_source_versions=plan.source_versions,
        )


@pytest.mark.parametrize(
    ("workflow_update", "steps_update", "message"),
    [
        ({"status": WorkflowStatus.SUCCEEDED}, None, "not runnable"),
        ({"lease_owner": "orphaned-worker"}, None, "lease state"),
        ({"current_step": 7}, None, "step cursor"),
        (None, {"step_name": "tampered_step"}, "stored steps"),
    ],
)
def test_executor_rejects_invalid_persisted_workflow_shape_before_claim(
    workflow_update: Any,
    steps_update: Any,
    message: str,
) -> None:
    """Leases, cursor, lifecycle, and stored declared-step shape all fail closed when corrupted."""
    from enterprise_agent.application.workflow_executor import WorkflowExecutionRejectedError
    from enterprise_agent.application.workflow_state import WorkflowStateService

    plan = approved_plan()
    staging_store = MemoryWorkflowStore()
    staged = WorkflowStateService(staging_store).stage(
        plan, created_at=NOW, workflow_id=WORKFLOW_ID
    )
    workflow = (
        staged.workflow if workflow_update is None else replace(staged.workflow, **workflow_update)
    )
    steps = staged.steps
    if steps_update is not None:
        steps = (replace(staged.steps[0], **steps_update), *staged.steps[1:])
    executor, store, _, _ = executor_setup(snapshot=replace(staged, workflow=workflow, steps=steps))

    with pytest.raises(WorkflowExecutionRejectedError, match=message):
        executor.claim(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=7),
            current_source_versions=plan.source_versions,
        )

    assert store.claim_calls == []


def test_guard_execution_rejects_undeclared_or_out_of_order_state_and_lost_transitions() -> None:
    """Guard advancement cannot bypass a declaration, lifecycle order, lease, or CAS result."""
    from enterprise_agent.application.workflow_executor import (
        WorkflowClaimLostError,
        WorkflowExecutionRejectedError,
    )

    executor, store, plan, _ = executor_setup()
    claimed = executor.claim(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=2),
        lease_expires_at=NOW + timedelta(minutes=7),
        current_source_versions=plan.source_versions,
    )

    with pytest.raises(WorkflowExecutionRejectedError, match="declared definition"):
        executor.advance_next_guard(
            replace(claimed, workflow=replace(claimed.workflow, definition_name="unreviewed")),
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=3),
        )
    with pytest.raises(WorkflowExecutionRejectedError, match="stored steps"):
        executor.advance_next_guard(
            replace(
                claimed, steps=(replace(claimed.steps[0], step_name="tampered"), *claimed.steps[1:])
            ),
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=3),
        )
    with pytest.raises(WorkflowExecutionRejectedError, match="no remaining"):
        executor.advance_next_guard(
            replace(claimed, workflow=replace(claimed.workflow, current_step=6)),
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=3),
        )
    with pytest.raises(WorkflowExecutionRejectedError, match="not pending"):
        executor.advance_next_guard(
            replace(
                claimed,
                steps=(
                    replace(claimed.steps[0], status=WorkflowStepStatus.SUCCEEDED),
                    *claimed.steps[1:],
                ),
            ),
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=3),
        )
    with pytest.raises(WorkflowExecutionRejectedError, match="incomplete prior"):
        executor.advance_next_guard(
            replace(claimed, workflow=replace(claimed.workflow, current_step=1)),
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=3),
        )
    with pytest.raises(WorkflowExecutionRejectedError, match="invalid future"):
        executor.advance_next_guard(
            replace(
                claimed,
                workflow=replace(claimed.workflow, current_step=1),
                steps=(
                    replace(claimed.steps[0], status=WorkflowStepStatus.SUCCEEDED),
                    claimed.steps[1],
                    replace(claimed.steps[2], status=WorkflowStepStatus.RUNNING),
                    *claimed.steps[3:],
                ),
            ),
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=3),
        )
    with pytest.raises(WorkflowClaimLostError, match="active lease"):
        executor.advance_next_guard(
            claimed,
            worker_id="worker-b",
            completed_at=NOW + timedelta(minutes=3),
        )

    store.lose_next_guard_transition = True
    with pytest.raises(WorkflowClaimLostError, match="not acquired"):
        executor.advance_next_guard(
            claimed,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=3),
        )


def test_guard_execution_rejects_a_missing_declared_step() -> None:
    """A persisted workflow must contain every reviewed step before a guard can advance."""
    from enterprise_agent.application.workflow_executor import WorkflowExecutionRejectedError

    executor, _, plan, _ = executor_setup()
    claimed = executor.claim(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=2),
        lease_expires_at=NOW + timedelta(minutes=7),
        current_source_versions=plan.source_versions,
    )

    with pytest.raises(WorkflowExecutionRejectedError, match="stored steps"):
        executor.advance_next_guard(
            replace(claimed, steps=claimed.steps[:-1]),
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=3),
        )


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for the PostgreSQL executor contract."""
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
def test_postgres_executor_claims_and_completes_only_the_first_declared_guard(
    disposable_database: str,
) -> None:
    """The real control plane retains its lease/guard transition without creating a PO yet."""
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
        "from datetime import UTC, datetime, timedelta\n"
        "from decimal import Decimal\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter, PostgresCalendarAdapter, PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter, PostgresMailAdapter, PostgresPlanApprovalAdapter,\n"
        "    PostgresWorkflowStateAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approvals import ScenarioAApprovalService\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.planning import EnterWorkflowRecommendation\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.application.workflow_executor import ScenarioAWorkflowExecutor\n"
        "from enterprise_agent.application.workflow_state import WorkflowStateService\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId, WorkflowStatus, WorkflowStepStatus\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "now = datetime(2026, 8, 24, 9, tzinfo=UTC)\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "actor = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "detection = StockoutDetector(erp, PostgresAttentionAdapter(database_url)).detect(actor, RunId('run-workflow-executor'), now)[0]\n"
        "context = ScenarioAContextAssembler(identity, erp, PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id='00000000-0000-0000-0000-000000000202', quantity=Decimal(60), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='Approved alternate meets production.')\n"
        "approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "pending = ScenarioAApprovalService(approvals).request_pending(context, recommendation, current_source_versions=context.source_versions, policy_version='scenario_a_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4))\n"
        "approved = ScenarioAApprovalService(approvals).approve(approval_id=pending.approval.approval_id, expected_plan_hash=pending.plan.plan_hash, current_source_versions=context.source_versions, decided_at=now + timedelta(minutes=1))\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "workflows = PostgresWorkflowStateAdapter(database_url)\n"
        "snapshot = WorkflowStateService(workflows).stage(pending.plan, created_at=now + timedelta(minutes=1))\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    purchase_orders_before = connection.execute(text('SELECT id::text, supplier_id::text, ordered_quantity::text, received_quantity::text, status, source_version FROM purchase_orders ORDER BY id')).all()\n"
        "executor = ScenarioAWorkflowExecutor(workflow_store=workflows, approvals=approvals, identity=identity)\n"
        "claimed = executor.claim(snapshot.workflow.workflow_id, worker_id='workflow-worker-a', now=now + timedelta(minutes=2), lease_expires_at=now + timedelta(minutes=10), current_source_versions=context.source_versions)\n"
        "advanced = executor.advance_next_guard(claimed, worker_id='workflow-worker-a', completed_at=now + timedelta(minutes=3))\n"
        "assert advanced.workflow.status is WorkflowStatus.RUNNING\n"
        "assert advanced.workflow.current_step == 1\n"
        "assert advanced.steps[0].status is WorkflowStepStatus.SUCCEEDED\n"
        "assert all(step.status is WorkflowStepStatus.PENDING for step in advanced.steps[1:])\n"
        "with engine.connect() as connection:\n"
        "    assert connection.execute(text('SELECT current_step FROM workflow_instances WHERE id = CAST(:workflow_id AS UUID)'), {'workflow_id': str(snapshot.workflow.workflow_id)}).scalar_one() == 1\n"
        "    assert connection.execute(text('SELECT status FROM workflow_steps WHERE workflow_instance_id = CAST(:workflow_id AS UUID) AND step_index = 1'), {'workflow_id': str(snapshot.workflow.workflow_id)}).scalar_one() == 'succeeded'\n"
        "    purchase_orders_after = connection.execute(text('SELECT id::text, supplier_id::text, ordered_quantity::text, received_quantity::text, status, source_version FROM purchase_orders ORDER BY id')).all()\n"
        "assert purchase_orders_after == purchase_orders_before\n"
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
