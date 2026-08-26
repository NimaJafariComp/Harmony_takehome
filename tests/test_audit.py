"""Contracts for the sanitized, append-only PostgreSQL audit ledger."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from enterprise_agent.adapters import audit
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
from enterprise_agent.ports import AuditPort

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)
RUN_ID = RunId("run-audit-writer")
EVENT_ID = AuditEventId("00000000-0000-0000-0000-000000000901")


def audit_event(
    *,
    event_id: AuditEventId = EVENT_ID,
    occurred_at: datetime = NOW,
    event_type: str = "tool.succeeded",
    payload: dict[str, object] | None = None,
) -> AuditEvent:
    """Build one fully attributed material event for the durable-writer contract."""
    return AuditEvent(
        event_id=event_id,
        occurred_at=occurred_at,
        event_type=event_type,
        run_id=RUN_ID,
        actor_id=UserId("00000000-0000-0000-0000-000000000001"),
        attention_id=AttentionId("00000000-0000-0000-0000-000000000601"),
        workflow_id=WorkflowId("00000000-0000-0000-0000-000000000701"),
        plan_id=PlanId("00000000-0000-0000-0000-000000000801"),
        evidence_ids=(EvidenceId("erp:purchase_order:00000000-0000-0000-0000-000000000499"),),
        payload={"outcome": "succeeded"} if payload is None else payload,
        policy_version="scenario_a_policy:v1",
        plan_hash="sha256:immutable-plan",
        idempotency_key="workflow:701:step:3",
        failure_category=None,
    )


def mapping_result(*, all_rows: list[dict[str, object]] | None = None) -> MagicMock:
    """Build the narrow SQLAlchemy result shape the audit adapter consumes."""
    result = MagicMock()
    result.mappings.return_value.all.return_value = [] if all_rows is None else all_rows
    return result


def audit_row(value: AuditEvent) -> dict[str, object]:
    """Serialize an audit event into the row shape returned by the ledger query."""
    return {
        "id": str(value.event_id),
        "occurred_at": value.occurred_at,
        "event_type": value.event_type,
        "run_id": str(value.run_id),
        "actor_id": str(value.actor_id) if value.actor_id is not None else None,
        "attention_id": str(value.attention_id) if value.attention_id is not None else None,
        "workflow_instance_id": str(value.workflow_id) if value.workflow_id is not None else None,
        "plan_id": str(value.plan_id) if value.plan_id is not None else None,
        "evidence_ids": [str(item) for item in value.evidence_ids],
        "payload": dict(value.payload),
        "policy_version": value.policy_version,
        "plan_hash": value.plan_hash,
        "idempotency_key": value.idempotency_key,
        "failure_category": value.failure_category,
    }


@pytest.mark.unit
@pytest.mark.parametrize("event_type", sorted(audit.REQUIRED_AUDIT_EVENT_TYPES))
def test_audit_writer_accepts_every_required_material_event_type(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    """The shared writer has one explicit supported vocabulary for every required event family."""
    engine = MagicMock()
    transaction = engine.begin.return_value.__enter__.return_value
    monkeypatch.setattr(audit, "create_engine", lambda _: engine)
    adapter = audit.PostgresAuditAdapter("postgresql+psycopg://ignored")

    adapter.append(audit_event(event_type=event_type))

    assert isinstance(adapter, AuditPort)
    assert transaction.execute.call_args.args[1]["event_type"] == event_type


@pytest.mark.unit
def test_audit_writer_sanitizes_sensitive_payloads_and_preserves_typed_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secrets and raw provider output never reach the ledger, while safe facts remain explainable."""
    engine = MagicMock()
    transaction = engine.begin.return_value.__enter__.return_value
    monkeypatch.setattr(audit, "create_engine", lambda _: engine)
    adapter = audit.PostgresAuditAdapter("postgresql+psycopg://ignored")

    adapter.append(
        audit_event(
            payload={
                "api_key": "should-never-be-stored",
                "nested": {"authorization": "Bearer should-never-be-stored"},
                "provider_response": {"unbounded": "raw provider content"},
                "estimated_value": Decimal("240.50"),
                "observed_at": NOW,
            }
        )
    )

    parameters = transaction.execute.call_args.args[1]
    assert json.loads(parameters["payload"]) == {
        "api_key": "[redacted]",
        "nested": {"authorization": "[redacted]"},
        "provider_response": "[redacted]",
        "estimated_value": "240.50",
        "observed_at": NOW.isoformat(),
    }
    assert parameters["actor_id"] == "00000000-0000-0000-0000-000000000001"
    assert parameters["attention_id"] == "00000000-0000-0000-0000-000000000601"
    assert parameters["workflow_id"] == "00000000-0000-0000-0000-000000000701"
    assert parameters["plan_id"] == "00000000-0000-0000-0000-000000000801"
    assert json.loads(parameters["evidence_ids"]) == [
        "erp:purchase_order:00000000-0000-0000-0000-000000000499"
    ]


