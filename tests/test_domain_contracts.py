"""Stable domain-contract tests."""

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    AuditEvent,
    AuditEventId,
    DateRange,
    Evidence,
    EvidenceId,
    Money,
    Plan,
    PlanId,
    PlantId,
    RunId,
    ScheduledTask,
    ScheduledTaskId,
    ScheduledTaskStatus,
    Scope,
    ToolInvocation,
    ToolInvocationId,
    ToolInvocationStatus,
    UserId,
    WorkflowId,
    WorkflowState,
    WorkflowStateSnapshot,
    WorkflowStatus,
    WorkflowStepId,
    WorkflowStepState,
    WorkflowStepStatus,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)


def test_money_and_date_range_are_validated_value_objects() -> None:
    """Amount/currency and date ordering are validated once at the boundary."""
    assert Money(amount=Decimal("12.50"), currency="usd").currency == "USD"
    assert DateRange(start=date(2026, 9, 2), end=date(2026, 9, 9)).days == 7

    with pytest.raises(ValueError, match="non-negative"):
        Money(amount=Decimal("-0.01"), currency="USD")
    with pytest.raises(ValueError, match="three uppercase letters"):
        Money(amount=Decimal(1), currency="US")
    with pytest.raises(ValueError, match="must not precede"):
        DateRange(start=date(2026, 9, 9), end=date(2026, 9, 2))


def test_planning_contracts_bind_actor_evidence_plan_and_approval_immutably() -> None:
    """Planning records retain the scope and source-version facts used for approval."""
    actor = ActorContext(
        user_id=UserId("dana"),
        role="purchasing_manager",
        scopes=frozenset({Scope("erp:po:read"), Scope("erp:po:create")}),
        plant_ids=frozenset({PlantId("plant-a")}),
        backup_approver_id=UserId("alex"),
        approval_limits={"usd": Decimal(25000)},
    )
    evidence = Evidence(
        evidence_id=EvidenceId("evidence-delay-email"),
        source="mail",
        record_type="supplier_shipment_update",
        record_id="message-42",
        source_version=3,
        observed_at=NOW,
        payload={"arrival_date": "2026-09-08"},
    )
    attention = AttentionItem(
        attention_id=AttentionId("attention-stockout"),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key="stockout:part-x:4812:v3",
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions={"inventory:part-x": 3},
    )
    plan = Plan(
        plan_id=PlanId("plan-reroute"),
        attention_id=attention.attention_id,
        actor_id=actor.user_id,
        approver_id=UserId("dana"),
        intent="enter_workflow",
        workflow_name="po_reroute",
        workflow_version=1,
        parameters={"supplier_id": "supplier-z", "quantity": "100"},
        source_versions={"purchase_order:po-7": 2},
        policy_version="policy-v1",
        plan_hash="sha256:plan-reroute",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )
    approval = Approval(
        approval_id=ApprovalId("approval-reroute"),
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        requester_id=actor.user_id,
        approver_id=UserId("dana"),
        status=ApprovalStatus.PENDING,
        requested_at=NOW,
        expires_at=plan.expires_at,
    )

    assert actor.approval_limit_for("USD") == Decimal(25000)
    assert evidence.payload["arrival_date"] == "2026-09-08"
    assert approval.plan_hash == plan.plan_hash
    with pytest.raises(FrozenInstanceError):
        actor.__setattr__("role", "other")
    with pytest.raises(TypeError):
        cast(dict[str, Decimal], actor.approval_limits)["USD"] = Decimal(1)


def test_operational_contracts_capture_workflow_scheduler_and_audit_state() -> None:
    """Execution, scheduling, and audit records carry stable identifiers and state."""
    workflow = WorkflowState(
        workflow_id=WorkflowId("workflow-reroute"),
        plan_id=PlanId("plan-reroute"),
        definition_name="po_reroute",
        definition_version=1,
        status=WorkflowStatus.RUNNING,
        current_step=3,
        started_at=NOW,
        completed_at=None,
        last_error=None,
        lease_owner=None,
        lease_expires_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    step = WorkflowStepState(
        step_id=WorkflowStepId("workflow-step-3"),
        workflow_id=workflow.workflow_id,
        step_index=3,
        step_name="create_replacement_po",
        tool_name="create_replacement_po",
        status=WorkflowStepStatus.SUCCEEDED,
        idempotency_key="workflow-reroute:3",
        input={"supplier_id": "supplier-z"},
        result={"replacement_po_id": "po-9"},
        error=None,
        attempt_count=1,
        started_at=NOW,
        completed_at=NOW,
        lease_owner=None,
        lease_expires_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    snapshot = WorkflowStateSnapshot(workflow=workflow, steps=(step,))
    invocation = ToolInvocation(
        invocation_id=ToolInvocationId("tool-create-po"),
        workflow_id=workflow.workflow_id,
        tool_name="create_replacement_po",
        idempotency_key="workflow-reroute:3",
        status=ToolInvocationStatus.SUCCEEDED,
        parameters={"supplier_id": "supplier-z"},
        result={"replacement_po_id": "po-9"},
        attempt_count=1,
        started_at=NOW,
        completed_at=NOW,
    )
    task = ScheduledTask(
        task_id=ScheduledTaskId("task-arrival-check"),
        task_type="arrival_check",
        due_at=NOW + timedelta(days=6),
        status=ScheduledTaskStatus.PENDING,
        idempotency_key="po-9:tuesday-arrival-check",
        payload={"purchase_order_id": "po-9"},
        attempt_count=0,
        lease_expires_at=None,
        completed_at=None,
    )
    event = AuditEvent(
        event_id=AuditEventId("audit-tool-succeeded"),
        occurred_at=NOW,
        event_type="tool.succeeded",
        run_id=RunId("run-scenario-a"),
        actor_id=UserId("dana"),
        attention_id=AttentionId("attention-stockout"),
        workflow_id=workflow.workflow_id,
        plan_id=PlanId("plan-reroute"),
        evidence_ids=(EvidenceId("evidence-delay-email"),),
        payload={"tool_name": invocation.tool_name},
        policy_version="policy-v1",
        plan_hash="sha256:plan-reroute",
        idempotency_key=invocation.idempotency_key,
        failure_category=None,
    )

    assert workflow.current_step == 3
    assert snapshot.steps[0].result == {"replacement_po_id": "po-9"}
    assert invocation.status is ToolInvocationStatus.SUCCEEDED
    assert task.status is ScheduledTaskStatus.PENDING
    assert event.evidence_ids == (EvidenceId("evidence-delay-email"),)
