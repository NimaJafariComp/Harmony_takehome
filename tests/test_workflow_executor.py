"""Safety contracts for claiming and advancing the declared Scenario A workflow."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionId,
    AuditEvent,
    Plan,
    PlanId,
    RunId,
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
    start_tool_calls: list[tuple[WorkflowId, str, int, str]]
    complete_tool_calls: list[tuple[WorkflowId, str, int, str]]
    failed_tool_calls: list[tuple[WorkflowId, str, int]]
    start_compensation_calls: list[tuple[WorkflowId, str, int]]
    complete_compensation_calls: list[tuple[WorkflowId, str, int]]
    lose_next_guard_transition: bool

    def __init__(self) -> None:
        self.snapshots = {}
        self.claim_calls = []
        self.complete_guard_calls = []
        self.start_tool_calls = []
        self.complete_tool_calls = []
        self.failed_tool_calls = []
        self.start_compensation_calls = []
        self.complete_compensation_calls = []
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

    def start_tool_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        idempotency_key: str,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Model the durable ``tool.started`` commit required before an external call."""
        self.start_tool_calls.append((workflow_id, worker_id, expected_step_index, idempotency_key))
        snapshot = self.snapshots.get(workflow_id)
        if snapshot is None:
            return None
        workflow = snapshot.workflow
        if (
            workflow.status is not WorkflowStatus.RUNNING
            or workflow.lease_owner != worker_id
            or workflow.lease_expires_at is None
            or workflow.lease_expires_at <= started_at
            or workflow.current_step != expected_step_index - 1
        ):
            return None
        step = snapshot.steps[expected_step_index - 1]
        if step.tool_name is None or step.status is not WorkflowStepStatus.PENDING:
            return None
        steps = list(snapshot.steps)
        steps[expected_step_index - 1] = replace(
            step,
            status=WorkflowStepStatus.RUNNING,
            idempotency_key=idempotency_key,
            attempt_count=step.attempt_count + 1,
            started_at=started_at,
            updated_at=started_at,
        )
        updated = WorkflowStateSnapshot(workflow=workflow, steps=tuple(steps))
        self.snapshots[workflow_id] = updated
        return updated

    def complete_tool_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        idempotency_key: str,
        result: Mapping[str, object],
        finish_workflow: bool,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Model the separate result-plus-cursor transaction after a tool returns."""
        self.complete_tool_calls.append(
            (workflow_id, worker_id, expected_step_index, idempotency_key)
        )
        snapshot = self.snapshots.get(workflow_id)
        if snapshot is None:
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
        if (
            step.tool_name is None
            or step.status is not WorkflowStepStatus.RUNNING
            or step.idempotency_key != idempotency_key
        ):
            return None
        steps = list(snapshot.steps)
        steps[expected_step_index - 1] = replace(
            step,
            status=WorkflowStepStatus.SUCCEEDED,
            result=result,
            completed_at=completed_at,
            updated_at=completed_at,
        )
        updated = WorkflowStateSnapshot(
            workflow=replace(
                workflow,
                current_step=expected_step_index,
                status=WorkflowStatus.SUCCEEDED if finish_workflow else workflow.status,
                completed_at=completed_at if finish_workflow else workflow.completed_at,
                lease_owner=None if finish_workflow else workflow.lease_owner,
                lease_expires_at=None if finish_workflow else workflow.lease_expires_at,
                updated_at=completed_at,
            ),
            steps=tuple(steps),
        )
        self.snapshots[workflow_id] = updated
        return updated

    def fail_tool_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        idempotency_key: str,
        error: str,
        failed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Model a terminal effect failure that retains the owner lease for compensation."""
        self.failed_tool_calls.append((workflow_id, worker_id, expected_step_index))
        snapshot = self.snapshots.get(workflow_id)
        if snapshot is None:
            return None
        workflow = snapshot.workflow
        if (
            workflow.status is not WorkflowStatus.RUNNING
            or workflow.lease_owner != worker_id
            or workflow.lease_expires_at is None
            or workflow.lease_expires_at <= failed_at
            or workflow.current_step != expected_step_index - 1
        ):
            return None
        step = snapshot.steps[expected_step_index - 1]
        if step.status is not WorkflowStepStatus.RUNNING or step.idempotency_key != idempotency_key:
            return None
        steps = list(snapshot.steps)
        steps[expected_step_index - 1] = replace(
            step,
            status=WorkflowStepStatus.FAILED,
            error=error,
            completed_at=failed_at,
            updated_at=failed_at,
        )
        updated = WorkflowStateSnapshot(
            workflow=replace(
                workflow,
                status=WorkflowStatus.FAILED,
                last_error=error,
                updated_at=failed_at,
            ),
            steps=tuple(steps),
        )
        self.snapshots[workflow_id] = updated
        return updated

    def begin_compensation(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Model the durable failed-to-compensating transition under the retained lease."""
        snapshot = self.snapshots.get(workflow_id)
        if snapshot is None:
            return None
        workflow = snapshot.workflow
        if (
            workflow.status is not WorkflowStatus.FAILED
            or workflow.lease_owner != worker_id
            or workflow.lease_expires_at is None
            or workflow.lease_expires_at <= started_at
        ):
            return None
        updated = replace(
            snapshot,
            workflow=replace(
                workflow,
                status=WorkflowStatus.COMPENSATING,
                updated_at=started_at,
            ),
        )
        self.snapshots[workflow_id] = updated
        return updated

    def start_compensation_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Model starting one already-succeeded effect's reverse action."""
        self.start_compensation_calls.append((workflow_id, worker_id, expected_step_index))
        snapshot = self.snapshots.get(workflow_id)
        if snapshot is None:
            return None
        workflow = snapshot.workflow
        if (
            workflow.status is not WorkflowStatus.COMPENSATING
            or workflow.lease_owner != worker_id
            or workflow.lease_expires_at is None
            or workflow.lease_expires_at <= started_at
        ):
            return None
        step = snapshot.steps[expected_step_index - 1]
        if step.tool_name is None or step.status is not WorkflowStepStatus.SUCCEEDED:
            return None
        steps = list(snapshot.steps)
        steps[expected_step_index - 1] = replace(
            step,
            status=WorkflowStepStatus.COMPENSATING,
            updated_at=started_at,
        )
        updated = WorkflowStateSnapshot(workflow=workflow, steps=tuple(steps))
        self.snapshots[workflow_id] = updated
        return updated

    def complete_compensation_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        result: Mapping[str, object],
        finish_workflow: bool,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Model the durable result for one compensation and terminal workflow closure."""
        self.complete_compensation_calls.append((workflow_id, worker_id, expected_step_index))
        snapshot = self.snapshots.get(workflow_id)
        if snapshot is None:
            return None
        workflow = snapshot.workflow
        if (
            workflow.status is not WorkflowStatus.COMPENSATING
            or workflow.lease_owner != worker_id
            or workflow.lease_expires_at is None
            or workflow.lease_expires_at <= completed_at
        ):
            return None
        step = snapshot.steps[expected_step_index - 1]
        if step.status is not WorkflowStepStatus.COMPENSATING:
            return None
        steps = list(snapshot.steps)
        steps[expected_step_index - 1] = replace(
            step,
            status=WorkflowStepStatus.COMPENSATED,
            result={"effect": dict(step.result or {}), "compensation": dict(result)},
            completed_at=completed_at,
            updated_at=completed_at,
        )
        updated = WorkflowStateSnapshot(
            workflow=replace(
                workflow,
                status=WorkflowStatus.COMPENSATED if finish_workflow else workflow.status,
                completed_at=completed_at if finish_workflow else workflow.completed_at,
                lease_owner=None if finish_workflow else workflow.lease_owner,
                lease_expires_at=None if finish_workflow else workflow.lease_expires_at,
                updated_at=completed_at,
            ),
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


@dataclass
class RecordingAudit:
    """Collect material workflow events without depending on PostgreSQL in unit contracts."""

    events: list[AuditEvent]

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        return tuple(event for event in self.events if event.run_id == run_id)


class MissingPlanApprovalBinding:
    """Model a durable workflow whose approval record has been removed or is unavailable."""

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        return None


def executor_setup(
    *,
    approval: Approval | None = None,
    actor: ActorContext | None = None,
    snapshot: WorkflowStateSnapshot | None = None,
    tool_executor: Any | None = None,
    crash_injector: Any | None = None,
    audit: RecordingAudit | None = None,
    audit_run_id: RunId | None = None,
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
            audit_run_id=audit_run_id,
        )
    else:
        store.create(snapshot)
    executor_arguments: dict[str, Any] = {
        "workflow_store": store,
        "approvals": MemoryApprovals((plan, stored_approval)),
        "identity": MemoryIdentity(actor or actor_with(ALL_SCENARIO_A_WRITE_SCOPES)),
    }
    if tool_executor is not None:
        executor_arguments["tool_executor"] = tool_executor
    if crash_injector is not None:
        executor_arguments["crash_injector"] = crash_injector
    if audit is not None:
        executor_arguments["audit"] = audit
    executor = ScenarioAWorkflowExecutor(**executor_arguments)
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
def test_executor_accepts_a_backup_approval_bound_to_the_immutable_original_plan() -> None:
    """A valid M5 reroute changes the active decider, not the durable plan's original approver."""
    plan = approved_plan()
    backup_approval = replace(
        approval_for(plan),
        approver_id=UserId("00000000-0000-0000-0000-000000000002"),
    )
    executor, store, _, _ = executor_setup(approval=backup_approval)

    claimed = executor.claim(
        WORKFLOW_ID,
        worker_id="workflow-worker-a",
        now=NOW + timedelta(minutes=2),
        lease_expires_at=NOW + timedelta(minutes=10),
        current_source_versions=plan.source_versions,
    )

    assert claimed.workflow.status is WorkflowStatus.RUNNING
    assert store.claim_calls == [(WORKFLOW_ID, "workflow-worker-a")]


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


@pytest.mark.critical
@pytest.mark.parametrize("mutation", ["model_added_step", "reordered_steps"])
def test_executor_rejects_model_mutated_workflow_steps_before_claim(mutation: str) -> None:
    """Persisted steps may neither gain a model-authored action nor change reviewed order."""
    from enterprise_agent.application.workflow_executor import WorkflowExecutionRejectedError

    executor, store, plan, _ = executor_setup()
    snapshot = store.load(WORKFLOW_ID)
    assert snapshot is not None
    steps = (
        (*snapshot.steps, snapshot.steps[-1])
        if mutation == "model_added_step"
        else (snapshot.steps[1], snapshot.steps[0], *snapshot.steps[2:])
    )
    store.snapshots[WORKFLOW_ID] = replace(snapshot, steps=steps)

    with pytest.raises(WorkflowExecutionRejectedError, match="stored steps"):
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


@dataclass
class RecordingToolExecutor:
    """Record a deliberately small external tool double without mutating workflow state."""

    events: list[str]
    result: dict[str, object]
    error: Exception | None = None

    def execute(self, actor: ActorContext, invocation: Any) -> dict[str, object]:
        """Represent the independently committed ERP/mail/scheduler side effect."""
        assert actor.user_id == UserId("00000000-0000-0000-0000-000000000001")
        assert invocation.tool_name == "create_replacement_po"
        assert invocation.status.value == "started"
        self.events.append("tool.execute")
        if self.error is not None:
            raise self.error
        return self.result


class ScenarioAToolExecutor:
    """Return only the bounded result each declared Scenario A effect may expose to the workflow."""

    def execute(self, actor: ActorContext, invocation: Any) -> dict[str, object]:
        """Model successful independently committed tools while preserving their declared order."""
        assert actor.user_id == UserId("00000000-0000-0000-0000-000000000001")
        if invocation.tool_name == "create_replacement_po":
            return {"replacement_purchase_order_id": "replacement-po-1"}
        if invocation.tool_name == "reduce_or_cancel_po":
            return {"status": "cancelled"}
        if invocation.tool_name == "notify_production":
            return {"message_id": "message-1"}
        if invocation.tool_name == "schedule_arrival_check":
            return {"scheduled_task_id": "task-1"}
        raise AssertionError("unexpected undeclared tool")


@dataclass
class RecordingBoundedToolExecutor:
    """Record Scenario B's already-approved catalog calls without inventing any new tool behavior."""

    invocations: list[Any]

    def execute(self, actor: ActorContext, invocation: Any) -> dict[str, object]:
        """Return one bounded result for each reviewed quality-remediation effect."""
        assert actor.user_id == UserId("00000000-0000-0000-0000-000000000001")
        self.invocations.append(invocation)
        if invocation.tool_name == "reallocate_lot":
            return {"allocation_id": "allocation-good"}
        if invocation.tool_name == "notify_production":
            return {"message_id": "message-quality"}
        raise AssertionError("unexpected bounded tool")


@dataclass
class CompensatingScenarioAToolExecutor:
    """Record declared compensations while returning the same bounded Scenario A effects."""

    compensation_actions: list[Any]
    terminal_tool_name: str | None = None

    def execute(self, actor: ActorContext, invocation: Any) -> dict[str, object]:
        """Return ordinary effects except for the explicitly terminal declared tool."""
        if invocation.tool_name == self.terminal_tool_name:
            from enterprise_agent.application.tools import TerminalToolExecutionError

            raise TerminalToolExecutionError("simulated terminal tool failure")
        return ScenarioAToolExecutor().execute(actor, invocation)

    def compensate(self, actor: ActorContext, compensation: Any) -> dict[str, object]:
        """Record only the declared reverse action that the executor selected."""
        assert actor.user_id == UserId("00000000-0000-0000-0000-000000000001")
        self.compensation_actions.append(compensation.action)
        return {"action": compensation.action}


def _advance_to_first_tool(executor: Any, plan: Plan) -> None:
    """Complete the two declared read-only guards before the first external workflow step."""
    claimed = executor.claim(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=2),
        lease_expires_at=NOW + timedelta(minutes=12),
        current_source_versions=plan.source_versions,
    )
    first_guard = executor.advance_next_guard(
        claimed,
        worker_id="worker-a",
        completed_at=NOW + timedelta(minutes=3),
    )
    executor.advance_next_guard(
        first_guard,
        worker_id="worker-a",
        completed_at=NOW + timedelta(minutes=4),
    )


