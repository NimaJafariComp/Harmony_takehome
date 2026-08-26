"""Read-only PostgreSQL projection for local terminal operator status."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, RowMapping

from enterprise_agent.application.operator_status import (
    OperatorStatusSnapshot,
    PendingApprovalStatus,
    WorkflowStatusSummary,
    recovery_state_for,
)
from enterprise_agent.domain import UserId, WorkflowStatus

_SELECT_PENDING_APPROVALS = text("""
    SELECT
        approvals.id AS approval_id,
        plans.id AS plan_id,
        requester.display_name AS requester,
        approver.display_name AS approver,
        approvals.status AS decision_state,
        approvals.expires_at,
        latest_audit.run_id AS audit_run_id
    FROM approvals
    JOIN plans ON plans.id = approvals.plan_id
    JOIN users AS requester ON requester.id = approvals.requester_id
    JOIN users AS approver ON approver.id = approvals.approver_id
    LEFT JOIN LATERAL (
        SELECT audit_events.run_id
        FROM audit_events
        WHERE audit_events.plan_id = plans.id
        ORDER BY audit_events.occurred_at DESC, audit_events.id DESC
        LIMIT 1
    ) AS latest_audit ON TRUE
    WHERE approvals.status IN ('pending', 'rerouted')
    ORDER BY approvals.expires_at ASC, approvals.id ASC
""")

_SELECT_WORKFLOWS = text("""
    SELECT
        workflow_instances.id AS workflow_id,
        workflow_instances.status,
        workflow_instances.current_step,
        workflow_instances.lease_expires_at,
        next_step.step_name,
        next_step.idempotency_key
    FROM workflow_instances
    LEFT JOIN LATERAL (
        SELECT workflow_steps.step_name, workflow_steps.idempotency_key
        FROM workflow_steps
        WHERE workflow_steps.workflow_instance_id = workflow_instances.id
          AND workflow_steps.step_index = workflow_instances.current_step + 1
        LIMIT 1
    ) AS next_step ON TRUE
    ORDER BY workflow_instances.updated_at DESC, workflow_instances.id ASC
""")

_SELECT_DEMO_CLOCK = text("SELECT current_at FROM demo_clock WHERE id = 1")

_SELECT_PENDING_APPROVALS_FOR_ACTOR = text(
    str(_SELECT_PENDING_APPROVALS).replace(
        "WHERE approvals.status IN ('pending', 'rerouted')",
        """WHERE approvals.status IN ('pending', 'rerouted')
          AND (
              approvals.requester_id = CAST(:actor_id AS UUID)
              OR approvals.approver_id = CAST(:actor_id AS UUID)
          )""",
    )
)
_SELECT_WORKFLOWS_FOR_ACTOR = text(
    str(_SELECT_WORKFLOWS)
    .replace(
        "FROM workflow_instances",
        """FROM workflow_instances
    JOIN plans ON plans.id = workflow_instances.plan_id""",
    )
    .replace(
        "ORDER BY workflow_instances.updated_at DESC, workflow_instances.id ASC",
        """WHERE plans.actor_id = CAST(:actor_id AS UUID)
       OR plans.approver_id = CAST(:actor_id AS UUID)
    ORDER BY workflow_instances.updated_at DESC, workflow_instances.id ASC""",
    )
)


class PostgresOperatorStatusAdapter:
    """Project narrowly selected, safe state from the local durable control plane."""

    def __init__(self, database_url: str) -> None:
        """Connect the read model to the configured local database without performing a write."""
        self._engine: Engine = create_engine(database_url)

    def read_status(self) -> OperatorStatusSnapshot:
        """Return pending approvals and durable workflow/recovery facts in stable display order."""
        return self._read_status(actor_id=None)

    def read_status_for_actor(self, actor_id: UserId) -> OperatorStatusSnapshot:
        """Return only pending approvals and workflows visible to one selected local demo actor."""
        return self._read_status(actor_id=actor_id)

    def _read_status(self, *, actor_id: UserId | None) -> OperatorStatusSnapshot:
        """Use the same safe projection for global CLI status and actor-scoped local UI status."""
        with self._engine.connect() as connection:
            clock_value = connection.execute(_SELECT_DEMO_CLOCK).scalar_one_or_none()
            now = cast(datetime | None, clock_value)
            if actor_id is None:
                approval_rows = connection.execute(_SELECT_PENDING_APPROVALS).mappings().all()
                workflow_rows = connection.execute(_SELECT_WORKFLOWS).mappings().all()
            else:
                parameters = {"actor_id": str(actor_id)}
                approval_rows = (
                    connection.execute(_SELECT_PENDING_APPROVALS_FOR_ACTOR, parameters)
                    .mappings()
                    .all()
                )
                workflow_rows = (
                    connection.execute(_SELECT_WORKFLOWS_FOR_ACTOR, parameters).mappings().all()
                )
        return OperatorStatusSnapshot(
            pending_approvals=tuple(_approval_from_row(row) for row in approval_rows),
            workflows=tuple(_workflow_from_row(row, now=now) for row in workflow_rows),
        )


def _approval_from_row(row: RowMapping) -> PendingApprovalStatus:
    """Map the selective approval projection without loading immutable plan parameters."""
    audit_run_id = row["audit_run_id"]
    return PendingApprovalStatus(
        approval_id=str(row["approval_id"]),
        plan_id=str(row["plan_id"]),
        requester=cast(str, row["requester"]),
        approver=cast(str, row["approver"]),
        decision_state=cast(str, row["decision_state"]),
        expires_at=cast(datetime, row["expires_at"]).isoformat(),
        audit_run_id=None if audit_run_id is None else cast(str, audit_run_id),
    )


def _workflow_from_row(row: RowMapping, *, now: datetime | None) -> WorkflowStatusSummary:
    """Map a workflow cursor and recovery category while withholding error and result payloads."""
    status = WorkflowStatus(cast(str, row["status"]))
    is_pending_approval = status is WorkflowStatus.PENDING
    return WorkflowStatusSummary(
        workflow_id=str(row["workflow_id"]),
        status=status.value,
        current_step=(
            "awaiting approval"
            if is_pending_approval
            else cast(str | None, row["step_name"]) or "workflow complete"
        ),
        idempotency_key_prefix=(
            "not started"
            if is_pending_approval
            else _idempotency_key_prefix(cast(str | None, row["idempotency_key"]))
        ),
        recovery_state=recovery_state_for(
            status,
            lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
            now=now,
        ),
    )


def _idempotency_key_prefix(value: str | None) -> str:
    """Keep a readable bounded identifier while never exposing any workflow input or result."""
    if not value:
        return "not started"
    return value[:24]
