"""Integration contracts for the deterministic local demo dataset."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
        "        'counts': {table: connection.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() for table in ('users', 'parts', 'suppliers', 'supplier_risk_bulletins', 'purchase_orders', 'production_orders', 'quality_lots', 'production_allocations', 'inventory', 'messages', 'calendar_events')},\n"
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
        "            'production': rows(\"SELECT orders.order_number, orders.required_quantity::text, orders.start_date::text, users.display_name AS supervisor FROM production_orders orders JOIN users ON users.id = orders.supervisor_id WHERE orders.order_number LIKE 'Q-%' ORDER BY orders.order_number\"),\n"
        "            'allocations': rows(\"SELECT lots.lot_number, lots.status, allocations.allocated_quantity::text, orders.order_number FROM production_allocations allocations JOIN quality_lots lots ON lots.id = allocations.quality_lot_id JOIN production_orders orders ON orders.id = allocations.production_order_id ORDER BY lots.lot_number\"),\n"
        "            'lots': rows(\"SELECT lot_number, status, quantity::text, source_version FROM quality_lots ORDER BY lot_number\"),\n"
        "        },\n"
        "        'scenario_c': {\n"
        "            'purchase_order': rows(\"SELECT po.po_number, supplier.supplier_code, po.status, po.ordered_quantity::text, po.expected_receipt_date::text, po.source_version FROM purchase_orders po JOIN suppliers supplier ON supplier.id = po.supplier_id WHERE po.po_number = 'PO-C-9001-W'\"),\n"
        "            'production': rows(\"SELECT orders.order_number, parts.part_number, orders.required_quantity::text, orders.start_date::text, users.display_name AS supervisor FROM production_orders orders JOIN parts ON parts.id = orders.part_id JOIN users ON users.id = orders.supervisor_id WHERE orders.order_number = 'C-9001'\"),\n"
        "            'bulletins': rows(\"SELECT bulletin.bulletin_key, supplier.supplier_code, bulletin.status, bulletin.risk_level, bulletin.source_version, successor.bulletin_key AS superseded_by_key FROM supplier_risk_bulletins bulletin JOIN suppliers supplier ON supplier.id = bulletin.supplier_id LEFT JOIN supplier_risk_bulletins successor ON successor.id = bulletin.superseded_by_id ORDER BY bulletin.bulletin_key, bulletin.source_version\"),\n"
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
        "assert {(item.record_type, item.payload.get('po_number')) for item in procurement if item.record_type == 'purchase_order'} == {('purchase_order', 'PO-4812-Y'), ('purchase_order', 'PO-C-9001-W'), ('purchase_order', 'PO-NOISE-77')}\n"
        "assert {item.payload['supplier_code'] for item in procurement if item.record_type == 'supplier'} == {'SUP-BAIT', 'SUP-SLOW', 'SUP-W', 'SUP-Y', 'SUP-Z'}\n"
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
        "production_orders": 4,
        "purchase_orders": 3,
        "quality_lots": 3,
        "supplier_risk_bulletins": 3,
        "suppliers": 5,
        "users": 4,
    }
    assert first_seed["scenario_a"]["production"] == [
        {"order_number": "4812", "required_quantity": "100.000", "start_date": "2026-08-26"}
    ]
    assert first_seed["scenario_a"]["purchase_order"] == [
        {
            "expected_receipt_date": "2026-08-28",
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
            "approved": False,
            "lead_time_days": 1,
            "part_number": "PART-X",
            "source_version": 1,
            "supplier_code": "SUP-BAIT",
            "unit_price": "4.00",
        },
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
    assert first_seed["scenario_b"]["production"] == [
        {
            "order_number": "Q-7001",
            "required_quantity": "80.000",
            "start_date": "2026-08-27",
            "supervisor": "Priya Production",
        },
        {
            "order_number": "Q-7002",
            "required_quantity": "200.000",
            "start_date": "2026-08-27",
            "supervisor": "Priya Production",
        },
    ]
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
    assert first_seed["scenario_c"]["purchase_order"] == [
        {
            "expected_receipt_date": "2026-08-27",
            "ordered_quantity": "75.000",
            "po_number": "PO-C-9001-W",
            "source_version": 1,
            "status": "open",
            "supplier_code": "SUP-W",
        }
    ]
    assert first_seed["scenario_c"]["production"] == [
        {
            "order_number": "C-9001",
            "part_number": "PART-NOISE",
            "required_quantity": "75.000",
            "start_date": "2026-08-28",
            "supervisor": "Priya Production",
        }
    ]
    assert first_seed["scenario_c"]["bulletins"] == [
        {
            "bulletin_key": "supplier-w-disruption",
            "risk_level": "medium",
            "source_version": 1,
            "status": "superseded",
            "supplier_code": "SUP-W",
            "superseded_by_key": "supplier-w-disruption",
        },
        {
            "bulletin_key": "supplier-w-disruption",
            "risk_level": "high",
            "source_version": 2,
            "status": "active",
            "supplier_code": "SUP-W",
            "superseded_by_key": None,
        },
        {
            "bulletin_key": "supplier-y-weather",
            "risk_level": "low",
            "source_version": 1,
            "status": "inactive",
            "supplier_code": "SUP-Y",
            "superseded_by_key": None,
        },
    ]
    scopes_by_user = {
        user: {row["scope"] for row in first_seed["scopes"] if row["display_name"] == user}
        for user in ("Quinn Quality", "Priya Production")
    }
    assert scopes_by_user["Quinn Quality"] == {
        "erp:lot:write",
        "production:notify",
        "purchasing:shortage:notify",
        "quality:lot:read",
        "quality:read",
    }
    assert scopes_by_user["Priya Production"] == {"production:notify", "production:read"}
    assert not (
        scopes_by_user["Quinn Quality"]
        & {"erp:po:read", "erp:po:create", "erp:po:cancel", "mail:read", "calendar:read"}
    )


def test_reset_refuses_any_database_other_than_the_local_demo_target() -> None:
    """The destructive reset guard rejects arbitrary PostgreSQL databases before connecting."""
    from enterprise_agent.seed import SeedSafetyError, reset_database

    with pytest.raises(SeedSafetyError, match="local demo database"):
        reset_database("postgresql+psycopg://agent:agent@db:5432/customer_production")


def test_demo_clock_starts_on_the_seeded_scenario_date() -> None:
    """The future mutable clock has one explicit, deterministic initial instant."""
    from enterprise_agent.seed import DEMO_CLOCK_START

    assert DEMO_CLOCK_START == datetime(2026, 8, 24, 9, tzinfo=UTC)


def test_scenario_a_seed_timeline_is_causally_consistent() -> None:
    """The delayed original shipment threatens production while a replacement can arrive in time."""
    from enterprise_agent.seed import DEMO_CLOCK_START, SEED_ROWS

    rows_by_table = {
        table: [row.values for row in SEED_ROWS if row.table == table]
        for table in ("inventory", "production_orders", "purchase_orders", "suppliers", "messages")
    }
    production = next(
        row for row in rows_by_table["production_orders"] if row["order_number"] == "4812"
    )
    original_po = next(
        row for row in rows_by_table["purchase_orders"] if row["po_number"] == "PO-4812-Y"
    )
    current_update = next(
        row
        for row in rows_by_table["messages"]
        if row["message_key"] == "shipment-update-po-4812-y-v2"
    )
    inventory = rows_by_table["inventory"][0]
    viable_replacement = next(
        row for row in rows_by_table["suppliers"] if row["supplier_code"] == "SUP-Z"
    )

    production_start = cast(date, production["start_date"])
    delayed_receipt = cast(date, original_po["expected_receipt_date"])
    replacement_lead_time = cast(int, viable_replacement["lead_time_days"])
    current_payload = cast(dict[str, object], current_update["payload"])
    current_expected_receipt = cast(str, current_payload["expected_receipt_date"])
    required_quantity = cast(Decimal, production["required_quantity"])
    safety_stock = cast(Decimal, inventory["safety_stock_quantity"])
    available_quantity = cast(Decimal, inventory["available_quantity"])

    assert DEMO_CLOCK_START.date() < production_start
    assert delayed_receipt > production_start
    assert current_expected_receipt == delayed_receipt.isoformat()
    assert DEMO_CLOCK_START.date() + timedelta(days=replacement_lead_time) <= production_start
    assert required_quantity + safety_stock - available_quantity > 0


@pytest.mark.scenario
def test_scenario_a_seed_contains_a_cheaper_faster_but_unapproved_supplier_bait() -> None:
    """The interview demo can visibly reject a supplier whose only disqualifier is approval."""
    from enterprise_agent.seed import ID_PART_X, ID_SUPPLIER_BAIT, PLANT_CHICAGO, SEED_ROWS

    bait = next(
        row.values
        for row in SEED_ROWS
        if row.table == "suppliers" and row.values["supplier_code"] == "SUP-BAIT"
    )

    assert bait == {
        "id": ID_SUPPLIER_BAIT,
        "supplier_code": "SUP-BAIT",
        "name": "Supplier Bait",
        "part_id": ID_PART_X,
        "plant_id": PLANT_CHICAGO,
        "approved": False,
        "lead_time_days": 1,
        "unit_price": Decimal("4.00"),
        "currency": "USD",
        "source_version": 1,
        "created_at": bait["created_at"],
        "updated_at": bait["updated_at"],
    }


def test_scenario_c_seed_binds_one_current_bulletin_to_one_open_po_and_production_impact() -> None:
    """Scenario C starts with current, superseded, inactive, and unauthorized knowledge facts."""
    from enterprise_agent.seed import (
        ID_DANA,
        ID_PART_NOISE,
        ID_PO_C9001_W,
        ID_PRODUCTION_C9001,
        ID_QUINN,
        ID_SUPPLIER_W,
        SEED_ROWS,
    )

    bulletins = [row.values for row in SEED_ROWS if row.table == "supplier_risk_bulletins"]
    active = next(row for row in bulletins if row["status"] == "active")
    superseded = next(row for row in bulletins if row["status"] == "superseded")
    inactive = next(row for row in bulletins if row["status"] == "inactive")
    dana_scopes = {
        row.values["scope"]
        for row in SEED_ROWS
        if row.table == "user_scopes" and row.values["user_id"] == ID_DANA
    }
    quinn_scopes = {
        row.values["scope"]
        for row in SEED_ROWS
        if row.table == "user_scopes" and row.values["user_id"] == ID_QUINN
    }
    open_purchase_order = next(
        row.values
        for row in SEED_ROWS
        if row.table == "purchase_orders" and row.values["id"] == ID_PO_C9001_W
    )
    production_impact = next(
        row.values
        for row in SEED_ROWS
        if row.table == "production_orders" and row.values["id"] == ID_PRODUCTION_C9001
    )

    assert active["supplier_id"] == ID_SUPPLIER_W
    assert active["source_version"] == 2
    assert open_purchase_order["supplier_id"] == active["supplier_id"]
    assert production_impact["part_id"] == open_purchase_order["part_id"] == ID_PART_NOISE
    assert production_impact["plant_id"] == open_purchase_order["plant_id"] == active["plant_id"]
    assert superseded["superseded_by_id"] == active["id"]
    assert superseded["source_version"] == 1
    assert inactive["supplier_id"] != active["supplier_id"]
    assert inactive["status"] == "inactive"
    assert "knowledge:bulletin:read" in dana_scopes
    assert "knowledge:bulletin:read" not in quinn_scopes


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
