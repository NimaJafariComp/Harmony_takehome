"""Transactional local stand-ins for the reviewed ERP, notification, and scheduler tool APIs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from enterprise_agent.application.tools import (
    CreateReplacementPOInput,
    NotifyProductionInput,
    ReduceOrCancelPOInput,
    ScheduleArrivalCheckInput,
    ToolInput,
    ToolName,
    authorize_tool,
)
from enterprise_agent.domain import ActorContext, ToolInvocation, ToolInvocationStatus

INSERT_INVOCATION = text("""
    INSERT INTO tool_invocations (
        id, workflow_instance_id, tool_name, idempotency_key, status, parameters, result,
        attempt_count, started_at, completed_at, created_at, updated_at
    ) VALUES (
        CAST(:invocation_id AS UUID), CAST(:workflow_id AS UUID), :tool_name,
        :idempotency_key, :started_status, CAST(:parameters AS JSONB), NULL, 1, :started_at,
        NULL, :started_at, :started_at
    )
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id
""")
SELECT_INVOCATION_FOR_UPDATE = text("""
    SELECT workflow_instance_id, tool_name, idempotency_key, status, parameters, result
    FROM tool_invocations
    WHERE idempotency_key = :idempotency_key
    FOR UPDATE
""")
RETRY_INVOCATION = text("""
    UPDATE tool_invocations
    SET attempt_count = attempt_count + 1, updated_at = :updated_at
    WHERE idempotency_key = :idempotency_key AND status = :started_status
""")
COMPLETE_INVOCATION = text("""
    UPDATE tool_invocations
    SET status = :succeeded_status,
        result = CAST(:result AS JSONB),
        completed_at = :completed_at,
        updated_at = :completed_at
    WHERE idempotency_key = :idempotency_key AND status = :started_status
""")
SELECT_CREATE_SOURCE = text("""
    SELECT original.part_id::text AS part_id,
           original.plant_id,
           supplier.lead_time_days,
           production.start_date
    FROM purchase_orders AS original
    JOIN suppliers AS supplier
      ON supplier.id = CAST(:supplier_id AS UUID)
     AND supplier.part_id = original.part_id
     AND supplier.plant_id = original.plant_id
     AND supplier.approved = TRUE
    JOIN production_orders AS production
      ON production.id = CAST(:production_order_id AS UUID)
     AND production.part_id = original.part_id
     AND production.plant_id = original.plant_id
    WHERE original.id = CAST(:original_purchase_order_id AS UUID)
    FOR UPDATE OF original, supplier, production
""")
INSERT_REPLACEMENT_PO = text("""
    INSERT INTO purchase_orders (
        id, po_number, part_id, supplier_id, plant_id, ordered_quantity, received_quantity,
        status, expected_receipt_date, source_version, created_at, updated_at
    ) VALUES (
        CAST(:purchase_order_id AS UUID), :po_number, CAST(:part_id AS UUID),
        CAST(:supplier_id AS UUID), :plant_id, CAST(:quantity AS NUMERIC), 0, 'open',
        :expected_receipt_date, 1, :occurred_at, :occurred_at
    )
    RETURNING id::text AS purchase_order_id, po_number, expected_receipt_date
""")
SELECT_ORIGINAL_PO_FOR_UPDATE = text("""
    SELECT ordered_quantity, received_quantity, status
    FROM purchase_orders
    WHERE id = CAST(:original_purchase_order_id AS UUID)
    FOR UPDATE
""")
REDUCE_OR_CANCEL_ORIGINAL_PO = text("""
    UPDATE purchase_orders
    SET ordered_quantity = received_quantity,
        status = 'cancelled',
        source_version = source_version + 1,
        updated_at = :occurred_at
    WHERE id = CAST(:original_purchase_order_id AS UUID)
    RETURNING ordered_quantity, received_quantity, status, source_version
""")
SELECT_PRODUCTION_RECIPIENT = text("""
    SELECT email
    FROM users
    WHERE role = 'production_supervisor' AND email IS NOT NULL
    ORDER BY id ASC
    LIMIT 1
