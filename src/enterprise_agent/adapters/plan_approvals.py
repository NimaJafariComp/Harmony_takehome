"""PostgreSQL persistence for immutable plans and their hash-bound approval decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, RowMapping

from enterprise_agent.domain import (
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionId,
    Plan,
    PlanId,
    UserId,
)

INSERT_PLAN = text("""
    INSERT INTO plans (
        id, attention_id, actor_id, approver_id, intent, workflow_name, workflow_version,
        parameters, source_versions, policy_version, plan_hash, created_at, expires_at
    ) VALUES (
        CAST(:plan_id AS UUID), CAST(:attention_id AS UUID), CAST(:actor_id AS UUID),
        CAST(:approver_id AS UUID), :intent, :workflow_name, :workflow_version,
        CAST(:parameters AS JSONB), CAST(:source_versions AS JSONB), :policy_version,
        :plan_hash, :created_at, :expires_at
    )
""")
INSERT_APPROVAL = text("""
    INSERT INTO approvals (
        id, plan_id, plan_hash, requester_id, approver_id, status, requested_at, expires_at,
        decided_at
    ) VALUES (
        CAST(:approval_id AS UUID), CAST(:plan_id AS UUID), :plan_hash,
        CAST(:requester_id AS UUID), CAST(:approver_id AS UUID), :status, :requested_at,
        :expires_at, NULL
    )
""")
SELECT_PLAN_AND_APPROVAL = text("""
    SELECT
        plans.id AS plan_id,
        plans.attention_id,
        plans.actor_id,
        plans.approver_id AS plan_approver_id,
        plans.intent,
        plans.workflow_name,
        plans.workflow_version,
        plans.parameters,
        plans.source_versions,
        plans.policy_version,
        plans.plan_hash AS persisted_plan_hash,
        plans.created_at AS plan_created_at,
        plans.expires_at AS plan_expires_at,
        approvals.id AS approval_id,
        approvals.plan_hash AS approval_plan_hash,
        approvals.requester_id,
        approvals.approver_id AS approval_approver_id,
        approvals.status AS approval_status,
        approvals.requested_at,
        approvals.expires_at AS approval_expires_at,
        approvals.decided_at
    FROM approvals
    JOIN plans ON plans.id = approvals.plan_id
    WHERE approvals.id = CAST(:approval_id AS UUID)
""")
SELECT_PLAN_AND_APPROVAL_FOR_PLAN = text("""
    SELECT
        plans.id AS plan_id,
        plans.attention_id,
        plans.actor_id,
        plans.approver_id AS plan_approver_id,
        plans.intent,
        plans.workflow_name,
        plans.workflow_version,
        plans.parameters,
        plans.source_versions,
        plans.policy_version,
        plans.plan_hash AS persisted_plan_hash,
        plans.created_at AS plan_created_at,
        plans.expires_at AS plan_expires_at,
        approvals.id AS approval_id,
        approvals.plan_hash AS approval_plan_hash,
        approvals.requester_id,
        approvals.approver_id AS approval_approver_id,
        approvals.status AS approval_status,
        approvals.requested_at,
        approvals.expires_at AS approval_expires_at,
        approvals.decided_at
    FROM approvals
    JOIN plans ON plans.id = approvals.plan_id
    WHERE approvals.plan_id = CAST(:plan_id AS UUID)
""")
APPROVE_PENDING = text("""
    UPDATE approvals
    SET status = :approved_status, decided_at = :decided_at
    WHERE id = CAST(:approval_id AS UUID)
      AND status = :pending_status
      AND plan_hash = :expected_plan_hash
      AND expires_at > :decided_at
    RETURNING id, plan_id, plan_hash, requester_id, approver_id, status, requested_at, expires_at,
              decided_at
