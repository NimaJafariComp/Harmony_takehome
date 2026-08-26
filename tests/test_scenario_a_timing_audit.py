"""End-to-end timing and audit-completeness contract for Scenario A."""

from __future__ import annotations

import subprocess

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for the Scenario A audit contract."""
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
def test_seeded_scenario_a_survives_to_tuesday_and_reconstructs_its_complete_audit_run(
    disposable_database: str,
) -> None:
    """One durable Scenario A correlation carries evidence, decisions, effects, and Tuesday re-entry."""
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
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter, PostgresAuditAdapter, PostgresCalendarAdapter,\n"
        "    PostgresDemoClock, PostgresErpAdapter, PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter, PostgresPlanApprovalAdapter, PostgresScenarioAToolAdapter,\n"
        "    PostgresSchedulerAdapter, PostgresWorkflowStateAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approval_routing import ApprovalRoutingOutcome, ApprovalRoutingService\n"
        "from enterprise_agent.application.approvals import ScenarioAApprovalService\n"
        "from enterprise_agent.application.arrival_check import ArrivalCheckOutcome, TuesdayArrivalCheckService\n"
        "from enterprise_agent.application.audit_explain import AuditExplainer\n"
        "from enterprise_agent.application.candidates import SupplierCandidateFilter\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.planning import EnterWorkflowRecommendation, FakeLLMPort, validate_scenario_a_recommendation\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.application.workflow_executor import ScenarioAWorkflowExecutor\n"
        "from enterprise_agent.application.workflow_state import WorkflowStateService\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId, WorkflowStatus\n"
        "from enterprise_agent.ports import LLMMessage, PromptEnvelope\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "run_id = RunId('run-scenario-a-timing-audit')\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "clock = PostgresDemoClock(database_url)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "dana = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "avery = identity.actor_for(UserId('00000000-0000-0000-0000-000000000002'))\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "audit = PostgresAuditAdapter(database_url)\n"
        "attention = PostgresAttentionAdapter(database_url)\n"
        "detection = StockoutDetector(erp, attention, clock).detect(dana, run_id)[0]\n"
        "context = ScenarioAContextAssembler(identity, erp, PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url), audit=audit).assemble(user_id=dana.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger, run_id=run_id)\n"
        "candidate = SupplierCandidateFilter().filter(context).candidates[0]\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id=candidate.supplier_id, quantity=Decimal('60'), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='The only eligible alternate meets production.')\n"
        "fake = FakeLLMPort({f'{context.attention.scenario}:{context.attention.cause}': recommendation})\n"
        "response = fake.generate(PromptEnvelope(run_id=run_id, actor=context.actor, attention=context.attention, evidence=context.evidence, messages=(LLMMessage(role='user', content='Recommend an authorized response.'),), purpose='scenario_a_recommendation', response_schema='scenario_a_recommendation:v1'))\n"
        "validated = validate_scenario_a_recommendation(response.output)\n"
        "approvals = PostgresPlanApprovalAdapter(database_url)\n"
        "scheduler = PostgresSchedulerAdapter(database_url, clock)\n"
        "router = ApprovalRoutingService(approvals, identity, PostgresCalendarAdapter(database_url), scheduler, audit=audit)\n"
        "approval_service = ScenarioAApprovalService(approvals, escalation_scheduler=router, audit=audit)\n"
        "now = clock.now()\n"
        "pending = approval_service.request_pending(context, validated, current_source_versions=context.source_versions, policy_version='scenario_a_policy:v1', requested_at=now, expires_at=now + timedelta(days=2), run_id=run_id)\n"
        "assert scheduler.claim_due(now, limit=10) == ()\n"
        "end_of_day = clock.advance(timedelta(hours=8))\n"
        "escalation = scheduler.claim_due(end_of_day, limit=10)\n"
        "assert len(escalation) == 1\n"
        "routed = router.handle_claimed_task(escalation[0], routed_at=end_of_day)\n"
        "assert routed.outcome is ApprovalRoutingOutcome.REROUTED\n"
        "scheduler.mark_succeeded(escalation[0].task_id, end_of_day)\n"
        "plan, active_approval = approvals.load(pending.approval.approval_id) or (None, None)\n"
        "assert plan is not None and active_approval is not None\n"
        "approved = approval_service.approve(approval_id=active_approval.approval_id, expected_plan_hash=plan.plan_hash, decider_id=avery.user_id, current_source_versions=context.source_versions, decided_at=end_of_day, run_id=run_id)\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "workflows = PostgresWorkflowStateAdapter(database_url)\n"
        "snapshot = WorkflowStateService(workflows).stage(plan, created_at=end_of_day, audit_run_id=run_id)\n"
        "executor = ScenarioAWorkflowExecutor(workflow_store=workflows, approvals=approvals, identity=identity, tool_executor=PostgresScenarioAToolAdapter(database_url), audit=audit)\n"
        "worker_id = 'scenario-a-worker'\n"
        "lease_expires_at = end_of_day + timedelta(hours=1)\n"
        "current = executor.claim(snapshot.workflow.workflow_id, worker_id=worker_id, now=end_of_day, lease_expires_at=lease_expires_at, current_source_versions=context.source_versions)\n"
        "current = executor.advance_next_guard(current, worker_id=worker_id, completed_at=end_of_day + timedelta(minutes=1))\n"
        "current = executor.advance_next_guard(current, worker_id=worker_id, completed_at=end_of_day + timedelta(minutes=2))\n"
        "for minute in (3, 5, 7, 9):\n"
        "    started = executor.begin_next_tool(snapshot.workflow.workflow_id, worker_id=worker_id, now=end_of_day + timedelta(minutes=minute), lease_expires_at=lease_expires_at, current_source_versions=context.source_versions)\n"
        "    current = executor.execute_started_tool(started, worker_id=worker_id, completed_at=end_of_day + timedelta(minutes=minute + 1))\n"
        "assert current.workflow.status is WorkflowStatus.SUCCEEDED\n"
        "tuesday = clock.advance(timedelta(days=1))\n"
        "arrival_tasks = scheduler.claim_due(tuesday, limit=10)\n"
        "assert len(arrival_tasks) == 1 and arrival_tasks[0].task_type == 'arrival_check'\n"
        "arrival = TuesdayArrivalCheckService(erp=erp, identity=identity, attention=attention, audit=audit)\n"
        "arrival_result = arrival.handle_claimed_task(arrival_tasks[0], checked_at=tuesday, run_id=run_id)\n"
        "scheduler.mark_succeeded(arrival_tasks[0].task_id, tuesday)\n"
        "assert arrival_result.outcome is ArrivalCheckOutcome.REOPENED\n"
        "events = audit.events_for_run(run_id)\n"
        "event_types = [event.event_type for event in events]\n"
        "assert set(('attention.detected', 'context.gathered', 'evidence.observed', 'planner.recommended', 'gate.allowed', 'approval.requested', 'approval.rerouted', 'approval.approved', 'workflow.started', 'workflow.step_started', 'workflow.step_completed', 'tool.started', 'tool.succeeded', 'schedule.created', 'schedule.fired', 'followup.reopened')) <= set(event_types)\n"
        "assert event_types.count('schedule.created') == 2\n"
        "assert event_types.count('schedule.fired') == 2\n"
        "assert all(event.run_id == run_id for event in events)\n"
        "rendered = AuditExplainer(audit).explain(run_id).render()\n"
        "assert 'Planner recommended ENTER_WORKFLOW using po_reroute.' in rendered\n"
        "assert 'Approval was rerouted by' in rendered\n"
        "assert 'Created replacement PO using create_replacement_po.' in rendered\n"
        "assert 'Receipt is still missing for replacement PO' in rendered\n"
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
