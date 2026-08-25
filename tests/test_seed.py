"""Integration contracts for the deterministic local demo dataset."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest


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


def reset_and_seed(database_url: str) -> None:
    """Invoke reset and seed against the disposable database inside Compose."""
    command = (
        "from os import environ\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
    )
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={database_url}",
        "app",
        "python",
        "-c",
        command,
    )


def inspect_seed(database_url: str) -> dict[str, Any]:
    """Read only seed facts that later scenario tests will depend on."""
    command = (
        "import json\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "with create_engine(environ['DATABASE_URL']).connect() as connection:\n"
        "    def rows(sql):\n"
        "        return [dict(row) for row in connection.execute(text(sql)).mappings()]\n"
        "    snapshot = {\n"
        "        'counts': {table: connection.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() for table in ('users', 'parts', 'suppliers', 'purchase_orders', 'production_orders', 'quality_lots', 'production_allocations', 'inventory', 'messages', 'calendar_events')},\n"
        "        'scopes': rows(\"SELECT users.display_name, user_scopes.scope FROM user_scopes JOIN users ON users.id = user_scopes.user_id ORDER BY users.display_name, user_scopes.scope\"),\n"
        "        'scenario_a': {\n"
        "            'production': rows(\"SELECT order_number, required_quantity::text, start_date::text FROM production_orders WHERE order_number = '4812'\"),\n"
        "            'purchase_order': rows(\"SELECT po.po_number, supplier.supplier_code, po.status, po.received_quantity::text, po.expected_receipt_date::text, po.source_version FROM purchase_orders po JOIN suppliers supplier ON supplier.id = po.supplier_id WHERE po.po_number = 'PO-4812-Y'\"),\n"
        "            'suppliers': rows(\"SELECT suppliers.supplier_code, parts.part_number, suppliers.approved, suppliers.lead_time_days, suppliers.unit_price::text, suppliers.source_version FROM suppliers JOIN parts ON parts.id = suppliers.part_id ORDER BY suppliers.supplier_code\"),\n"
        "            'shipment_updates': rows(\"SELECT message_key, received_at::text, payload->>'superseded_by' AS superseded_by FROM messages WHERE purchase_order_id = (SELECT id FROM purchase_orders WHERE po_number = 'PO-4812-Y') ORDER BY received_at\"),\n"
        "            'inventory': rows(\"SELECT available_quantity::text, safety_stock_quantity::text, source_version FROM inventory WHERE part_id = (SELECT id FROM parts WHERE part_number = 'PART-X')\"),\n"
        "            'out_of_office': rows(\"SELECT users.display_name, starts_at::text, ends_at::text FROM calendar_events JOIN users ON users.id = calendar_events.user_id WHERE event_type = 'out_of_office'\"),\n"
        "        },\n"
        "        'scenario_b': {\n"
        "            'allocations': rows(\"SELECT lots.lot_number, lots.status, allocations.allocated_quantity::text, orders.order_number FROM production_allocations allocations JOIN quality_lots lots ON lots.id = allocations.quality_lot_id JOIN production_orders orders ON orders.id = allocations.production_order_id ORDER BY lots.lot_number\"),\n"
        "            'lots': rows(\"SELECT lot_number, status, quantity::text, source_version FROM quality_lots ORDER BY lot_number\"),\n"
        "        },\n"
        "    }\n"
        "print(json.dumps(snapshot, default=str, sort_keys=True))\n"
    )
    result = compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={database_url}",
        "app",
        "python",
        "-c",
        command,
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def assert_unfiltered_erp_inventory_is_queryable(database_url: str) -> None:
    """Exercise PostgreSQL's common unfiltered provider path against the seeded database."""
    command = (
        "from os import environ\n"
        "from enterprise_agent.adapters import PostgresErpAdapter, PostgresIdentityAdapter\n"
        "from enterprise_agent.domain import UserId\n"
        "from enterprise_agent.ports import EvidenceQuery\n"
        "database_url = environ['DATABASE_URL']\n"
        "actor = PostgresIdentityAdapter(database_url).actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "evidence = PostgresErpAdapter(database_url).query(actor, EvidenceQuery(record_types=frozenset({'inventory'})))\n"
        "assert len(evidence) == 1\n"
    )
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={database_url}",
        "app",
        "python",
        "-c",
        command,
    )