@pytest.mark.critical
def test_executor_requires_approval_then_runs_only_the_plan_bound_quality_tools_in_order() -> None:
    """Scenario B uses the existing durable executor without a static Scenario A workflow branch."""
    from enterprise_agent.application.bounded_tool_plan import (
        BoundedToolCall,
        build_bounded_tool_plan,
    )
    from enterprise_agent.application.tools import (
        NotifyProductionInput,
        ReallocateLotInput,
        ToolName,
    )
    from enterprise_agent.application.workflow_executor import (
        ScenarioAWorkflowExecutor,
        WorkflowExecutionRejectedError,
    )
    from enterprise_agent.application.workflow_state import WorkflowStateService

    plan = build_bounded_tool_plan(
        attention_id=AttentionId("00000000-0000-0000-0000-000000000611"),
        actor_id=UserId("00000000-0000-0000-0000-000000000001"),
        approver_id=UserId("00000000-0000-0000-0000-000000000004"),
        tool_calls=(
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
                    message="Released replacement lot will cover the held allocation.",
                ),
            ),
        ),
        source_versions={"quality:quality_lot:lot-held": 3},
        policy_version="scenario_b_policy:v1",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )
    store = MemoryWorkflowStore()
    WorkflowStateService(store).stage_bounded_tool_plan(
        plan,
        created_at=NOW,
        workflow_id=WORKFLOW_ID,
    )
    tools = RecordingBoundedToolExecutor(invocations=[])
    actor = actor_with(frozenset({Scope("erp:lot:write"), Scope("production:notify")}))
    pending_executor = ScenarioAWorkflowExecutor(
        workflow_store=store,
        approvals=MemoryApprovals((plan, approval_for(plan, status=ApprovalStatus.PENDING))),
        identity=MemoryIdentity(actor),
        tool_executor=tools,
    )

    with pytest.raises(WorkflowExecutionRejectedError, match="approved"):
        pending_executor.begin_next_tool(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=1),
            lease_expires_at=NOW + timedelta(minutes=10),
            current_source_versions=plan.source_versions,
        )

    executor = ScenarioAWorkflowExecutor(
        workflow_store=store,
        approvals=MemoryApprovals((plan, approval_for(plan))),
        identity=MemoryIdentity(actor),
        tool_executor=tools,
    )
    first = executor.begin_next_tool(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=1),
        lease_expires_at=NOW + timedelta(minutes=10),
        current_source_versions=plan.source_versions,
    )
    after_first = executor.execute_started_tool(
        first,
        worker_id="worker-a",
        completed_at=NOW + timedelta(minutes=2),
    )
    second = executor.begin_next_tool(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=3),
        lease_expires_at=NOW + timedelta(minutes=10),
        current_source_versions={"quality:quality_lot:lot-held": 4},
    )
    completed = executor.execute_started_tool(
        second,
        worker_id="worker-a",
        completed_at=NOW + timedelta(minutes=4),
    )

    assert after_first.workflow.current_step == 1
    assert completed.workflow.status is WorkflowStatus.SUCCEEDED
    assert [invocation.tool_name for invocation in tools.invocations] == [
        "reallocate_lot",
        "notify_production",
    ]
    assert first.invocation.parameters == {
        "quality_lot_id": "lot-good",
        "from_production_order_id": None,
        "to_production_order_id": "production-q7001",
        "quantity": "80",
    }
    assert second.invocation.parameters == {
        "production_order_id": "production-q7001",
        "message": "Released replacement lot will cover the held allocation.",
    }
    assert [call[2] for call in store.start_tool_calls] == [1, 2]


