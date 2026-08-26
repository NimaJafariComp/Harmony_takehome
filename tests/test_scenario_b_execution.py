"""Seeded end-to-end control-plane contract for Scenario B quality holds."""

from __future__ import annotations

import subprocess

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for the Scenario B control contract."""
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
def test_seeded_scenario_b_requires_approval_executes_each_path_once_and_explains_it(
    disposable_database: str,
) -> None:
    """Quality-only evidence drives reviewed no-cover and covered actions with durable recovery."""
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
        "from decimal import Decimal\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter, PostgresAuditAdapter, PostgresDemoClock, PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter, PostgresPlanApprovalAdapter, PostgresQualityAdapter,\n"
        "    PostgresToolAdapter, PostgresWorkflowStateAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approvals import PlanApprovalService\n"
        "from enterprise_agent.application.audit_explain import AuditExplainer\n"
        "from enterprise_agent.application.planning import (\n"
        "    FlagShortageToPurchasingRecommendation, ReallocateAndNotifyRecommendation,\n"
        ")\n"
        "from enterprise_agent.application.quality_context import ScenarioBContextAssembler\n"
        "from enterprise_agent.application.quality_hold import QualityHoldDetector\n"
        "from enterprise_agent.application.scenario_b_control import ScenarioBControlService\n"
        "from enterprise_agent.application.tools import (\n"
        "    FlagShortageToPurchasingInput, NotifyProductionInput, ReallocateLotInput, ToolName,\n"
        ")\n"
        "from enterprise_agent.application.workflow_executor import (\n"
        "    DeterministicCrashInjector, ScenarioAWorkflowExecutor, WorkflowCrashInjectedError,\n"
        "    WorkflowExecutionRejectedError,\n"
        ")\n"
        "from enterprise_agent.application.workflow_state import WorkflowStateService\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId, WorkflowStatus, WorkflowStepStatus\n"
        "from enterprise_agent.ports import EvidenceQuery\n"
        "from enterprise_agent.seed import (\n"
        "    ID_LOT_GOOD, ID_PART_QUALITY, ID_PRODUCTION_Q7001, ID_PRODUCTION_Q7002,\n"
        "    reset_database, seed_database,\n"
        ")\n"
        "database_url = environ['DATABASE_URL']\n"
        "run_id = RunId('run-scenario-b-control-plane')\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "clock = PostgresDemoClock(database_url)\n"
        "now = clock.now()\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "quinn = identity.actor_for(UserId('00000000-0000-0000-0000-000000000003'))\n"
        "assert 'quality:lot:read' in quinn.scopes and 'erp:read' not in quinn.scopes\n"
        "assert PostgresErpAdapter(database_url).query(quinn, EvidenceQuery(record_types=frozenset({'purchase_order'}))) == ()\n"
        "quality = PostgresQualityAdapter(database_url)\n"
        "audit = PostgresAuditAdapter(database_url)\n"
        "detections = QualityHoldDetector(quality, PostgresAttentionAdapter(database_url), clock).detect(quinn, run_id)\n"
        "by_order = {detection.risk.production_order_id: detection for detection in detections}\n"
        "assert set(by_order) == {str(ID_PRODUCTION_Q7001), str(ID_PRODUCTION_Q7002)}\n"
        "assembler = ScenarioBContextAssembler(identity, quality, audit=audit)\n"
        "covered = assembler.assemble(user_id=quinn.user_id, attention=by_order[str(ID_PRODUCTION_Q7001)].registration.attention, trigger=by_order[str(ID_PRODUCTION_Q7001)].risk.trigger, run_id=run_id)\n"
        "no_cover = assembler.assemble(user_id=quinn.user_id, attention=by_order[str(ID_PRODUCTION_Q7002)].registration.attention, trigger=by_order[str(ID_PRODUCTION_Q7002)].risk.trigger, run_id=run_id)\n"
        "assert {item.source for item in covered.evidence} == {'quality'}\n"
        "assert Decimal(str(no_cover.alternative_lots[0].payload['quantity'])) == Decimal('120')\n"
        "assert by_order[str(ID_PRODUCTION_Q7002)].risk.allocated_quantity == Decimal('200')\n"
        "approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "approval_service = PlanApprovalService(approvals, audit=audit)\n"
        "workflows = PostgresWorkflowStateAdapter(database_url)\n"
        "control = ScenarioBControlService(approvals=approval_service, workflow_state=WorkflowStateService(workflows))\n"
        "tools = PostgresToolAdapter(database_url)\n"
        "executor = ScenarioAWorkflowExecutor(workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=tools, audit=audit)\n"
        "no_cover_result = control.request_pending(context=no_cover, recommendation=FlagShortageToPurchasingRecommendation(outcome='FLAG_SHORTAGE_TO_PURCHASING', shortage=FlagShortageToPurchasingInput(production_order_id=str(ID_PRODUCTION_Q7002), part_id=str(ID_PART_QUALITY), shortage_quantity=Decimal('80')), rationale='The released lot covers only 120 of the 200-unit requirement.'), current_source_versions=no_cover.source_versions, policy_version='scenario_b_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4), run_id=run_id)\n"
        "assert no_cover_result.pending is not None and no_cover_result.workflow is not None\n"
        "no_cover_pending = no_cover_result.pending\n"
        "approved = approval_service.approve(approval_id=no_cover_pending.approval.approval_id, expected_plan_hash=no_cover_pending.plan.plan_hash, decider_id=no_cover.production_supervisor_id, current_source_versions=no_cover.source_versions, decided_at=now + timedelta(minutes=1), run_id=run_id)\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "started = executor.begin_next_tool(no_cover_result.workflow.workflow.workflow_id, worker_id='quality-worker-a', now=now + timedelta(minutes=2), lease_expires_at=now + timedelta(minutes=20), current_source_versions=no_cover.source_versions)\n"
        "completed = executor.execute_started_tool(started, worker_id='quality-worker-a', completed_at=now + timedelta(minutes=3))\n"
        "assert completed.workflow.status is WorkflowStatus.SUCCEEDED\n"
        "covered_result = control.request_pending(context=covered, recommendation=ReallocateAndNotifyRecommendation(outcome='REALLOCATE_AND_NOTIFY', reallocate_lot=ReallocateLotInput(quality_lot_id=str(ID_LOT_GOOD), to_production_order_id=str(ID_PRODUCTION_Q7001), quantity=Decimal('80')), notify_production=NotifyProductionInput(production_order_id=str(ID_PRODUCTION_Q7001), message='Released replacement lot will cover the held allocation.'), rationale='The released lot covers the held 80-unit allocation.'), current_source_versions=covered.source_versions, policy_version='scenario_b_policy:v1', requested_at=now + timedelta(minutes=4), expires_at=now + timedelta(hours=4), run_id=run_id)\n"
        "assert covered_result.pending is not None and covered_result.workflow is not None\n"
        "covered_pending = covered_result.pending\n"
        "try:\n"
        "    executor.begin_next_tool(covered_result.workflow.workflow.workflow_id, worker_id='quality-worker-a', now=now + timedelta(minutes=5), lease_expires_at=now + timedelta(minutes=20), current_source_versions=covered.source_versions)\n"
        "except WorkflowExecutionRejectedError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('pending Scenario B plan executed before approval')\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    allocation_count = connection.execute(text(\"SELECT COUNT(*) FROM production_allocations WHERE quality_lot_id = CAST(:lot_id AS UUID) AND production_order_id = CAST(:production_order_id AS UUID)\"), {'lot_id': str(ID_LOT_GOOD), 'production_order_id': str(ID_PRODUCTION_Q7001)}).scalar_one()\n"
        "    notification_count = connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'tool:v1:%:notify_production:%'\")).scalar_one()\n"
        "assert allocation_count == 0 and notification_count == 0\n"
        "approved = approval_service.approve(approval_id=covered_pending.approval.approval_id, expected_plan_hash=covered_pending.plan.plan_hash, decider_id=covered.production_supervisor_id, current_source_versions=covered.source_versions, decided_at=now + timedelta(minutes=6), run_id=run_id)\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "crashing = ScenarioAWorkflowExecutor(workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=tools, audit=audit, crash_injector=DeterministicCrashInjector(target_tool_name=ToolName.REALLOCATE_LOT))\n"
        "started = crashing.begin_next_tool(covered_result.workflow.workflow.workflow_id, worker_id='quality-worker-a', now=now + timedelta(minutes=7), lease_expires_at=now + timedelta(minutes=8), current_source_versions=covered.source_versions)\n"
        "try:\n"
        "    crashing.execute_started_tool(started, worker_id='quality-worker-a', completed_at=now + timedelta(minutes=7, seconds=30))\n"
        "except WorkflowCrashInjectedError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('reallocation crash was not injected')\n"
        "crashed = workflows.load(covered_result.workflow.workflow.workflow_id)\n"
        "assert crashed is not None and crashed.steps[0].status is WorkflowStepStatus.RUNNING\n"
        "restart = ScenarioAWorkflowExecutor(workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=tools, audit=audit)\n"
        "resumed = restart.begin_next_tool(covered_result.workflow.workflow.workflow_id, worker_id='quality-worker-b', now=now + timedelta(minutes=9), lease_expires_at=now + timedelta(minutes=20), current_source_versions=covered.source_versions)\n"
        "assert resumed.invocation.idempotency_key == started.invocation.idempotency_key\n"
        "replayed = tools.execute(resumed.actor, resumed.invocation)\n"
        "assert replayed == tools.execute(resumed.actor, resumed.invocation)\n"
        "completed = restart.execute_started_tool(resumed, worker_id='quality-worker-b', completed_at=now + timedelta(minutes=10))\n"
        "started = restart.begin_next_tool(covered_result.workflow.workflow.workflow_id, worker_id='quality-worker-b', now=now + timedelta(minutes=11), lease_expires_at=now + timedelta(minutes=20), current_source_versions=covered.source_versions)\n"
        "completed = restart.execute_started_tool(started, worker_id='quality-worker-b', completed_at=now + timedelta(minutes=12))\n"
        "assert completed.workflow.status is WorkflowStatus.SUCCEEDED\n"
        "with engine.connect() as connection:\n"
        "    allocation = connection.execute(text(\"SELECT allocated_quantity::text FROM production_allocations WHERE quality_lot_id = CAST(:lot_id AS UUID) AND production_order_id = CAST(:production_order_id AS UUID)\"), {'lot_id': str(ID_LOT_GOOD), 'production_order_id': str(ID_PRODUCTION_Q7001)}).scalar_one()\n"
        "    assert allocation == '80.000'\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'tool:v1:%:notify_production:%'\")).scalar_one() == 1\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'tool:v1:%:flag_shortage_to_purchasing:%'\")).scalar_one() == 1\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM tool_invocations WHERE status = 'succeeded'\")).scalar_one() == 3\n"
        "event_types = [event.event_type for event in audit.events_for_run(run_id)]\n"
        "assert set(('context.gathered', 'evidence.observed', 'planner.recommended', 'gate.allowed', 'approval.requested', 'approval.approved', 'workflow.started', 'tool.started', 'tool.succeeded')) <= set(event_types)\n"
        "rendered = AuditExplainer(audit).explain(run_id).render()\n"
        "assert 'Planner recommended REALLOCATE_AND_NOTIFY using bounded_tool_plan.' in rendered\n"
        "assert 'Planner recommended FLAG_SHORTAGE_TO_PURCHASING using bounded_tool_plan.' in rendered\n"
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
