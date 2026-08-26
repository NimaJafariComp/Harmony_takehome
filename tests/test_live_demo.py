"""Contracts for the explicitly guarded live-provider local-demo runner."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import pytest

from enterprise_agent.domain import AuditEvent, RunId

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def test_live_demo_catalogue_is_fixed_to_one_seeded_story_per_scenario() -> None:
    """A live request cannot select an arbitrary prompt, actor, or business record."""
    from enterprise_agent.application.live_demo import (
        LiveDemoSelectionError,
        live_demo_cases,
        select_live_demo_case,
    )

    assert [(item.case_id, item.scenario) for item in live_demo_cases()] == [
        ("scenario-a-reroute", "scenario_a"),
        ("scenario-b-quality-hold", "scenario_b"),
        ("scenario-c-supplier-risk", "scenario_c"),
    ]
    assert select_live_demo_case("scenario-a-reroute").scenario == "scenario_a"

    with pytest.raises(LiveDemoSelectionError, match="unknown live-demo case"):
        select_live_demo_case("arbitrary-live-prompt")


def test_live_demo_matches_the_claude_profile_to_its_anthropic_adapter_identity() -> None:
    """A completed Claude result reaches schema validation instead of failing on a naming alias."""
    from enterprise_agent.application.live_demo import _PROVIDER_NAMES_BY_PROFILE

    assert _PROVIDER_NAMES_BY_PROFILE["claude"] == "anthropic"


@pytest.mark.parametrize(
    ("profile", "adapter_name"),
    (
        ("openai", "OpenAIResponsesAdapter"),
        ("claude", "ClaudeMessagesAdapter"),
        ("openrouter", "OpenRouterChatCompletionsAdapter"),
    ),
)
def test_live_demo_composes_only_the_explicitly_selected_provider_adapter(
    profile: str, adapter_name: str
) -> None:
    """Constructing a guarded live demo chooses one adapter and does not send a request."""
    from enterprise_agent.application.live_demo import create_live_demo_adapter
    from enterprise_agent.config import ProviderConfiguration

    class Audit:
        def append(self, event: AuditEvent) -> None:
            pass

        def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
            return ()

    class Clock:
        def now(self) -> datetime:
            return datetime(2026, 8, 24, 9, tzinfo=UTC)

    adapter = create_live_demo_adapter(
        ProviderConfiguration(profile=profile, model="reviewed-test-model", api_key="test-key"),
        audit=Audit(),
        clock=Clock(),
    )

    assert type(adapter).__name__ == adapter_name


def _compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command while retaining diagnostics for local live-demo control checks."""
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
def test_live_demo_stages_each_fixed_story_through_the_real_control_plane_without_effects(
    disposable_database: str,
) -> None:
    """A structured provider proposal can reach only pending review, never an immediate tool effect."""
    _compose(
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
        "from decimal import Decimal\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import PostgresAuditAdapter\n"
        "from enterprise_agent.application.live_demo import run_live_demo\n"
        "from enterprise_agent.config import ProviderConfiguration\n"
        "from enterprise_agent.ports import LLMCostSource, LLMGenerationResult, LLMUsage\n"
        "from enterprise_agent.review_provenance import GateStatus, PlannerMode, SchemaValidation\n"
        "from enterprise_agent.seed import (\n"
        "    ID_LOT_GOOD, ID_PO_4812_Y, ID_PO_C9001_W, ID_PRODUCTION_4812,\n"
        "    ID_PRODUCTION_C9001, ID_PRODUCTION_Q7001, ID_SUPPLIER_Z,\n"
        ")\n"
        "database_url = environ['DATABASE_URL']\n"
        "configuration = ProviderConfiguration(profile='openai', model='test-live-model', api_key='test-key')\n"
        "usage = LLMUsage(input_tokens=21, cached_input_tokens=0, output_tokens=8, total_tokens=29, cost_usd=Decimal('0.00021'), cost_source=LLMCostSource.PROVIDER_REPORTED)\n"
        "class StaticAdapter:\n"
        "    def __init__(self, output):\n"
        "        self._output = output\n"
        "    def generate(self, prompt):\n"
        "        assert prompt.messages and 'untrusted data' in prompt.messages[0].content\n"
        "        return LLMGenerationResult.succeeded(provider='openai', model='test-live-model', output=self._output, usage=usage)\n"
        "def factory_for(output):\n"
        "    def factory(configuration, *, audit, clock):\n"
        "        return StaticAdapter(output)\n"
        "    return factory\n"
        "def assert_pending(result):\n"
        "    assert result.provenance.mode is PlannerMode.LIVE\n"
        "    assert result.provenance.schema_validation is SchemaValidation.PASSED\n"
        "    assert result.provenance.gate_status is GateStatus.PENDING_APPROVAL\n"
        "    assert result.plan_id is not None and result.approval_id is not None\n"
        "    assert result.workflow_id is not None and result.escalation_task_id is not None\n"
        "    assert result.usage == usage\n"
        "    assert 'RAW_PROVIDER_CONTENT_MUST_NOT_ESCAPE' not in repr(result)\n"
        "    audit = PostgresAuditAdapter(database_url)\n"
        "    event_types = {event.event_type for event in audit.events_for_run(result.run_id)}\n"
        "    assert {'planner.recommended', 'gate.allowed', 'approval.requested', 'schedule.created'} <= event_types\n"
        "    engine = create_engine(database_url)\n"
        "    with engine.connect() as connection:\n"
        "        plans = connection.execute(text('SELECT COUNT(*) FROM plans')).scalar_one()\n"
        "        approvals = connection.execute(text(\"SELECT COUNT(*) FROM approvals WHERE status = 'pending'\")).scalar_one()\n"
        "        workflows = connection.execute(text('SELECT COUNT(*) FROM workflow_instances')).scalar_one()\n"
        "        scheduled = connection.execute(text('SELECT COUNT(*) FROM scheduled_tasks')).scalar_one()\n"
        "        effects = connection.execute(text('SELECT COUNT(*) FROM tool_invocations')).scalar_one()\n"
        "        audit_payloads = connection.execute(text('SELECT payload::text FROM audit_events')).scalars().all()\n"
        "    assert plans == approvals == workflows == scheduled == 1 and effects == 0\n"
        "    assert all('RAW_PROVIDER_CONTENT_MUST_NOT_ESCAPE' not in payload for payload in audit_payloads)\n"
        "scenario_a = run_live_demo(\n"
        "    database_url, configuration=configuration, case_id='scenario-a-reroute', allow_test_database=True,\n"
        "    adapter_factory=factory_for({\n"
        "        'outcome': 'ENTER_WORKFLOW', 'workflow_name': 'po_reroute', 'workflow_version': 1,\n"
        "        'supplier_id': str(ID_SUPPLIER_Z), 'quantity': '60',\n"
        "        'original_purchase_order_id': str(ID_PO_4812_Y), 'production_order_id': str(ID_PRODUCTION_4812),\n"
        "        'rationale': 'RAW_PROVIDER_CONTENT_MUST_NOT_ESCAPE',\n"
        "    }),\n"
        ")\n"
        "assert scenario_a.outcome == 'ENTER_WORKFLOW'\n"
        "assert_pending(scenario_a)\n"
        "scenario_b = run_live_demo(\n"
        "    database_url, configuration=configuration, case_id='scenario-b-quality-hold', allow_test_database=True,\n"
        "    adapter_factory=factory_for({\n"
        "        'outcome': 'REALLOCATE_AND_NOTIFY',\n"
        "        'reallocate_lot': {'quality_lot_id': str(ID_LOT_GOOD), 'to_production_order_id': str(ID_PRODUCTION_Q7001), 'quantity': '80'},\n"
        "        'notify_production': {'production_order_id': str(ID_PRODUCTION_Q7001), 'message': 'Quality replacement can cover the held allocation.'},\n"
        "        'rationale': 'RAW_PROVIDER_CONTENT_MUST_NOT_ESCAPE',\n"
        "    }),\n"
        ")\n"
        "assert scenario_b.outcome == 'REALLOCATE_AND_NOTIFY'\n"
        "assert_pending(scenario_b)\n"
        "scenario_c = run_live_demo(\n"
        "    database_url, configuration=configuration, case_id='scenario-c-supplier-risk', allow_test_database=True,\n"
        "    adapter_factory=factory_for({\n"
        "        'outcome': 'HOLD_AND_NOTIFY',\n"
        "        'hold_purchase_order': {'purchase_order_id': str(ID_PO_C9001_W), 'production_order_id': str(ID_PRODUCTION_C9001), 'expected_purchase_order_version': 1},\n"
        "        'notify_production': {'production_order_id': str(ID_PRODUCTION_C9001), 'message': 'Current supplier risk needs a reviewed temporary hold.'},\n"
        "        'rationale': 'RAW_PROVIDER_CONTENT_MUST_NOT_ESCAPE',\n"
        "    }),\n"
        ")\n"
        "assert scenario_c.outcome == 'HOLD_AND_NOTIFY'\n"
        "assert_pending(scenario_c)\n"
    )
    _compose(
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


@pytest.mark.critical
@pytest.mark.integration
@pytest.mark.scenario
def test_live_demo_treats_failed_manual_and_policy_denied_provider_results_as_non_executable(
    disposable_database: str,
) -> None:
    """A failing, uncertain, mismatched, or disallowed provider response cannot create a plan or effect."""
    _compose(
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
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.application.live_demo import run_live_demo\n"
        "from enterprise_agent.config import ProviderConfiguration\n"
        "from enterprise_agent.ports import LLMGenerationResult, LLMGenerationStatus\n"
        "from enterprise_agent.review_provenance import GateStatus, SchemaValidation\n"
        "from enterprise_agent.seed import ID_PO_4812_Y, ID_PRODUCTION_4812, ID_SUPPLIER_BAIT\n"
        "database_url = environ['DATABASE_URL']\n"
        "configuration = ProviderConfiguration(profile='openai', model='test-live-model', api_key='test-key')\n"
        "class StaticAdapter:\n"
        "    def __init__(self, response):\n"
        "        self._response = response\n"
        "    def generate(self, prompt):\n"
        "        return self._response\n"
        "def factory_for(response):\n"
        "    def factory(configuration, *, audit, clock):\n"
        "        return StaticAdapter(response)\n"
        "    return factory\n"
        "def assert_non_executable(result, schema_validation, gate_status):\n"
        "    assert result.provenance.schema_validation is schema_validation\n"
        "    assert result.provenance.gate_status is gate_status\n"
        "    assert result.plan_id is result.approval_id is result.workflow_id is result.escalation_task_id is None\n"
        "    engine = create_engine(database_url)\n"
        "    with engine.connect() as connection:\n"
        "        assert connection.execute(text('SELECT COUNT(*) FROM plans')).scalar_one() == 0\n"
        "        assert connection.execute(text('SELECT COUNT(*) FROM approvals')).scalar_one() == 0\n"
        "        assert connection.execute(text('SELECT COUNT(*) FROM workflow_instances')).scalar_one() == 0\n"
        "        assert connection.execute(text('SELECT COUNT(*) FROM scheduled_tasks')).scalar_one() == 0\n"
        "        assert connection.execute(text('SELECT COUNT(*) FROM tool_invocations')).scalar_one() == 0\n"
        "failed = run_live_demo(\n"
        "    database_url, configuration=configuration, case_id='scenario-a-reroute', allow_test_database=True,\n"
        "    adapter_factory=factory_for(LLMGenerationResult.failed(provider='openai', model='test-live-model', status=LLMGenerationStatus.TIMEOUT)),\n"
        ")\n"
        "assert_non_executable(failed, SchemaValidation.FAILED, GateStatus.NOT_INVOKED_PLANNER_FAILURE)\n"
        "manual = run_live_demo(\n"
        "    database_url, configuration=configuration, case_id='scenario-b-quality-hold', allow_test_database=True,\n"
        "    adapter_factory=factory_for(LLMGenerationResult.succeeded(provider='openai', model='test-live-model', output={'outcome': 'MANUAL_REVIEW', 'reason': 'Evidence needs a person.'})),\n"
        ")\n"
        "assert manual.outcome == 'MANUAL_REVIEW'\n"
        "assert_non_executable(manual, SchemaValidation.PASSED, GateStatus.NOT_INVOKED_MANUAL_REVIEW)\n"
        "mismatched_provider = run_live_demo(\n"
        "    database_url, configuration=configuration, case_id='scenario-c-supplier-risk', allow_test_database=True,\n"
        "    adapter_factory=factory_for(LLMGenerationResult.succeeded(provider='claude', model='test-live-model', output={'outcome': 'MANUAL_REVIEW', 'reason': 'Wrong provider.'})),\n"
        ")\n"
        "assert_non_executable(mismatched_provider, SchemaValidation.FAILED, GateStatus.NOT_INVOKED_PLANNER_FAILURE)\n"
        "denied = run_live_demo(\n"
        "    database_url, configuration=configuration, case_id='scenario-a-reroute', allow_test_database=True,\n"
        "    adapter_factory=factory_for(LLMGenerationResult.succeeded(provider='openai', model='test-live-model', output={\n"
        "        'outcome': 'ENTER_WORKFLOW', 'workflow_name': 'po_reroute', 'workflow_version': 1,\n"
        "        'supplier_id': str(ID_SUPPLIER_BAIT), 'quantity': '60',\n"
        "        'original_purchase_order_id': str(ID_PO_4812_Y), 'production_order_id': str(ID_PRODUCTION_4812),\n"
        "        'rationale': 'The unapproved supplier should be rejected by policy.',\n"
        "    })),\n"
        ")\n"
        "assert denied.outcome == 'ENTER_WORKFLOW'\n"
        "assert_non_executable(denied, SchemaValidation.PASSED, GateStatus.DENIED)\n"
    )
    _compose(
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