@pytest.mark.critical
def test_executor_blocks_skipped_guards_and_stale_source_before_provider_invocation() -> None:
    """No provider action begins until both guards and exact current source evidence hold."""
    from enterprise_agent.application.workflow_executor import WorkflowExecutionRejectedError

    tools = RecordingToolExecutor(
        events=[],
        result={"replacement_purchase_order_id": "replacement-po-1"},
    )
    executor, store, plan, _ = executor_setup(tool_executor=tools)

    with pytest.raises(WorkflowExecutionRejectedError, match="read-only guard"):
        executor.begin_next_tool(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=2),
            lease_expires_at=NOW + timedelta(minutes=12),
            current_source_versions=plan.source_versions,
        )

    assert store.start_tool_calls == []
    assert tools.events == []
    _advance_to_first_tool(executor, plan)

    with pytest.raises(WorkflowExecutionRejectedError, match="source evidence"):
        executor.begin_next_tool(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=5),
            lease_expires_at=NOW + timedelta(minutes=12),
            current_source_versions={"erp:purchase_order:po-4812-y": 3},
        )

    assert store.start_tool_calls == []
    assert tools.events == []


@pytest.mark.critical
def test_executor_durably_starts_an_external_tool_before_invocation_then_commits_result() -> None:
    """Tool effects occur only between separate durable started and succeeded transitions."""
    events: list[str] = []
    tools = RecordingToolExecutor(
        events=events,
        result={"replacement_purchase_order_id": "replacement-po-1"},
    )
    executor, store, plan, _ = executor_setup(tool_executor=tools)
    _advance_to_first_tool(executor, plan)

    started = executor.begin_next_tool(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=5),
        lease_expires_at=NOW + timedelta(minutes=12),
        current_source_versions=plan.source_versions,
    )

    assert len(store.start_tool_calls) == 1
    assert store.complete_tool_calls == []
    assert events == []
    retained = store.load(WORKFLOW_ID)
    assert retained is not None
    started_step = retained.steps[2]
    assert started_step.status is WorkflowStepStatus.RUNNING
    assert started_step.idempotency_key == started.invocation.idempotency_key
    assert started.invocation.parameters == {
        "original_purchase_order_id": "po-4812-y",
        "supplier_id": "supplier-z",
        "production_order_id": "production-4812",
        "quantity": "60",
    }

    completed = executor.execute_started_tool(
        started,
        worker_id="worker-a",
        completed_at=NOW + timedelta(minutes=6),
    )

    assert events == ["tool.execute"]
    assert len(store.complete_tool_calls) == 1
    assert completed.workflow.current_step == 3
    assert completed.steps[2].status is WorkflowStepStatus.SUCCEEDED
    assert completed.steps[2].result == {"replacement_purchase_order_id": "replacement-po-1"}


