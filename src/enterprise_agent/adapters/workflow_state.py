"""Atomic PostgreSQL persistence for workflow instances and their ordered durable steps."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, RowMapping

from enterprise_agent.domain import (
    PlanId,
    WorkflowId,
    WorkflowState,
    WorkflowStateSnapshot,
    WorkflowStatus,
    WorkflowStepId,
    WorkflowStepState,
    WorkflowStepStatus,
)

INSERT_WORKFLOW = text("""
    INSERT INTO workflow_instances (
        id, plan_id, definition_name, definition_version, status, current_step, started_at,
        completed_at, last_error, lease_owner, lease_expires_at, created_at, updated_at
    ) VALUES (
        CAST(:workflow_id AS UUID), CAST(:plan_id AS UUID), :definition_name,
        :definition_version, :status, :current_step, :started_at, :completed_at, :last_error,
        :lease_owner, :lease_expires_at, :created_at, :updated_at
    )
""")
INSERT_STEP = text("""
    INSERT INTO workflow_steps (
        id, workflow_instance_id, step_index, step_name, tool_name, status, idempotency_key,
        input, result, error, attempt_count, started_at, completed_at, lease_owner,
        lease_expires_at, created_at, updated_at
    ) VALUES (
        CAST(:step_id AS UUID), CAST(:workflow_id AS UUID), :step_index, :step_name, :tool_name,
        :status, :idempotency_key, CAST(:input AS JSONB), CAST(:result AS JSONB), :error,
        :attempt_count, :started_at, :completed_at, :lease_owner, :lease_expires_at, :created_at,
        :updated_at
    )
""")
SELECT_WORKFLOW = text("""
    SELECT id, plan_id, definition_name, definition_version, status, current_step, started_at,
           completed_at, last_error, lease_owner, lease_expires_at, created_at, updated_at
    FROM workflow_instances
    WHERE id = CAST(:workflow_id AS UUID)
""")
SELECT_STEPS = text("""
    SELECT id, workflow_instance_id, step_index, step_name, tool_name, status, idempotency_key,
           input, result, error, attempt_count, started_at, completed_at, lease_owner,
           lease_expires_at, created_at, updated_at
    FROM workflow_steps
    WHERE workflow_instance_id = CAST(:workflow_id AS UUID)
    ORDER BY step_index ASC
""")
CLAIM_WORKFLOW = text("""
    UPDATE workflow_instances
    SET status = :running_status,
        lease_owner = :worker_id,
        lease_expires_at = :lease_expires_at,
        started_at = COALESCE(started_at, :claimed_at),
        updated_at = :claimed_at
    WHERE id = CAST(:workflow_id AS UUID)
      AND (
          status = :pending_status
          OR (
              status = :running_status
              AND (
                  (lease_owner = :worker_id AND lease_expires_at > :claimed_at)
                  OR lease_expires_at <= :claimed_at
              )
          )
      )
    RETURNING id
""")
COMPLETE_GUARD_STEP = text("""
    UPDATE workflow_steps AS step
    SET status = :succeeded_status,
        attempt_count = step.attempt_count + 1,
        started_at = COALESCE(step.started_at, :completed_at),
        completed_at = :completed_at,
        result = CAST(:result AS JSONB),
        updated_at = :completed_at
    FROM workflow_instances AS workflow
    WHERE step.workflow_instance_id = workflow.id
      AND workflow.id = CAST(:workflow_id AS UUID)
      AND workflow.status = :running_status
      AND workflow.lease_owner = :worker_id
      AND workflow.lease_expires_at > :completed_at
      AND workflow.current_step = :previous_step_index
      AND step.step_index = :expected_step_index
      AND step.tool_name IS NULL
      AND step.status = :pending_status
    RETURNING step.id
""")
ADVANCE_WORKFLOW_AFTER_GUARD = text("""
    UPDATE workflow_instances
    SET current_step = :expected_step_index,
        updated_at = :completed_at
    WHERE id = CAST(:workflow_id AS UUID)
      AND status = :running_status
      AND lease_owner = :worker_id
      AND lease_expires_at > :completed_at
      AND current_step = :previous_step_index
    RETURNING id
