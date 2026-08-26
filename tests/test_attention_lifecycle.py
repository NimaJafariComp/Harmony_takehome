"""Contracts for durable Scenario A attention registration and state changes."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from enterprise_agent.domain import (
    AttentionId,
    AttentionItem,
    AttentionStatus,
    AuditEvent,
    Evidence,
    EvidenceId,
    InvalidAttentionTransitionError,
    RunId,
    ScenarioAStockoutTrigger,
)

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
ATTENTION_ID = "00000000-0000-0000-0000-000000000a01"


def stockout_trigger() -> ScenarioAStockoutTrigger:
    """Build the immutable Scenario A signal that later detector code will submit."""
    return ScenarioAStockoutTrigger(
        detector="stockout_detector:v1",
        part_id="00000000-0000-0000-0000-000000000101",
        production_order_id="00000000-0000-0000-0000-000000000301",
        inventory_version=4,
        production_start_date=date(2026, 8, 27),
        detected_at=NOW,
        source_versions={"inventory:00000000-0000-0000-0000-000000000501": 4},
    )


def attention_row(
    *,
    status: str = "open",
    cause: str = "projected_stockout",
    dedupe_key: str | None = None,
    source_versions: dict[str, int] | None = None,
    resolved_at: datetime | None = None,
) -> dict[str, object]:
    """Return the database mapping shape owned by the attention adapter."""
    return {
        "id": ATTENTION_ID,
        "scenario": "scenario_a",
        "cause": cause,
        "dedupe_key": stockout_trigger().dedupe_key if dedupe_key is None else dedupe_key,
        "status": status,
        "created_at": NOW,
        "source_versions": (
            {"inventory:00000000-0000-0000-0000-000000000501": 4}
            if source_versions is None
            else source_versions
        ),
        "resolved_at": resolved_at,
    }


def mapping_result(*, one_or_none: object | None = None, one: object | None = None) -> MagicMock:
    """Build one SQLAlchemy result double with the mapping method the adapter uses."""
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = one_or_none
    result.mappings.return_value.one.return_value = one
    return result


def configured_engine(*results: MagicMock) -> MagicMock:
    """Return an engine double that exposes one transaction and fixed statement results."""
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.execute.side_effect = results
    return engine


def replacement_purchase_order(*, source_version: int = 3) -> Evidence:
    """Build the exact ERP row that causally supports a missing-receipt follow-up."""
    return Evidence(
        evidence_id=EvidenceId("erp:purchase_order:replacement"),
        source="erp",
        record_type="purchase_order",
        record_id="00000000-0000-0000-0000-000000000499",
        source_version=source_version,
        observed_at=NOW,
        payload={"ordered_quantity": "60", "received_quantity": "0"},
    )


def test_stockout_trigger_key_is_stable_and_tracks_every_risk_input() -> None:
    """Only a materially changed detector signal may produce another attention item."""
    trigger = stockout_trigger()
    changed_keys = {
        trigger.dedupe_key,
        replace(trigger, detector="stockout_detector:v2").dedupe_key,
        replace(trigger, part_id="00000000-0000-0000-0000-000000000102").dedupe_key,
        replace(trigger, production_order_id="00000000-0000-0000-0000-000000000302").dedupe_key,
        replace(trigger, inventory_version=5).dedupe_key,
        replace(trigger, production_start_date=date(2026, 8, 28)).dedupe_key,
    }

    assert trigger.dedupe_key == stockout_trigger().dedupe_key
    assert len(changed_keys) == 6


def test_stockout_trigger_rejects_an_unversioned_inventory_signal() -> None:
    """A stockout signal without a positive inventory version cannot be deduplicated safely."""
    with pytest.raises(ValueError, match="inventory version"):
        ScenarioAStockoutTrigger(
            detector="stockout_detector:v1",
            part_id="part-x",
            production_order_id="production-4812",
            inventory_version=0,
            production_start_date=date(2026, 8, 27),
            detected_at=NOW,
            source_versions={},
        )


def test_attention_adapter_registers_a_new_signal_and_audits_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first signal atomically creates work and a matching detection audit row."""
    from enterprise_agent.adapters import attention

    engine = configured_engine(mapping_result(one_or_none=attention_row()), MagicMock())
    monkeypatch.setattr(attention, "create_engine", lambda _: engine)
    adapter = attention.PostgresAttentionAdapter("postgresql+psycopg://ignored")

    registration = adapter.register(stockout_trigger(), RunId("run-stockout-1"))

    connection = engine.begin.return_value.__enter__.return_value
    assert registration.created is True
    assert registration.attention.attention_id == AttentionId(ATTENTION_ID)
    assert connection.execute.call_count == 2
    assert connection.execute.call_args_list[1].args[1]["event_type"] == "attention.detected"