def assert_seeded_provider_boundaries(database_url: str) -> None:
    """Prove realistic seed data passes only through the owning provider boundaries."""
    command = (
        "from os import environ\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresCalendarAdapter,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter,\n"
        ")\n"
        "from enterprise_agent.domain import UserId\n"
        "from enterprise_agent.ports import EvidenceQuery\n"
        "database_url = environ['DATABASE_URL']\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "mail = PostgresMailAdapter(database_url)\n"
        "calendar = PostgresCalendarAdapter(database_url)\n"
        "dana = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "quinn = identity.actor_for(UserId('00000000-0000-0000-0000-000000000003'))\n"
        "avery = identity.actor_for(UserId('00000000-0000-0000-0000-000000000002'))\n"
        "procurement = erp.query(dana, EvidenceQuery(record_types=frozenset({'inventory', 'purchase_order', 'production_order', 'supplier'})))\n"
        "assert {(item.record_type, item.payload.get('po_number')) for item in procurement if item.record_type == 'purchase_order'} == {('purchase_order', 'PO-4812-Y'), ('purchase_order', 'PO-NOISE-77')}\n"
        "assert {item.payload['supplier_code'] for item in procurement if item.record_type == 'supplier'} == {'SUP-SLOW', 'SUP-W', 'SUP-Y', 'SUP-Z'}\n"
        "updates = mail.query(dana, EvidenceQuery(record_types=frozenset({'message'}), record_ids=frozenset({'00000000-0000-0000-0000-000000000801', '00000000-0000-0000-0000-000000000802'})))\n"
        "assert [item.payload['message_key'] for item in updates] == ['shipment-update-po-4812-y-v1', 'shipment-update-po-4812-y-v2']\n"
        "assert updates[-1].payload['payload']['current'] is True\n"
        "assert len(calendar.query(dana, EvidenceQuery(record_types=frozenset({'calendar_event'})))) == 1\n"
        "assert erp.query(quinn, EvidenceQuery(record_types=frozenset({'purchase_order'}), record_ids=frozenset({'00000000-0000-0000-0000-000000000401'}))) == ()\n"
        "assert mail.query(quinn, EvidenceQuery(record_types=frozenset({'message'}))) == ()\n"
        "assert calendar.query(quinn, EvidenceQuery(record_types=frozenset({'calendar_event'}))) == ()\n"
        "assert mail.query(avery, EvidenceQuery(record_types=frozenset({'message'}))) == ()\n"
        "assert calendar.query(avery, EvidenceQuery(record_types=frozenset({'calendar_event'}))) == ()\n"
    )
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={database_url}",
        "app",
        "python",
        "-c",
        command,
    )


