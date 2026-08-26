"""Sanitized, append-only PostgreSQL audit-event persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from math import isfinite
from typing import cast
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, RowMapping

from enterprise_agent.domain import (
    AttentionId,
    AuditEvent,
    AuditEventId,
    EvidenceId,
    PlanId,
    RunId,
    UserId,
    WorkflowId,
)

REQUIRED_AUDIT_EVENT_TYPES = frozenset(
    {
        "attention.detected",
        "attention.deduplicated",
        "context.gathered",
        "evidence.observed",
        "llm.completed",
        "planner.recommended",
        "gate.allowed",
        "gate.denied",
        "approval.requested",
        "approval.rerouted",
        "approval.approved",
        "approval.rejected",
        "workflow.started",
        "workflow.step_started",
        "workflow.step_completed",
        "workflow.failed",
        "tool.started",
        "tool.succeeded",
        "tool.failed",
        "compensation.started",
        "compensation.completed",
        "schedule.created",
        "schedule.fired",
        "followup.resolved",
        "followup.reopened",
    }
)
_ADDITIONAL_AUDIT_EVENT_TYPES = frozenset({"attention.status_changed"})
_SUPPORTED_AUDIT_EVENT_TYPES = REQUIRED_AUDIT_EVENT_TYPES | _ADDITIONAL_AUDIT_EVENT_TYPES
_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "password",
        "provider_response",
        "raw_response",
        "refresh_token",
        "secret",
        "token",
    }
)
_REDACTED = "[redacted]"

INSERT_AUDIT_EVENT = text("""
    INSERT INTO audit_events (
        id, occurred_at, event_type, run_id, actor_id, attention_id, workflow_instance_id,
        plan_id, evidence_ids, payload, policy_version, plan_hash, idempotency_key,
        failure_category
    ) VALUES (
        CAST(:event_id AS UUID), :occurred_at, :event_type, :run_id, CAST(:actor_id AS UUID),
        CAST(:attention_id AS UUID), CAST(:workflow_id AS UUID), CAST(:plan_id AS UUID),
        CAST(:evidence_ids AS JSONB), CAST(:payload AS JSONB), :policy_version, :plan_hash,
        :idempotency_key, :failure_category
    )
""")
SELECT_AUDIT_EVENTS_FOR_RUN = text("""
    SELECT id, occurred_at, event_type, run_id, actor_id, attention_id, workflow_instance_id,
           plan_id, evidence_ids, payload, policy_version, plan_hash, idempotency_key,
           failure_category
    FROM audit_events
    WHERE run_id = :run_id
    ORDER BY occurred_at ASC, id ASC
""")
SELECT_LATEST_AUDIT_RUN_FOR_PLAN = text("""
    SELECT run_id
    FROM audit_events
    WHERE plan_id = CAST(:plan_id AS UUID)
    ORDER BY occurred_at DESC, id DESC
    LIMIT 1
""")
SELECT_LLM_USAGE_EVENTS = text("""
    SELECT id, occurred_at, event_type, run_id, actor_id, attention_id, workflow_instance_id,
           plan_id, evidence_ids, payload, policy_version, plan_hash, idempotency_key,
           failure_category
    FROM audit_events
    WHERE event_type = 'llm.completed'
    ORDER BY occurred_at ASC, id ASC
