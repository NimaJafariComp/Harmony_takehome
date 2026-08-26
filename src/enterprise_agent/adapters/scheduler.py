"""Atomic PostgreSQL persistence and leasing for deterministic scheduled work."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, RowMapping

from enterprise_agent.domain import ScheduledTask, ScheduledTaskId, ScheduledTaskStatus
from enterprise_agent.ports import ClockPort

DEFAULT_TASK_LEASE_DURATION = timedelta(minutes=5)

INSERT_SCHEDULED_TASK = text("""
    INSERT INTO scheduled_tasks (
        id, attention_id, workflow_instance_id, task_type, due_at, status, idempotency_key,
        payload, attempt_count, lease_expires_at, completed_at, created_at, updated_at
    ) VALUES (
        CAST(:task_id AS UUID), NULL, NULL, :task_type, :due_at, :status, :idempotency_key,
        CAST(:payload AS JSONB), 0, NULL, NULL, :created_at, :created_at
    )
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id::text AS id
""")
SELECT_TASK_BY_IDEMPOTENCY_KEY = text("""
    SELECT id::text AS id, task_type, due_at, status, idempotency_key, payload, attempt_count,
           lease_expires_at, completed_at
    FROM scheduled_tasks
    WHERE idempotency_key = :idempotency_key
""")
CLAIM_DUE_TASKS = text("""
    WITH due AS (
        SELECT id
        FROM scheduled_tasks
        WHERE (status = :pending_status AND due_at <= :now)
           OR (status = :claimed_status AND lease_expires_at <= :now)
        ORDER BY due_at ASC, id ASC
        FOR UPDATE SKIP LOCKED
        LIMIT :limit
    )
    UPDATE scheduled_tasks AS task
    SET status = :claimed_status,
        attempt_count = task.attempt_count + 1,
        lease_expires_at = :lease_expires_at,
        updated_at = :now
    FROM due
    WHERE task.id = due.id
    RETURNING task.id::text AS id, task.task_type, task.due_at, task.status,
              task.idempotency_key, task.payload, task.attempt_count, task.lease_expires_at,
              task.completed_at
""")
MARK_TASK_SUCCEEDED = text("""
    UPDATE scheduled_tasks
    SET status = :succeeded_status,
        lease_expires_at = NULL,
        completed_at = :completed_at,
        updated_at = :completed_at
    WHERE id = CAST(:task_id AS UUID)
      AND status = :claimed_status
      AND lease_expires_at > :completed_at
    RETURNING id::text AS id