def test_executor_leaves_a_durable_started_tool_for_safe_retry_when_the_effect_fails() -> None:
    """An effect failure cannot accidentally advance its workflow cursor or hide the started key."""
    tools = RecordingToolExecutor(
        events=[],
        result={},
        error=RuntimeError("simulated external outage"),
    )
    executor, store, plan, _ = executor_setup(tool_executor=tools)
    _advance_to_first_tool(executor, plan)
    started = executor.begin_next_tool(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=5),
        lease_expires_at=NOW + timedelta(minutes=12),
        current_source_versions=plan.source_versions,
    )

    with pytest.raises(RuntimeError, match="external outage"):
        executor.execute_started_tool(
            started,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=6),
        )

    retained = store.load(WORKFLOW_ID)
    assert retained is not None
    assert retained.workflow.current_step == 2
    assert retained.steps[2].status is WorkflowStepStatus.RUNNING
    assert retained.steps[2].idempotency_key == started.invocation.idempotency_key
    assert store.complete_tool_calls == []


@pytest.mark.critical
def test_crash_after_replacement_effect_restarts_with_the_same_started_key() -> None:
    """A process crash before local completion replays one started provider action without a second start."""
    from enterprise_agent.application.tools import ToolName
    from enterprise_agent.application.workflow_executor import (
        DeterministicCrashInjector,
        ScenarioAWorkflowExecutor,
        WorkflowCrashInjectedError,
    )

    tools = ScenarioAToolExecutor()
    executor, store, plan, approval = executor_setup(
        tool_executor=tools,
        crash_injector=DeterministicCrashInjector(target_tool_name=ToolName.CREATE_REPLACEMENT_PO),
    )
    _advance_to_first_tool(executor, plan)
    started = executor.begin_next_tool(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=5),
        lease_expires_at=NOW + timedelta(minutes=7),
        current_source_versions=plan.source_versions,
    )

    with pytest.raises(WorkflowCrashInjectedError, match="after external effect"):
        executor.execute_started_tool(
            started,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=6),
        )

    crashed = store.load(WORKFLOW_ID)
    assert crashed is not None
    assert crashed.workflow.status is WorkflowStatus.RUNNING
    assert crashed.workflow.current_step == 2
    assert crashed.steps[2].status is WorkflowStepStatus.RUNNING
    assert crashed.steps[2].idempotency_key == started.invocation.idempotency_key
    assert len(store.start_tool_calls) == 1
    assert store.complete_tool_calls == []

    restarted = ScenarioAWorkflowExecutor(
        workflow_store=store,
        approvals=MemoryApprovals((plan, approval)),
        identity=MemoryIdentity(actor_with(ALL_SCENARIO_A_WRITE_SCOPES)),
        tool_executor=tools,
    )
    resumed = restarted.begin_next_tool(
        WORKFLOW_ID,
        worker_id="restart-worker",
        now=NOW + timedelta(minutes=8),
        lease_expires_at=NOW + timedelta(minutes=20),
        current_source_versions=plan.source_versions,
    )
    completed = restarted.execute_started_tool(
        resumed,
        worker_id="restart-worker",
        completed_at=NOW + timedelta(minutes=9),
    )

    assert resumed.invocation.idempotency_key == started.invocation.idempotency_key
    assert len(store.start_tool_calls) == 1
    assert len(store.complete_tool_calls) == 1
    assert completed.workflow.current_step == 3
    assert completed.steps[2].status is WorkflowStepStatus.SUCCEEDED


