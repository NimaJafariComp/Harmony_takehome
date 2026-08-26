"""Atomic PostgreSQL persistence for deduplicated attention items and their audit evidence."""

from __future__ import annotations

import hashlib
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
    AttentionTrigger,
    AuditEvent,
    AuditEventId,
    Evidence,
    EvidenceId,
    InvalidAttentionTransitionError,
    RunId,
    require_attention_transition,
)

from .audit import append_audit_event

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
SELECT_ATTENTION_BY_ID = text("""
    SELECT id, scenario, cause, dedupe_key, status, source_versions, created_at, resolved_at
    FROM attention_items
    WHERE id = CAST(:attention_id AS UUID)
""")
UPDATE_ATTENTION_STATUS = text("""
    UPDATE attention_items
    SET status = :target_status, resolved_at = :resolved_at
    WHERE id = CAST(:attention_id AS UUID) AND status = :current_status
    RETURNING id, scenario, cause, dedupe_key, status, source_versions, created_at, resolved_at
""")


class PostgresAttentionAdapter:
    """Persist detector attempts and lifecycle changes in the same transaction as audit evidence."""

    def __init__(self, database_url: str) -> None:
        """Connect this attention adapter to one durable PostgreSQL database."""
        self._engine: Engine = create_engine(database_url)

    def register(self, trigger: AttentionTrigger, run_id: RunId) -> AttentionRegistration:
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

    def load(self, attention_id: AttentionId) -> AttentionItem | None:
        """Load exactly one durable attention item for a later causal follow-up transition."""
        with self._engine.connect() as connection:
            row = (
                connection.execute(SELECT_ATTENTION_BY_ID, {"attention_id": str(attention_id)})
                .mappings()
                .one_or_none()
            )
        return None if row is None else _attention_from_row(row)

    def register_arrival_followup(
        self,
        *,
        original_attention: AttentionItem,
        purchase_order: Evidence,
        run_id: RunId,
        detected_at: datetime,
    ) -> AttentionRegistration:
        """Create or reuse one source-version-bound missing-receipt attention item."""
        source_versions = {
            f"purchase_order:{purchase_order.record_id}": purchase_order.source_version
        }
        dedupe_key = _arrival_followup_dedupe_key(
            original_attention.attention_id,
            purchase_order.record_id,
            source_versions,
        )
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    INSERT_ATTENTION,
                    {
                        "attention_id": str(uuid4()),
                        "scenario": "scenario_a",
                        "cause": "arrival_check",
                        "dedupe_key": dedupe_key,
                        "status": AttentionStatus.OPEN.value,
                        "source_versions": json.dumps(source_versions, sort_keys=True),
                        "created_at": detected_at,
                    },
                )
                .mappings()
                .one_or_none()
            )
            created = row is not None
            if row is None:
                row = (
                    connection.execute(SELECT_ATTENTION_BY_KEY, {"dedupe_key": dedupe_key})
                    .mappings()
                    .one()
                )
            attention = _attention_from_row(row)
            _append_audit_attempt(
                connection=connection,
                attention=attention,
                trigger=None,
                run_id=run_id,
                event_type="followup.reopened",
                occurred_at=detected_at,
                payload={
                    "created": created,
                    "original_attention_id": str(original_attention.attention_id),
                    "purchase_order_id": purchase_order.record_id,
                },
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
                event_type=(
                    "followup.resolved"
                    if target is AttentionStatus.RESOLVED
                    else "attention.status_changed"
                ),
                occurred_at=occurred_at,
                payload={"from_status": attention.status.value, "to_status": target.value},
            )
        return updated


_TERMINAL_STATUSES = frozenset({AttentionStatus.RESOLVED, AttentionStatus.CANCELLED})


def _insert_parameters(trigger: AttentionTrigger) -> dict[str, object]:
    """Bind one immutable trigger into the insert statement without caller SQL interpolation."""
    return {
        "attention_id": str(uuid4()),
        "scenario": trigger.scenario,
        "cause": trigger.cause,
        "dedupe_key": trigger.dedupe_key,
        "status": AttentionStatus.OPEN.value,
        "source_versions": json.dumps(dict(trigger.source_versions)),
        "created_at": trigger.detected_at,
    }


def _arrival_followup_dedupe_key(
    original_attention_id: AttentionId,
    purchase_order_id: str,
    source_versions: Mapping[str, int],
) -> str:
    """Bind missing-receipt re-entry to causal attention, PO identity, and current source versions."""
    canonical = json.dumps(
        {
            "cause": "arrival_check",
            "original_attention_id": str(original_attention_id),
            "purchase_order_id": purchase_order_id,
            "source_versions": dict(source_versions),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"scenario_a:arrival_check:v1:{hashlib.sha256(canonical.encode()).hexdigest()}"


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
    trigger: AttentionTrigger | None,
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
        base_payload.update(trigger.audit_payload)
    append_audit_event(
        connection,
        AuditEvent(
            event_id=AuditEventId(str(uuid4())),
            occurred_at=occurred_at,
            event_type=event_type,
            run_id=run_id,
            actor_id=None,
            attention_id=attention.attention_id,
            workflow_id=None,
            plan_id=None,
            evidence_ids=(
                ()
                if trigger is None
                else tuple(EvidenceId(source) for source in sorted(trigger.source_versions))
            ),
            payload=base_payload,
            policy_version=None,
            plan_hash=None,
            idempotency_key=None,
            failure_category=None,
        ),
    )