""")
INSERT_PRODUCTION_NOTIFICATION = text("""
    INSERT INTO messages (
        id, message_key, purchase_order_id, supplier_id, sender, recipient, subject, body,
        received_at, payload
    ) VALUES (
        CAST(:message_id AS UUID), :message_key, NULL, NULL, :sender, :recipient, :subject,
        :body, :occurred_at, CAST(:payload AS JSONB)
    )
    RETURNING id::text AS message_id
""")
SELECT_WORKFLOW_ATTENTION = text("""
    SELECT plan.attention_id::text AS attention_id
    FROM workflow_instances AS workflow
    JOIN plans AS plan ON plan.id = workflow.plan_id
    WHERE workflow.id = CAST(:workflow_id AS UUID)
""")
INSERT_ARRIVAL_CHECK = text("""
    INSERT INTO scheduled_tasks (
        id, attention_id, workflow_instance_id, task_type, due_at, status, idempotency_key,
        payload, attempt_count, lease_expires_at, completed_at, created_at, updated_at
    ) VALUES (
        CAST(:task_id AS UUID), CAST(:attention_id AS UUID), CAST(:workflow_id AS UUID),
        'arrival_check', :due_at, 'pending', :idempotency_key, CAST(:payload AS JSONB), 0,
        NULL, NULL, :occurred_at, :occurred_at
    )
    RETURNING id::text AS task_id, due_at
""")


class ToolExecutionError(RuntimeError):
    """Raised when a typed tool request cannot safely produce its declared side effect."""


class PostgresScenarioAToolAdapter:
    """Run reviewed Scenario A effects behind an external-style idempotency journal boundary."""

    def __init__(self, database_url: str) -> None:
        """Connect the independently committed simulated provider boundary to PostgreSQL."""
        self._engine: Engine = create_engine(database_url)

    def execute(self, actor: ActorContext, invocation: ToolInvocation) -> Mapping[str, object]:
        """Authorize, execute once by stable key, and durably retain only sanitized tool output."""
        input_value = _validated_input(actor, invocation)
        with self._engine.begin() as connection:
            inserted = connection.execute(
                INSERT_INVOCATION,
                {
                    "invocation_id": str(invocation.invocation_id),
                    "workflow_id": str(invocation.workflow_id),
                    "tool_name": invocation.tool_name,
                    "idempotency_key": invocation.idempotency_key,
                    "started_status": ToolInvocationStatus.STARTED.value,
                    "parameters": _as_json(invocation.parameters),
                    "started_at": invocation.started_at,
                },
            )
            existing = (
                connection.execute(
                    SELECT_INVOCATION_FOR_UPDATE,
                    {"idempotency_key": invocation.idempotency_key},
                )
                .mappings()
                .one()
            )
            persisted = cast(Mapping[str, object], existing)
            _validate_invocation_binding(persisted, invocation)
            if existing["status"] == ToolInvocationStatus.SUCCEEDED.value:
                return _stored_result(persisted)
            if existing["status"] != ToolInvocationStatus.STARTED.value:
                raise ToolExecutionError("tool invocation is not retryable")
            if inserted.scalar_one_or_none() is None:
                connection.execute(
                    RETRY_INVOCATION,
                    {
                        "idempotency_key": invocation.idempotency_key,
                        "started_status": ToolInvocationStatus.STARTED.value,
                        "updated_at": invocation.started_at,
                    },
                )
            result = _execute_effect(connection, invocation, input_value)
            connection.execute(
                COMPLETE_INVOCATION,
                {
                    "idempotency_key": invocation.idempotency_key,
                    "started_status": ToolInvocationStatus.STARTED.value,
                    "succeeded_status": ToolInvocationStatus.SUCCEEDED.value,
                    "result": _as_json(result),
                    "completed_at": invocation.started_at,
                },
            )
            return result


def _validated_input(actor: ActorContext, invocation: ToolInvocation) -> ToolInput:
    """Make each concrete tool enforce its own declared scope and strict input model."""
    if (
        invocation.status is not ToolInvocationStatus.STARTED
        or invocation.started_at is None
        or not invocation.idempotency_key.strip()
    ):
        raise ToolExecutionError("tool invocation is not a valid started action")
    definition = authorize_tool(actor, invocation.tool_name)
    try:
        input_value = definition.input_model.model_validate(dict(invocation.parameters))
    except ValidationError as error:
        raise ToolExecutionError("tool invocation parameters are invalid") from error
    if not isinstance(
        input_value,
        (
            CreateReplacementPOInput,
            ReduceOrCancelPOInput,
            NotifyProductionInput,
            ScheduleArrivalCheckInput,
        ),
    ):
        raise ToolExecutionError("tool is outside the Scenario A execution boundary")
    return input_value


def _validate_invocation_binding(row: Mapping[str, object], invocation: ToolInvocation) -> None:
    """Reject a stable key that is replayed against another workflow, tool, or payload."""
    if (
        str(row["workflow_instance_id"]) != str(invocation.workflow_id)
        or row["tool_name"] != invocation.tool_name
        or row["idempotency_key"] != invocation.idempotency_key
        or dict(cast(Mapping[str, object], row["parameters"])) != dict(invocation.parameters)
    ):
        raise ToolExecutionError("tool idempotency key does not match its persisted invocation")


def _stored_result(row: Mapping[str, object]) -> Mapping[str, object]:
    """Return the original provider response verbatim for a completed idempotent invocation."""
    result = row["result"]
    if not isinstance(result, Mapping):
        raise ToolExecutionError("completed tool invocation has no result")
    return dict(cast(Mapping[str, object], result))


def _execute_effect(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: ToolInput,
) -> dict[str, object]:
    """Run one reviewed effect after its provider journal row is locked by the stable key."""
    tool_name = ToolName(invocation.tool_name)
    if tool_name is ToolName.CREATE_REPLACEMENT_PO and isinstance(
        input_value, CreateReplacementPOInput
    ):
        return _create_replacement_purchase_order(connection, invocation, input_value)
    if tool_name is ToolName.REDUCE_OR_CANCEL_PO and isinstance(input_value, ReduceOrCancelPOInput):
        return _reduce_or_cancel_original_purchase_order(connection, invocation, input_value)
    if tool_name is ToolName.NOTIFY_PRODUCTION and isinstance(input_value, NotifyProductionInput):
        return _notify_production(connection, invocation, input_value)
    if tool_name is ToolName.SCHEDULE_ARRIVAL_CHECK and isinstance(
        input_value, ScheduleArrivalCheckInput
    ):
        return _schedule_arrival_check(connection, invocation, input_value)
    raise ToolExecutionError("tool input does not match its declared Scenario A effect")


def _create_replacement_purchase_order(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: CreateReplacementPOInput,
) -> dict[str, object]:
    """Create a replacement only for an approved matching supplier that still meets production."""
    source = (
        connection.execute(
            SELECT_CREATE_SOURCE,
            {
                "original_purchase_order_id": input_value.original_purchase_order_id,
                "supplier_id": input_value.supplier_id,
                "production_order_id": input_value.production_order_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    if source is None:
        raise ToolExecutionError("replacement purchase order source is not currently actionable")
    started_at = invocation.started_at
    if started_at is None:
        raise ToolExecutionError("tool invocation is not a valid started action")
    expected_receipt_date = started_at.date() + timedelta(days=cast(int, source["lead_time_days"]))
    if expected_receipt_date > source["start_date"]:
        raise ToolExecutionError("approved supplier cannot meet the production date")
    created = (
        connection.execute(
            INSERT_REPLACEMENT_PO,
            {
                "purchase_order_id": str(uuid4()),
                "po_number": f"RPL-{uuid4().hex[:12].upper()}",
                "part_id": source["part_id"],
                "supplier_id": input_value.supplier_id,
                "plant_id": source["plant_id"],
                "quantity": str(input_value.quantity),
                "expected_receipt_date": expected_receipt_date,
                "occurred_at": started_at,
            },
        )
        .mappings()
        .one()
    )
    return {
        "replacement_purchase_order_id": cast(str, created["purchase_order_id"]),
        "replacement_po_number": cast(str, created["po_number"]),
        "expected_receipt_date": created["expected_receipt_date"].isoformat(),
    }


def _reduce_or_cancel_original_purchase_order(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: ReduceOrCancelPOInput,
) -> dict[str, object]:
    """Close exactly the remaining approved quantity while retaining compensation facts."""
    original = (
        connection.execute(
            SELECT_ORIGINAL_PO_FOR_UPDATE,
            {"original_purchase_order_id": input_value.original_purchase_order_id},
        )
        .mappings()
        .one_or_none()
    )
    if original is None:
        raise ToolExecutionError("original purchase order does not exist")
    ordered_quantity = cast(Decimal, original["ordered_quantity"])
    received_quantity = cast(Decimal, original["received_quantity"])
    if ordered_quantity - received_quantity != input_value.quantity or original["status"] not in {
        "open",
        "delayed",
    }:
        raise ToolExecutionError(
            "original purchase order is no longer actionable for this quantity"
        )
    updated = (
        connection.execute(
            REDUCE_OR_CANCEL_ORIGINAL_PO,
            {
                "original_purchase_order_id": input_value.original_purchase_order_id,
                "occurred_at": invocation.started_at,
            },
        )
        .mappings()
        .one()
    )
    return {
        "original_purchase_order_id": input_value.original_purchase_order_id,
        "previous_ordered_quantity": str(ordered_quantity),
        "previous_status": cast(str, original["status"]),
        "ordered_quantity": str(cast(Decimal, updated["ordered_quantity"])),
        "received_quantity": str(cast(Decimal, updated["received_quantity"])),
        "status": cast(str, updated["status"]),
        "source_version": cast(int, updated["source_version"]),
    }


def _notify_production(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: NotifyProductionInput,
) -> dict[str, object]:
    """Persist one idempotent, minimally scoped production notification."""
    recipient = connection.execute(SELECT_PRODUCTION_RECIPIENT).scalar_one_or_none()
    if not isinstance(recipient, str):
        raise ToolExecutionError("production notification recipient is unavailable")
    created = (
        connection.execute(
            INSERT_PRODUCTION_NOTIFICATION,
            {
                "message_id": str(uuid4()),
                "message_key": invocation.idempotency_key,
                "sender": "enterprise-agent@example.invalid",
                "recipient": recipient,
                "subject": f"Production order {input_value.production_order_id}: purchase-order update",
                "body": input_value.message,
                "occurred_at": invocation.started_at,
                "payload": _as_json(
                    {
                        "production_order_id": input_value.production_order_id,
                        "workflow_id": str(invocation.workflow_id),
                    }
                ),
            },
        )
        .mappings()
        .one()
    )
    return {
        "message_id": cast(str, created["message_id"]),
        "recipient": recipient,
        "production_order_id": input_value.production_order_id,
    }


def _schedule_arrival_check(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: ScheduleArrivalCheckInput,
) -> dict[str, object]:
    """Create the durable Tuesday arrival-check record without implementing its worker yet."""
    attention = (
        connection.execute(
            SELECT_WORKFLOW_ATTENTION,
            {"workflow_id": str(invocation.workflow_id)},
        )
        .mappings()
        .one_or_none()
    )
    if attention is None:
        raise ToolExecutionError("workflow attention binding is unavailable")
    scheduled = (
        connection.execute(
            INSERT_ARRIVAL_CHECK,
            {
                "task_id": str(uuid4()),
                "attention_id": attention["attention_id"],
                "workflow_id": str(invocation.workflow_id),
                "idempotency_key": invocation.idempotency_key,
                "due_at": input_value.due_at,
                "payload": _as_json({"purchase_order_id": input_value.purchase_order_id}),
                "occurred_at": invocation.started_at,
            },
        )
        .mappings()
        .one()
    )
    return {
        "scheduled_task_id": cast(str, scheduled["task_id"]),
        "purchase_order_id": input_value.purchase_order_id,
        "due_at": scheduled["due_at"].isoformat(),
    }


def _as_json(values: Mapping[str, object]) -> str:
    """Serialize only JSON-compatible typed tool payloads through bound SQL parameters."""
    return json.dumps(dict(values), sort_keys=True)