@pytest.mark.unit
def test_audit_writer_preserves_json_safe_scalar_date_uuid_and_sequence_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger retains structured, explainable values without accepting opaque provider objects."""
    engine = MagicMock()
    transaction = engine.begin.return_value.__enter__.return_value
    monkeypatch.setattr(audit, "create_engine", lambda _: engine)
    adapter = audit.PostgresAuditAdapter("postgresql+psycopg://ignored")

    adapter.append(
        audit_event(
            payload={
                "confidence": 0.75,
                "expected_receipt_date": NOW.date(),
                "replacement_id": UUID("00000000-0000-0000-0000-000000000499"),
                "candidate_ids": ("supplier-z", "supplier-y"),
            }
        )
    )

    assert json.loads(transaction.execute.call_args.args[1]["payload"]) == {
        "candidate_ids": ["supplier-z", "supplier-y"],
        "confidence": 0.75,
        "expected_receipt_date": NOW.date().isoformat(),
        "replacement_id": "00000000-0000-0000-0000-000000000499",
    }


@pytest.mark.unit
def test_audit_writer_rejects_unsafe_event_shapes_before_opening_a_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported event types, naïve times, invalid IDs, and opaque payloads fail closed."""
    engine = MagicMock()
    monkeypatch.setattr(audit, "create_engine", lambda _: engine)
    adapter = audit.PostgresAuditAdapter("postgresql+psycopg://ignored")

    with pytest.raises(audit.AuditEventError, match="unsupported event type"):
        adapter.append(audit_event(event_type="tool.unreviewed"))
    with pytest.raises(audit.AuditEventError, match="timezone"):
        adapter.append(audit_event(occurred_at=NOW.replace(tzinfo=None)))
    with pytest.raises(audit.AuditEventError, match="UUID"):
        adapter.append(audit_event(event_id=AuditEventId("not-a-uuid")))
    with pytest.raises(audit.AuditEventError, match="unsupported payload value"):
        adapter.append(audit_event(payload={"opaque": object()}))
    with pytest.raises(audit.AuditEventError, match="non-finite float"):
        adapter.append(audit_event(payload={"confidence": float("inf")}))
    with pytest.raises(audit.AuditEventError, match="non-finite decimal"):
        adapter.append(audit_event(payload={"estimated_value": Decimal("NaN")}))
    with pytest.raises(audit.AuditEventError, match="keys must be strings"):
        adapter.append(audit_event(payload={1: "not-json"}))  # type: ignore[dict-item]

    engine.begin.assert_not_called()


@pytest.mark.unit
def test_audit_writer_reads_a_chronological_run_ledger_without_mutating_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explainers receive immutable event records in timestamp and event-ID order from the ledger."""
    first = audit_event(
        event_id=AuditEventId("00000000-0000-0000-0000-000000000902"),
        occurred_at=NOW,
        event_type="attention.detected",
    )
    second = audit_event(
        event_id=AuditEventId("00000000-0000-0000-0000-000000000903"),
        occurred_at=NOW + timedelta(seconds=1),
        event_type="planner.recommended",
    )
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value = mapping_result(all_rows=[audit_row(first), audit_row(second)])
    monkeypatch.setattr(audit, "create_engine", lambda _: engine)
    adapter = audit.PostgresAuditAdapter("postgresql+psycopg://ignored")

    events = adapter.events_for_run(RUN_ID)

    assert events == (first, second)
    assert connection.execute.call_args.args[1] == {"run_id": str(RUN_ID)}


@pytest.mark.unit
def test_audit_writer_rejects_malformed_persisted_json_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted audit row cannot be silently presented as trustworthy reconstruction evidence."""
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    malformed = audit_row(audit_event())
    malformed["evidence_ids"] = "not-a-json-list"
    connection.execute.return_value = mapping_result(all_rows=[malformed])
    monkeypatch.setattr(audit, "create_engine", lambda _: engine)
    adapter = audit.PostgresAuditAdapter("postgresql+psycopg://ignored")

    with pytest.raises(audit.AuditEventError, match="invalid JSON shape"):
        adapter.events_for_run(RUN_ID)


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and preserve diagnostics for durable-ledger failures."""
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
def test_postgres_audit_writer_sanitizes_and_database_rejects_mutation(
    disposable_database: str,
) -> None:
    """The migrated database, not just application convention, protects audit event history."""
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
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from sqlalchemy.exc import DBAPIError\n"
        "from enterprise_agent.adapters import PostgresAuditAdapter\n"
        "from enterprise_agent.domain import AuditEvent, AuditEventId, RunId\n"
        "now = datetime(2026, 8, 25, 9, tzinfo=UTC)\n"
        "event = AuditEvent(event_id=AuditEventId('00000000-0000-0000-0000-000000000901'), occurred_at=now, event_type='tool.succeeded', run_id=RunId('run-audit-integration'), actor_id=None, attention_id=None, workflow_id=None, plan_id=None, evidence_ids=(), payload={'api_key': 'never-store-this', 'result': 'replacement-created'}, policy_version=None, plan_hash=None, idempotency_key='tool:replacement', failure_category=None)\n"
        "database_url = environ['DATABASE_URL']\n"
        "adapter = PostgresAuditAdapter(database_url)\n"
        "adapter.append(event)\n"
        "stored = adapter.events_for_run(RunId('run-audit-integration'))\n"
        "assert len(stored) == 1 and stored[0].payload == {'api_key': '[redacted]', 'result': 'replacement-created'}\n"
        'for statement in ("UPDATE audit_events SET event_type = \'tool.failed\'", "DELETE FROM audit_events"):\n'
        "    try:\n"
        "        with create_engine(database_url).begin() as connection: connection.execute(text(statement))\n"
        "    except DBAPIError as error:\n"
        "        assert 'append-only' in str(error)\n"
        "    else:\n"
        "        raise AssertionError('audit mutation unexpectedly succeeded')\n"
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
