"""Deterministic, local-only reset and seed support for the demo database."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.sql.elements import TextClause


class SeedSafetyError(ValueError):
    """Raised when an operator attempts to reset a database outside the demo boundary."""


@dataclass(frozen=True)
class SeedRow:
    """One trusted, deterministic database row in the local demo dataset."""

    table: str
    values: Mapping[str, object]


DEMO_DATABASE_NAME = "enterprise_agent"
DEMO_DATABASE_HOST = "db"
DEMO_CLOCK_ID = 1
DEMO_CLOCK_START = datetime(2026, 8, 24, 9, tzinfo=UTC)
DEMO_TUESDAY = datetime(2026, 8, 25, 9, tzinfo=UTC)
DEMO_CREATED_AT = datetime(2026, 8, 20, 9, tzinfo=UTC)
SCENARIO_A_PRODUCTION_START = DEMO_CLOCK_START.date() + timedelta(days=2)
SCENARIO_A_DELAYED_RECEIPT = DEMO_CLOCK_START.date() + timedelta(days=4)
PLANT_CHICAGO = "PLANT-CHI"

ID_DANA = UUID("00000000-0000-0000-0000-000000000001")
ID_AVERY = UUID("00000000-0000-0000-0000-000000000002")
ID_QUINN = UUID("00000000-0000-0000-0000-000000000003")
ID_PRIYA = UUID("00000000-0000-0000-0000-000000000004")
ID_PART_X = UUID("00000000-0000-0000-0000-000000000101")
ID_PART_QUALITY = UUID("00000000-0000-0000-0000-000000000102")
ID_PART_NOISE = UUID("00000000-0000-0000-0000-000000000103")
ID_SUPPLIER_Y = UUID("00000000-0000-0000-0000-000000000201")
ID_SUPPLIER_Z = UUID("00000000-0000-0000-0000-000000000202")
ID_SUPPLIER_W = UUID("00000000-0000-0000-0000-000000000203")
ID_SUPPLIER_SLOW = UUID("00000000-0000-0000-0000-000000000204")
ID_PRODUCTION_4812 = UUID("00000000-0000-0000-0000-000000000301")
ID_PRODUCTION_Q7001 = UUID("00000000-0000-0000-0000-000000000302")
ID_PRODUCTION_Q7002 = UUID("00000000-0000-0000-0000-000000000303")
ID_PO_4812_Y = UUID("00000000-0000-0000-0000-000000000401")
ID_PO_NOISE = UUID("00000000-0000-0000-0000-000000000402")
ID_INVENTORY_X = UUID("00000000-0000-0000-0000-000000000501")
ID_LOT_HELD = UUID("00000000-0000-0000-0000-000000000601")
ID_LOT_GOOD = UUID("00000000-0000-0000-0000-000000000602")
ID_LOT_NO_COVER = UUID("00000000-0000-0000-0000-000000000603")
ID_ALLOCATION_HELD = UUID("00000000-0000-0000-0000-000000000701")
ID_ALLOCATION_NO_COVER = UUID("00000000-0000-0000-0000-000000000702")
ID_MESSAGE_OLD = UUID("00000000-0000-0000-0000-000000000801")
ID_MESSAGE_NEW = UUID("00000000-0000-0000-0000-000000000802")
ID_MESSAGE_NOISE = UUID("00000000-0000-0000-0000-000000000803")
ID_DANA_OOO = UUID("00000000-0000-0000-0000-000000000901")
SCOPE_NAMESPACE = UUID("00000000-0000-0000-0000-000000000999")

JSON_COLUMNS = frozenset({"payload", "source_versions", "parameters", "evidence_ids"})
TRUNCATE_SQL = """
TRUNCATE TABLE
    audit_events,
    scheduled_tasks,
    demo_clock,
    tool_invocations,
    workflow_steps,
    workflow_instances,
    approvals,
    plans,
    attention_items,
    calendar_events,
    messages,
    production_allocations,
    quality_lots,
    inventory,
    purchase_orders,
    production_orders,
    suppliers,
    parts,
    user_scopes,
    users
