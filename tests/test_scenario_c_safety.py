"""Freshness and compensation contracts for Scenario C's registered hold tool."""

from __future__ import annotations

import subprocess

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for Scenario C safety regressions."""
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
def test_scenario_c_stale_approval_and_failed_notification_cannot_leave_a_hold(
    disposable_database: str,
) -> None:
    """Fresh approval evidence is mandatory, and a later notification failure restores the PO."""
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
        "from enterprise_agent.application.approvals import PlanApprovalService, PlanNotApprovableError\n"
        "from enterprise_agent.application.planning import HoldAndNotifyRecommendation\n"
        "from enterprise_agent.application.scenario_c_context import ScenarioCContextAssembler\n"
        "from enterprise_agent.application.scenario_c_control import ScenarioCControlService\n"
        "from enterprise_agent.application.supplier_risk import SupplierRiskDetector\n"
        "from enterprise_agent.application.tools import (\n"
        "    NotifyProductionInput, PlacePurchaseOrderHoldInput, TerminalToolExecutionError,\n"
        ")\n"
        "from enterprise_agent.application.workflow_executor import ScenarioAWorkflowExecutor\n"
        "from enterprise_agent.application.workflow_state import WorkflowStateService\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId, WorkflowStatus\n"
        "from enterprise_agent.seed import ID_DANA, ID_PO_C9001_W, ID_PRODUCTION_C9001, reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "def stage(run_id):\n"
        "    clock = PostgresDemoClock(database_url)\n"
        "    now = clock.now()\n"
        "    identity = PostgresIdentityAdapter(database_url)\n"
        "    dana = identity.actor_for(UserId(str(ID_DANA)))\n"
        "    knowledge = PostgresKnowledgeAdapter(database_url)\n"
        "    erp = PostgresErpAdapter(database_url)\n"
        "    detection = SupplierRiskDetector(knowledge, erp, PostgresAttentionAdapter(database_url), clock).detect(dana, run_id)[0]\n"
        "    context = ScenarioCContextAssembler(identity, knowledge, erp).assemble(\n"
        "        user_id=dana.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger\n"
        "    )\n"
        "    recommendation = HoldAndNotifyRecommendation(\n"
        "        outcome='HOLD_AND_NOTIFY',\n"
        "        hold_purchase_order=PlacePurchaseOrderHoldInput(\n"
        "            purchase_order_id=context.purchase_order.record_id,\n"
        "            production_order_id=context.production_order.record_id,\n"
        "            expected_purchase_order_version=context.purchase_order.source_version,\n"
        "        ),\n"
        "        notify_production=NotifyProductionInput(\n"
        "            production_order_id=context.production_order.record_id, message='Hold and review the affected PO.'\n"
        "        ),\n"
        "        rationale='The active bulletin affects an open purchase order.',\n"
        "    )\n"
        "    audit = PostgresAuditAdapter(database_url)\n"
        "    approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "    approval_service = PlanApprovalService(approvals, audit=audit)\n"
        "    workflows = PostgresWorkflowStateAdapter(database_url)\n"
        "    result = ScenarioCControlService(\n"
        "        approvals=approval_service, workflow_state=WorkflowStateService(workflows)\n"
        "    ).request_pending(\n"
        "        context=context, recommendation=recommendation, current_source_versions=context.source_versions,\n"
        "        policy_version='scenario_c_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4), run_id=run_id,\n"
        "    )\n"
        "    assert result.pending is not None and result.workflow is not None\n"
        "    return now, dana, context, audit, approvals, approval_service, workflows, result\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "now, dana, context, audit, approvals, approval_service, workflows, pending = stage(RunId('run-scenario-c-stale'))\n"
        "stale_versions = dict(context.source_versions)\n"
        "stale_versions[str(context.purchase_order.evidence_id)] += 1\n"
        "try:\n"
        "    approval_service.approve(\n"
        "        approval_id=pending.pending.approval.approval_id, expected_plan_hash=pending.pending.plan.plan_hash,\n"
        "        decider_id=dana.user_id, current_source_versions=stale_versions, decided_at=now + timedelta(minutes=1),\n"
        "    )\n"
        "except PlanNotApprovableError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('stale Scenario C plan was approved')\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    assert connection.execute(text(\"SELECT status FROM purchase_orders WHERE id = CAST(:po_id AS UUID)\"), {'po_id': str(ID_PO_C9001_W)}).scalar_one() == 'open'\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "run_id = RunId('run-scenario-c-compensation')\n"
        "now, dana, context, audit, approvals, approval_service, workflows, pending = stage(run_id)\n"
        "approved = approval_service.approve(\n"
        "    approval_id=pending.pending.approval.approval_id, expected_plan_hash=pending.pending.plan.plan_hash,\n"
        "    decider_id=dana.user_id, current_source_versions=context.source_versions, decided_at=now + timedelta(minutes=1), run_id=run_id,\n"
        ")\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "tools = PostgresToolAdapter(database_url)\n"
        "executor = ScenarioAWorkflowExecutor(\n"
        "    workflow_store=workflows, approvals=approvals, identity=PostgresIdentityAdapter(database_url),\n"
        "    tool_executor=tools, audit=audit,\n"
        ")\n"
        "workflow_id = pending.workflow.workflow.workflow_id\n"
        "hold = executor.begin_next_tool(\n"
        "    workflow_id, worker_id='supplier-risk-worker', now=now + timedelta(minutes=2),\n"
        "    lease_expires_at=now + timedelta(minutes=20), current_source_versions=context.source_versions,\n"
        ")\n"
        "executor.execute_started_tool(hold, worker_id='supplier-risk-worker', completed_at=now + timedelta(minutes=3))\n"
        "with engine.begin() as connection:\n"
        "    connection.execute(text(\"UPDATE production_orders SET status = 'cancelled' WHERE id = CAST(:production_id AS UUID)\"), {'production_id': str(ID_PRODUCTION_C9001)})\n"
        "notification = executor.begin_next_tool(\n"
        "    workflow_id, worker_id='supplier-risk-worker', now=now + timedelta(minutes=4),\n"
        "    lease_expires_at=now + timedelta(minutes=20), current_source_versions=context.source_versions,\n"
        ")\n"
        "try:\n"
        "    executor.execute_started_tool(\n"
        "        notification, worker_id='supplier-risk-worker', completed_at=now + timedelta(minutes=5)\n"
        "    )\n"
        "except TerminalToolExecutionError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('cancelled production accepted a Scenario C notification')\n"
        "compensated = workflows.load(workflow_id)\n"
        "assert compensated is not None and compensated.workflow.status is WorkflowStatus.COMPENSATED\n"
        "with engine.connect() as connection:\n"
        "    po = connection.execute(text(\"SELECT status, source_version FROM purchase_orders WHERE id = CAST(:po_id AS UUID)\"), {'po_id': str(ID_PO_C9001_W)}).mappings().one()\n"
        "    hold_status = connection.execute(text(\"SELECT status FROM tool_invocations WHERE tool_name = 'place_purchase_order_hold'\")).scalar_one()\n"
        "assert po['status'] == 'open' and po['source_version'] == 3 and hold_status == 'compensated'\n"
        "event_types = [event.event_type for event in audit.events_for_run(run_id)]\n"
        "assert set(('tool.failed', 'workflow.failed', 'compensation.started', 'compensation.completed')) <= set(event_types)\n"
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