@pytest.mark.critical
def test_terminal_tool_failure_compensates_only_completed_effects_in_reverse_order() -> None:
    """Terminal failure reverses prior effects, never an effect that did not successfully commit."""
    from enterprise_agent.application.tools import CompensationAction, TerminalToolExecutionError

    tools = CompensatingScenarioAToolExecutor(
        compensation_actions=[], terminal_tool_name="schedule_arrival_check"
    )
    audit = RecordingAudit(events=[])
    run_id = RunId("run-workflow-compensation-audit")
    executor, store, plan, _ = executor_setup(
        tool_executor=tools,
        audit=audit,
        audit_run_id=run_id,
    )
    _advance_to_first_tool(executor, plan)

    for minute in (5, 7, 9):
        started = executor.begin_next_tool(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=minute),
            lease_expires_at=NOW + timedelta(minutes=20),
            current_source_versions=plan.source_versions,
        )
        executor.execute_started_tool(
            started,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=minute + 1),
        )
    terminal = executor.begin_next_tool(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=11),
        lease_expires_at=NOW + timedelta(minutes=20),
        current_source_versions=plan.source_versions,
    )

    with pytest.raises(TerminalToolExecutionError, match="terminal tool failure"):
        executor.execute_started_tool(
            terminal,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=12),
        )

    compensated = store.load(WORKFLOW_ID)
    assert compensated is not None
    assert compensated.workflow.status is WorkflowStatus.COMPENSATED
    assert compensated.workflow.lease_owner is None
    assert [step.status for step in compensated.steps] == [
        WorkflowStepStatus.SUCCEEDED,
        WorkflowStepStatus.SUCCEEDED,
        WorkflowStepStatus.COMPENSATED,
        WorkflowStepStatus.COMPENSATED,
        WorkflowStepStatus.COMPENSATED,
        WorkflowStepStatus.FAILED,
    ]
    assert tools.compensation_actions == [
        CompensationAction.SEND_CORRECTION_NOTIFICATION,
        CompensationAction.RESTORE_ORIGINAL_PURCHASE_ORDER,
        CompensationAction.CANCEL_CREATED_REPLACEMENT_PO,
    ]
    assert [call[2] for call in store.start_compensation_calls] == [5, 4, 3]
    assert [call[2] for call in store.complete_compensation_calls] == [5, 4, 3]
    assert [event.event_type for event in audit.events] == [
        "workflow.started",
        "workflow.step_started",
        "workflow.step_completed",
        "workflow.step_started",
        "workflow.step_completed",
        "workflow.step_started",
        "tool.started",
        "tool.succeeded",
        "workflow.step_completed",
        "workflow.step_started",
        "tool.started",
        "tool.succeeded",
        "workflow.step_completed",
        "workflow.step_started",
        "tool.started",
        "tool.succeeded",
        "workflow.step_completed",
        "workflow.step_started",
        "tool.started",
        "tool.failed",
        "workflow.failed",
        "compensation.started",
        "compensation.completed",
        "compensation.started",
        "compensation.completed",
        "compensation.started",
        "compensation.completed",
    ]
    assert {event.run_id for event in audit.events} == {run_id}