RESTART IDENTITY
"""


def _scope_row(user_id: UUID, scope: str) -> SeedRow:
    """Create one scoped permission row for the single demo plant."""
    return SeedRow(
        table="user_scopes",
        values={
            "id": uuid5(SCOPE_NAMESPACE, f"{user_id}:{scope}"),
            "user_id": user_id,
            "scope": scope,
            "plant_id": PLANT_CHICAGO,
            "created_at": DEMO_CREATED_AT,
        },
    )


SEED_ROWS = (
    SeedRow(
        table="demo_clock",
        values={
            "id": DEMO_CLOCK_ID,
            "current_at": DEMO_CLOCK_START,
            "updated_at": DEMO_CLOCK_START,
        },
    ),
    SeedRow(
        table="users",
        values={
            "id": ID_AVERY,
            "display_name": "Avery Backup",
            "email": "avery.backup@example.com",
            "role": "purchasing_director",
            "backup_approver_id": None,
            "approval_limit_amount": Decimal("50000.00"),
            "approval_limit_currency": "USD",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="users",
        values={
            "id": ID_DANA,
            "display_name": "Dana Buyer",
            "email": "dana.buyer@example.com",
            "role": "purchasing_manager",
            "backup_approver_id": ID_AVERY,
            "approval_limit_amount": Decimal("10000.00"),
            "approval_limit_currency": "USD",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="users",
        values={
            "id": ID_QUINN,
            "display_name": "Quinn Quality",
            "email": "quinn.quality@example.com",
            "role": "quality_manager",
            "backup_approver_id": None,
            "approval_limit_amount": Decimal("5000.00"),
            "approval_limit_currency": "USD",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="users",
        values={
            "id": ID_PRIYA,
            "display_name": "Priya Production",
            "email": "priya.production@example.com",
            "role": "production_supervisor",
            "backup_approver_id": None,
            "approval_limit_amount": Decimal("0.00"),
            "approval_limit_currency": "USD",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    *(
        _scope_row(ID_DANA, scope)
        for scope in (
            "calendar:read",
            "erp:po:cancel",
            "erp:po:create",
            "erp:po:read",
            "erp:po:reroute",
            "erp:read",
            "mail:read",
            "production:notify",
            "scheduler:write",
        )
    ),
    *(
        _scope_row(ID_AVERY, scope)
        for scope in ("approval:decide", "calendar:read", "erp:read", "mail:read")
    ),
    *(
        _scope_row(ID_QUINN, scope)
        for scope in (
            "erp:lot:write",
            "production:notify",
            "purchasing:shortage:notify",
            "quality:lot:read",
            "quality:read",
        )
    ),
    *(_scope_row(ID_PRIYA, scope) for scope in ("production:notify", "production:read")),
    SeedRow(
        table="parts",
        values={
            "id": ID_PART_X,
            "part_number": "PART-X",
            "description": "Production-critical assembly part",
            "plant_id": PLANT_CHICAGO,
            "unit": "each",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="parts",
        values={
            "id": ID_PART_QUALITY,
            "part_number": "PART-QUALITY",
            "description": "Quality-controlled raw material",
            "plant_id": PLANT_CHICAGO,
            "unit": "each",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="parts",
        values={
            "id": ID_PART_NOISE,
            "part_number": "PART-NOISE",
            "description": "Unrelated material used as a filtering trap",
            "plant_id": PLANT_CHICAGO,
            "unit": "each",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="suppliers",
        values={
            "id": ID_SUPPLIER_Y,
            "supplier_code": "SUP-Y",
            "name": "Supplier Y",
            "part_id": ID_PART_X,
            "plant_id": PLANT_CHICAGO,
            "approved": True,
            "lead_time_days": 4,
            "unit_price": Decimal("14.00"),
            "currency": "USD",
            "source_version": 1,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="suppliers",
        values={
            "id": ID_SUPPLIER_Z,
            "supplier_code": "SUP-Z",
            "name": "Supplier Z",
            "part_id": ID_PART_X,
            "plant_id": PLANT_CHICAGO,
            "approved": True,
            "lead_time_days": 1,
            "unit_price": Decimal("18.00"),
            "currency": "USD",
            "source_version": 1,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="suppliers",
        values={
            "id": ID_SUPPLIER_W,
            "supplier_code": "SUP-W",
            "name": "Supplier W",
            "part_id": ID_PART_NOISE,
            "plant_id": PLANT_CHICAGO,
            "approved": True,
            "lead_time_days": 1,
            "unit_price": Decimal("5.00"),
            "currency": "USD",
            "source_version": 1,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="suppliers",
        values={
            "id": ID_SUPPLIER_SLOW,
            "supplier_code": "SUP-SLOW",
            "name": "Supplier Slow",
            "part_id": ID_PART_X,
            "plant_id": PLANT_CHICAGO,
            "approved": True,
            "lead_time_days": 8,
            "unit_price": Decimal("11.00"),
            "currency": "USD",
            "source_version": 1,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="production_orders",
        values={
            "id": ID_PRODUCTION_4812,
            "order_number": "4812",
            "part_id": ID_PART_X,
            "plant_id": PLANT_CHICAGO,
            "supervisor_id": ID_PRIYA,
            "required_quantity": Decimal("100.000"),
            "start_date": SCENARIO_A_PRODUCTION_START,
            "status": "scheduled",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="production_orders",
        values={
            "id": ID_PRODUCTION_Q7001,
            "order_number": "Q-7001",
            "part_id": ID_PART_QUALITY,
            "plant_id": PLANT_CHICAGO,
            "supervisor_id": ID_PRIYA,
            "required_quantity": Decimal("80.000"),
            "start_date": DEMO_TUESDAY.date().replace(day=27),
            "status": "scheduled",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="production_orders",
        values={
            "id": ID_PRODUCTION_Q7002,
            "order_number": "Q-7002",
            "part_id": ID_PART_QUALITY,
            "plant_id": PLANT_CHICAGO,
            "supervisor_id": ID_PRIYA,
            "required_quantity": Decimal("200.000"),
            "start_date": DEMO_TUESDAY.date().replace(day=27),
            "status": "scheduled",
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="purchase_orders",
        values={
            "id": ID_PO_4812_Y,
            "po_number": "PO-4812-Y",
            "part_id": ID_PART_X,
            "supplier_id": ID_SUPPLIER_Y,
            "plant_id": PLANT_CHICAGO,
            "ordered_quantity": Decimal("100.000"),
            "received_quantity": Decimal("40.000"),
            "status": "delayed",
            "expected_receipt_date": SCENARIO_A_DELAYED_RECEIPT,
            "source_version": 2,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CLOCK_START,
        },
    ),
    SeedRow(
        table="purchase_orders",
        values={
            "id": ID_PO_NOISE,
            "po_number": "PO-NOISE-77",
            "part_id": ID_PART_NOISE,
            "supplier_id": ID_SUPPLIER_W,
            "plant_id": PLANT_CHICAGO,
            "ordered_quantity": Decimal("20.000"),
            "received_quantity": Decimal("0.000"),
            "status": "delayed",
            "expected_receipt_date": DEMO_TUESDAY.date().replace(day=28),
            "source_version": 1,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="inventory",
        values={
            "id": ID_INVENTORY_X,
            "part_id": ID_PART_X,
            "plant_id": PLANT_CHICAGO,
            "available_quantity": Decimal("30.000"),
            "safety_stock_quantity": Decimal("20.000"),
            "source_version": 4,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CLOCK_START,
        },
    ),
    SeedRow(
        table="quality_lots",
        values={
            "id": ID_LOT_HELD,
            "lot_number": "LOT-QUALITY-HELD",
            "part_id": ID_PART_QUALITY,
            "plant_id": PLANT_CHICAGO,
            "quantity": Decimal("80.000"),
            "status": "held",
            "production_order_id": ID_PRODUCTION_Q7001,
            "allocated_quantity": Decimal("80.000"),
            "source_version": 3,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CLOCK_START,
        },
    ),
    SeedRow(
        table="quality_lots",
        values={
            "id": ID_LOT_GOOD,
            "lot_number": "LOT-QUALITY-GOOD",
            "part_id": ID_PART_QUALITY,
            "plant_id": PLANT_CHICAGO,
            "quantity": Decimal("120.000"),
            "status": "released",
            "production_order_id": None,
            "allocated_quantity": Decimal("0.000"),
            "source_version": 1,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CREATED_AT,
        },
    ),
    SeedRow(
        table="quality_lots",
        values={
            "id": ID_LOT_NO_COVER,
            "lot_number": "LOT-QUALITY-NO-COVER",
            "part_id": ID_PART_QUALITY,
            "plant_id": PLANT_CHICAGO,
            "quantity": Decimal("200.000"),
            "status": "held",
            "production_order_id": ID_PRODUCTION_Q7002,
            "allocated_quantity": Decimal("200.000"),
            "source_version": 2,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CLOCK_START,
        },
    ),
    SeedRow(
        table="production_allocations",
        values={
            "id": ID_ALLOCATION_HELD,
            "quality_lot_id": ID_LOT_HELD,
            "production_order_id": ID_PRODUCTION_Q7001,
            "allocated_quantity": Decimal("80.000"),
            "source_version": 3,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CLOCK_START,
        },
    ),
    SeedRow(
        table="production_allocations",
        values={
            "id": ID_ALLOCATION_NO_COVER,
            "quality_lot_id": ID_LOT_NO_COVER,
            "production_order_id": ID_PRODUCTION_Q7002,
            "allocated_quantity": Decimal("200.000"),
            "source_version": 2,
            "created_at": DEMO_CREATED_AT,
            "updated_at": DEMO_CLOCK_START,
        },
    ),
    SeedRow(
        table="messages",
        values={
            "id": ID_MESSAGE_OLD,
            "message_key": "shipment-update-po-4812-y-v1",
            "purchase_order_id": ID_PO_4812_Y,
            "supplier_id": ID_SUPPLIER_Y,
            "sender": "operations@supplier-y.example",
            "recipient": "dana.buyer@example.com",
            "subject": "PO-4812-Y shipment update",
            "body": "Prior estimate for the delayed shipment.",
            "received_at": datetime(2026, 8, 24, 8, tzinfo=UTC),
            "payload": {"superseded_by": "shipment-update-po-4812-y-v2"},
        },
    ),
    SeedRow(
        table="messages",
        values={
            "id": ID_MESSAGE_NEW,
            "message_key": "shipment-update-po-4812-y-v2",
            "purchase_order_id": ID_PO_4812_Y,
            "supplier_id": ID_SUPPLIER_Y,
            "sender": "operations@supplier-y.example",
            "recipient": "dana.buyer@example.com",
            "subject": "PO-4812-Y shipment update",
            "body": "Current estimate: remaining quantity arrives Friday, August 28.",
            "received_at": DEMO_CLOCK_START,
            "payload": {
                "current": True,
                "expected_receipt_date": SCENARIO_A_DELAYED_RECEIPT.isoformat(),
                "shipment_status": "delayed",
            },
        },
    ),
    SeedRow(
        table="messages",
        values={
            "id": ID_MESSAGE_NOISE,
            "message_key": "shipment-update-po-noise-77",
            "purchase_order_id": ID_PO_NOISE,
            "supplier_id": ID_SUPPLIER_W,
            "sender": "operations@supplier-w.example",
            "recipient": "dana.buyer@example.com",
            "subject": "PO-NOISE-77 shipment update",
            "body": "Unrelated delayed shipment.",
            "received_at": DEMO_CLOCK_START,
            "payload": {"shipment_status": "delayed"},
        },
    ),
    SeedRow(
        table="calendar_events",
        values={
            "id": ID_DANA_OOO,
            "event_key": "dana-out-of-office-2026-08-25",
            "user_id": ID_DANA,
            "event_type": "out_of_office",
            "starts_at": DEMO_TUESDAY,
            "ends_at": datetime(2026, 8, 25, 17, tzinfo=UTC),
            "payload": {"reason": "business travel"},
        },
    ),
)


def reset_database(database_url: str, *, allow_test_database: bool = False) -> None:
    """Remove all local demo data in one transaction after a strict target check."""
    _require_local_demo_database(database_url, allow_test_database=allow_test_database)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(TRUNCATE_SQL))
    finally:
        engine.dispose()


def seed_database(database_url: str, *, allow_test_database: bool = False) -> None:
    """Insert the fixed demo dataset atomically into a reset local database."""
    _require_local_demo_database(database_url, allow_test_database=allow_test_database)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            for row in SEED_ROWS:
                connection.execute(_insert_statement(row), _parameters(row.values))
    finally:
        engine.dispose()


def _require_local_demo_database(database_url: str, *, allow_test_database: bool) -> None:
    """Reject non-Compose hosts and every database except the explicit demo/test target."""
    try:
        target = make_url(database_url)
    except Exception as error:
        raise SeedSafetyError("reset is restricted to the local demo database") from error

    is_test_database = (target.database or "").startswith("enterprise_agent_test_")
    database_is_allowed = target.database == DEMO_DATABASE_NAME or (
        allow_test_database and is_test_database
    )
    if target.get_backend_name() != "postgresql" or target.host != DEMO_DATABASE_HOST:
        raise SeedSafetyError("reset is restricted to the local demo database")
    if not database_is_allowed:
        raise SeedSafetyError("reset is restricted to the local demo database")


def _insert_statement(row: SeedRow) -> TextClause:
    """Build an INSERT statement solely from trusted, module-defined seed columns."""
    columns = tuple(row.values)
    placeholders = tuple(
        f"CAST(:{column} AS jsonb)" if column in JSON_COLUMNS else f":{column}"
        for column in columns
    )
    return text(
        f"INSERT INTO {row.table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
    )


def _parameters(values: Mapping[str, object]) -> dict[str, object]:
    """Encode JSON fields explicitly while preserving native PostgreSQL scalar values."""
    return {
        column: json.dumps(value) if column in JSON_COLUMNS else value
        for column, value in values.items()
    }
