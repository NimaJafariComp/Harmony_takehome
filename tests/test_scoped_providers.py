"""Contracts for authorization enforced inside seeded provider queries."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from enterprise_agent.domain import ActorContext, PlantId, Scope, UserId
from enterprise_agent.ports import EvidenceQuery

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)


def actor(*scopes: str, plant_ids: frozenset[str] = frozenset({"PLANT-CHI"})) -> ActorContext:
    """Build one actor context with only the requested provider read permissions."""
    return ActorContext(
        user_id=UserId("00000000-0000-0000-0000-000000000001"),
        role="purchasing_manager",
        scopes=frozenset(Scope(scope) for scope in scopes),
        plant_ids=frozenset(PlantId(plant_id) for plant_id in plant_ids),
        backup_approver_id=None,
        approval_limits={"USD": Decimal("10000.00")},
    )


def configured_engine(rows: list[dict[str, object]]) -> MagicMock:
    """Return an engine double that returns one deterministic query result."""
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.all.return_value = rows
    return engine


def test_erp_provider_filters_by_scope_and_plant_inside_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERP evidence is emitted only for a read-scoped actor and its permitted plant."""
    from enterprise_agent.adapters import providers

    engine = configured_engine(
        [
            {
                "id": "inventory-x",
                "part_id": "part-x",
                "part_number": "PART-X",
                "plant_id": "PLANT-CHI",
                "available_quantity": Decimal("30.000"),
                "safety_stock_quantity": Decimal("20.000"),
                "source_version": 4,
                "updated_at": NOW,
            }
        ]
    )
    monkeypatch.setattr(providers, "create_engine", lambda _: engine)
    adapter = providers.PostgresErpAdapter("postgresql+psycopg://ignored")
    query = EvidenceQuery(record_types=frozenset({"inventory"}))

    evidence = adapter.query(actor("erp:read"), query)
    denied_evidence = adapter.query(actor(), query)

    assert evidence[0].record_type == "inventory"
    assert evidence[0].source_version == 4
    assert evidence[0].payload["part_number"] == "PART-X"
    assert evidence[0].payload["available_quantity"] == Decimal("30.000")
    assert denied_evidence == ()
    assert engine.connect.return_value.__enter__.return_value.execute.call_count == 1


def test_quality_provider_returns_only_quality_records_to_a_quality_read_scoped_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quality actor can read scoped lot evidence without acquiring purchasing ERP visibility."""
    from enterprise_agent.adapters import providers

    engine = configured_engine(
        [
            {
                "id": "lot-held",
                "lot_number": "LOT-QUALITY-HELD",
                "part_id": "part-quality",
                "part_number": "PART-QUALITY",
                "plant_id": "PLANT-CHI",
                "quantity": Decimal("80.000"),
                "allocated_quantity": Decimal("80.000"),
                "status": "held",
                "production_order_id": "production-q7001",
                "source_version": 3,
                "updated_at": NOW,
            }
        ]
    )
    monkeypatch.setattr(providers, "create_engine", lambda _: engine)
    adapter = providers.PostgresQualityAdapter("postgresql+psycopg://ignored")
    query = EvidenceQuery(record_types=frozenset({"quality_lot"}))

    evidence = adapter.query(actor("quality:lot:read"), query)
    denied_evidence = adapter.query(actor(), query)

    assert evidence[0].source == "quality"
    assert evidence[0].record_type == "quality_lot"
    assert evidence[0].payload["lot_number"] == "LOT-QUALITY-HELD"
    assert denied_evidence == ()
    assert engine.connect.return_value.__enter__.return_value.execute.call_count == 1


def test_mail_and_calendar_providers_bind_the_actor_to_authorized_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mailbox and calendar reads carry the actor identifier into provider-owned SQL."""
    from enterprise_agent.adapters import providers

    mail_engine = configured_engine(
        [
            {
                "id": "message-current",
                "message_key": "shipment-update-po-4812-y-v2",
                "purchase_order_id": "po-4812-y",
                "supplier_id": "supplier-y",
                "sender": "operations@supplier-y.example",
                "subject": "PO-4812-Y shipment update",
                "body": "Current estimate: remaining quantity arrives Tuesday.",
                "received_at": NOW,
                "payload": {"current": True},
            }
        ]
    )
    calendar_engine = configured_engine(
        [
            {
                "id": "dana-ooo",
                "event_key": "dana-out-of-office-2026-08-25",
                "user_id": "00000000-0000-0000-0000-000000000001",
                "event_type": "out_of_office",
                "starts_at": datetime(2026, 8, 25, 9, tzinfo=UTC),
                "ends_at": datetime(2026, 8, 25, 17, tzinfo=UTC),
                "payload": {"reason": "business travel"},
            }
        ]
    )
    monkeypatch.setattr(providers, "create_engine", lambda _: mail_engine)
    mail = providers.PostgresMailAdapter("postgresql+psycopg://ignored")
    monkeypatch.setattr(providers, "create_engine", lambda _: calendar_engine)
    calendar = providers.PostgresCalendarAdapter("postgresql+psycopg://ignored")

    mail_evidence = mail.query(
        actor("mail:read"), EvidenceQuery(record_types=frozenset({"message"}))
    )
    calendar_evidence = calendar.query(
        actor("calendar:read"),
        EvidenceQuery(record_types=frozenset({"calendar_event"})),
    )

    assert mail_evidence[0].payload["message_key"] == "shipment-update-po-4812-y-v2"
    assert calendar_evidence[0].payload["event_type"] == "out_of_office"
    assert (
        mail_engine.connect.return_value.__enter__.return_value.execute.call_args.args[1][
            "actor_id"
        ]
        == "00000000-0000-0000-0000-000000000001"
    )
    assert (
        calendar_engine.connect.return_value.__enter__.return_value.execute.call_args.args[1][
            "actor_id"
        ]
        == "00000000-0000-0000-0000-000000000001"
    )


def test_scoped_providers_reject_unknown_record_types_before_querying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported evidence types fail closed instead of widening provider access."""
    from enterprise_agent.adapters import providers

    engine = configured_engine([])
    monkeypatch.setattr(providers, "create_engine", lambda _: engine)
    adapter = providers.PostgresErpAdapter("postgresql+psycopg://ignored")

    with pytest.raises(
        providers.UnsupportedEvidenceTypeError, match="unsupported ERP evidence type"
    ):
        adapter.query(actor("erp:read"), EvidenceQuery(record_types=frozenset({"all_tables"})))

    assert engine.connect.call_count == 0