@pytest.mark.critical
def test_compensation_cancels_a_succeeded_arrival_task_before_earlier_effects() -> None:
    """A failed workflow with all four completed effects unwinds every declared action in LIFO order."""
    from enterprise_agent.application.tools import CompensationAction

    tools = CompensatingScenarioAToolExecutor(compensation_actions=[])
    executor, store, plan, _ = executor_setup(tool_executor=tools)
    _advance_to_first_tool(executor, plan)
    for minute in (5, 7, 9, 11):
        started = executor.begin_next_tool(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=minute),
            lease_expires_at=NOW + timedelta(minutes=20),
            current_source_versions=plan.source_versions,
        )
        executor.execute_started_tool(
            started,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=minute + 1),
        )
    successful = store.load(WORKFLOW_ID)
    assert successful is not None
    store.snapshots[WORKFLOW_ID] = replace(
        successful,
        workflow=replace(
            successful.workflow,
            status=WorkflowStatus.FAILED,
            completed_at=None,
            lease_owner="worker-a",
            lease_expires_at=NOW + timedelta(minutes=20),
        ),
    )

    compensated = executor.compensate_failed_workflow(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=14),
    )

    assert compensated.workflow.status is WorkflowStatus.COMPENSATED
    assert tools.compensation_actions == [
        CompensationAction.CANCEL_ARRIVAL_CHECK,
        CompensationAction.SEND_CORRECTION_NOTIFICATION,
        CompensationAction.RESTORE_ORIGINAL_PURCHASE_ORDER,
        CompensationAction.CANCEL_CREATED_REPLACEMENT_PO,
    ]
    assert [call[2] for call in store.start_compensation_calls] == [6, 5, 4, 3]


def test_executor_runs_all_declared_tool_inputs_in_order_and_finishes_the_workflow() -> None:
    """Later effects use only prior durable results and continue the existing active workflow lease."""
    audit = RecordingAudit(events=[])
    run_id = RunId("run-workflow-success-audit")
    executor, store, plan, _ = executor_setup(
        tool_executor=ScenarioAToolExecutor(),
        audit=audit,
        audit_run_id=run_id,
    )
    _advance_to_first_tool(executor, plan)

    current = None
    for minute in (5, 7, 9, 11):
        started = executor.begin_next_tool(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=minute),
            lease_expires_at=NOW + timedelta(minutes=15),
            current_source_versions=plan.source_versions,
        )
        current = executor.execute_started_tool(
            started,
            worker_id="worker-a",
            completed_at=NOW + timedelta(minutes=minute + 1),
        )

    assert current is not None
    assert current.workflow.status is WorkflowStatus.SUCCEEDED
    assert current.workflow.current_step == 6
    assert current.workflow.lease_owner is None
    assert [step.status for step in current.steps] == [WorkflowStepStatus.SUCCEEDED] * 6
    assert [call[2] for call in store.start_tool_calls] == [3, 4, 5, 6]
    assert [call[2] for call in store.complete_tool_calls] == [3, 4, 5, 6]
    assert current.steps[5].input["plan_parameters"]["original_purchase_order_id"] == "po-4812-y"
    assert current.steps[5].input["audit_run_id"] == str(run_id)
    assert [event.event_type for event in audit.events].count("schedule.created") == 1
    assert audit.events[-1].payload == {"task_type": "arrival_check", "due_at": "unknown"}


