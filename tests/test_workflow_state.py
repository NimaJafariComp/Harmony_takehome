"""Contracts for durable, non-executing workflow-instance and step-state persistence."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from enterprise_agent.domain import AttentionId, Plan, PlanId, UserId, WorkflowId

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)


def workflow_plan() -> Plan:
    """Build the immutable approved-intent shape that can later be staged for execution."""
    return Plan(
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
        plan_hash="sha256:workflow-state-test",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )


@dataclass
class MemoryWorkflowStateStore:
    """Record staged snapshots so service behavior is testable without PostgreSQL."""

    snapshots: dict[WorkflowId, Any]

    def __init__(self) -> None:
        self.snapshots = {}

    def create(self, snapshot: Any) -> None:
        self.snapshots[snapshot.workflow.workflow_id] = snapshot

    def load(self, workflow_id: WorkflowId) -> Any | None:
        return self.snapshots.get(workflow_id)

    def claim(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> Any | None:
        return None

    def complete_guard_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        completed_at: datetime,
    ) -> Any | None:
        return None


def staged_snapshot() -> Any:
    """Stage a fresh pending workflow once for adapter-mapping contract tests."""
    from enterprise_agent.application.workflow_state import WorkflowStateService

    return WorkflowStateService(MemoryWorkflowStateStore()).stage(
        workflow_plan(),
        created_at=NOW,
        workflow_id=WorkflowId("00000000-0000-0000-0000-000000000901"),
    )


@pytest.mark.critical
def test_staging_persists_the_declared_pending_workflow_and_all_six_steps() -> None:
    """Staging stores the immutable plan snapshot only; it neither claims nor executes a step."""
    from enterprise_agent.application.workflow_state import WorkflowStateService
    from enterprise_agent.domain import WorkflowStatus, WorkflowStepStatus

    store = MemoryWorkflowStateStore()
    snapshot = WorkflowStateService(store).stage(
        workflow_plan(),
        created_at=NOW,
        workflow_id=WorkflowId("00000000-0000-0000-0000-000000000901"),
    )

    assert store.load(snapshot.workflow.workflow_id) == snapshot
    assert snapshot.workflow.status is WorkflowStatus.PENDING
    assert snapshot.workflow.current_step == 0
    assert snapshot.workflow.lease_owner is None
    assert snapshot.workflow.lease_expires_at is None
    assert snapshot.workflow.created_at == NOW
    assert [step.step_index for step in snapshot.steps] == [1, 2, 3, 4, 5, 6]
    assert all(step.status is WorkflowStepStatus.PENDING for step in snapshot.steps)
    assert all(step.attempt_count == 0 for step in snapshot.steps)
    assert all(step.idempotency_key is None for step in snapshot.steps)
    assert all(step.input["plan_hash"] == workflow_plan().plan_hash for step in snapshot.steps)
    assert snapshot.steps[2].tool_name == "create_replacement_po"


def test_staging_rejects_a_plan_without_a_declared_workflow() -> None:
    """Only an immutable enter-workflow plan with an exact definition can obtain durable state."""
    from enterprise_agent.application.workflow_state import (
        WorkflowStateInitializationError,
        WorkflowStateService,
    )

    store = MemoryWorkflowStateStore()
    without_definition = replace(workflow_plan(), workflow_name=None, workflow_version=None)

    with pytest.raises(WorkflowStateInitializationError, match="declared workflow"):
        WorkflowStateService(store).stage(without_definition, created_at=NOW)
    with pytest.raises(WorkflowStateInitializationError, match="declared workflow"):
        WorkflowStateService(store).stage(
            replace(workflow_plan(), workflow_name="unreviewed_workflow"),
            created_at=NOW,
        )

    assert store.snapshots == {}


def test_staging_generates_a_workflow_id_when_no_durable_id_is_supplied() -> None:
    """The service owns opaque instance identity while callers may still inject it for tests."""
    from enterprise_agent.application.workflow_state import WorkflowStateService

    snapshot = WorkflowStateService(MemoryWorkflowStateStore()).stage(
        workflow_plan(),
        created_at=NOW,
    )

    assert snapshot.workflow.workflow_id


def mapping_result(
    *, one_or_none: dict[str, object] | None = None, all_rows: list[dict[str, object]] | None = None
) -> MagicMock:
    """Build one SQLAlchemy result double with the mapping accessors used by the adapter."""
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = one_or_none
    result.mappings.return_value.all.return_value = [] if all_rows is None else all_rows
    return result


def test_postgres_adapter_serializes_and_restores_a_complete_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter persists every workflow/step field in one transaction and maps it back."""
    from enterprise_agent.adapters import workflow_state

    snapshot = staged_snapshot()
    workflow = snapshot.workflow
    workflow_row = {
        "id": str(workflow.workflow_id),
        "plan_id": str(workflow.plan_id),
        "definition_name": workflow.definition_name,
        "definition_version": workflow.definition_version,
        "status": workflow.status.value,
        "current_step": workflow.current_step,
        "started_at": workflow.started_at,
        "completed_at": workflow.completed_at,
        "last_error": workflow.last_error,
        "lease_owner": workflow.lease_owner,
        "lease_expires_at": workflow.lease_expires_at,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
    }
    step_rows = [
        {
            "id": str(step.step_id),
            "workflow_instance_id": str(step.workflow_id),
            "step_index": step.step_index,
            "step_name": step.step_name,
            "tool_name": step.tool_name,
            "status": step.status.value,
            "idempotency_key": step.idempotency_key,
            "input": dict(step.input),
            "result": None if step.result is None else dict(step.result),
            "error": step.error,
            "attempt_count": step.attempt_count,
            "started_at": step.started_at,
            "completed_at": step.completed_at,
            "lease_owner": step.lease_owner,
            "lease_expires_at": step.lease_expires_at,
            "created_at": step.created_at,
            "updated_at": step.updated_at,
        }
        for step in snapshot.steps
    ]
    engine = MagicMock()
    transaction = engine.begin.return_value.__enter__.return_value
    reader = engine.connect.return_value.__enter__.return_value
    reader.execute.side_effect = [
        mapping_result(one_or_none=workflow_row),
        mapping_result(all_rows=step_rows),
        mapping_result(one_or_none=None),
    ]
    monkeypatch.setattr(workflow_state, "create_engine", lambda _: engine)

    adapter = workflow_state.PostgresWorkflowStateAdapter("postgresql+psycopg://ignored")
    adapter.create(snapshot)
    loaded = adapter.load(workflow.workflow_id)

    assert transaction.execute.call_count == 7
    assert transaction.execute.call_args_list[0].args[1]["workflow_id"] == str(workflow.workflow_id)
    assert transaction.execute.call_args_list[1].args[1]["step_index"] == 1
    assert loaded == snapshot
    assert adapter.load(WorkflowId("00000000-0000-0000-0000-000000000999")) is None


