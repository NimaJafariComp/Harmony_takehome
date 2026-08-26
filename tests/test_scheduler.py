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


def mapping_result(
    *,
    one: dict[str, object] | None = None,
    all_rows: list[dict[str, object]] | None = None,
    scalar: str | None = None,
) -> MagicMock:
    """Build the narrow SQLAlchemy result shape used by scheduler adapter methods."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.mappings.return_value.one.return_value = one
    result.mappings.return_value.all.return_value = [] if all_rows is None else all_rows
    return result


def scheduled_row(
    value: ScheduledTask,
    *,
    status: ScheduledTaskStatus = ScheduledTaskStatus.PENDING,
    attempt_count: int = 0,
    lease_expires_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict[str, object]:
    """Serialize one task into the selected database-row contract."""
    return {
        "id": str(value.task_id),
        "task_type": value.task_type,
        "due_at": value.due_at,
        "status": status.value,
        "idempotency_key": value.idempotency_key,
        "payload": dict(value.payload),
        "attempt_count": attempt_count,
        "lease_expires_at": lease_expires_at,
        "completed_at": completed_at,
    }


@pytest.mark.unit
def test_scheduler_rejects_invalid_new_task_state_claim_limit_and_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed scheduling requests fail before they can mutate durable work."""
    from enterprise_agent.adapters import scheduler

    engine = MagicMock()
    monkeypatch.setattr(scheduler, "create_engine", lambda _: engine)
    adapter = scheduler.PostgresSchedulerAdapter("postgresql+psycopg://ignored", FixedClock(NOW))

    with pytest.raises(ValueError, match="pending"):
        adapter.schedule(replace(task(), status=ScheduledTaskStatus.CLAIMED))
    with pytest.raises(ValueError, match="zero attempts"):
        adapter.schedule(replace(task(), attempt_count=1))
    with pytest.raises(ValueError, match="lease or completion"):
        adapter.schedule(replace(task(), lease_expires_at=NOW + timedelta(minutes=5)))
    with pytest.raises(ValueError, match="nonblank"):
        adapter.schedule(replace(task(), task_type=""))
    with pytest.raises(ValueError, match="positive"):
        adapter.claim_due(NOW, limit=0)
    with pytest.raises(ValueError, match="positive"):
        scheduler.PostgresSchedulerAdapter(
            "postgresql+psycopg://ignored", FixedClock(NOW), lease_duration=timedelta()
        )
    with pytest.raises(ValueError, match="timezone"):
        adapter.schedule(replace(task(), due_at=NOW.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="timezone"):
        adapter.claim_due(NOW.replace(tzinfo=None), limit=1)
    with pytest.raises(ValueError, match="timezone"):
        adapter.mark_succeeded(TASK_ID, NOW.replace(tzinfo=None))

    engine.begin.assert_not_called()


@pytest.mark.unit
def test_scheduler_serializes_replays_claims_in_due_order_and_fences_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter preserves immutable keys, deterministic clock time, lease, and task transitions."""
    from enterprise_agent.adapters import scheduler

    due = task()
    later = task(task_id=FUTURE_TASK_ID, due_at=NOW + timedelta(minutes=1))
    engine = MagicMock()
    transaction = engine.begin.return_value.__enter__.return_value
    transaction.execute.side_effect = [
        mapping_result(scalar=str(due.task_id)),
        mapping_result(scalar=None),
        mapping_result(one=scheduled_row(due)),
        mapping_result(scalar=None),
        mapping_result(one=scheduled_row(replace(due, task_id=FUTURE_TASK_ID))),
        mapping_result(
            all_rows=[
                scheduled_row(
                    later,
                    status=ScheduledTaskStatus.CLAIMED,
                    attempt_count=1,
                    lease_expires_at=NOW + timedelta(minutes=5),
                ),
                scheduled_row(
                    due,
                    status=ScheduledTaskStatus.CLAIMED,
                    attempt_count=1,
                    lease_expires_at=NOW + timedelta(minutes=5),
                ),
            ]
        ),
        mapping_result(scalar=str(due.task_id)),
        mapping_result(scalar=None),
    ]
    monkeypatch.setattr(scheduler, "create_engine", lambda _: engine)
    adapter = scheduler.PostgresSchedulerAdapter("postgresql+psycopg://ignored", FixedClock(NOW))

    adapter.schedule(due)
    adapter.schedule(due)
    with pytest.raises(scheduler.ScheduledTaskIdempotencyError, match="different task input"):
        adapter.schedule(due)
    claimed = adapter.claim_due(NOW, limit=2)
    adapter.mark_succeeded(due.task_id, NOW + timedelta(minutes=1))
    with pytest.raises(scheduler.ScheduledTaskClaimLostError, match="live lease"):
        adapter.mark_succeeded(due.task_id, NOW + timedelta(minutes=1))

    assert [item.task_id for item in claimed] == [due.task_id, later.task_id]
    assert transaction.execute.call_args_list[0].args[1]["created_at"] == NOW
    assert transaction.execute.call_args_list[5].args[1]["lease_expires_at"] == NOW + timedelta(
        minutes=5
    )
    assert transaction.execute.call_args_list[6].args[1]["succeeded_status"] == "succeeded"


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
        '    rows = connection.execute(text("SELECT status, attempt_count, completed_at, created_at FROM scheduled_tasks ORDER BY id")).mappings().all()\n'
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