""")


class AuditEventError(ValueError):
    """Raised when an audit event is unsafe or cannot be faithfully persisted."""


class PostgresAuditAdapter:
    """Persist and read sanitized material events without permitting ledger mutation."""

    def __init__(self, database_url: str) -> None:
        """Connect this audit adapter to one durable PostgreSQL ledger."""
        self._engine: Engine = create_engine(database_url)

    def append(self, event: AuditEvent) -> None:
        """Insert one validated event as a new immutable ledger record."""
        _validate_event(event)
        with self._engine.begin() as connection:
            append_audit_event(connection, event)

    def events_for_run(self, run_id: RunId) -> Sequence[AuditEvent]:
        """Return one deterministic chronological run ledger for audit-only reconstruction."""
        with self._engine.connect() as connection:
            rows = (
                connection.execute(SELECT_AUDIT_EVENTS_FOR_RUN, {"run_id": str(run_id)})
                .mappings()
                .all()
            )
        return tuple(_event_from_row(row) for row in rows)

    def latest_run_for_plan(self, plan_id: PlanId) -> RunId | None:
        """Find the existing append-only run that owns a later approval decision event."""
        with self._engine.connect() as connection:
            run_id = connection.execute(
                SELECT_LATEST_AUDIT_RUN_FOR_PLAN,
                {"plan_id": str(plan_id)},
            ).scalar_one_or_none()
        return None if run_id is None else RunId(str(run_id))

    def llm_usage_events(self) -> Sequence[AuditEvent]:
        """Return immutable LLM completion events for read-only metering aggregation only."""
        with self._engine.connect() as connection:
            rows = connection.execute(SELECT_LLM_USAGE_EVENTS).mappings().all()
        return tuple(_event_from_row(row) for row in rows)


def append_audit_event(connection: Connection, event: AuditEvent) -> None:
    """Write one event inside an owning adapter transaction without opening a second transaction."""
    _validate_event(event)
    connection.execute(INSERT_AUDIT_EVENT, _event_parameters(event))


def _validate_event(event: AuditEvent) -> None:
    """Require supported event vocabulary, durable primary key, and unambiguous business time."""
    if event.event_type not in _SUPPORTED_AUDIT_EVENT_TYPES:
        raise AuditEventError(f"unsupported event type: {event.event_type}")
    _require_timezone(event.occurred_at)
    _require_uuid(event.event_id, name="audit event ID")
    for value, name in (
        (event.actor_id, "actor ID"),
        (event.attention_id, "attention ID"),
        (event.workflow_id, "workflow ID"),
        (event.plan_id, "plan ID"),
    ):
        if value is not None:
            _require_uuid(value, name=name)
    _sanitize_payload(event.payload)


def _event_parameters(event: AuditEvent) -> dict[str, object]:
    """Convert a validated event into fully bound and sanitized SQL parameters."""
    return {
        "event_id": str(event.event_id),
        "occurred_at": event.occurred_at,
        "event_type": event.event_type,
        "run_id": str(event.run_id),
        "actor_id": _optional_identifier(event.actor_id),
        "attention_id": _optional_identifier(event.attention_id),
        "workflow_id": _optional_identifier(event.workflow_id),
        "plan_id": _optional_identifier(event.plan_id),
        "evidence_ids": json.dumps([str(item) for item in event.evidence_ids]),
        "payload": json.dumps(_sanitize_payload(event.payload), sort_keys=True),
        "policy_version": event.policy_version,
        "plan_hash": event.plan_hash,
        "idempotency_key": event.idempotency_key,
        "failure_category": event.failure_category,
    }


def _event_from_row(row: RowMapping) -> AuditEvent:
    """Map one chronological ledger row back to its immutable domain contract."""
    evidence_ids = row["evidence_ids"]
    payload = row["payload"]
    if (
        not isinstance(evidence_ids, Sequence)
        or isinstance(evidence_ids, str | bytes)
        or not isinstance(payload, Mapping)
    ):
        raise AuditEventError("persisted audit event has an invalid JSON shape")
    return AuditEvent(
        event_id=AuditEventId(str(row["id"])),
        occurred_at=cast(datetime, row["occurred_at"]),
        event_type=cast(str, row["event_type"]),
        run_id=RunId(str(row["run_id"])),
        actor_id=_optional_user_id(row["actor_id"]),
        attention_id=_optional_attention_id(row["attention_id"]),
        workflow_id=_optional_workflow_id(row["workflow_instance_id"]),
        plan_id=_optional_plan_id(row["plan_id"]),
        evidence_ids=tuple(EvidenceId(str(item)) for item in evidence_ids),
        payload=dict(cast(Mapping[str, object], payload)),
        policy_version=cast(str | None, row["policy_version"]),
        plan_hash=cast(str | None, row["plan_hash"]),
        idempotency_key=cast(str | None, row["idempotency_key"]),
        failure_category=cast(str | None, row["failure_category"]),
    )


def _sanitize_payload(value: object, *, key: str | None = None) -> object:
    """Recursively retain JSON-safe facts while redacting credential and raw-response fields."""
    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise AuditEventError("audit payload contains non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AuditEventError("audit payload contains non-finite decimal")
        return str(value)
    if isinstance(value, datetime):
        _require_timezone(value)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):
                raise AuditEventError("audit payload keys must be strings")
            sanitized[child_key] = _sanitize_payload(child_value, key=child_key)
        return sanitized
    if isinstance(value, tuple | list):
        return [_sanitize_payload(item) for item in value]
    raise AuditEventError(
        f"audit payload contains unsupported payload value: {type(value).__name__}"
    )


def _is_sensitive_key(key: str) -> bool:
    """Recognize a deliberately small credential/raw-provider key vocabulary case-insensitively."""
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_PAYLOAD_KEYS or normalized.endswith("_api_key")


def _require_timezone(value: datetime) -> None:
    """Prevent implicit local-time interpretation in a chronological durable ledger."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise AuditEventError("audit event time must include a timezone")


def _require_uuid(value: object, *, name: str) -> None:
    """Fail before opening a transaction when a PostgreSQL UUID field has an invalid value."""
    try:
        UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise AuditEventError(f"{name} must be a UUID") from error


def _optional_identifier(value: object | None) -> str | None:
    """Return the already validated nullable identifier in the form expected by bound SQL."""
    return None if value is None else str(value)


def _optional_user_id(value: object | None) -> UserId | None:
    """Map a nullable persisted actor foreign key into its domain ID."""
    return None if value is None else UserId(str(value))


def _optional_attention_id(value: object | None) -> AttentionId | None:
    """Map a nullable persisted attention foreign key into its domain ID."""
    return None if value is None else AttentionId(str(value))


def _optional_workflow_id(value: object | None) -> WorkflowId | None:
    """Map a nullable persisted workflow foreign key into its domain ID."""
    return None if value is None else WorkflowId(str(value))


def _optional_plan_id(value: object | None) -> PlanId | None:
    """Map a nullable persisted plan foreign key into its domain ID."""
    return None if value is None else PlanId(str(value))