""")
START_TOOL_STEP = text("""
    UPDATE workflow_steps AS step
    SET status = :running_step_status,
        idempotency_key = :idempotency_key,
        attempt_count = step.attempt_count + 1,
        started_at = COALESCE(step.started_at, :started_at),
        lease_owner = :worker_id,
        lease_expires_at = workflow.lease_expires_at,
        updated_at = :started_at
    FROM workflow_instances AS workflow
    WHERE step.workflow_instance_id = workflow.id
      AND workflow.id = CAST(:workflow_id AS UUID)
      AND workflow.status = :running_workflow_status
      AND workflow.lease_owner = :worker_id
      AND workflow.lease_expires_at > :started_at
      AND workflow.current_step = :previous_step_index
      AND step.step_index = :expected_step_index
      AND step.tool_name IS NOT NULL
      AND step.status = :pending_step_status
    RETURNING step.id
""")
COMPLETE_TOOL_STEP = text("""
    UPDATE workflow_steps AS step
    SET status = :succeeded_step_status,
        result = CAST(:result AS JSONB),
        completed_at = :completed_at,
        lease_owner = NULL,
        lease_expires_at = NULL,
        updated_at = :completed_at
    FROM workflow_instances AS workflow
    WHERE step.workflow_instance_id = workflow.id
      AND workflow.id = CAST(:workflow_id AS UUID)
      AND workflow.status = :running_workflow_status
      AND workflow.lease_owner = :worker_id
      AND workflow.lease_expires_at > :completed_at
      AND workflow.current_step = :previous_step_index
      AND step.step_index = :expected_step_index
      AND step.tool_name IS NOT NULL
      AND step.status = :running_step_status
      AND step.idempotency_key = :idempotency_key
    RETURNING step.id
""")
ADVANCE_WORKFLOW_AFTER_TOOL = text("""
    UPDATE workflow_instances
    SET current_step = :expected_step_index,
        status = CASE WHEN :finish_workflow THEN :succeeded_workflow_status ELSE status END,
        completed_at = CASE WHEN :finish_workflow THEN :completed_at ELSE completed_at END,
        lease_owner = CASE WHEN :finish_workflow THEN NULL ELSE lease_owner END,
        lease_expires_at = CASE WHEN :finish_workflow THEN NULL ELSE lease_expires_at END,
        updated_at = :completed_at
    WHERE id = CAST(:workflow_id AS UUID)
      AND status = :running_workflow_status
      AND lease_owner = :worker_id
      AND lease_expires_at > :completed_at
      AND current_step = :previous_step_index
    RETURNING id
""")
FAIL_TOOL_STEP = text("""
    UPDATE workflow_steps AS step
    SET status = :failed_step_status,
        error = :error,
        completed_at = :failed_at,
        lease_owner = NULL,
        lease_expires_at = NULL,
        updated_at = :failed_at
    FROM workflow_instances AS workflow
    WHERE step.workflow_instance_id = workflow.id
      AND workflow.id = CAST(:workflow_id AS UUID)
      AND workflow.status = :running_workflow_status
      AND workflow.lease_owner = :worker_id
      AND workflow.lease_expires_at > :failed_at
      AND workflow.current_step = :previous_step_index
      AND step.step_index = :expected_step_index
      AND step.tool_name IS NOT NULL
      AND step.status = :running_step_status
      AND step.idempotency_key = :idempotency_key
    RETURNING step.id
""")
FAIL_WORKFLOW_AFTER_TOOL = text("""
    UPDATE workflow_instances
    SET status = :failed_workflow_status,
        last_error = :error,
        updated_at = :failed_at
    WHERE id = CAST(:workflow_id AS UUID)
      AND status = :running_workflow_status
      AND lease_owner = :worker_id
      AND lease_expires_at > :failed_at
      AND current_step = :previous_step_index
    RETURNING id