def test_attention_adapter_returns_existing_work_and_audits_a_duplicate_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated signal uses the existing row but remains reconstructable from audit."""
    from enterprise_agent.adapters import attention

    engine = configured_engine(
        mapping_result(one_or_none=None),
        mapping_result(one=attention_row()),
        MagicMock(),
    )
    monkeypatch.setattr(attention, "create_engine", lambda _: engine)
    adapter = attention.PostgresAttentionAdapter("postgresql+psycopg://ignored")

    registration = adapter.register(stockout_trigger(), RunId("run-stockout-2"))

    connection = engine.begin.return_value.__enter__.return_value
    assert registration.created is False
    assert registration.attention.dedupe_key == stockout_trigger().dedupe_key
    assert connection.execute.call_count == 3
    assert connection.execute.call_args_list[2].args[1]["event_type"] == "attention.deduplicated"


def test_attention_adapter_allows_only_forward_lifecycle_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State updates are guarded by both lifecycle policy and the current database status."""
    from enterprise_agent.adapters import attention

    engine = configured_engine(
        mapping_result(one_or_none=attention_row(status="pending_approval")), MagicMock()
    )
    monkeypatch.setattr(attention, "create_engine", lambda _: engine)
    adapter = attention.PostgresAttentionAdapter("postgresql+psycopg://ignored")
    opened = AttentionItem(
        attention_id=AttentionId(ATTENTION_ID),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key=stockout_trigger().dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions={"inventory:00000000-0000-0000-0000-000000000501": 4},
    )

    pending = adapter.transition(
        opened,
        AttentionStatus.PENDING_APPROVAL,
        RunId("run-stockout-1"),
        NOW + timedelta(minutes=1),
    )

    assert pending.status is AttentionStatus.PENDING_APPROVAL
    with pytest.raises(InvalidAttentionTransitionError, match="not allowed"):
        adapter.transition(
            pending,
            AttentionStatus.OPEN,
            RunId("run-stockout-1"),
            NOW + timedelta(minutes=2),
        )