""")


class PostgresPlanApprovalAdapter:
    """Store plans and approvals in one transaction while preserving database-side immutability."""

    def __init__(self, database_url: str) -> None:
        """Connect this durable plan/approval adapter to one PostgreSQL database."""
        self._engine: Engine = create_engine(database_url)

    def create_pending(self, plan: Plan, approval: Approval) -> None:
        """Persist exactly one immutable plan with its pending approval in a single transaction."""
        with self._engine.begin() as connection:
            connection.execute(INSERT_PLAN, _plan_parameters(plan))
            connection.execute(INSERT_APPROVAL, _approval_parameters(approval))

    def load(self, approval_id: ApprovalId) -> tuple[Plan, Approval] | None:
        """Load the exact stored binding that application policy must validate before approval."""
        with self._engine.connect() as connection:
            row = (
                connection.execute(SELECT_PLAN_AND_APPROVAL, {"approval_id": str(approval_id)})
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return _plan_from_row(row), _approval_from_join_row(row)

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        """Load the unique plan/approval binding that authorizes one workflow instance."""
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    SELECT_PLAN_AND_APPROVAL_FOR_PLAN,
                    {"plan_id": str(plan_id)},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return _plan_from_row(row), _approval_from_join_row(row)

    def approve(
        self,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decided_at: datetime,
    ) -> Approval | None:
        """Atomically advance only an unexpired pending approval with the requested plan hash."""
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    APPROVE_PENDING,
                    {
                        "approval_id": str(approval_id),
                        "expected_plan_hash": expected_plan_hash,
                        "approved_status": ApprovalStatus.APPROVED.value,
                        "pending_status": ApprovalStatus.PENDING.value,
                        "decided_at": decided_at,
                    },
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _approval_from_row(row)


def _plan_parameters(plan: Plan) -> dict[str, object]:
    """Serialize only immutable plan fields into bound SQL parameters."""
    return {
        "plan_id": str(plan.plan_id),
        "attention_id": str(plan.attention_id),
        "actor_id": str(plan.actor_id),
        "approver_id": str(plan.approver_id),
        "intent": plan.intent,
        "workflow_name": plan.workflow_name,
        "workflow_version": plan.workflow_version,
        "parameters": json.dumps(dict(plan.parameters), sort_keys=True),
        "source_versions": json.dumps(dict(plan.source_versions), sort_keys=True),
        "policy_version": plan.policy_version,
        "plan_hash": plan.plan_hash,
        "created_at": plan.created_at,
        "expires_at": plan.expires_at,
    }


def _approval_parameters(approval: Approval) -> dict[str, object]:
    """Serialize the pending approval without exposing mutable caller SQL construction."""
    return {
        "approval_id": str(approval.approval_id),
        "plan_id": str(approval.plan_id),
        "plan_hash": approval.plan_hash,
        "requester_id": str(approval.requester_id),
        "approver_id": str(approval.approver_id),
        "status": approval.status.value,
        "requested_at": approval.requested_at,
        "expires_at": approval.expires_at,
    }


def _plan_from_row(row: RowMapping) -> Plan:
    """Map the plan side of a joined database row into an immutable domain record."""
    return Plan(
        plan_id=PlanId(str(row["plan_id"])),
        attention_id=AttentionId(str(row["attention_id"])),
        actor_id=UserId(str(row["actor_id"])),
        approver_id=UserId(str(row["plan_approver_id"])),
        intent=cast(str, row["intent"]),
        workflow_name=cast(str | None, row["workflow_name"]),
        workflow_version=cast(int | None, row["workflow_version"]),
        parameters=dict(cast(Mapping[str, object], row["parameters"])),
        source_versions=dict(cast(Mapping[str, int], row["source_versions"])),
        policy_version=cast(str, row["policy_version"]),
        plan_hash=cast(str, row["persisted_plan_hash"]),
        created_at=cast(datetime, row["plan_created_at"]),
        expires_at=cast(datetime, row["plan_expires_at"]),
    )


def _approval_from_join_row(row: RowMapping) -> Approval:
    """Map the approval side of a joined database row into its immutable domain counterpart."""
    return Approval(
        approval_id=ApprovalId(str(row["approval_id"])),
        plan_id=PlanId(str(row["plan_id"])),
        plan_hash=cast(str, row["approval_plan_hash"]),
        requester_id=UserId(str(row["requester_id"])),
        approver_id=UserId(str(row["approval_approver_id"])),
        status=ApprovalStatus(cast(str, row["approval_status"])),
        requested_at=cast(datetime, row["requested_at"]),
        expires_at=cast(datetime, row["approval_expires_at"]),
        decided_at=cast(datetime | None, row["decided_at"]),
    )


def _approval_from_row(row: RowMapping) -> Approval:
    """Map an approval-update row into its immutable domain counterpart."""
    return Approval(
        approval_id=ApprovalId(str(row["id"])),
        plan_id=PlanId(str(row["plan_id"])),
        plan_hash=cast(str, row["plan_hash"]),
        requester_id=UserId(str(row["requester_id"])),
        approver_id=UserId(str(row["approver_id"])),
        status=ApprovalStatus(cast(str, row["status"])),
        requested_at=cast(datetime, row["requested_at"]),
        expires_at=cast(datetime, row["expires_at"]),
        decided_at=cast(datetime | None, row["decided_at"]),
    )