""")


class ScheduledTaskClaimLostError(RuntimeError):
    """Raised when a task completion is attempted without a still-live durable lease."""


class ScheduledTaskIdempotencyError(RuntimeError):
    """Raised when a stable task key is reused for different immutable task input."""


class PostgresSchedulerAdapter:
    """Store deterministic task work and atomically lease due rows without a queue runtime."""

    def __init__(
        self,
        database_url: str,
        clock: ClockPort,
        *,
        lease_duration: timedelta = DEFAULT_TASK_LEASE_DURATION,
    ) -> None:
        """Connect to one database and require a positive deterministic lease duration."""
        if lease_duration <= timedelta():
            raise ValueError("scheduled task lease duration must be positive")
        self._engine: Engine = create_engine(database_url)
        self._clock = clock
        self._lease_duration = lease_duration

    def schedule(self, task: ScheduledTask) -> None:
        """Insert one fresh task or prove a replay matches its immutable stable key."""
        _validate_new_task(task)
        created_at = self._clock.now()
        _require_timezone(created_at, name="clock time")
        with self._engine.begin() as connection:
            inserted = connection.execute(INSERT_SCHEDULED_TASK, _task_parameters(task, created_at))
            if inserted.scalar_one_or_none() is not None:
                return
            existing = (
                connection.execute(
                    SELECT_TASK_BY_IDEMPOTENCY_KEY,
                    {"idempotency_key": task.idempotency_key},
                )
                .mappings()
                .one()
            )
            if not _matches_scheduled_task(existing, task):
                raise ScheduledTaskIdempotencyError(
                    "scheduled task idempotency key is already bound to different task input"
                )

    def claim_due(self, now: datetime, limit: int) -> tuple[ScheduledTask, ...]:
        """Atomically lease due or expired work once, in deterministic due-time order."""
        _require_timezone(now, name="claim time")
        if limit <= 0:
            raise ValueError("scheduled task claim limit must be positive")
        lease_expires_at = now + self._lease_duration
        with self._engine.begin() as connection:
            rows = (
                connection.execute(
                    CLAIM_DUE_TASKS,
                    {
                        "now": now,
                        "limit": limit,
                        "lease_expires_at": lease_expires_at,
                        "pending_status": ScheduledTaskStatus.PENDING.value,
                        "claimed_status": ScheduledTaskStatus.CLAIMED.value,
                    },
                )
                .mappings()
                .all()
            )
        return tuple(sorted((_task_from_row(row) for row in rows), key=_task_order_key))

    def mark_succeeded(self, task_id: ScheduledTaskId, completed_at: datetime) -> None:
        """Complete only a currently claimed task before its lease expires."""
        _require_timezone(completed_at, name="completion time")
        with self._engine.begin() as connection:
            completed = connection.execute(
                MARK_TASK_SUCCEEDED,
                {
                    "task_id": str(task_id),
                    "completed_at": completed_at,
                    "claimed_status": ScheduledTaskStatus.CLAIMED.value,
                    "succeeded_status": ScheduledTaskStatus.SUCCEEDED.value,
                },
            ).scalar_one_or_none()
        if completed is None:
            raise ScheduledTaskClaimLostError(
                "scheduled task is not claimed with a live lease for completion"
            )


def _validate_new_task(task: ScheduledTask) -> None:
    """Ensure storage begins only at the first immutable pending-task lifecycle state."""
    if task.status is not ScheduledTaskStatus.PENDING:
        raise ValueError("new scheduled tasks must be pending")
    if task.attempt_count != 0:
        raise ValueError("new scheduled tasks must have zero attempts")
    if task.lease_expires_at is not None or task.completed_at is not None:
        raise ValueError("new scheduled tasks cannot have a lease or completion timestamp")
    if not task.task_type.strip() or not task.idempotency_key.strip():
        raise ValueError("scheduled task type and idempotency key must be nonblank")
    _require_timezone(task.due_at, name="task due time")


def _require_timezone(value: datetime, *, name: str) -> None:
    """Reject naive business timestamps before PostgreSQL can normalize them implicitly."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def _task_parameters(task: ScheduledTask, created_at: datetime) -> dict[str, object]:
    """Serialize only the fresh immutable fields that a scheduler may create."""
    return {
        "task_id": str(task.task_id),
        "task_type": task.task_type,
        "due_at": task.due_at,
        "status": ScheduledTaskStatus.PENDING.value,
        "idempotency_key": task.idempotency_key,
        "payload": json.dumps(dict(task.payload), sort_keys=True),
        "created_at": created_at,
    }


def _matches_scheduled_task(row: RowMapping, task: ScheduledTask) -> bool:
    """Accept an idempotent replay only when immutable task identity and input still agree."""
    return (
        str(row["id"]) == str(task.task_id)
        and row["task_type"] == task.task_type
        and row["due_at"] == task.due_at
        and row["idempotency_key"] == task.idempotency_key
        and dict(cast(Mapping[str, object], row["payload"])) == dict(task.payload)
    )


def _task_from_row(row: RowMapping) -> ScheduledTask:
    """Map the scheduler's persisted shape into its immutable port contract."""
    return ScheduledTask(
        task_id=ScheduledTaskId(str(row["id"])),
        task_type=cast(str, row["task_type"]),
        due_at=cast(datetime, row["due_at"]),
        status=ScheduledTaskStatus(cast(str, row["status"])),
        idempotency_key=cast(str, row["idempotency_key"]),
        payload=dict(cast(Mapping[str, object], row["payload"])),
        attempt_count=cast(int, row["attempt_count"]),
        lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
        completed_at=cast(datetime | None, row["completed_at"]),
    )


def _task_order_key(task: ScheduledTask) -> tuple[datetime, str]:
    """Keep the port result ordered even though SQL UPDATE RETURNING is unordered."""
    return (task.due_at, str(task.task_id))