@pytest.mark.integration
def test_reset_and_seed_create_repeatable_scenario_and_edge_case_data(
    disposable_database: str,
) -> None:
    """The demo dataset is repeatable and contains every planned scenario precondition."""
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

    reset_and_seed(disposable_database)
    assert_unfiltered_erp_inventory_is_queryable(disposable_database)
    assert_seeded_provider_boundaries(disposable_database)
    first_seed = inspect_seed(disposable_database)
    reset_and_seed(disposable_database)
    second_seed = inspect_seed(disposable_database)

    assert first_seed == second_seed
    assert first_seed["counts"] == {
        "calendar_events": 1,
        "inventory": 1,
        "messages": 3,
        "parts": 3,
        "production_allocations": 2,
        "production_orders": 3,
        "purchase_orders": 2,
        "quality_lots": 3,
        "suppliers": 4,
        "users": 4,
    }
    assert first_seed["scenario_a"]["production"] == [
        {"order_number": "4812", "required_quantity": "100.000", "start_date": "2026-08-27"}
    ]
    assert first_seed["scenario_a"]["purchase_order"] == [
        {
            "expected_receipt_date": "2026-08-25",
            "po_number": "PO-4812-Y",
            "received_quantity": "40.000",
            "source_version": 2,
            "status": "delayed",
            "supplier_code": "SUP-Y",
        }
    ]
    assert first_seed["scenario_a"]["shipment_updates"] == [
        {
            "message_key": "shipment-update-po-4812-y-v1",
            "received_at": "2026-08-24 08:00:00+00",
            "superseded_by": "shipment-update-po-4812-y-v2",
        },
        {
            "message_key": "shipment-update-po-4812-y-v2",
            "received_at": "2026-08-24 09:00:00+00",
            "superseded_by": None,
        },
    ]
    assert first_seed["scenario_a"]["inventory"] == [
        {"available_quantity": "30.000", "safety_stock_quantity": "20.000", "source_version": 4}
    ]
    assert first_seed["scenario_a"]["suppliers"] == [
        {
            "approved": True,
            "lead_time_days": 8,
            "part_number": "PART-X",
            "source_version": 1,
            "supplier_code": "SUP-SLOW",
            "unit_price": "11.00",
        },
        {
            "approved": True,
            "lead_time_days": 1,
            "part_number": "PART-NOISE",
            "source_version": 1,
            "supplier_code": "SUP-W",
            "unit_price": "5.00",
        },
        {
            "approved": True,
            "lead_time_days": 4,
            "part_number": "PART-X",
            "source_version": 1,
            "supplier_code": "SUP-Y",
            "unit_price": "14.00",
        },
        {
            "approved": True,
            "lead_time_days": 1,
            "part_number": "PART-X",
            "source_version": 1,
            "supplier_code": "SUP-Z",
            "unit_price": "18.00",
        },
    ]
    assert first_seed["scenario_a"]["out_of_office"][0]["display_name"] == "Dana Buyer"
    assert first_seed["scenario_b"]["allocations"] == [
        {
            "allocated_quantity": "80.000",
            "lot_number": "LOT-QUALITY-HELD",
            "order_number": "Q-7001",
            "status": "held",
        },
        {
            "allocated_quantity": "200.000",
            "lot_number": "LOT-QUALITY-NO-COVER",
            "order_number": "Q-7002",
            "status": "held",
        },
    ]
    assert first_seed["scenario_b"]["lots"] == [
        {
            "lot_number": "LOT-QUALITY-GOOD",
            "quantity": "120.000",
            "source_version": 1,
            "status": "released",
        },
        {
            "lot_number": "LOT-QUALITY-HELD",
            "quantity": "80.000",
            "source_version": 3,
            "status": "held",
        },
        {
            "lot_number": "LOT-QUALITY-NO-COVER",
            "quantity": "200.000",
            "source_version": 2,
            "status": "held",
        },
    ]


def test_reset_refuses_any_database_other_than_the_local_demo_target() -> None:
    """The destructive reset guard rejects arbitrary PostgreSQL databases before connecting."""
    from enterprise_agent.seed import SeedSafetyError, reset_database

    with pytest.raises(SeedSafetyError, match="local demo database"):
        reset_database("postgresql+psycopg://agent:agent@db:5432/customer_production")


def test_demo_clock_starts_on_the_seeded_scenario_date() -> None:
    """The future mutable clock has one explicit, deterministic initial instant."""
    from enterprise_agent.seed import DEMO_CLOCK_START

    assert DEMO_CLOCK_START == datetime(2026, 8, 24, 9, tzinfo=UTC)


def test_reset_and_seed_execute_one_safe_transaction_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reset truncates the local target and seed inserts its fixed dataset atomically."""
    from enterprise_agent import seed

    engine = MagicMock()
    connection = MagicMock()
    engine.begin.return_value.__enter__.return_value = connection
    monkeypatch.setattr(seed, "create_engine", lambda _: engine)

    database_url = "postgresql+psycopg://agent:agent@db:5432/enterprise_agent"
    seed.reset_database(database_url)
    seed.seed_database(database_url)

    assert engine.begin.call_count == 2
    assert engine.dispose.call_count == 2
    assert "TRUNCATE TABLE" in str(connection.execute.call_args_list[0].args[0])
    assert connection.execute.call_count == len(seed.SEED_ROWS) + 1
