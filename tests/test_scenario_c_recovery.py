"""Durability contract for a Scenario C hold that commits before its worker records completion."""

from __future__ import annotations

import subprocess

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for the Scenario C recovery contract."""
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
@pytest.mark.scenario
def test_scenario_c_crash_after_hold_replays_once_then_finishes_and_explains(
    disposable_database: str,
) -> None:
    """A committed hold survives a crash without a duplicate effect or an unexplained workflow."""
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
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter, PostgresAuditAdapter, PostgresDemoClock, PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter, PostgresKnowledgeAdapter, PostgresPlanApprovalAdapter,\n"
        "    PostgresToolAdapter, PostgresWorkflowStateAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approvals import PlanApprovalService\n"
        "from enterprise_agent.application.audit_explain import AuditExplainer\n"
        "from enterprise_agent.application.planning import HoldAndNotifyRecommendation\n"
        "from enterprise_agent.application.scenario_c_context import ScenarioCContextAssembler\n"
        "from enterprise_agent.application.scenario_c_control import ScenarioCControlService\n"
        "from enterprise_agent.application.supplier_risk import SupplierRiskDetector\n"
        "from enterprise_agent.application.tools import NotifyProductionInput, PlacePurchaseOrderHoldInput, ToolName\n"
        "from enterprise_agent.application.workflow_executor import (\n"
        "    DeterministicCrashInjector, ScenarioAWorkflowExecutor, WorkflowCrashInjectedError,\n"
        ")\n"
        "from enterprise_agent.application.workflow_state import WorkflowStateService\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId, WorkflowStatus, WorkflowStepStatus\n"
        "from enterprise_agent.seed import ID_DANA, ID_PO_C9001_W, reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "run_id = RunId('run-scenario-c-crash-recovery')\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "clock = PostgresDemoClock(database_url)\n"
        "now = clock.now()\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "dana = identity.actor_for(UserId(str(ID_DANA)))\n"
        "knowledge = PostgresKnowledgeAdapter(database_url)\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "detection = SupplierRiskDetector(knowledge, erp, PostgresAttentionAdapter(database_url), clock).detect(dana, run_id)[0]\n"
        "context = ScenarioCContextAssembler(identity, knowledge, erp).assemble(\n"
        "    user_id=dana.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger\n"
        ")\n"
        "recommendation = HoldAndNotifyRecommendation(\n"
        "    outcome='HOLD_AND_NOTIFY',\n"
        "    hold_purchase_order=PlacePurchaseOrderHoldInput(\n"
        "        purchase_order_id=context.purchase_order.record_id,\n"
        "        production_order_id=context.production_order.record_id,\n"
        "        expected_purchase_order_version=context.purchase_order.source_version,\n"
        "    ),\n"
        "    notify_production=NotifyProductionInput(\n"
        "        production_order_id=context.production_order.record_id, message='Hold and review the affected PO.'\n"
        "    ),\n"
        "    rationale='The current supplier-risk bulletin affects this open purchase order.',\n"
        ")\n"
        "audit = PostgresAuditAdapter(database_url)\n"
        "approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "approval_service = PlanApprovalService(approvals, audit=audit)\n"
        "workflows = PostgresWorkflowStateAdapter(database_url)\n"
        "pending = ScenarioCControlService(\n"
        "    approvals=approval_service, workflow_state=WorkflowStateService(workflows)\n"
        ").request_pending(\n"
        "    context=context, recommendation=recommendation, current_source_versions=context.source_versions,\n"
        "    policy_version='scenario_c_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4), run_id=run_id,\n"
        ")\n"
        "assert pending.pending is not None and pending.workflow is not None\n"
        "approved = approval_service.approve(\n"
        "    approval_id=pending.pending.approval.approval_id, expected_plan_hash=pending.pending.plan.plan_hash,\n"
        "    decider_id=dana.user_id, current_source_versions=context.source_versions,\n"
        "    decided_at=now + timedelta(minutes=1), run_id=run_id,\n"
        ")\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "tools = PostgresToolAdapter(database_url)\n"
        "crashing = ScenarioAWorkflowExecutor(\n"
        "    workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=tools, audit=audit,\n"
        "    crash_injector=DeterministicCrashInjector(target_tool_name=ToolName.PLACE_PURCHASE_ORDER_HOLD),\n"
        ")\n"
        "workflow_id = pending.workflow.workflow.workflow_id\n"
        "started = crashing.begin_next_tool(\n"
        "    workflow_id, worker_id='supplier-risk-worker-a', now=now + timedelta(minutes=2),\n"
        "    lease_expires_at=now + timedelta(minutes=3), current_source_versions=context.source_versions,\n"
        ")\n"
        "try:\n"
        "    crashing.execute_started_tool(\n"
        "        started, worker_id='supplier-risk-worker-a', completed_at=now + timedelta(minutes=2, seconds=30)\n"
        "    )\n"
        "except WorkflowCrashInjectedError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('hold crash was not injected')\n"
        "crashed = workflows.load(workflow_id)\n"
        "assert crashed is not None and crashed.steps[0].status is WorkflowStepStatus.RUNNING\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    po = connection.execute(text(\"SELECT status, source_version FROM purchase_orders WHERE id = CAST(:po_id AS UUID)\"), {'po_id': str(ID_PO_C9001_W)}).mappings().one()\n"
        "    hold_count = connection.execute(text(\"SELECT COUNT(*) FROM tool_invocations WHERE tool_name = 'place_purchase_order_hold'\")).scalar_one()\n"
        "assert po['status'] == 'on_hold' and po['source_version'] == 2 and hold_count == 1\n"
        "restarted = ScenarioAWorkflowExecutor(\n"
        "    workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=tools, audit=audit\n"
        ")\n"
        "resumed = restarted.begin_next_tool(\n"
        "    workflow_id, worker_id='supplier-risk-worker-b', now=now + timedelta(minutes=4),\n"
        "    lease_expires_at=now + timedelta(minutes=20), current_source_versions=context.source_versions,\n"
        ")\n"
        "assert resumed.invocation.idempotency_key == started.invocation.idempotency_key\n"
        "restarted.execute_started_tool(\n"
        "    resumed, worker_id='supplier-risk-worker-b', completed_at=now + timedelta(minutes=5)\n"
        ")\n"
        "notification = restarted.begin_next_tool(\n"
        "    workflow_id, worker_id='supplier-risk-worker-b', now=now + timedelta(minutes=6),\n"
        "    lease_expires_at=now + timedelta(minutes=20), current_source_versions=context.source_versions,\n"
        ")\n"
        "completed = restarted.execute_started_tool(\n"
        "    notification, worker_id='supplier-risk-worker-b', completed_at=now + timedelta(minutes=7)\n"
        ")\n"
        "assert completed.workflow.status is WorkflowStatus.SUCCEEDED\n"
        "with engine.connect() as connection:\n"
        "    hold_count = connection.execute(text(\"SELECT COUNT(*) FROM tool_invocations WHERE tool_name = 'place_purchase_order_hold'\")).scalar_one()\n"
        "    notices = connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'tool:v1:%:notify_production:%'\")).scalar_one()\n"
        "assert hold_count == 1 and notices == 1\n"
        "event_types = [event.event_type for event in audit.events_for_run(run_id)]\n"
        "assert set(('approval.approved', 'workflow.started', 'tool.started', 'tool.succeeded')) <= set(event_types)\n"
        "rendered = AuditExplainer(audit).explain(run_id).render()\n"
        "assert 'Planner recommended HOLD_AND_NOTIFY using bounded_tool_plan.' in rendered\n"
        "assert 'Tool place_purchase_order_hold succeeded.' in rendered\n"
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