def test_executor_rejects_unavailable_or_lost_external_tool_execution_before_an_effect() -> None:
    """A tool cannot run without its provider boundary, an active lease, or its durable started state."""
    from enterprise_agent.application.workflow_executor import (
        WorkflowClaimLostError,
        WorkflowToolExecutionUnavailableError,
    )

    executor, store, plan, _ = executor_setup()
    _advance_to_first_tool(executor, plan)
    with pytest.raises(WorkflowToolExecutionUnavailableError, match="not configured"):
        executor.begin_next_tool(
            WORKFLOW_ID,
            worker_id="worker-a",
            now=NOW + timedelta(minutes=5),
            lease_expires_at=NOW + timedelta(minutes=12),
            current_source_versions=plan.source_versions,
        )

    executor, store, plan, _ = executor_setup(tool_executor=ScenarioAToolExecutor())
    _advance_to_first_tool(executor, plan)
    started = executor.begin_next_tool(
        WORKFLOW_ID,
        worker_id="worker-a",
        now=NOW + timedelta(minutes=5),
        lease_expires_at=NOW + timedelta(minutes=12),
        current_source_versions=plan.source_versions,
    )
    with pytest.raises(WorkflowClaimLostError, match="active lease"):
        executor.execute_started_tool(
            started,
            worker_id="worker-b",
            completed_at=NOW + timedelta(minutes=6),
        )

    retained = store.load(WORKFLOW_ID)
    assert retained is not None
    assert retained.steps[2].status is WorkflowStepStatus.RUNNING


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
        "    PostgresAttentionAdapter, PostgresCalendarAdapter, PostgresDemoClock, PostgresErpAdapter,\n"
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
        "detection = StockoutDetector(erp, PostgresAttentionAdapter(database_url), PostgresDemoClock(database_url)).detect(actor, RunId('run-workflow-executor'))[0]\n"
        "context = ScenarioAContextAssembler(identity, erp, PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id='00000000-0000-0000-0000-000000000202', quantity=Decimal(60), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='Approved alternate meets production.')\n"
        "approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "pending = ScenarioAApprovalService(approvals).request_pending(context, recommendation, current_source_versions=context.source_versions, policy_version='scenario_a_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4))\n"
        "approved = ScenarioAApprovalService(approvals).approve(approval_id=pending.approval.approval_id, expected_plan_hash=pending.plan.plan_hash, decider_id=actor.user_id, current_source_versions=context.source_versions, decided_at=now + timedelta(minutes=1))\n"
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


@pytest.mark.critical
@pytest.mark.integration
def test_postgres_crash_restart_runs_declared_effects_once_and_compensates_them(
    disposable_database: str,
) -> None:
    """A crash after replacement creation restarts by its key without duplicating any effect."""
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
        "    PostgresAttentionAdapter, PostgresCalendarAdapter, PostgresDemoClock, PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter, PostgresMailAdapter, PostgresPlanApprovalAdapter,\n"
        "    PostgresScenarioAToolAdapter, PostgresWorkflowStateAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approvals import ScenarioAApprovalService\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.planning import EnterWorkflowRecommendation\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.application.tools import ToolName\n"
        "from enterprise_agent.application.workflow_executor import DeterministicCrashInjector, ScenarioAWorkflowExecutor, WorkflowCrashInjectedError\n"
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
        "detection = StockoutDetector(erp, PostgresAttentionAdapter(database_url), PostgresDemoClock(database_url)).detect(actor, RunId('run-external-tools'))[0]\n"
        "context = ScenarioAContextAssembler(identity, erp, PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id='00000000-0000-0000-0000-000000000202', quantity=Decimal(60), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='Approved alternate meets production.')\n"
        "approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "pending = ScenarioAApprovalService(approvals).request_pending(context, recommendation, current_source_versions=context.source_versions, policy_version='scenario_a_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4))\n"
        "approved = ScenarioAApprovalService(approvals).approve(approval_id=pending.approval.approval_id, expected_plan_hash=pending.plan.plan_hash, decider_id=actor.user_id, current_source_versions=context.source_versions, decided_at=now + timedelta(minutes=1))\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "workflows = PostgresWorkflowStateAdapter(database_url)\n"
        "snapshot = WorkflowStateService(workflows).stage(pending.plan, created_at=now + timedelta(minutes=1))\n"
        "tool_adapter = PostgresScenarioAToolAdapter(database_url)\n"
        "executor = ScenarioAWorkflowExecutor(workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=tool_adapter)\n"
        "claimed = executor.claim(snapshot.workflow.workflow_id, worker_id='workflow-worker-a', now=now + timedelta(minutes=2), lease_expires_at=now + timedelta(minutes=20), current_source_versions=context.source_versions)\n"
        "after_first_guard = executor.advance_next_guard(claimed, worker_id='workflow-worker-a', completed_at=now + timedelta(minutes=3))\n"
        "executor.advance_next_guard(after_first_guard, worker_id='workflow-worker-a', completed_at=now + timedelta(minutes=4))\n"
        "crashing_executor = ScenarioAWorkflowExecutor(workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=tool_adapter, crash_injector=DeterministicCrashInjector(target_tool_name=ToolName.CREATE_REPLACEMENT_PO))\n"
        "started = crashing_executor.begin_next_tool(snapshot.workflow.workflow_id, worker_id='workflow-worker-a', now=now + timedelta(minutes=5), lease_expires_at=now + timedelta(minutes=7), current_source_versions=context.source_versions)\n"
        "try:\n"
        "    crashing_executor.execute_started_tool(started, worker_id='workflow-worker-a', completed_at=now + timedelta(minutes=6))\n"
        "except WorkflowCrashInjectedError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('replacement crash was not injected')\n"
        "crashed = workflows.load(snapshot.workflow.workflow_id)\n"
        "assert crashed is not None and crashed.workflow.current_step == 2 and crashed.steps[2].status is WorkflowStepStatus.RUNNING\n"
        "restart = ScenarioAWorkflowExecutor(workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=tool_adapter)\n"
        "resumed = restart.begin_next_tool(snapshot.workflow.workflow_id, worker_id='workflow-worker-b', now=now + timedelta(minutes=8), lease_expires_at=now + timedelta(minutes=20), current_source_versions=context.source_versions)\n"
        "assert resumed.invocation.idempotency_key == started.invocation.idempotency_key\n"
        "first_result = tool_adapter.execute(resumed.actor, resumed.invocation)\n"
        "assert first_result == tool_adapter.execute(resumed.actor, resumed.invocation)\n"
        "current = restart.execute_started_tool(resumed, worker_id='workflow-worker-b', completed_at=now + timedelta(minutes=9))\n"
        "for minute in (10, 12, 14):\n"
        "    started = restart.begin_next_tool(snapshot.workflow.workflow_id, worker_id='workflow-worker-b', now=now + timedelta(minutes=minute), lease_expires_at=now + timedelta(minutes=20), current_source_versions=context.source_versions)\n"
        "    current = restart.execute_started_tool(started, worker_id='workflow-worker-b', completed_at=now + timedelta(minutes=minute + 1))\n"
        "assert current.workflow.status is WorkflowStatus.SUCCEEDED and current.workflow.current_step == 6\n"
        "assert all(step.status is WorkflowStepStatus.SUCCEEDED for step in current.steps)\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM purchase_orders WHERE po_number LIKE 'RPL-%'\")).scalar_one() == 1\n"
        "    original = connection.execute(text(\"SELECT ordered_quantity::text, received_quantity::text, status, source_version FROM purchase_orders WHERE id = '00000000-0000-0000-0000-000000000401'\")).one()\n"
        "    assert original == ('40.000', '40.000', 'cancelled', 3)\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'tool:v1:%:notify_production:%'\")).scalar_one() == 1\n"
        "    arrival = connection.execute(text(\"SELECT task_type, status, payload->>'purchase_order_id' FROM scheduled_tasks WHERE workflow_instance_id = CAST(:workflow_id AS UUID)\"), {'workflow_id': str(snapshot.workflow.workflow_id)}).one()\n"
        "    assert arrival[0:2] == ('arrival_check', 'pending') and arrival[2] == first_result['replacement_purchase_order_id']\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM tool_invocations WHERE workflow_instance_id = CAST(:workflow_id AS UUID) AND status = 'succeeded'\"), {'workflow_id': str(snapshot.workflow.workflow_id)}).scalar_one() == 4\n"
        "with engine.begin() as connection:\n"
        "    connection.execute(text(\"UPDATE workflow_instances SET status = 'failed', completed_at = NULL, last_error = 'terminal test failure', lease_owner = 'workflow-worker-a', lease_expires_at = :lease_expires_at, updated_at = :updated_at WHERE id = CAST(:workflow_id AS UUID)\"), {'workflow_id': str(snapshot.workflow.workflow_id), 'lease_expires_at': now + timedelta(minutes=30), 'updated_at': now + timedelta(minutes=22)})\n"
        "compensated = executor.compensate_failed_workflow(snapshot.workflow.workflow_id, worker_id='workflow-worker-a', now=now + timedelta(minutes=22))\n"
        "assert compensated.workflow.status is WorkflowStatus.COMPENSATED\n"
        "assert [step.status for step in compensated.steps] == [WorkflowStepStatus.SUCCEEDED, WorkflowStepStatus.SUCCEEDED, WorkflowStepStatus.COMPENSATED, WorkflowStepStatus.COMPENSATED, WorkflowStepStatus.COMPENSATED, WorkflowStepStatus.COMPENSATED]\n"
        "with engine.connect() as connection:\n"
        "    assert connection.execute(text(\"SELECT status FROM purchase_orders WHERE po_number LIKE 'RPL-%'\")).scalar_one() == 'cancelled'\n"
        "    original = connection.execute(text(\"SELECT ordered_quantity::text, received_quantity::text, status, source_version FROM purchase_orders WHERE id = '00000000-0000-0000-0000-000000000401'\")).one()\n"
        "    assert original == ('100.000', '40.000', 'delayed', 4)\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'compensation:v1:%:send_correction_notification:%'\")).scalar_one() == 1\n"
        "    assert connection.execute(text(\"SELECT status FROM scheduled_tasks WHERE workflow_instance_id = CAST(:workflow_id AS UUID)\"), {'workflow_id': str(snapshot.workflow.workflow_id)}).scalar_one() == 'cancelled'\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM tool_invocations WHERE workflow_instance_id = CAST(:workflow_id AS UUID) AND status = 'compensated'\"), {'workflow_id': str(snapshot.workflow.workflow_id)}).scalar_one() == 4\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM tool_invocations WHERE workflow_instance_id = CAST(:workflow_id AS UUID) AND (tool_name LIKE 'cancel_%' OR tool_name LIKE 'restore_%' OR tool_name LIKE 'send_correction_%')\"), {'workflow_id': str(snapshot.workflow.workflow_id)}).scalar_one() == 4\n"
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
