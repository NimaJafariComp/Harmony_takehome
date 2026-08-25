"""Atomic PostgreSQL persistence for workflow instances and their ordered durable steps."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, RowMapping

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
            workflow_row = (
                connection.execute(SELECT_WORKFLOW, {"workflow_id": str(workflow_id)})
                .mappings()
                .one_or_none()
            )
            if workflow_row is None:
                return None
            step_rows = (
                connection.execute(SELECT_STEPS, {"workflow_id": str(workflow_id)}).mappings().all()
            )
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