def _workflow_row(snapshot: Any) -> dict[str, object]:
    """Serialize one test snapshot workflow into the adapter's selected-row shape."""
    workflow = snapshot.workflow
    return {
        "id": str(workflow.workflow_id),
        "plan_id": str(workflow.plan_id),
        "definition_name": workflow.definition_name,
        "definition_version": workflow.definition_version,
        "status": workflow.status.value,
        "current_step": workflow.current_step,
        "started_at": workflow.started_at,
        "completed_at": workflow.completed_at,
        "last_error": workflow.last_error,
        "lease_owner": workflow.lease_owner,
        "lease_expires_at": workflow.lease_expires_at,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
    }


def _step_rows(snapshot: Any) -> list[dict[str, object]]:
    """Serialize a test snapshot's ordered steps into the adapter's selected-row shape."""
    return [
        {
            "id": str(step.step_id),
            "workflow_instance_id": str(step.workflow_id),
            "step_index": step.step_index,
            "step_name": step.step_name,
            "tool_name": step.tool_name,
            "status": step.status.value,
            "idempotency_key": step.idempotency_key,
            "input": dict(step.input),
            "result": None if step.result is None else dict(step.result),
            "error": step.error,
            "attempt_count": step.attempt_count,
            "started_at": step.started_at,
            "completed_at": step.completed_at,
            "lease_owner": step.lease_owner,
            "lease_expires_at": step.lease_expires_at,
            "created_at": step.created_at,
            "updated_at": step.updated_at,
        }
        for step in snapshot.steps
    ]