""")
BEGIN_COMPENSATION = text("""
    UPDATE workflow_instances AS workflow
    SET status = CASE
            WHEN EXISTS (
                SELECT 1
                FROM workflow_steps AS step
                WHERE step.workflow_instance_id = workflow.id
                  AND step.tool_name IS NOT NULL
                  AND step.status = :succeeded_step_status
            ) THEN :compensating_workflow_status
            ELSE :compensated_workflow_status
        END,
        completed_at = CASE
            WHEN EXISTS (
                SELECT 1
                FROM workflow_steps AS step
                WHERE step.workflow_instance_id = workflow.id
                  AND step.tool_name IS NOT NULL
                  AND step.status = :succeeded_step_status
            ) THEN completed_at
            ELSE :started_at
        END,
        lease_owner = CASE
            WHEN EXISTS (
                SELECT 1
                FROM workflow_steps AS step
                WHERE step.workflow_instance_id = workflow.id
                  AND step.tool_name IS NOT NULL
                  AND step.status = :succeeded_step_status
            ) THEN lease_owner
            ELSE NULL
        END,
        lease_expires_at = CASE
            WHEN EXISTS (
                SELECT 1
                FROM workflow_steps AS step
                WHERE step.workflow_instance_id = workflow.id
                  AND step.tool_name IS NOT NULL
                  AND step.status = :succeeded_step_status
            ) THEN lease_expires_at
            ELSE NULL
        END,
        updated_at = :started_at
    WHERE workflow.id = CAST(:workflow_id AS UUID)
      AND workflow.status = :failed_workflow_status
      AND workflow.lease_owner = :worker_id
      AND workflow.lease_expires_at > :started_at
    RETURNING workflow.id
""")
START_COMPENSATION_STEP = text("""
    UPDATE workflow_steps AS step
    SET status = :compensating_step_status,
        lease_owner = :worker_id,
        lease_expires_at = workflow.lease_expires_at,
        updated_at = :started_at
    FROM workflow_instances AS workflow
    WHERE step.workflow_instance_id = workflow.id
      AND workflow.id = CAST(:workflow_id AS UUID)
      AND workflow.status = :compensating_workflow_status
      AND workflow.lease_owner = :worker_id
      AND workflow.lease_expires_at > :started_at
      AND step.step_index = :expected_step_index
      AND step.tool_name IS NOT NULL
      AND step.status = :succeeded_step_status
    RETURNING step.id
""")
COMPLETE_COMPENSATION_STEP = text("""
    UPDATE workflow_steps AS step
    SET status = :compensated_step_status,
        result = jsonb_build_object(
            'effect', COALESCE(step.result, '{}'::jsonb),
            'compensation', CAST(:result AS JSONB)
        ),
        completed_at = :completed_at,
        lease_owner = NULL,
        lease_expires_at = NULL,
        updated_at = :completed_at
    FROM workflow_instances AS workflow
    WHERE step.workflow_instance_id = workflow.id
      AND workflow.id = CAST(:workflow_id AS UUID)
      AND workflow.status = :compensating_workflow_status
      AND workflow.lease_owner = :worker_id
      AND workflow.lease_expires_at > :completed_at
      AND step.step_index = :expected_step_index
      AND step.tool_name IS NOT NULL
      AND step.status = :compensating_step_status
    RETURNING step.id
""")
FINISH_COMPENSATION = text("""
    UPDATE workflow_instances AS workflow
    SET status = :compensated_workflow_status,
        completed_at = :completed_at,
        lease_owner = NULL,
        lease_expires_at = NULL,
        updated_at = :completed_at
    WHERE workflow.id = CAST(:workflow_id AS UUID)
      AND workflow.status = :compensating_workflow_status
      AND workflow.lease_owner = :worker_id
      AND workflow.lease_expires_at > :completed_at
      AND NOT EXISTS (
          SELECT 1
          FROM workflow_steps AS step
          WHERE step.workflow_instance_id = workflow.id
            AND step.tool_name IS NOT NULL
            AND step.status = :succeeded_step_status
      )
    RETURNING workflow.id
