"""Atomic PostgreSQL persistence for deduplicated attention items and their audit evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, RowMapping

from enterprise_agent.domain import (
    AttentionId,
    AttentionItem,
    AttentionRegistration,
    AttentionStatus,
    InvalidAttentionTransitionError,
    RunId,
    ScenarioAStockoutTrigger,
    require_attention_transition,
)

INSERT_ATTENTION = text("""
    INSERT INTO attention_items (
        id, scenario, cause, dedupe_key, status, source_versions, created_at, resolved_at
    ) VALUES (
        CAST(:attention_id AS UUID), :scenario, :cause, :dedupe_key, :status,
        CAST(:source_versions AS JSONB), :created_at, NULL
    )
    ON CONFLICT (dedupe_key) DO NOTHING
    RETURNING id, scenario, cause, dedupe_key, status, source_versions, created_at, resolved_at
""")
SELECT_ATTENTION_BY_KEY = text("""
    SELECT id, scenario, cause, dedupe_key, status, source_versions, created_at, resolved_at
    FROM attention_items
    WHERE dedupe_key = :dedupe_key
""")
UPDATE_ATTENTION_STATUS = text("""
    UPDATE attention_items
    SET status = :target_status, resolved_at = :resolved_at
    WHERE id = CAST(:attention_id AS UUID) AND status = :current_status
    RETURNING id, scenario, cause, dedupe_key, status, source_versions, created_at, resolved_at
""")
INSERT_AUDIT_EVENT = text("""
    INSERT INTO audit_events (
        id, occurred_at, event_type, run_id, actor_id, attention_id, workflow_instance_id,
        plan_id, evidence_ids, payload, policy_version, plan_hash, idempotency_key,
        failure_category
    ) VALUES (
        CAST(:event_id AS UUID), :occurred_at, :event_type, :run_id, NULL,
        CAST(:attention_id AS UUID), NULL, NULL, CAST(:evidence_ids AS JSONB),
        CAST(:payload AS JSONB), NULL, NULL, NULL, NULL
    )
""")


class PostgresAttentionAdapter:
    """Persist detector attempts and lifecycle changes in the same transaction as audit evidence."""

    def __init__(self, database_url: str) -> None:
        """Connect this attention adapter to one durable PostgreSQL database."""
        self._engine: Engine = create_engine(database_url)

    def register(self, trigger: ScenarioAStockoutTrigger, run_id: RunId) -> AttentionRegistration:
        """Create one open attention item or return the existing item for an equivalent signal."""
        with self._engine.begin() as connection:
            row = (
                connection.execute(INSERT_ATTENTION, _insert_parameters(trigger))
                .mappings()
                .one_or_none()
            )
            created = row is not None
            if row is None:
                row = (
                    connection.execute(SELECT_ATTENTION_BY_KEY, {"dedupe_key": trigger.dedupe_key})
                    .mappings()
                    .one()
                )
            attention = _attention_from_row(row)
            _append_audit_attempt(
                connection=connection,
                attention=attention,
                trigger=trigger,
                run_id=run_id,
                event_type="attention.detected" if created else "attention.deduplicated",
                occurred_at=trigger.detected_at,
                payload={"created": created},
            )
        return AttentionRegistration(attention=attention, created=created)

    def transition(
        self,
        attention: AttentionItem,
        target: AttentionStatus,
        run_id: RunId,
        occurred_at: datetime,
    ) -> AttentionItem:
        """Advance an item only from its current status and retain the transition in audit."""
        require_attention_transition(attention.status, target)
        resolved_at = occurred_at if target in _TERMINAL_STATUSES else None
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    UPDATE_ATTENTION_STATUS,
                    {
                        "attention_id": str(attention.attention_id),
                        "current_status": attention.status.value,
                        "target_status": target.value,
                        "resolved_at": resolved_at,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise InvalidAttentionTransitionError(
                    "attention transition is no longer valid for the persisted current status"
                )
            updated = _attention_from_row(row)
            _append_audit_attempt(
                connection=connection,
                attention=updated,
                trigger=None,
                run_id=run_id,
                event_type="attention.status_changed",
                occurred_at=occurred_at,
                payload={"from_status": attention.status.value, "to_status": target.value},
            )
        return updated


_TERMINAL_STATUSES = frozenset({AttentionStatus.RESOLVED, AttentionStatus.CANCELLED})


def _insert_parameters(trigger: ScenarioAStockoutTrigger) -> dict[str, object]:
    """Bind one immutable trigger into the insert statement without caller SQL interpolation."""
    return {
        "attention_id": str(uuid4()),
        "scenario": "scenario_a",
        "cause": "projected_stockout",
        "dedupe_key": trigger.dedupe_key,
        "status": AttentionStatus.OPEN.value,
        "source_versions": json.dumps(dict(trigger.source_versions)),
        "created_at": trigger.detected_at,
    }


def _attention_from_row(row: RowMapping) -> AttentionItem:
    """Map one database row into its immutable domain counterpart."""
    source_versions = cast(Mapping[str, int], row["source_versions"])
    return AttentionItem(
        attention_id=AttentionId(str(row["id"])),
        scenario=cast(str, row["scenario"]),
        cause=cast(str, row["cause"]),
        dedupe_key=cast(str, row["dedupe_key"]),
        status=AttentionStatus(cast(str, row["status"])),
        created_at=cast(datetime, row["created_at"]),
        source_versions=dict(source_versions),
        resolved_at=cast(datetime | None, row["resolved_at"]),
    )


def _append_audit_attempt(
    *,
    connection: Connection,
    attention: AttentionItem,
    trigger: ScenarioAStockoutTrigger | None,
    run_id: RunId,
    event_type: str,
    occurred_at: datetime,
    payload: Mapping[str, object],
) -> None:
    """Append sanitized trigger or lifecycle metadata inside the caller's transaction."""
    base_payload: dict[str, object] = {
        "dedupe_key": attention.dedupe_key,
        "scenario": attention.scenario,
        "cause": attention.cause,
        **payload,
    }
    if trigger is not None:
        base_payload.update(
            {
                "detector": trigger.detector,
                "part_id": trigger.part_id,
                "production_order_id": trigger.production_order_id,
                "inventory_version": trigger.inventory_version,
                "production_start_date": trigger.production_start_date.isoformat(),
            }
        )
    connection.execute(
        INSERT_AUDIT_EVENT,
        {
            "event_id": str(uuid4()),
            "occurred_at": occurred_at,
            "event_type": event_type,
            "run_id": str(run_id),
            "attention_id": str(attention.attention_id),
            "evidence_ids": "[]",
            "payload": json.dumps(base_payload),
        },
    )