def test_postgres_adapter_claims_and_advances_only_one_leased_pending_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persistence boundary uses CAS predicates for claims and exact-next guard completion."""
    from enterprise_agent.adapters import workflow_state
    from enterprise_agent.domain import WorkflowStatus, WorkflowStepStatus

    snapshot = staged_snapshot()
    claimed_at = NOW + timedelta(minutes=1)
    lease_expires_at = NOW + timedelta(minutes=6)
    claimed_snapshot = replace(
        snapshot,
        workflow=replace(
            snapshot.workflow,
            status=WorkflowStatus.RUNNING,
            started_at=claimed_at,
            lease_owner="worker-a",
            lease_expires_at=lease_expires_at,
            updated_at=claimed_at,
        ),
    )
    completed_at = NOW + timedelta(minutes=2)
    completed_steps = list(claimed_snapshot.steps)
    completed_steps[0] = replace(
        completed_steps[0],
        status=WorkflowStepStatus.SUCCEEDED,
        attempt_count=1,
        started_at=completed_at,
        completed_at=completed_at,
        result={"guard": "confirmed"},
        updated_at=completed_at,
    )
    completed_snapshot = replace(
        claimed_snapshot,
        workflow=replace(claimed_snapshot.workflow, current_step=1, updated_at=completed_at),
        steps=tuple(completed_steps),
    )
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = str(snapshot.workflow.workflow_id)
    completed_result = MagicMock()
    completed_result.scalar_one_or_none.return_value = str(snapshot.steps[0].step_id)
    advanced_result = MagicMock()
    advanced_result.scalar_one_or_none.return_value = str(snapshot.workflow.workflow_id)
    lost_result = MagicMock()
    lost_result.scalar_one_or_none.return_value = None
    engine = MagicMock()
    transaction = engine.begin.return_value.__enter__.return_value
    transaction.execute.side_effect = [
        claim_result,
        mapping_result(one_or_none=_workflow_row(claimed_snapshot)),
        mapping_result(all_rows=_step_rows(claimed_snapshot)),
        completed_result,
        advanced_result,
        mapping_result(one_or_none=_workflow_row(completed_snapshot)),
        mapping_result(all_rows=_step_rows(completed_snapshot)),
        lost_result,
    ]
    monkeypatch.setattr(workflow_state, "create_engine", lambda _: engine)
    adapter = workflow_state.PostgresWorkflowStateAdapter("postgresql+psycopg://ignored")

    claimed = adapter.claim(
        snapshot.workflow.workflow_id,
        worker_id="worker-a",
        claimed_at=claimed_at,
        lease_expires_at=lease_expires_at,
    )
    completed = adapter.complete_guard_step(
        snapshot.workflow.workflow_id,
        worker_id="worker-a",
        expected_step_index=1,
        completed_at=completed_at,
    )
    lost = adapter.claim(
        snapshot.workflow.workflow_id,
        worker_id="worker-b",
        claimed_at=claimed_at,
        lease_expires_at=lease_expires_at,
    )

    assert claimed == claimed_snapshot
    assert completed == completed_snapshot
    assert lost is None
    assert transaction.execute.call_args_list[0].args[1]["worker_id"] == "worker-a"
    assert transaction.execute.call_args_list[3].args[1]["expected_step_index"] == 1
    assert transaction.execute.call_args_list[3].args[1]["result"] == '{"guard": "confirmed"}'


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and retain diagnostics when durable-contract setup fails."""
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
def test_postgres_persists_one_plan_bound_workflow_and_its_six_steps(
    disposable_database: str,
) -> None:
    """The database retains the initial state atomically and rejects a second instance per plan."""
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
        "from sqlalchemy.exc import IntegrityError\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresCalendarAdapter,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter,\n"
        "    PostgresPlanApprovalAdapter,\n"
        "    PostgresWorkflowStateAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approvals import ScenarioAApprovalService\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.planning import EnterWorkflowRecommendation\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.application.workflow_state import WorkflowStateService\n"
        "from enterprise_agent.domain import RunId, UserId, WorkflowStatus\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "now = datetime(2026, 8, 24, 9, tzinfo=UTC)\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "actor = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "detection = StockoutDetector(erp, PostgresAttentionAdapter(database_url)).detect(actor, RunId('run-workflow-state'), now)[0]\n"
        "context = ScenarioAContextAssembler(identity, erp, PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id='00000000-0000-0000-0000-000000000202', quantity=Decimal(60), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='Approved alternate meets the production date.')\n"
        "plan = ScenarioAApprovalService(PostgresPlanApprovalAdapter(database_url)).request_pending(context, recommendation, current_source_versions=context.source_versions, policy_version='scenario_a_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4)).plan\n"
        "store = PostgresWorkflowStateAdapter(database_url)\n"
        "snapshot = WorkflowStateService(store).stage(plan, created_at=now + timedelta(minutes=1))\n"
        "loaded = store.load(snapshot.workflow.workflow_id)\n"
        "assert loaded == snapshot\n"
        "assert loaded.workflow.status is WorkflowStatus.PENDING and loaded.workflow.current_step == 0\n"
        "assert len(loaded.steps) == 6 and [step.step_index for step in loaded.steps] == [1, 2, 3, 4, 5, 6]\n"
        "assert all(step.idempotency_key is None and step.attempt_count == 0 for step in loaded.steps)\n"
        "try:\n"
        "    WorkflowStateService(store).stage(plan, created_at=now + timedelta(minutes=2))\n"
        "except IntegrityError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('a plan may stage exactly one workflow instance')\n"
        "with create_engine(database_url).connect() as connection:\n"
        "    assert connection.execute(text('SELECT COUNT(*) FROM workflow_instances')).scalar_one() == 1\n"
        "    assert connection.execute(text('SELECT COUNT(*) FROM workflow_steps')).scalar_one() == 6\n"
        "    assert connection.execute(text('SELECT lease_expires_at FROM workflow_instances')).scalar_one() is None\n"
        "    assert connection.execute(text('SELECT input->>\\'plan_hash\\' FROM workflow_steps WHERE step_index = 1')).scalar_one() == plan.plan_hash\n"
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
