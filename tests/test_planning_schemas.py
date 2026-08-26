"""Contracts for the bounded Scenario A planning schema and deterministic fake LLM."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    Evidence,
    EvidenceId,
    PlantId,
    RunId,
    Scope,
    UserId,
)
from enterprise_agent.ports import LLMMessage, LLMPort, PromptEnvelope

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
ACTOR = ActorContext(
    user_id=UserId("00000000-0000-0000-0000-000000000001"),
    role="purchasing_manager",
    scopes=frozenset({Scope("erp:read")}),
    plant_ids=frozenset({PlantId("PLANT-CHI")}),
    backup_approver_id=None,
    approval_limits={"USD": Decimal("10000.00")},
)


def prompt(*, scenario: str = "scenario_a", cause: str = "projected_stockout") -> PromptEnvelope:
    """Build the smallest authorized planning prompt accepted by the provider-neutral port."""
    attention = AttentionItem(
        attention_id=AttentionId("attention-1"),
        scenario=scenario,
        cause=cause,
        dedupe_key="scenario:dedupe:1",
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions={"inventory:inventory-x": 4},
    )
    evidence = Evidence(
        evidence_id=EvidenceId("erp:inventory:inventory-x"),
        source="erp",
        record_type="inventory",
        record_id="inventory-x",
        source_version=4,
        observed_at=NOW,
        payload={"part_id": "part-x"},
    )
    return PromptEnvelope(
        run_id=RunId("run-planning"),
        actor=ACTOR,
        attention=attention,
        evidence=(evidence,),
        messages=(LLMMessage(role="user", content="Recommend a safe Scenario A outcome."),),
        purpose="scenario_a_recommendation",
        response_schema="scenario_a_recommendation:v1",
    )


def test_schema_accepts_only_the_three_bounded_scenario_a_outcomes() -> None:
    """Each permitted recommendation has an explicit, typed shape before any gate is involved."""
    from enterprise_agent.application.planning import (
        EnterWorkflowRecommendation,
        ManualReviewRecommendation,
        NoActionRecommendation,
        validate_scenario_a_recommendation,
    )

    no_action = validate_scenario_a_recommendation(
        {"outcome": "NO_ACTION", "rationale": "Current evidence does not require a change."}
    )
    manual_review = validate_scenario_a_recommendation(
        {"outcome": "MANUAL_REVIEW", "reason": "Supplier data is inconclusive."}
    )
    workflow = validate_scenario_a_recommendation(
        {
            "outcome": "ENTER_WORKFLOW",
            "workflow_name": "po_reroute",
            "workflow_version": 1,
            "supplier_id": "supplier-z",
            "quantity": "60",
            "original_purchase_order_id": "po-4812-y",
            "production_order_id": "production-4812",
            "rationale": "Supplier Z can arrive before production starts.",
        }
    )

    assert isinstance(no_action, NoActionRecommendation)
    assert isinstance(manual_review, ManualReviewRecommendation)
    assert isinstance(workflow, EnterWorkflowRecommendation)
    assert workflow.quantity == Decimal(60)


@pytest.mark.parametrize(
    "output",
    [
        {"outcome": "FREEFORM_TOOL_CALL", "tool": "delete_all_pos"},
        {"outcome": "ENTER_WORKFLOW", "workflow_name": "invented_workflow", "workflow_version": 1},
        {
            "outcome": "ENTER_WORKFLOW",
            "workflow_name": "po_reroute",
            "workflow_version": 2,
            "supplier_id": "supplier-z",
            "quantity": 1,
            "original_purchase_order_id": "po-4812-y",
            "production_order_id": "production-4812",
            "rationale": "Invalid workflow version.",
        },
        {
            "outcome": "ENTER_WORKFLOW",
            "workflow_name": "po_reroute",
            "workflow_version": 1,
            "supplier_id": "supplier-z",
            "quantity": 0,
            "original_purchase_order_id": "po-4812-y",
            "production_order_id": "production-4812",
            "rationale": "Zero quantity is not an actionable reroute.",
        },
    ],
)
def test_schema_rejects_unknown_or_invalid_workflow_recommendations(
    output: dict[str, object],
) -> None:
    """Model output cannot add outcomes, workflows, versions, or non-actionable quantities."""
    from enterprise_agent.application.planning import (
        InvalidScenarioARecommendationError,
        validate_scenario_a_recommendation,
    )

    with pytest.raises(InvalidScenarioARecommendationError):
        validate_scenario_a_recommendation(output)


def test_fake_llm_is_scenario_configurable_and_defaults_to_manual_review() -> None:
    """Tests can configure deterministic outcomes without any network call or unsafe fallback."""
    from enterprise_agent.application.planning import (
        EnterWorkflowRecommendation,
        FakeLLMPort,
        ManualReviewRecommendation,
        validate_scenario_a_recommendation,
    )

    configured = EnterWorkflowRecommendation(
        outcome="ENTER_WORKFLOW",
        workflow_name="po_reroute",
        workflow_version=1,
        supplier_id="supplier-z",
        quantity=Decimal(60),
        original_purchase_order_id="po-4812-y",
        production_order_id="production-4812",
        rationale="Supplier Z can meet the start date.",
    )
    fake = FakeLLMPort({"scenario_a:projected_stockout": configured})

    first = fake.generate(prompt())
    second = fake.generate(prompt())
    unconfigured = fake.generate(prompt(scenario="scenario_b", cause="quality_hold"))

    assert isinstance(fake, LLMPort)
    assert first == second
    assert first.provider == "fake"
    assert first.model == "deterministic-fake-v1"
    assert validate_scenario_a_recommendation(first.require_output()) == configured
    fallback = validate_scenario_a_recommendation(unconfigured.require_output())
    assert isinstance(fallback, ManualReviewRecommendation)
    assert fallback.reason == "No fake recommendation configured for scenario_b:quality_hold."


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and expose diagnostics if it fails."""
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
def test_seeded_authorized_context_and_candidates_support_a_valid_fake_recommendation(
    disposable_database: str,
) -> None:
    """The deterministic fake can recommend only the pre-filtered seeded Supplier Z reroute."""
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
        "from datetime import UTC, datetime\n"
        "from decimal import Decimal\n"
        "from os import environ\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresCalendarAdapter,\n"
        "    PostgresDemoClock,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter,\n"
        ")\n"
        "from enterprise_agent.application.candidates import SupplierCandidateFilter\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.planning import EnterWorkflowRecommendation, FakeLLMPort, validate_scenario_a_recommendation\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.domain import RunId, UserId\n"
        "from enterprise_agent.ports import LLMMessage, PromptEnvelope\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "actor = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "detection = StockoutDetector(PostgresErpAdapter(database_url), PostgresAttentionAdapter(database_url), PostgresDemoClock(database_url)).detect(actor, RunId('run-seeded-planning'))[0]\n"
        "context = ScenarioAContextAssembler(identity, PostgresErpAdapter(database_url), PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "candidate = SupplierCandidateFilter().filter(context).candidates[0]\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id=candidate.supplier_id, quantity=Decimal('60'), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='The pre-filtered alternate can meet production.')\n"
        "fake = FakeLLMPort({f'{context.attention.scenario}:{context.attention.cause}': recommendation})\n"
        "response = fake.generate(PromptEnvelope(run_id=RunId('run-seeded-planning'), actor=context.actor, attention=context.attention, evidence=context.evidence, messages=(LLMMessage(role='user', content='Recommend an authorized response.'),), purpose='scenario_a_recommendation', response_schema='scenario_a_recommendation:v1'))\n"
        "validated = validate_scenario_a_recommendation(response.output)\n"
        "assert validated.supplier_id == candidate.supplier_id\n"
        "assert candidate.evidence.payload['supplier_code'] == 'SUP-Z'\n"
        "assert validated.workflow_name == 'po_reroute' and validated.workflow_version == 1\n"
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
