"""PostgreSQL contract for the deterministic, approval-only Scenario C CLI service."""

from __future__ import annotations

import subprocess

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for the Scenario C demo-service contract."""
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
def test_scenario_c_demo_service_stages_one_pending_plan_without_auto_holding(
    disposable_database: str,
) -> None:
    """The deterministic CLI boundary only stages one reviewable plan for a freshly seeded database."""
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
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.application.scenario_c_demo import (\n"
        "    ScenarioCDeterministicRunError, stage_scenario_c_pending,\n"
        ")\n"
        "from enterprise_agent.domain import RunId\n"
        "from enterprise_agent.seed import ID_PO_C9001_W, reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "staged = stage_scenario_c_pending(database_url, run_id=RunId('run-scenario-c-cli-test'))\n"
        "assert str(staged.run_id) == 'run-scenario-c-cli-test'\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    status = connection.execute(text(\"SELECT status FROM purchase_orders WHERE id = CAST(:po_id AS UUID)\"), {'po_id': str(ID_PO_C9001_W)}).scalar_one()\n"
        "    messages = connection.execute(text(\"SELECT COUNT(*) FROM messages WHERE message_key LIKE 'tool:v1:%'\")).scalar_one()\n"
        "    approvals = connection.execute(text(\"SELECT COUNT(*) FROM approvals WHERE status = 'pending'\")).scalar_one()\n"
        "assert status == 'open' and messages == 0 and approvals == 1\n"
        "try:\n"
        "    stage_scenario_c_pending(database_url, run_id=RunId('run-scenario-c-cli-test-repeat'))\n"
        "except ScenarioCDeterministicRunError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('deterministic Scenario C service staged a duplicate pending plan')\n"
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
