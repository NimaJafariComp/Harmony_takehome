"""Seeded end-to-end control-plane contract for Scenario C supplier-risk holds."""

from __future__ import annotations

import subprocess

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for the Scenario C control contract."""
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
def test_seeded_scenario_c_hold_requires_approval_then_uses_shared_tool_workflow(
    disposable_database: str,
) -> None:
    """Supplier risk reaches the generic durable workflow without a scenario-specific executor path."""
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
        "from enterprise_agent.application.planning import HoldAndNotifyRecommendation\n"
        "from enterprise_agent.application.scenario_c_context import ScenarioCContextAssembler\n"
        "from enterprise_agent.application.scenario_c_control import ScenarioCControlService\n"
        "from enterprise_agent.application.supplier_risk import SupplierRiskDetector\n"
        "from enterprise_agent.application.tools import NotifyProductionInput, PlacePurchaseOrderHoldInput\n"
        "from enterprise_agent.application.workflow_executor import (\n"
        "    ScenarioAWorkflowExecutor, WorkflowExecutionRejectedError,\n"
        ")\n"
        "from enterprise_agent.application.workflow_state import WorkflowStateService\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId, WorkflowStatus\n"
        "from enterprise_agent.seed import ID_DANA, ID_PO_C9001_W, ID_PRODUCTION_C9001, reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "run_id = RunId('run-scenario-c-control-plane')\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "clock = PostgresDemoClock(database_url)\n"
        "now = clock.now()\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "dana = identity.actor_for(UserId(str(ID_DANA)))\n"
        "assert {'erp:po:hold', 'production:notify'} <= dana.scopes\n"
        "knowledge = PostgresKnowledgeAdapter(database_url)\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "attention = PostgresAttentionAdapter(database_url)\n"
        "detections = SupplierRiskDetector(knowledge, erp, attention, clock).detect(dana, run_id)\n"
        "assert len(detections) == 1\n"
        "detection = detections[0]\n"
        "context = ScenarioCContextAssembler(identity, knowledge, erp).assemble(\n"
        "    user_id=dana.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger\n"
        ")\n"
        "assert context.purchase_order.record_id == str(ID_PO_C9001_W)\n"
        "assert context.production_order.record_id == str(ID_PRODUCTION_C9001)\n"
        "recommendation = HoldAndNotifyRecommendation(\n"
        "    outcome='HOLD_AND_NOTIFY',\n"
        "    hold_purchase_order=PlacePurchaseOrderHoldInput(\n"
        "        purchase_order_id=context.purchase_order.record_id,\n"
        "        production_order_id=context.production_order.record_id,\n"
        "        expected_purchase_order_version=context.purchase_order.source_version,\n"
        "    ),\n"
        "    notify_production=NotifyProductionInput(\n"
        "        production_order_id=context.production_order.record_id,\n"
        "        message='Supplier-risk bulletin requires a temporary purchase-order hold and review.',\n"
        "    ),\n"
        "    rationale='The current supplier-risk bulletin affects an open PO before production starts.',\n"
        ")\n"
        "audit = PostgresAuditAdapter(database_url)\n"
        "approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "approval_service = PlanApprovalService(approvals, audit=audit)\n"
        "workflows = PostgresWorkflowStateAdapter(database_url)\n"
        "result = ScenarioCControlService(\n"
        "    approvals=approval_service, workflow_state=WorkflowStateService(workflows)\n"
        ").request_pending(\n"
        "    context=context, recommendation=recommendation,\n"
        "    current_source_versions=context.source_versions, policy_version='scenario_c_policy:v1',\n"
        "    requested_at=now, expires_at=now + timedelta(hours=4), run_id=run_id,\n"
        ")\n"
        "assert result.pending is not None and result.workflow is not None\n"
        "executor = ScenarioAWorkflowExecutor(\n"
        "    workflow_store=workflows, approvals=approvals, identity=identity,\n"
        "    tool_executor=PostgresToolAdapter(database_url), audit=audit,\n"
        ")\n"
        "try:\n"
        "    executor.begin_next_tool(\n"
        "        result.workflow.workflow.workflow_id, worker_id='supplier-risk-worker',\n"
        "        now=now + timedelta(minutes=1), lease_expires_at=now + timedelta(minutes=20),\n"
        "        current_source_versions=context.source_versions,\n"
        "    )\n"
        "except WorkflowExecutionRejectedError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('pending Scenario C plan executed before approval')\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    status = connection.execute(text(\"SELECT status FROM purchase_orders WHERE id = CAST(:po_id AS UUID)\"), {'po_id': str(ID_PO_C9001_W)}).scalar_one()\n"
        "    notices = connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'tool:v1:%:notify_production:%'\")).scalar_one()\n"
        "assert status == 'open' and notices == 0\n"
        "approved = approval_service.approve(\n"
        "    approval_id=result.pending.approval.approval_id, expected_plan_hash=result.pending.plan.plan_hash,\n"
        "    decider_id=dana.user_id, current_source_versions=context.source_versions,\n"
        "    decided_at=now + timedelta(minutes=2), run_id=run_id,\n"
        ")\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "started = executor.begin_next_tool(\n"
        "    result.workflow.workflow.workflow_id, worker_id='supplier-risk-worker',\n"
        "    now=now + timedelta(minutes=3), lease_expires_at=now + timedelta(minutes=20),\n"
        "    current_source_versions=context.source_versions,\n"
        ")\n"
        "executor.execute_started_tool(started, worker_id='supplier-risk-worker', completed_at=now + timedelta(minutes=4))\n"
        "started = executor.begin_next_tool(\n"
        "    result.workflow.workflow.workflow_id, worker_id='supplier-risk-worker',\n"
        "    now=now + timedelta(minutes=5), lease_expires_at=now + timedelta(minutes=20),\n"
        "    current_source_versions=context.source_versions,\n"
        ")\n"
        "completed = executor.execute_started_tool(\n"
        "    started, worker_id='supplier-risk-worker', completed_at=now + timedelta(minutes=6)\n"
        ")\n"
        "assert completed.workflow.status is WorkflowStatus.SUCCEEDED\n"
        "with engine.connect() as connection:\n"
        "    purchase_order = connection.execute(text(\"SELECT status, source_version FROM purchase_orders WHERE id = CAST(:po_id AS UUID)\"), {'po_id': str(ID_PO_C9001_W)}).mappings().one()\n"
        "    notices = connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'tool:v1:%:notify_production:%'\")).scalar_one()\n"
        "    invocations = connection.execute(text(\"SELECT COUNT(*) FROM tool_invocations WHERE status = 'succeeded'\")).scalar_one()\n"
        "assert purchase_order['status'] == 'on_hold' and purchase_order['source_version'] == 2\n"
        "assert notices == 1 and invocations == 2\n"
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