""")


class PostgresWorkflowStateAdapter:
    """Store an initial declared workflow snapshot atomically and load it without side effects."""

    def __init__(self, database_url: str) -> None:
        """Connect this durable workflow-state adapter to one PostgreSQL database."""
        self._engine: Engine = create_engine(database_url)

    def create(self, snapshot: WorkflowStateSnapshot) -> None:
        """Insert the workflow and all of its declared steps in one database transaction."""
        with self._engine.begin() as connection:
            connection.execute(INSERT_WORKFLOW, _workflow_parameters(snapshot.workflow))
            for step in snapshot.steps:
                connection.execute(INSERT_STEP, _step_parameters(step))

    def load(self, workflow_id: WorkflowId) -> WorkflowStateSnapshot | None:
        """Load one workflow and all stored steps in declared order for later execution work."""
        with self._engine.connect() as connection:
            return _load_snapshot(connection, workflow_id)

    def claim(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Claim one runnable workflow using a database compare-and-swap plus lease expiry."""
        with self._engine.begin() as connection:
            claimed = connection.execute(
                CLAIM_WORKFLOW,
                {
                    "workflow_id": str(workflow_id),
                    "worker_id": worker_id,
                    "claimed_at": claimed_at,
                    "lease_expires_at": lease_expires_at,
                    "pending_status": WorkflowStatus.PENDING.value,
                    "running_status": WorkflowStatus.RUNNING.value,
                },
            ).scalar_one_or_none()
            if claimed is None:
                return None
            return _load_snapshot(connection, workflow_id)

    def complete_guard_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Advance exactly one leased pending guard and then its workflow cursor atomically."""
        with self._engine.begin() as connection:
            parameters = {
                "workflow_id": str(workflow_id),
                "worker_id": worker_id,
                "expected_step_index": expected_step_index,
                "previous_step_index": expected_step_index - 1,
                "completed_at": completed_at,
                "pending_status": WorkflowStepStatus.PENDING.value,
                "succeeded_status": WorkflowStepStatus.SUCCEEDED.value,
                "running_status": WorkflowStatus.RUNNING.value,
                "result": json.dumps({"guard": "confirmed"}, sort_keys=True),
            }
            completed = connection.execute(COMPLETE_GUARD_STEP, parameters).scalar_one_or_none()
            if completed is None:
                return None
            advanced = connection.execute(
                ADVANCE_WORKFLOW_AFTER_GUARD, parameters
            ).scalar_one_or_none()
            if advanced is None:
                raise RuntimeError("workflow cursor was lost while completing a declared guard")
            return _load_snapshot(connection, workflow_id)

    def start_tool_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        idempotency_key: str,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Commit an exact leased external step before its effect leaves the control plane."""
        with self._engine.begin() as connection:
            started = connection.execute(
                START_TOOL_STEP,
                {
                    "workflow_id": str(workflow_id),
                    "worker_id": worker_id,
                    "expected_step_index": expected_step_index,
                    "previous_step_index": expected_step_index - 1,
                    "idempotency_key": idempotency_key,
                    "started_at": started_at,
                    "pending_step_status": WorkflowStepStatus.PENDING.value,
                    "running_step_status": WorkflowStepStatus.RUNNING.value,
                    "running_workflow_status": WorkflowStatus.RUNNING.value,
                },
            ).scalar_one_or_none()
            if started is None:
                return None
            return _load_snapshot(connection, workflow_id)

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
        """Commit the external result and its exact cursor transition in one new transaction."""
        with self._engine.begin() as connection:
            parameters = {
                "workflow_id": str(workflow_id),
                "worker_id": worker_id,
                "expected_step_index": expected_step_index,
                "previous_step_index": expected_step_index - 1,
                "idempotency_key": idempotency_key,
                "result": json.dumps(dict(result), sort_keys=True),
                "finish_workflow": finish_workflow,
                "completed_at": completed_at,
                "running_step_status": WorkflowStepStatus.RUNNING.value,
                "succeeded_step_status": WorkflowStepStatus.SUCCEEDED.value,
                "running_workflow_status": WorkflowStatus.RUNNING.value,
                "succeeded_workflow_status": WorkflowStatus.SUCCEEDED.value,
            }
            completed = connection.execute(COMPLETE_TOOL_STEP, parameters).scalar_one_or_none()
            if completed is None:
                return None
            advanced = connection.execute(
                ADVANCE_WORKFLOW_AFTER_TOOL, parameters
            ).scalar_one_or_none()
            if advanced is None:
                raise RuntimeError("workflow cursor was lost while completing a declared tool")
            return _load_snapshot(connection, workflow_id)

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
        """Persist one terminal external failure without releasing the lease needed to unwind it."""
        with self._engine.begin() as connection:
            parameters = {
                "workflow_id": str(workflow_id),
                "worker_id": worker_id,
                "expected_step_index": expected_step_index,
                "previous_step_index": expected_step_index - 1,
                "idempotency_key": idempotency_key,
                "error": error,
                "failed_at": failed_at,
                "running_workflow_status": WorkflowStatus.RUNNING.value,
                "failed_workflow_status": WorkflowStatus.FAILED.value,
                "running_step_status": WorkflowStepStatus.RUNNING.value,
                "failed_step_status": WorkflowStepStatus.FAILED.value,
            }
            failed = connection.execute(FAIL_TOOL_STEP, parameters).scalar_one_or_none()
            if failed is None:
                return None
            workflow_failed = connection.execute(
                FAIL_WORKFLOW_AFTER_TOOL, parameters
            ).scalar_one_or_none()
            if workflow_failed is None:
                raise RuntimeError("workflow transition was lost while recording a tool failure")
            return _load_snapshot(connection, workflow_id)

    def begin_compensation(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Durably enter compensation, closing immediately when no external effect succeeded."""
        with self._engine.begin() as connection:
            started = connection.execute(
                BEGIN_COMPENSATION,
                {
                    "workflow_id": str(workflow_id),
                    "worker_id": worker_id,
                    "started_at": started_at,
                    "failed_workflow_status": WorkflowStatus.FAILED.value,
                    "compensating_workflow_status": WorkflowStatus.COMPENSATING.value,
                    "compensated_workflow_status": WorkflowStatus.COMPENSATED.value,
                    "succeeded_step_status": WorkflowStepStatus.SUCCEEDED.value,
                },
            ).scalar_one_or_none()
            if started is None:
                return None
            return _load_snapshot(connection, workflow_id)

    def start_compensation_step(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        expected_step_index: int,
        started_at: datetime,
    ) -> WorkflowStateSnapshot | None:
        """Commit one reverse action's started lifecycle before calling the provider boundary."""
        with self._engine.begin() as connection:
            started = connection.execute(
                START_COMPENSATION_STEP,
                {
                    "workflow_id": str(workflow_id),
                    "worker_id": worker_id,
                    "expected_step_index": expected_step_index,
                    "started_at": started_at,
                    "compensating_workflow_status": WorkflowStatus.COMPENSATING.value,
                    "succeeded_step_status": WorkflowStepStatus.SUCCEEDED.value,
                    "compensating_step_status": WorkflowStepStatus.COMPENSATING.value,
                },
            ).scalar_one_or_none()
            if started is None:
                return None
            return _load_snapshot(connection, workflow_id)

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
        """Commit one reverse result and close the workflow only when no effect remains to unwind."""
        with self._engine.begin() as connection:
            parameters = {
                "workflow_id": str(workflow_id),
                "worker_id": worker_id,
                "expected_step_index": expected_step_index,
                "result": json.dumps(dict(result), sort_keys=True),
                "completed_at": completed_at,
                "compensating_workflow_status": WorkflowStatus.COMPENSATING.value,
                "compensated_workflow_status": WorkflowStatus.COMPENSATED.value,
                "compensating_step_status": WorkflowStepStatus.COMPENSATING.value,
                "compensated_step_status": WorkflowStepStatus.COMPENSATED.value,
                "succeeded_step_status": WorkflowStepStatus.SUCCEEDED.value,
            }
            completed = connection.execute(
                COMPLETE_COMPENSATION_STEP, parameters
            ).scalar_one_or_none()
            if completed is None:
                return None
            if finish_workflow:
                finished = connection.execute(FINISH_COMPENSATION, parameters).scalar_one_or_none()
                if finished is None:
                    raise RuntimeError("workflow compensation completion was lost")
            return _load_snapshot(connection, workflow_id)


def _load_snapshot(connection: Connection, workflow_id: WorkflowId) -> WorkflowStateSnapshot | None:
    """Read one workflow and ordered steps from an existing transactional connection."""
    workflow_row = (
        connection.execute(SELECT_WORKFLOW, {"workflow_id": str(workflow_id)})
        .mappings()
        .one_or_none()
    )
    if workflow_row is None:
        return None
    step_rows = connection.execute(SELECT_STEPS, {"workflow_id": str(workflow_id)}).mappings().all()
    return WorkflowStateSnapshot(
        workflow=_workflow_from_row(workflow_row),
        steps=tuple(_step_from_row(row) for row in step_rows),
    )


def _workflow_parameters(workflow: WorkflowState) -> dict[str, object]:
    """Serialize every durable workflow instance field into bound SQL parameters."""
    return {
        "workflow_id": str(workflow.workflow_id),
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


def _step_parameters(step: WorkflowStepState) -> dict[str, object]:
    """Serialize one durable step state while preserving JSON and nullable effect fields."""
    return {
        "step_id": str(step.step_id),
        "workflow_id": str(step.workflow_id),
        "step_index": step.step_index,
        "step_name": step.step_name,
        "tool_name": step.tool_name,
        "status": step.status.value,
        "idempotency_key": step.idempotency_key,
        "input": json.dumps(dict(step.input), sort_keys=True),
        "result": None if step.result is None else json.dumps(dict(step.result), sort_keys=True),
        "error": step.error,
        "attempt_count": step.attempt_count,
        "started_at": step.started_at,
        "completed_at": step.completed_at,
        "lease_owner": step.lease_owner,
        "lease_expires_at": step.lease_expires_at,
        "created_at": step.created_at,
        "updated_at": step.updated_at,
    }


def _workflow_from_row(row: RowMapping) -> WorkflowState:
    """Map one PostgreSQL workflow row into the immutable domain state record."""
    return WorkflowState(
        workflow_id=WorkflowId(str(row["id"])),
        plan_id=PlanId(str(row["plan_id"])),
        definition_name=cast(str, row["definition_name"]),
        definition_version=cast(int, row["definition_version"]),
        status=WorkflowStatus(cast(str, row["status"])),
        current_step=cast(int, row["current_step"]),
        started_at=cast(datetime | None, row["started_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        last_error=cast(str | None, row["last_error"]),
        lease_owner=cast(str | None, row["lease_owner"]),
        lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


def _step_from_row(row: RowMapping) -> WorkflowStepState:
    """Map one PostgreSQL workflow-step row into its immutable domain state record."""
    result = cast(Mapping[str, object] | None, row["result"])
    return WorkflowStepState(
        step_id=WorkflowStepId(str(row["id"])),
        workflow_id=WorkflowId(str(row["workflow_instance_id"])),
        step_index=cast(int, row["step_index"]),
        step_name=cast(str, row["step_name"]),
        tool_name=cast(str | None, row["tool_name"]),
        status=WorkflowStepStatus(cast(str, row["status"])),
        idempotency_key=cast(str | None, row["idempotency_key"]),
        input=dict(cast(Mapping[str, object], row["input"])),
        result=None if result is None else dict(result),
        error=cast(str | None, row["error"]),
        attempt_count=cast(int, row["attempt_count"]),
        started_at=cast(datetime | None, row["started_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
        lease_owner=cast(str | None, row["lease_owner"]),
        lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )
