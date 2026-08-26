"""Scoped PostgreSQL evidence providers for the seeded company systems."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.sql.elements import TextClause

from enterprise_agent.domain import ActorContext, Evidence, EvidenceId
from enterprise_agent.ports import EvidenceQuery


class UnsupportedEvidenceTypeError(ValueError):
    """Raised when a caller asks a provider for a record type it does not own."""


def _scoped_statement(sql: str) -> TextClause:
    """Bind the plant and optional record-ID filters safely for fixed provider SQL."""
    return text(sql).bindparams(bindparam("plant_ids", expanding=True))


ERP_QUERIES = {
    "inventory": _scoped_statement("""
        SELECT
            inventory.id,
            inventory.part_id,
            parts.part_number,
            inventory.plant_id,
            inventory.available_quantity,
            inventory.safety_stock_quantity,
            inventory.source_version,
            inventory.updated_at
        FROM inventory
        JOIN parts ON parts.id = inventory.part_id
        WHERE inventory.plant_id IN :plant_ids
          AND (
              :has_record_filter = FALSE
              OR CAST(inventory.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
          )
        ORDER BY parts.part_number
    """),
    "purchase_order": _scoped_statement("""
        SELECT
            purchase_orders.id,
            purchase_orders.po_number,
            purchase_orders.part_id,
            purchase_orders.supplier_id,
            purchase_orders.plant_id,
            purchase_orders.ordered_quantity,
            purchase_orders.received_quantity,
            purchase_orders.status,
            purchase_orders.expected_receipt_date,
            purchase_orders.source_version,
            purchase_orders.updated_at
        FROM purchase_orders
        WHERE purchase_orders.plant_id IN :plant_ids
          AND (
              :has_record_filter = FALSE
              OR CAST(purchase_orders.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
          )
        ORDER BY purchase_orders.po_number
    """),
    "production_order": _scoped_statement("""
        SELECT
            production_orders.id,
            production_orders.order_number,
            production_orders.part_id,
            production_orders.plant_id,
            production_orders.required_quantity,
            production_orders.start_date,
            production_orders.status,
            1 AS source_version,
            production_orders.updated_at
        FROM production_orders
        WHERE production_orders.plant_id IN :plant_ids
          AND (
              :has_record_filter = FALSE
              OR CAST(production_orders.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
          )
        ORDER BY production_orders.order_number
    """),
    "supplier": _scoped_statement("""
        SELECT
            suppliers.id,
            suppliers.supplier_code,
            suppliers.name,
            suppliers.part_id,
            suppliers.plant_id,
            suppliers.approved,
            suppliers.lead_time_days,
            suppliers.unit_price,
            suppliers.currency,
            suppliers.source_version,
            suppliers.updated_at
        FROM suppliers
        WHERE suppliers.plant_id IN :plant_ids
          AND (
              :has_record_filter = FALSE
              OR CAST(suppliers.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
          )
        ORDER BY suppliers.supplier_code
    """),
}
QUALITY_QUERIES = {
    "quality_lot": _scoped_statement("""
        SELECT
            quality_lots.id,
            quality_lots.lot_number,
            quality_lots.part_id,
            parts.part_number,
            quality_lots.plant_id,
            quality_lots.quantity,
            quality_lots.status,
            quality_lots.production_order_id,
            quality_lots.allocated_quantity,
            quality_lots.source_version,
            quality_lots.updated_at
        FROM quality_lots
        JOIN parts ON parts.id = quality_lots.part_id
        WHERE quality_lots.plant_id IN :plant_ids
          AND (
              :has_record_filter = FALSE
              OR CAST(quality_lots.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
          )
        ORDER BY quality_lots.lot_number
    """),
    "production_allocation": _scoped_statement("""
        SELECT
            production_allocations.id,
            production_allocations.quality_lot_id,
            production_allocations.production_order_id,
            production_allocations.allocated_quantity,
            quality_lots.part_id,
            quality_lots.plant_id,
            quality_lots.status AS quality_lot_status,
            production_allocations.source_version,
            production_allocations.updated_at
        FROM production_allocations
        JOIN quality_lots ON quality_lots.id = production_allocations.quality_lot_id
        WHERE quality_lots.plant_id IN :plant_ids
          AND (
              :has_record_filter = FALSE
              OR CAST(production_allocations.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
          )
        ORDER BY production_allocations.id
    """),
    "production_impact": _scoped_statement("""
        SELECT DISTINCT
            production_orders.id,
            production_orders.order_number,
            production_orders.part_id,
            production_orders.plant_id,
            production_orders.required_quantity,
            production_orders.start_date,
            production_orders.status,
            production_orders.supervisor_id,
            users.email AS supervisor_email,
            1 AS source_version,
            production_orders.updated_at
        FROM production_orders
        JOIN production_allocations
          ON production_allocations.production_order_id = production_orders.id
        JOIN quality_lots ON quality_lots.id = production_allocations.quality_lot_id
        LEFT JOIN users ON users.id = production_orders.supervisor_id
        WHERE quality_lots.plant_id IN :plant_ids
          AND (
              :has_record_filter = FALSE
              OR CAST(production_orders.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
          )
        ORDER BY production_orders.order_number
    """),
}
MAIL_QUERY = text("""
    SELECT
        messages.id,
        messages.message_key,
        messages.purchase_order_id,
        messages.supplier_id,
        messages.sender,
        messages.subject,
        messages.body,
        messages.received_at,
        messages.payload
    FROM messages
    JOIN users ON users.id = CAST(:actor_id AS UUID)
    WHERE messages.recipient = users.email
      AND (
          :has_record_filter = FALSE
          OR CAST(messages.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
      )
    ORDER BY messages.received_at
""")
CALENDAR_QUERY = text("""
    SELECT
        calendar_events.id,
        calendar_events.event_key,
        calendar_events.user_id,
        calendar_events.event_type,
        calendar_events.starts_at,
        calendar_events.ends_at,
        calendar_events.payload
    FROM calendar_events
    WHERE calendar_events.user_id = CAST(:actor_id AS UUID)
      AND (
          :has_record_filter = FALSE
          OR CAST(calendar_events.id AS TEXT) = ANY(CAST(:record_ids AS TEXT[]))
      )
      AND (CAST(:range_start AS DATE) IS NULL OR calendar_events.ends_at::DATE >= :range_start)
      AND (CAST(:range_end AS DATE) IS NULL OR calendar_events.starts_at::DATE <= :range_end)
    ORDER BY calendar_events.starts_at
""")


class _PostgresProvider:
    """Own a provider-local database connection pool without leaking it into the domain."""

    def __init__(self, database_url: str) -> None:
        self._engine: Engine = create_engine(database_url)


class PostgresErpAdapter(_PostgresProvider):
    """Return only permitted plant-level ERP evidence for an actor with ERP read scope."""

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        """Filter visibility in each ERP SQL statement before constructing evidence."""
        requested_types = _validate_record_types(query.record_types, ERP_QUERIES, "ERP")
        if not requested_types or "erp:read" not in actor.scopes or not actor.plant_ids:
            return ()

        parameters = _record_filter_parameters(query.record_ids)
        parameters["plant_ids"] = sorted(actor.plant_ids)
        evidence: list[Evidence] = []
        with self._engine.connect() as connection:
            for record_type in requested_types:
                rows = connection.execute(ERP_QUERIES[record_type], parameters).mappings().all()
                evidence.extend(_erp_evidence(record_type, row) for row in rows)
        return tuple(evidence)


class PostgresQualityAdapter(_PostgresProvider):
    """Return only plant-scoped quality facts to actors with the dedicated quality-read scope."""

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        """Enforce quality ownership before mapping lots or production-impact projections."""
        requested_types = _validate_record_types(query.record_types, QUALITY_QUERIES, "quality")
        if not requested_types or "quality:lot:read" not in actor.scopes or not actor.plant_ids:
            return ()

        parameters = _record_filter_parameters(query.record_ids)
        parameters["plant_ids"] = sorted(actor.plant_ids)
        evidence: list[Evidence] = []
        with self._engine.connect() as connection:
            for record_type in requested_types:
                rows = connection.execute(QUALITY_QUERIES[record_type], parameters).mappings().all()
                evidence.extend(_quality_evidence(record_type, row) for row in rows)
        return tuple(evidence)


class PostgresMailAdapter(_PostgresProvider):
    """Return only messages in the actor's seeded mailbox after scope validation."""

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        """Bind actor ID into the recipient comparison owned by the mail query itself."""
        requested_types = _validate_record_types(
            query.record_types, {"message": MAIL_QUERY}, "mail"
        )
        if not requested_types or "mail:read" not in actor.scopes:
            return ()

        parameters = _record_filter_parameters(query.record_ids)
        parameters["actor_id"] = str(actor.user_id)
        with self._engine.connect() as connection:
            rows = connection.execute(MAIL_QUERY, parameters).mappings().all()
        return tuple(_mail_evidence(row) for row in rows)


class PostgresCalendarAdapter(_PostgresProvider):
    """Return only calendar events owned by the actor after scope validation."""

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        """Bind actor ID and requested date range into provider-owned calendar SQL."""
        requested_types = _validate_record_types(
            query.record_types,
            {"calendar_event": CALENDAR_QUERY},
            "calendar",
        )
        if not requested_types or "calendar:read" not in actor.scopes:
            return ()

        parameters = _record_filter_parameters(query.record_ids)
        parameters.update(
            {
                "actor_id": str(actor.user_id),
                "range_start": None if query.date_range is None else query.date_range.start,
                "range_end": None if query.date_range is None else query.date_range.end,
            }
        )
        with self._engine.connect() as connection:
            rows = connection.execute(CALENDAR_QUERY, parameters).mappings().all()
        return tuple(_calendar_evidence(row) for row in rows)


def _validate_record_types(
    requested_types: frozenset[str],
    supported_types: Mapping[str, TextClause],
    provider_name: str,
) -> tuple[str, ...]:
    """Validate a narrow provider-owned record-type vocabulary before any database access."""
    unsupported_types = requested_types - supported_types.keys()
    if unsupported_types:
        unsupported = ", ".join(sorted(unsupported_types))
        raise UnsupportedEvidenceTypeError(
            f"unsupported {provider_name} evidence type: {unsupported}"
        )
    return tuple(sorted(requested_types))


def _record_filter_parameters(record_ids: frozenset[str]) -> dict[str, object]:
    """Build parameters for an optional record-ID filter without interpolating caller data."""
    return {
        "has_record_filter": bool(record_ids),
        "record_ids": sorted(record_ids),
    }


def _erp_evidence(record_type: str, row: RowMapping) -> Evidence:
    """Map a scoped ERP row to the generic evidence contract without exposing SQL state."""
    payload = {
        key: value
        for key, value in row.items()
        if key not in {"id", "source_version", "updated_at"}
    }
    return Evidence(
        evidence_id=EvidenceId(f"erp:{record_type}:{row['id']}"),
        source="erp",
        record_type=record_type,
        record_id=str(row["id"]),
        source_version=cast(int, row["source_version"]),
        observed_at=cast(datetime, row["updated_at"]),
        payload=payload,
    )


def _quality_evidence(record_type: str, row: RowMapping) -> Evidence:
    """Map quality-owned rows to evidence without claiming general ERP visibility."""
    payload = {
        key: value
        for key, value in row.items()
        if key not in {"id", "source_version", "updated_at"}
    }
    return Evidence(
        evidence_id=EvidenceId(f"quality:{record_type}:{row['id']}"),
        source="quality",
        record_type=record_type,
        record_id=str(row["id"]),
        source_version=cast(int, row["source_version"]),
        observed_at=cast(datetime, row["updated_at"]),
        payload=payload,
    )


def _mail_evidence(row: RowMapping) -> Evidence:
    """Map a mailbox-visible row to evidence while retaining its received timestamp."""
    payload = {key: value for key, value in row.items() if key not in {"id", "received_at"}}
    return Evidence(
        evidence_id=EvidenceId(f"mail:message:{row['id']}"),
        source="mail",
        record_type="message",
        record_id=str(row["id"]),
        source_version=1,
        observed_at=cast(datetime, row["received_at"]),
        payload=payload,
    )


def _calendar_evidence(row: RowMapping) -> Evidence:
    """Map an actor-owned calendar row to evidence without exposing other calendars."""
    payload = {key: value for key, value in row.items() if key not in {"id", "starts_at"}}
    return Evidence(
        evidence_id=EvidenceId(f"calendar:event:{row['id']}"),
        source="calendar",
        record_type="calendar_event",
        record_id=str(row["id"]),
        source_version=1,
        observed_at=cast(datetime, row["starts_at"]),
        payload=payload,
    )
