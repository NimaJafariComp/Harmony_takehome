"""Durable PostgreSQL scheduled-task storage and lease-claiming contracts."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from enterprise_agent.domain import ScheduledTask, ScheduledTaskId, ScheduledTaskStatus

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
TASK_ID = ScheduledTaskId("00000000-0000-0000-0000-000000000a01")
FUTURE_TASK_ID = ScheduledTaskId("00000000-0000-0000-0000-000000000a02")


@dataclass(frozen=True)
class FixedClock:
    """Small clock double proving scheduler persistence does not use wall time."""

    current_at: datetime

    def now(self) -> datetime:
        """Return the fixed deterministic business time."""
        return self.current_at


def task(*, task_id: ScheduledTaskId = TASK_ID, due_at: datetime = NOW) -> ScheduledTask:
    """Build one fresh arrival check suitable for durable scheduling."""
    return ScheduledTask(
        task_id=task_id,
        task_type="arrival_check",
        due_at=due_at,
        status=ScheduledTaskStatus.PENDING,
        idempotency_key=f"arrival-check:{task_id}",
        payload={"purchase_order_id": "00000000-0000-0000-0000-000000000499"},
        attempt_count=0,
        lease_expires_at=None,
        completed_at=None,
    )


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for scheduler-contract failures."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.unit
def test_scheduler_rejects_invalid_new_task_state_claim_limit_and_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed scheduling requests fail before they can mutate durable work."""
    from enterprise_agent.adapters import scheduler

    engine = MagicMock()
    monkeypatch.setattr(scheduler, "create_engine", lambda _: engine)
    adapter = scheduler.PostgresSchedulerAdapter(
        "postgresql+psycopg://ignored", FixedClock(NOW)
    )

    with pytest.raises(ValueError, match="pending"):
        adapter.schedule(replace(task(), status=ScheduledTaskStatus.CLAIMED))
    with pytest.raises(ValueError, match="positive"):
        adapter.claim_due(NOW, limit=0)
    with pytest.raises(ValueError, match="positive"):
        scheduler.PostgresSchedulerAdapter(
            "postgresql+psycopg://ignored", FixedClock(NOW), lease_duration=timedelta()
        )

    engine.begin.assert_not_called()


@pytest.mark.critical
@pytest.mark.integration
def test_postgres_scheduler_persists_idempotently_claims_safely_and_reclaims_expired_work(
    disposable_database: str,
) -> None:
    """One durable task is leased once, recoverable after expiry, and fenced on completion."""
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
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresSchedulerAdapter,\n"
        "    ScheduledTaskClaimLostError,\n"
        ")\n"
        "from enterprise_agent.domain import ScheduledTask, ScheduledTaskId, ScheduledTaskStatus\n"
        "from enterprise_agent.ports import SchedulerPort\n"
        "class FixedClock:\n"
        "    def __init__(self, current_at): self.current_at = current_at\n"
        "    def now(self): return self.current_at\n"
        "now = datetime(2026, 8, 24, 9, tzinfo=UTC)\n"
        "database_url = environ['DATABASE_URL']\n"
        "scheduler = PostgresSchedulerAdapter(database_url, FixedClock(now))\n"
        "assert isinstance(scheduler, SchedulerPort)\n"
        "task_id = ScheduledTaskId('00000000-0000-0000-0000-000000000a01')\n"
        "due = ScheduledTask(task_id=task_id, task_type='arrival_check', due_at=now, status=ScheduledTaskStatus.PENDING, idempotency_key='arrival-check:po-499', payload={'purchase_order_id': '00000000-0000-0000-0000-000000000499'}, attempt_count=0, lease_expires_at=None, completed_at=None)\n"
        "future = ScheduledTask(task_id=ScheduledTaskId('00000000-0000-0000-0000-000000000a02'), task_type='arrival_check', due_at=now + timedelta(days=1), status=ScheduledTaskStatus.PENDING, idempotency_key='arrival-check:future', payload={'purchase_order_id': '00000000-0000-0000-0000-000000000498'}, attempt_count=0, lease_expires_at=None, completed_at=None)\n"
        "scheduler.schedule(due)\n"
        "scheduler.schedule(due)\n"
        "scheduler.schedule(future)\n"
        "claimed = scheduler.claim_due(now, limit=10)\n"
        "assert len(claimed) == 1 and claimed[0].task_id == task_id\n"
        "assert claimed[0].status is ScheduledTaskStatus.CLAIMED and claimed[0].attempt_count == 1\n"
        "assert claimed[0].lease_expires_at == now + timedelta(minutes=5)\n"
        "assert scheduler.claim_due(now + timedelta(minutes=1), limit=10) == ()\n"
        "try:\n"
        "    scheduler.mark_succeeded(task_id, now + timedelta(minutes=5))\n"
        "except ScheduledTaskClaimLostError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('a worker with an expired lease cannot complete the task')\n"
        "restarted = PostgresSchedulerAdapter(database_url, FixedClock(now + timedelta(minutes=6)))\n"
        "reclaimed = restarted.claim_due(now + timedelta(minutes=6), limit=1)\n"
        "assert len(reclaimed) == 1 and reclaimed[0].task_id == task_id\n"
        "assert reclaimed[0].attempt_count == 2 and reclaimed[0].lease_expires_at == now + timedelta(minutes=11)\n"
        "restarted.mark_succeeded(task_id, now + timedelta(minutes=7))\n"
        "with create_engine(database_url).connect() as connection:\n"
        "    rows = connection.execute(text(\"SELECT status, attempt_count, completed_at, created_at FROM scheduled_tasks ORDER BY id\")).mappings().all()\n"
        "assert len(rows) == 2\n"
        "assert rows[0]['status'] == 'succeeded' and rows[0]['attempt_count'] == 2\n"
        "assert rows[0]['completed_at'] == now + timedelta(minutes=7) and rows[0]['created_at'] == now\n"
        "assert rows[1]['status'] == 'pending'\n"
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