def test_attention_adapter_rejects_a_persisted_transition_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale worker cannot write an attention lifecycle transition after another worker wins."""
    from enterprise_agent.adapters import attention

    engine = configured_engine(mapping_result(one_or_none=None))
    monkeypatch.setattr(attention, "create_engine", lambda _: engine)
    adapter = attention.PostgresAttentionAdapter("postgresql+psycopg://ignored")
    opened = AttentionItem(
        attention_id=AttentionId(ATTENTION_ID),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key=stockout_trigger().dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions={"inventory:00000000-0000-0000-0000-000000000501": 4},
    )

    with pytest.raises(InvalidAttentionTransitionError, match="no longer valid"):
        adapter.transition(
            opened,
            AttentionStatus.PENDING_APPROVAL,
            RunId("run-transition-race"),
            NOW + timedelta(minutes=1),
        )


def test_attention_adapter_delegates_detection_and_resolution_to_shared_audit_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attention and Tuesday-resolution events share the ledger sanitizer and event vocabulary."""
    from enterprise_agent.adapters import attention

    detected_row = attention_row()
    resolved_row = attention_row(status="resolved", resolved_at=NOW + timedelta(minutes=1))
    engine = configured_engine(
        mapping_result(one_or_none=detected_row),
        mapping_result(one_or_none=resolved_row),
    )
    emitted: list[AuditEvent] = []
    monkeypatch.setattr(attention, "create_engine", lambda _: engine)
    monkeypatch.setattr(
        attention,
        "append_audit_event",
        lambda _connection, event: emitted.append(event),
    )
    adapter = attention.PostgresAttentionAdapter("postgresql+psycopg://ignored")
    original = adapter.register(stockout_trigger(), RunId("run-shared-audit-detected")).attention

    adapter.transition(
        original,
        AttentionStatus.RESOLVED,
        RunId("run-shared-audit-resolved"),
        NOW + timedelta(minutes=1),
    )

    assert [event.event_type for event in emitted] == [
        "attention.detected",
        "followup.resolved",
    ]
    assert emitted[0].attention_id == original.attention_id
    assert emitted[0].evidence_ids == (
        EvidenceId("inventory:00000000-0000-0000-0000-000000000501"),
    )


