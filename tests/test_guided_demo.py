"""Contracts for the local-only guided deterministic demo catalogue."""

import subprocess

import pytest

from enterprise_agent.application.guided_demo import (
    DemoCaseSelectionError,
    DemoExecutionMode,
    select_guided_demo_cases,
)

pytestmark = pytest.mark.unit


def test_safety_tour_selects_every_required_company_story_with_honest_execution_labels() -> None:
    """The default tour covers the assignment's messy cases without implying a live LLM ran."""
    cases = select_guided_demo_cases(("safety-tour",))

    assert [case.case_id for case in cases] == [
        "scenario-a-reroute-bait",
        "scenario-a-crash-recovery",
        "scenario-a-current-evidence",
        "scenario-a-tuesday-follow-up",
        "scenario-b-capacity",
        "scenario-c-pending-review",
    ]
    assert cases[0].execution_mode is DemoExecutionMode.STAGE_PENDING
    assert cases[-1].execution_mode is DemoExecutionMode.STAGE_PENDING
    assert all(case.planner_provenance.mode_label == "FAKE / DETERMINISTIC" for case in cases)
    assert "unapproved" in cases[0].outcome.lower()
    assert "exactly one" in cases[1].outcome.lower()
    assert "malicious" in cases[2].outcome.lower()
    assert "missing" in cases[3].outcome.lower()
    assert "committed" in cases[4].outcome.lower()


def test_demo_case_selection_rejects_unknown_or_conflicting_aliases_before_any_write() -> None:
    """A typo or an ambiguous tour request fails while selection is still entirely read-only."""
    with pytest.raises(DemoCaseSelectionError, match="unknown guided demo case"):
        select_guided_demo_cases(("not-a-case",))

    with pytest.raises(DemoCaseSelectionError, match="cannot be combined"):
        select_guided_demo_cases(("safety-tour", "scenario-b-capacity"))


def _compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run an isolated Compose command while retaining diagnostics for the real demo test."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.integration
@pytest.mark.scenario
def test_guided_demo_stages_real_a_and_c_pending_plans_without_live_provider_or_effect(
    disposable_database: str,
) -> None:
    """The local runner resets once, uses fake planning, and leaves all effects pending review."""
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
        "from enterprise_agent.application.guided_demo import run_guided_demo\n"
        "database_url = environ['DATABASE_URL']\n"
        "demo = run_guided_demo(\n"
        "    database_url,\n"
        "    case_ids=('scenario-a-reroute-bait', 'scenario-c-pending-review'),\n"
        "    allow_test_database=True,\n"
        ")\n"
        "assert [item.case.case_id for item in demo.results] == [\n"
        "    'scenario-a-reroute-bait', 'scenario-c-pending-review'\n"
        "]\n"
        "assert demo.results[0].scenario_a_pending is not None\n"
        "assert demo.results[1].scenario_c_pending is not None\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    approvals = connection.execute(text(\"SELECT COUNT(*) FROM approvals WHERE status = 'pending'\")).scalar_one()\n"
        '    workflows = connection.execute(text("SELECT COUNT(*) FROM workflow_instances")).scalar_one()\n'
        '    effects = connection.execute(text("SELECT COUNT(*) FROM tool_invocations")).scalar_one()\n'
        "    provider_calls = connection.execute(text(\"SELECT COUNT(*) FROM audit_events WHERE event_type = 'llm.completed'\")).scalar_one()\n"
        "    held_purchase_orders = connection.execute(text(\"SELECT COUNT(*) FROM purchase_orders WHERE status = 'on_hold'\")).scalar_one()\n"
        "assert approvals == 2 and workflows == 1 and effects == 0\n"
        "assert provider_calls == 0 and held_purchase_orders == 0\n"
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
