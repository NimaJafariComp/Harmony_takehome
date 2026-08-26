"""Critical cross-boundary safety contract for the completed Scenario A planning core."""

from __future__ import annotations

import subprocess

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and retain diagnostics when the isolated safety test fails."""
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
def test_seeded_scenario_a_creates_only_one_pending_approval_and_no_erp_writes(
    disposable_database: str,
) -> None:
    """Detection through human approval creates no workflow or PO side effect before M4 exists."""
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
        "from datetime import UTC, datetime, timedelta\n"
        "from decimal import Decimal\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresCalendarAdapter,\n"
        "    PostgresDemoClock,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter,\n"
        "    PostgresPlanApprovalAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approvals import ScenarioAApprovalService\n"
        "from enterprise_agent.application.candidates import SupplierCandidateFilter\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.gate import GateStatus, ScenarioAGate\n"
        "from enterprise_agent.application.planning import EnterWorkflowRecommendation, FakeLLMPort, validate_scenario_a_recommendation\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId\n"
        "from enterprise_agent.ports import LLMMessage, PromptEnvelope\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "now = datetime(2026, 8, 24, 9, tzinfo=UTC)\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "actor = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "attention_store = PostgresAttentionAdapter(database_url)\n"
        "detector = StockoutDetector(erp, attention_store, PostgresDemoClock(database_url))\n"
        "first = detector.detect(actor, RunId('run-safe-planning-1'))\n"
        "duplicate = detector.detect(actor, RunId('run-safe-planning-2'))\n"
        "assert len(first) == len(duplicate) == 1\n"
        "assert first[0].registration.created is True\n"
        "assert duplicate[0].registration.created is False\n"
        "assert duplicate[0].registration.attention.attention_id == first[0].registration.attention.attention_id\n"
        "context = ScenarioAContextAssembler(identity, erp, PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=first[0].registration.attention, trigger=first[0].risk.trigger)\n"
        "candidates = SupplierCandidateFilter().filter(context)\n"
        "assert [candidate.evidence.payload['supplier_code'] for candidate in candidates.candidates] == ['SUP-Z']\n"
        "assert {entry.evidence.payload['supplier_code'] for entry in candidates.exclusions} == {'SUP-SLOW', 'SUP-W', 'SUP-Y'}\n"
        "candidate = candidates.candidates[0]\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id=candidate.supplier_id, quantity=Decimal(60), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='The only eligible alternate meets production.')\n"
        "fake = FakeLLMPort({f'{context.attention.scenario}:{context.attention.cause}': recommendation})\n"
        "response = fake.generate(PromptEnvelope(run_id=RunId('run-safe-planning-1'), actor=context.actor, attention=context.attention, evidence=context.evidence, messages=(LLMMessage(role='user', content='Recommend an authorized response.'),), purpose='scenario_a_recommendation', response_schema='scenario_a_recommendation:v1'))\n"
        "validated = validate_scenario_a_recommendation(response.output)\n"
        "decision = ScenarioAGate().evaluate(context, validated, current_source_versions=context.source_versions)\n"
        "assert decision.status is GateStatus.PENDING_APPROVAL and decision.approval_required is True\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        '    before_purchase_orders = connection.execute(text("SELECT id::text, supplier_id::text, ordered_quantity::text, received_quantity::text, status, source_version FROM purchase_orders ORDER BY id")).all()\n'
        "    assert connection.execute(text('SELECT COUNT(*) FROM plans')).scalar_one() == 0\n"
        "    assert connection.execute(text('SELECT COUNT(*) FROM approvals')).scalar_one() == 0\n"
        "    assert connection.execute(text('SELECT COUNT(*) FROM workflow_instances')).scalar_one() == 0\n"
        "service = ScenarioAApprovalService(PostgresPlanApprovalAdapter(database_url))\n"
        "pending = service.request_pending(context, validated, current_source_versions=context.source_versions, policy_version='scenario_a_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4))\n"
        "with engine.connect() as connection:\n"
        "    assert connection.execute(text('SELECT COUNT(*) FROM plans')).scalar_one() == 1\n"
        "    assert connection.execute(text('SELECT COUNT(*) FROM approvals')).scalar_one() == 1\n"
        "    assert connection.execute(text(\"SELECT status FROM approvals WHERE id = CAST(:approval_id AS UUID)\"), {'approval_id': str(pending.approval.approval_id)}).scalar_one() == 'pending'\n"
        '    assert connection.execute(text("SELECT id::text, supplier_id::text, ordered_quantity::text, received_quantity::text, status, source_version FROM purchase_orders ORDER BY id")).all() == before_purchase_orders\n'
        "    assert connection.execute(text('SELECT COUNT(*) FROM workflow_instances')).scalar_one() == 0\n"
        "approved = service.approve(approval_id=pending.approval.approval_id, expected_plan_hash=pending.plan.plan_hash, decider_id=actor.user_id, current_source_versions=context.source_versions, decided_at=now + timedelta(minutes=1))\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "with engine.connect() as connection:\n"
        "    assert connection.execute(text(\"SELECT status FROM approvals WHERE id = CAST(:approval_id AS UUID)\"), {'approval_id': str(pending.approval.approval_id)}).scalar_one() == 'approved'\n"
        '    assert connection.execute(text("SELECT id::text, supplier_id::text, ordered_quantity::text, received_quantity::text, status, source_version FROM purchase_orders ORDER BY id")).all() == before_purchase_orders\n'
        "    assert connection.execute(text('SELECT COUNT(*) FROM workflow_instances')).scalar_one() == 0\n"
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