def test_attention_adapter_loads_one_current_item_or_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker can safely distinguish a current attention record from stale scheduler work."""
    from enterprise_agent.adapters import attention

    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value = mapping_result(one_or_none=attention_row())
    monkeypatch.setattr(attention, "create_engine", lambda _: engine)
    adapter = attention.PostgresAttentionAdapter("postgresql+psycopg://ignored")

    loaded = adapter.load(AttentionId(ATTENTION_ID))
    connection.execute.return_value = mapping_result(one_or_none=None)
    missing = adapter.load(AttentionId("00000000-0000-0000-0000-000000000a02"))

    assert loaded is not None
    assert loaded.attention_id == AttentionId(ATTENTION_ID)
    assert missing is None
    assert connection.execute.call_args_list[0].args[1] == {"attention_id": ATTENTION_ID}


def test_attention_adapter_creates_or_reuses_versioned_arrival_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated receipt checks share one causal version, while a newer PO version creates new work."""
    from enterprise_agent.adapters import attention

    original = AttentionItem(
        attention_id=AttentionId(ATTENTION_ID),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key=stockout_trigger().dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions={"inventory:00000000-0000-0000-0000-000000000501": 4},
    )
    created_row = attention_row(
        cause="arrival_check",
        dedupe_key="arrival-check-v3",
        source_versions={"purchase_order:00000000-0000-0000-0000-000000000499": 3},
    )
    existing_row = attention_row(
        cause="arrival_check",
        dedupe_key="arrival-check-v3",
        source_versions={"purchase_order:00000000-0000-0000-0000-000000000499": 3},
    )
    newer_row = attention_row(
        cause="arrival_check",
        dedupe_key="arrival-check-v4",
        source_versions={"purchase_order:00000000-0000-0000-0000-000000000499": 4},
    )
    engine = configured_engine(
        mapping_result(one_or_none=created_row),
        MagicMock(),
        mapping_result(one_or_none=None),
        mapping_result(one=existing_row),
        MagicMock(),
        mapping_result(one_or_none=newer_row),
        MagicMock(),
    )
    monkeypatch.setattr(attention, "create_engine", lambda _: engine)
    adapter = attention.PostgresAttentionAdapter("postgresql+psycopg://ignored")

    created = adapter.register_arrival_followup(
        original_attention=original,
        purchase_order=replacement_purchase_order(),
        run_id=RunId("run-arrival-create"),
        detected_at=NOW,
    )
    reused = adapter.register_arrival_followup(
        original_attention=original,
        purchase_order=replacement_purchase_order(),
        run_id=RunId("run-arrival-reuse"),
        detected_at=NOW,
    )
    newer = adapter.register_arrival_followup(
        original_attention=original,
        purchase_order=replacement_purchase_order(source_version=4),
        run_id=RunId("run-arrival-newer"),
        detected_at=NOW,
    )

    connection = engine.begin.return_value.__enter__.return_value
    first_params = connection.execute.call_args_list[0].args[1]
    second_params = connection.execute.call_args_list[2].args[1]
    newer_params = connection.execute.call_args_list[5].args[1]
    assert created.created is True
    assert reused.created is False
    assert newer.created is True
    assert created.attention.cause == "arrival_check"
    assert first_params["source_versions"] == (
        '{"purchase_order:00000000-0000-0000-0000-000000000499": 3}'
    )
    assert first_params["dedupe_key"] == second_params["dedupe_key"]
    assert first_params["dedupe_key"] != newer_params["dedupe_key"]
    assert newer.attention.source_versions == {
        "purchase_order:00000000-0000-0000-0000-000000000499": 4
    }
    assert connection.execute.call_args_list[1].args[1]["event_type"] == "followup.reopened"
    assert connection.execute.call_args_list[4].args[1]["event_type"] == "followup.reopened"
    assert connection.execute.call_args_list[6].args[1]["event_type"] == "followup.reopened"


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and expose diagnostics if it fails."""
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
def test_postgres_attention_adapter_deduplicates_attempts_and_persists_audit_evidence(
    disposable_database: str,
) -> None:
    """PostgreSQL retains one item, every attempt, and the final lifecycle state."""
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
        "from datetime import UTC, date, datetime, timedelta\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import PostgresAttentionAdapter\n"
        "from enterprise_agent.domain import AttentionStatus, RunId, ScenarioAStockoutTrigger\n"
        "now = datetime(2026, 8, 24, 9, tzinfo=UTC)\n"
        "def trigger(version):\n"
        "    return ScenarioAStockoutTrigger(\n"
        "        detector='stockout_detector:v1',\n"
        "        part_id='00000000-0000-0000-0000-000000000101',\n"
        "        production_order_id='00000000-0000-0000-0000-000000000301',\n"
        "        inventory_version=version,\n"
        "        production_start_date=date(2026, 8, 27),\n"
        "        detected_at=now,\n"
        "        source_versions={'inventory:00000000-0000-0000-0000-000000000501': version},\n"
        "    )\n"
        "adapter = PostgresAttentionAdapter(environ['DATABASE_URL'])\n"
        "first = adapter.register(trigger(4), RunId('run-stockout-1'))\n"
        "duplicate = adapter.register(trigger(4), RunId('run-stockout-2'))\n"
        "assert first.created is True\n"
        "assert duplicate.created is False\n"
        "assert duplicate.attention.attention_id == first.attention.attention_id\n"
        "pending = adapter.transition(first.attention, AttentionStatus.PENDING_APPROVAL, RunId('run-stockout-1'), now + timedelta(minutes=1))\n"
        "running = adapter.transition(pending, AttentionStatus.IN_PROGRESS, RunId('run-stockout-1'), now + timedelta(minutes=2))\n"
        "resolved = adapter.transition(running, AttentionStatus.RESOLVED, RunId('run-stockout-1'), now + timedelta(minutes=3))\n"
        "assert resolved.resolved_at == now + timedelta(minutes=3)\n"
        "changed_inventory = adapter.register(trigger(5), RunId('run-stockout-3'))\n"
        "assert changed_inventory.created is True\n"
        "with create_engine(environ['DATABASE_URL']).connect() as connection:\n"
        "    attention_count = connection.execute(text('SELECT COUNT(*) FROM attention_items')).scalar_one()\n"
        '    audit_types = [row[0] for row in connection.execute(text("SELECT event_type FROM audit_events ORDER BY occurred_at, event_type"))]\n'
        "assert attention_count == 2\n"
        "assert audit_types.count('attention.detected') == 2\n"
        "assert audit_types.count('attention.deduplicated') == 1\n"
        "assert audit_types.count('attention.status_changed') == 2\n"
        "assert audit_types.count('followup.resolved') == 1\n"
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
