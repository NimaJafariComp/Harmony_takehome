"""Transactional local stand-ins for the reviewed ERP, notification, and scheduler tool APIs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, RowMapping

from enterprise_agent.application.tools import (
    CompensationAction,
    CreateReplacementPOInput,
    FlagShortageToPurchasingInput,
    NotifyProductionInput,
    PlacePurchaseOrderHoldInput,
    ReallocateLotInput,
    ReduceOrCancelPOInput,
    ScheduleArrivalCheckInput,
    TerminalToolExecutionError,
    ToolInput,
    ToolName,
    authorize_tool,
)
from enterprise_agent.domain import (
    ActorContext,
    ToolCompensation,
    ToolInvocation,
    ToolInvocationStatus,
)

ProviderRow = Mapping[str, object] | RowMapping

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
COMPENSATE_ORIGINAL_INVOCATION = text("""
    UPDATE tool_invocations
    SET status = :compensated_status,
        updated_at = :completed_at
    WHERE workflow_instance_id = CAST(:workflow_id AS UUID)
      AND tool_name = :tool_name
      AND idempotency_key = :original_idempotency_key
      AND status = :succeeded_status
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
SELECT_PURCHASE_ORDER_HOLD_SOURCE = text("""
    SELECT purchase_orders.id::text AS purchase_order_id,
           purchase_orders.status AS purchase_order_status,
           purchase_orders.source_version AS purchase_order_source_version,
           purchase_orders.part_id::text AS purchase_order_part_id,
           purchase_orders.plant_id AS purchase_order_plant_id,
           production_orders.id::text AS production_order_id,
           production_orders.status AS production_order_status,
           production_orders.part_id::text AS production_order_part_id,
           production_orders.plant_id AS production_order_plant_id
    FROM purchase_orders
    JOIN production_orders
      ON production_orders.id = CAST(:production_order_id AS UUID)
    WHERE purchase_orders.id = CAST(:purchase_order_id AS UUID)
    FOR UPDATE OF purchase_orders, production_orders
""")
PLACE_PURCHASE_ORDER_HOLD = text("""
    UPDATE purchase_orders
    SET status = 'on_hold',
        source_version = source_version + 1,
        updated_at = :occurred_at
    WHERE id = CAST(:purchase_order_id AS UUID)
      AND status = 'open'
      AND source_version = :expected_purchase_order_version
    RETURNING id::text AS purchase_order_id, status, source_version
""")
SELECT_PRODUCTION_RECIPIENT = text("""
    SELECT users.email
    FROM production_orders
    JOIN users ON users.id = production_orders.supervisor_id
    WHERE production_orders.id = CAST(:production_order_id AS UUID)
      AND production_orders.status IN ('scheduled', 'in_progress')
      AND users.role = 'production_supervisor'
      AND users.email IS NOT NULL
    FOR SHARE OF production_orders, users
""")
SELECT_REALLOCATE_LOT_FOR_UPDATE = text("""
    SELECT id::text AS quality_lot_id,
           part_id::text AS part_id,
           plant_id,
           quantity,
           status,
           production_order_id::text AS production_order_id,
           allocated_quantity,
           source_version
    FROM quality_lots
    WHERE id = CAST(:quality_lot_id AS UUID)
    FOR UPDATE
""")
SELECT_REALLOCATION_PRODUCTION_FOR_UPDATE = text("""
    SELECT id::text AS production_order_id, part_id::text AS part_id, plant_id, status
    FROM production_orders
    WHERE id = CAST(:production_order_id AS UUID)
    FOR UPDATE
""")
SELECT_ALLOCATION_FOR_UPDATE = text("""
    SELECT id::text AS allocation_id, allocated_quantity, source_version
    FROM production_allocations
    WHERE quality_lot_id = CAST(:quality_lot_id AS UUID)
      AND production_order_id = CAST(:production_order_id AS UUID)
    FOR UPDATE
""")
INSERT_ALLOCATION = text("""
    INSERT INTO production_allocations (
        id, quality_lot_id, production_order_id, allocated_quantity, source_version,
        created_at, updated_at
    ) VALUES (
        CAST(:allocation_id AS UUID), CAST(:quality_lot_id AS UUID),
        CAST(:production_order_id AS UUID), CAST(:allocated_quantity AS NUMERIC), 1,
        :occurred_at, :occurred_at
    )
    RETURNING id::text AS allocation_id, allocated_quantity, source_version
""")
UPDATE_ALLOCATION_QUANTITY = text("""
    UPDATE production_allocations
    SET allocated_quantity = CAST(:allocated_quantity AS NUMERIC),
        source_version = source_version + 1,
        updated_at = :occurred_at
    WHERE id = CAST(:allocation_id AS UUID)
      AND source_version = :source_version
    RETURNING id::text AS allocation_id, allocated_quantity, source_version
""")
UPDATE_LOT_ALLOCATION = text("""
    UPDATE quality_lots
    SET allocated_quantity = CAST(:allocated_quantity AS NUMERIC),
        production_order_id = CAST(:production_order_id AS UUID),
        source_version = source_version + 1,
        updated_at = :occurred_at
    WHERE id = CAST(:quality_lot_id AS UUID)
      AND source_version = :source_version
    RETURNING id::text AS quality_lot_id, allocated_quantity, source_version
""")
SELECT_SHORTAGE_PRODUCTION_FOR_UPDATE = text("""
    SELECT id::text AS production_order_id,
           part_id::text AS part_id,
           plant_id,
           required_quantity,
           status
    FROM production_orders
    WHERE id = CAST(:production_order_id AS UUID)
    FOR UPDATE
""")
SELECT_RELEASED_LOTS_FOR_SHARE = text("""
    SELECT lots.quantity,
           lots.allocated_quantity,
           COALESCE(
               (
                   SELECT allocations.allocated_quantity
                   FROM production_allocations AS allocations
                   WHERE allocations.quality_lot_id = lots.id
                     AND allocations.production_order_id = CAST(:production_order_id AS UUID)
               ),
               0
           ) AS allocated_to_production_quantity
    FROM quality_lots AS lots
    WHERE lots.part_id = CAST(:part_id AS UUID)
      AND lots.plant_id = :plant_id
      AND lots.status = 'released'
    FOR SHARE OF lots
""")
SELECT_PURCHASING_RECIPIENT = text("""
    SELECT email
    FROM users
    WHERE role = 'purchasing_manager' AND email IS NOT NULL
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
    SELECT plan.attention_id::text AS attention_id,
           plan.actor_id::text AS actor_id
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
COMPENSATE_REPLACEMENT_PO = text("""
    UPDATE purchase_orders
    SET status = 'cancelled',
        source_version = source_version + 1,
        updated_at = :occurred_at
    WHERE id = CAST(:replacement_purchase_order_id AS UUID)
      AND status = 'open'
    RETURNING id::text AS purchase_order_id, status, source_version
""")
RESTORE_ORIGINAL_PO = text("""
    UPDATE purchase_orders
    SET ordered_quantity = CAST(:previous_ordered_quantity AS NUMERIC),
        status = :previous_status,
        source_version = source_version + 1,
        updated_at = :occurred_at
    WHERE id = CAST(:original_purchase_order_id AS UUID)
      AND ordered_quantity = CAST(:ordered_quantity AS NUMERIC)
      AND received_quantity = CAST(:received_quantity AS NUMERIC)
      AND status = :status
      AND source_version = :source_version
    RETURNING id::text AS purchase_order_id, ordered_quantity, received_quantity, status, source_version
""")
RESTORE_HELD_PURCHASE_ORDER = text("""
    UPDATE purchase_orders
    SET status = :previous_status,
        source_version = source_version + 1,
        updated_at = :occurred_at
    WHERE id = CAST(:purchase_order_id AS UUID)
      AND status = 'on_hold'
      AND source_version = :expected_source_version
    RETURNING id::text AS purchase_order_id, status, source_version
""")
SELECT_ORIGINAL_NOTIFICATION = text("""
    SELECT id::text AS message_id, recipient, subject
    FROM messages
    WHERE id = CAST(:message_id AS UUID)
      AND message_key = :original_idempotency_key
    FOR UPDATE
""")
INSERT_CORRECTION_NOTIFICATION = text("""
    INSERT INTO messages (
        id, message_key, purchase_order_id, supplier_id, sender, recipient, subject, body,
        received_at, payload
    ) VALUES (
        CAST(:message_id AS UUID), :message_key, NULL, NULL, :sender, :recipient, :subject,
        :body, :occurred_at, CAST(:payload AS JSONB)
    )
    RETURNING id::text AS message_id
""")
CANCEL_ARRIVAL_CHECK = text("""
    UPDATE scheduled_tasks
    SET status = 'cancelled',
        completed_at = :occurred_at,
        updated_at = :occurred_at
    WHERE id = CAST(:scheduled_task_id AS UUID)
      AND workflow_instance_id = CAST(:workflow_id AS UUID)
      AND idempotency_key = :original_idempotency_key
      AND status = 'pending'
    RETURNING id::text AS task_id, status
""")
DELETE_CREATED_ALLOCATION = text("""
    DELETE FROM production_allocations
    WHERE id = CAST(:allocation_id AS UUID)
      AND allocated_quantity = CAST(:allocated_quantity AS NUMERIC)
      AND source_version = :source_version
    RETURNING id::text AS allocation_id
""")
RESTORE_LOT_ALLOCATION = text("""
    UPDATE quality_lots
    SET allocated_quantity = CAST(:allocated_quantity AS NUMERIC),
        production_order_id = CAST(:production_order_id AS UUID),
        source_version = source_version + 1,
        updated_at = :occurred_at
    WHERE id = CAST(:quality_lot_id AS UUID)
      AND allocated_quantity = CAST(:expected_allocated_quantity AS NUMERIC)
      AND source_version = :source_version
    RETURNING id::text AS quality_lot_id, allocated_quantity, source_version
""")


class ToolExecutionError(TerminalToolExecutionError):
    """Raised when a typed tool request cannot safely produce its declared side effect."""


class PostgresToolAdapter:
    """Run reviewed catalog effects behind an external-style idempotency journal boundary."""

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

    def compensate(
        self, actor: ActorContext, compensation: ToolCompensation
    ) -> Mapping[str, object]:
        """Reverse one journaled effect by a separate stable key after strict provenance checks."""
        _validate_compensation(actor, compensation)
        parameters = _compensation_parameters(compensation)
        with self._engine.begin() as connection:
            inserted = connection.execute(
                INSERT_INVOCATION,
                {
                    "invocation_id": str(uuid4()),
                    "workflow_id": str(compensation.workflow_id),
                    "tool_name": compensation.action,
                    "idempotency_key": compensation.idempotency_key,
                    "started_status": ToolInvocationStatus.STARTED.value,
                    "parameters": _as_json(parameters),
                    "started_at": compensation.requested_at,
                },
            )
            journal = (
                connection.execute(
                    SELECT_INVOCATION_FOR_UPDATE,
                    {"idempotency_key": compensation.idempotency_key},
                )
                .mappings()
                .one()
            )
            persisted = cast(Mapping[str, object], journal)
            _validate_compensation_binding(persisted, compensation, parameters)
            if journal["status"] == ToolInvocationStatus.SUCCEEDED.value:
                return _stored_result(persisted)
            if journal["status"] != ToolInvocationStatus.STARTED.value:
                raise ToolExecutionError("tool compensation invocation is not retryable")
            if inserted.scalar_one_or_none() is None:
                connection.execute(
                    RETRY_INVOCATION,
                    {
                        "idempotency_key": compensation.idempotency_key,
                        "started_status": ToolInvocationStatus.STARTED.value,
                        "updated_at": compensation.requested_at,
                    },
                )
            original = (
                connection.execute(
                    SELECT_INVOCATION_FOR_UPDATE,
                    {"idempotency_key": compensation.original_idempotency_key},
                )
                .mappings()
                .one_or_none()
            )
            if original is None:
                raise ToolExecutionError("original tool invocation is unavailable for compensation")
            _validate_original_for_compensation(cast(Mapping[str, object], original), compensation)
            result = _execute_compensation_effect(connection, compensation)
            connection.execute(
                COMPLETE_INVOCATION,
                {
                    "idempotency_key": compensation.idempotency_key,
                    "started_status": ToolInvocationStatus.STARTED.value,
                    "succeeded_status": ToolInvocationStatus.SUCCEEDED.value,
                    "result": _as_json(result),
                    "completed_at": compensation.requested_at,
                },
            )
            connection.execute(
                COMPENSATE_ORIGINAL_INVOCATION,
                {
                    "workflow_id": str(compensation.workflow_id),
                    "tool_name": compensation.tool_name,
                    "original_idempotency_key": compensation.original_idempotency_key,
                    "succeeded_status": ToolInvocationStatus.SUCCEEDED.value,
                    "compensated_status": ToolInvocationStatus.COMPENSATED.value,
                    "completed_at": compensation.requested_at,
                },
            )
            return result


PostgresScenarioAToolAdapter = PostgresToolAdapter


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
    return cast(ToolInput, input_value)


def _validate_compensation(actor: ActorContext, compensation: ToolCompensation) -> None:
    """Require a declared original tool, its same scope, matching reverse action, and opaque keys."""
    if (
        not compensation.original_idempotency_key.strip()
        or not compensation.idempotency_key.strip()
        or not compensation.effect_result
    ):
        raise ToolExecutionError("tool compensation has incomplete provenance")
    try:
        original_tool = ToolName(compensation.tool_name)
        action = CompensationAction(compensation.action)
    except ValueError as error:
        raise ToolExecutionError(
            "tool compensation is outside the reviewed tool catalog"
        ) from error
    definition = authorize_tool(actor, original_tool)
    if definition.compensation is not action:
        raise ToolExecutionError("tool compensation action does not match the original effect")


def _compensation_parameters(compensation: ToolCompensation) -> dict[str, object]:
    """Persist enough immutable provenance to reject a replay against a different original effect."""
    return {
        "original_tool_name": compensation.tool_name,
        "original_idempotency_key": compensation.original_idempotency_key,
        "effect_result": dict(compensation.effect_result),
    }


def _validate_invocation_binding(row: Mapping[str, object], invocation: ToolInvocation) -> None:
    """Reject a stable key that is replayed against another workflow, tool, or payload."""
    if (
        str(row["workflow_instance_id"]) != str(invocation.workflow_id)
        or row["tool_name"] != invocation.tool_name
        or row["idempotency_key"] != invocation.idempotency_key
        or dict(cast(Mapping[str, object], row["parameters"])) != dict(invocation.parameters)
    ):
        raise ToolExecutionError("tool idempotency key does not match its persisted invocation")


def _validate_compensation_binding(
    row: Mapping[str, object], compensation: ToolCompensation, parameters: Mapping[str, object]
) -> None:
    """Reject a compensation key replayed for another workflow, action, or original result."""
    if (
        str(row["workflow_instance_id"]) != str(compensation.workflow_id)
        or row["tool_name"] != compensation.action
        or row["idempotency_key"] != compensation.idempotency_key
        or dict(cast(Mapping[str, object], row["parameters"])) != dict(parameters)
    ):
        raise ToolExecutionError("tool compensation key does not match its persisted invocation")


def _validate_original_for_compensation(
    row: Mapping[str, object], compensation: ToolCompensation
) -> None:
    """Allow reversal only of this workflow's exact successfully journaled effect and result."""
    if (
        str(row["workflow_instance_id"]) != str(compensation.workflow_id)
        or row["tool_name"] != compensation.tool_name
        or row["idempotency_key"] != compensation.original_idempotency_key
        or row["status"] != ToolInvocationStatus.SUCCEEDED.value
        or not isinstance(row["result"], Mapping)
        or dict(cast(Mapping[str, object], row["result"])) != dict(compensation.effect_result)
    ):
        raise ToolExecutionError("original tool invocation is not safely compensable")


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
    if tool_name is ToolName.PLACE_PURCHASE_ORDER_HOLD and isinstance(
        input_value, PlacePurchaseOrderHoldInput
    ):
        return _place_purchase_order_hold(connection, invocation, input_value)
    if tool_name is ToolName.NOTIFY_PRODUCTION and isinstance(input_value, NotifyProductionInput):
        return _notify_production(connection, invocation, input_value)
    if tool_name is ToolName.SCHEDULE_ARRIVAL_CHECK and isinstance(
        input_value, ScheduleArrivalCheckInput
    ):
        return _schedule_arrival_check(connection, invocation, input_value)
    if tool_name is ToolName.REALLOCATE_LOT and isinstance(input_value, ReallocateLotInput):
        return _reallocate_lot(connection, invocation, input_value)
    if tool_name is ToolName.FLAG_SHORTAGE_TO_PURCHASING and isinstance(
        input_value, FlagShortageToPurchasingInput
    ):
        return _flag_shortage_to_purchasing(connection, invocation, input_value)
    raise ToolExecutionError("tool input does not match its declared effect")


def _execute_compensation_effect(
    connection: Connection, compensation: ToolCompensation
) -> dict[str, object]:
    """Run only the reverse operation declared for the exact journaled original effect."""
    action = CompensationAction(compensation.action)
    if action is CompensationAction.CANCEL_CREATED_REPLACEMENT_PO:
        return _cancel_created_replacement_purchase_order(connection, compensation)
    if action is CompensationAction.RESTORE_ORIGINAL_PURCHASE_ORDER:
        return _restore_original_purchase_order(connection, compensation)
    if action is CompensationAction.RESTORE_HELD_PURCHASE_ORDER:
        return _restore_held_purchase_order(connection, compensation)
    if action is CompensationAction.SEND_CORRECTION_NOTIFICATION:
        return _send_correction_notification(connection, compensation)
    if action is CompensationAction.CANCEL_ARRIVAL_CHECK:
        return _cancel_arrival_check(connection, compensation)
    if action is CompensationAction.RESTORE_PRIOR_ALLOCATION:
        return _restore_prior_allocation(connection, compensation)
    raise ToolExecutionError("tool compensation action is outside the reviewed tool catalog")


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


def _place_purchase_order_hold(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: PlacePurchaseOrderHoldInput,
) -> dict[str, object]:
    """Hold only a current open PO that still belongs to the exact runnable production demand."""
    source = (
        connection.execute(
            SELECT_PURCHASE_ORDER_HOLD_SOURCE,
            {
                "purchase_order_id": input_value.purchase_order_id,
                "production_order_id": input_value.production_order_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    if (
        source is None
        or source["purchase_order_status"] != "open"
        or _required_row_int(source, "purchase_order_source_version")
        != input_value.expected_purchase_order_version
        or source["production_order_status"] not in {"scheduled", "in_progress"}
        or source["purchase_order_part_id"] != source["production_order_part_id"]
        or source["purchase_order_plant_id"] != source["production_order_plant_id"]
    ):
        raise ToolExecutionError("purchase-order hold source is not currently actionable")
    held = (
        connection.execute(
            PLACE_PURCHASE_ORDER_HOLD,
            {
                "purchase_order_id": input_value.purchase_order_id,
                "expected_purchase_order_version": input_value.expected_purchase_order_version,
                "occurred_at": invocation.started_at,
            },
        )
        .mappings()
        .one_or_none()
    )
    if held is None:
        raise ToolExecutionError("purchase-order hold source changed before the hold completed")
    return {
        "purchase_order_id": _required_row_text(held, "purchase_order_id"),
        "previous_status": cast(str, source["purchase_order_status"]),
        "status": cast(str, held["status"]),
        "source_version": _required_row_int(held, "source_version"),
    }


def _notify_production(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: NotifyProductionInput,
) -> dict[str, object]:
    """Persist one idempotent, minimally scoped production notification."""
    recipient = connection.execute(
        SELECT_PRODUCTION_RECIPIENT,
        {"production_order_id": input_value.production_order_id},
    ).scalar_one_or_none()
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
                "subject": f"Production order {input_value.production_order_id}: enterprise-agent update",
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


def _reallocate_lot(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: ReallocateLotInput,
) -> dict[str, object]:
    """Move only a current released lot between matching, runnable production allocations."""
    lot = (
        connection.execute(
            SELECT_REALLOCATE_LOT_FOR_UPDATE,
            {"quality_lot_id": input_value.quality_lot_id},
        )
        .mappings()
        .one_or_none()
    )
    if lot is None:
        raise ToolExecutionError("quality lot is not currently reallocatable")
    if lot["status"] != "released":
        raise ToolExecutionError("quality lot is not currently reallocatable")

    previous_lot_allocation = _decimal_value(lot["allocated_quantity"])
    quantity = input_value.quantity
    source_allocation: ProviderRow | None = None
    source_previous_quantity: Decimal | None = None
    source_effect_version: int | None = None
    remaining_source_quantity: Decimal | None = None
    if input_value.from_production_order_id is None:
        if _decimal_value(lot["quantity"]) - previous_lot_allocation < quantity:
            raise ToolExecutionError("quality lot is not currently reallocatable")
        next_lot_allocation = previous_lot_allocation + quantity
        next_lot_production_order_id: str | None = input_value.to_production_order_id
    else:
        source_allocation = (
            connection.execute(
                SELECT_ALLOCATION_FOR_UPDATE,
                {
                    "quality_lot_id": input_value.quality_lot_id,
                    "production_order_id": input_value.from_production_order_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if source_allocation is None:
            raise ToolExecutionError("quality lot is not currently reallocatable")
        source_previous_quantity = _decimal_value(source_allocation["allocated_quantity"])
        if (
            source_previous_quantity < quantity
            or source_previous_quantity > previous_lot_allocation
        ):
            raise ToolExecutionError("quality lot is not currently reallocatable")
        remaining_source_quantity = source_previous_quantity - quantity
        updated_source = _update_allocation_quantity(
            connection,
            allocation_id=_required_row_text(source_allocation, "allocation_id"),
            allocated_quantity=remaining_source_quantity,
            source_version=_required_row_int(source_allocation, "source_version"),
            occurred_at=invocation.started_at,
        )
        if updated_source is None:
            raise ToolExecutionError("source allocation changed before reallocation completed")
        source_effect_version = _required_row_int(updated_source, "source_version")
        next_lot_allocation = previous_lot_allocation
        next_lot_production_order_id = (
            input_value.to_production_order_id
            if lot["production_order_id"] == input_value.from_production_order_id
            and remaining_source_quantity == Decimal(0)
            else _optional_row_text(lot, "production_order_id")
        )

    target = (
        connection.execute(
            SELECT_REALLOCATION_PRODUCTION_FOR_UPDATE,
            {"production_order_id": input_value.to_production_order_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        target is None
        or target["status"] not in {"scheduled", "in_progress"}
        or target["part_id"] != lot["part_id"]
        or target["plant_id"] != lot["plant_id"]
    ):
        raise ToolExecutionError("quality lot is not currently reallocatable")

    destination = (
        connection.execute(
            SELECT_ALLOCATION_FOR_UPDATE,
            {
                "quality_lot_id": input_value.quality_lot_id,
                "production_order_id": input_value.to_production_order_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    updated_destination: ProviderRow | None
    if destination is None:
        destination_previous_quantity = Decimal(0)
        updated_destination = (
            connection.execute(
                INSERT_ALLOCATION,
                {
                    "allocation_id": str(uuid4()),
                    "quality_lot_id": input_value.quality_lot_id,
                    "production_order_id": input_value.to_production_order_id,
                    "allocated_quantity": str(quantity),
                    "occurred_at": invocation.started_at,
                },
            )
            .mappings()
            .one()
        )
    else:
        destination_previous_quantity = _decimal_value(destination["allocated_quantity"])
        updated_destination = _update_allocation_quantity(
            connection,
            allocation_id=_required_row_text(destination, "allocation_id"),
            allocated_quantity=destination_previous_quantity + quantity,
            source_version=_required_row_int(destination, "source_version"),
            occurred_at=invocation.started_at,
        )
    if updated_destination is None:
        raise ToolExecutionError("destination allocation changed before reallocation completed")
    updated_lot = (
        connection.execute(
            UPDATE_LOT_ALLOCATION,
            {
                "quality_lot_id": input_value.quality_lot_id,
                "allocated_quantity": str(next_lot_allocation),
                "production_order_id": next_lot_production_order_id,
                "source_version": _required_row_int(lot, "source_version"),
                "occurred_at": invocation.started_at,
            },
        )
        .mappings()
        .one_or_none()
    )
    if updated_lot is None:
        raise ToolExecutionError("quality lot changed before reallocation completed")
    return {
        "quality_lot_id": input_value.quality_lot_id,
        "to_production_order_id": input_value.to_production_order_id,
        "quantity": _decimal_text(quantity),
        "previous_lot_allocated_quantity": _decimal_text(previous_lot_allocation),
        "previous_lot_production_order_id": _optional_row_text(lot, "production_order_id"),
        "lot_source_version": _required_row_int(updated_lot, "source_version"),
        "destination_allocation_id": _required_row_text(updated_destination, "allocation_id"),
        "destination_previous_quantity": _decimal_text(destination_previous_quantity),
        "destination_source_version": _required_row_int(updated_destination, "source_version"),
        "source_allocation_id": (
            _required_row_text(source_allocation, "allocation_id")
            if source_allocation is not None
            else None
        ),
        "source_previous_quantity": (
            _decimal_text(source_previous_quantity)
            if source_previous_quantity is not None
            else None
        ),
        "source_source_version": source_effect_version,
    }


def _flag_shortage_to_purchasing(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: FlagShortageToPurchasingInput,
) -> dict[str, object]:
    """Escalate a current exact shortage to purchasing without trusting stale planner quantities."""
    production = (
        connection.execute(
            SELECT_SHORTAGE_PRODUCTION_FOR_UPDATE,
            {"production_order_id": input_value.production_order_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        production is None
        or production["part_id"] != input_value.part_id
        or production["status"] not in {"scheduled", "in_progress"}
    ):
        raise ToolExecutionError("production shortage is not currently actionable")
    released_lots = (
        connection.execute(
            SELECT_RELEASED_LOTS_FOR_SHARE,
            {
                "part_id": input_value.part_id,
                "plant_id": production["plant_id"],
                "production_order_id": input_value.production_order_id,
            },
        )
        .mappings()
        .all()
    )
    available_quantity = sum(
        (
            _decimal_value(row["quantity"])
            - _decimal_value(row["allocated_quantity"])
            + _decimal_value(row["allocated_to_production_quantity"])
            for row in released_lots
        ),
        start=Decimal(0),
    )
    current_shortage = _decimal_value(production["required_quantity"]) - available_quantity
    if current_shortage <= Decimal(0) or current_shortage != input_value.shortage_quantity:
        raise ToolExecutionError("production shortage is not currently actionable")
    recipient = connection.execute(SELECT_PURCHASING_RECIPIENT).scalar_one_or_none()
    if not isinstance(recipient, str):
        raise ToolExecutionError("purchasing shortage recipient is unavailable")
    created = (
        connection.execute(
            INSERT_PRODUCTION_NOTIFICATION,
            {
                "message_id": str(uuid4()),
                "message_key": invocation.idempotency_key,
                "sender": "enterprise-agent@example.invalid",
                "recipient": recipient,
                "subject": f"Production shortage: {input_value.production_order_id}",
                "body": (
                    f"Current released-lot coverage is short by {_decimal_text(current_shortage)} units for "
                    f"part {input_value.part_id}."
                ),
                "occurred_at": invocation.started_at,
                "payload": _as_json(
                    {
                        "production_order_id": input_value.production_order_id,
                        "part_id": input_value.part_id,
                        "shortage_quantity": _decimal_text(current_shortage),
                        "workflow_id": str(invocation.workflow_id),
                    }
                ),
            },
        )
        .mappings()
        .one()
    )
    return {
        "message_id": _required_row_text(created, "message_id"),
        "recipient": recipient,
        "production_order_id": input_value.production_order_id,
        "part_id": input_value.part_id,
        "shortage_quantity": _decimal_text(current_shortage),
    }


def _schedule_arrival_check(
    connection: Connection,
    invocation: ToolInvocation,
    input_value: ScheduleArrivalCheckInput,
) -> dict[str, object]:
    """Create the durable Tuesday arrival-check record with all worker-required causal bindings."""
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
    payload: dict[str, object] = {
        "purchase_order_id": input_value.purchase_order_id,
        "original_attention_id": cast(str, attention["attention_id"]),
        "actor_id": cast(str, attention["actor_id"]),
    }
    if invocation.audit_run_id is not None:
        payload["audit_run_id"] = str(invocation.audit_run_id)
    scheduled = (
        connection.execute(
            INSERT_ARRIVAL_CHECK,
            {
                "task_id": str(uuid4()),
                "attention_id": attention["attention_id"],
                "workflow_id": str(invocation.workflow_id),
                "idempotency_key": invocation.idempotency_key,
                "due_at": input_value.due_at,
                "payload": _as_json(payload),
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


def _cancel_created_replacement_purchase_order(
    connection: Connection, compensation: ToolCompensation
) -> dict[str, object]:
    """Cancel only the open replacement purchase order returned by the original provider action."""
    cancelled = (
        connection.execute(
            COMPENSATE_REPLACEMENT_PO,
            {
                "replacement_purchase_order_id": _required_result_text(
                    compensation.effect_result, "replacement_purchase_order_id"
                ),
                "occurred_at": compensation.requested_at,
            },
        )
        .mappings()
        .one_or_none()
    )
    if cancelled is None:
        raise ToolExecutionError("replacement purchase order is not safely cancellable")
    return {
        "replacement_purchase_order_id": cast(str, cancelled["purchase_order_id"]),
        "status": cast(str, cancelled["status"]),
        "source_version": cast(int, cancelled["source_version"]),
    }


def _restore_original_purchase_order(
    connection: Connection, compensation: ToolCompensation
) -> dict[str, object]:
    """Restore only the original order state that still exactly matches this workflow's reduction."""
    result = compensation.effect_result
    restored = (
        connection.execute(
            RESTORE_ORIGINAL_PO,
            {
                "original_purchase_order_id": _required_result_text(
                    result, "original_purchase_order_id"
                ),
                "previous_ordered_quantity": _required_result_text(
                    result, "previous_ordered_quantity"
                ),
                "previous_status": _required_result_text(result, "previous_status"),
                "ordered_quantity": _required_result_text(result, "ordered_quantity"),
                "received_quantity": _required_result_text(result, "received_quantity"),
                "status": _required_result_text(result, "status"),
                "source_version": _required_result_int(result, "source_version"),
                "occurred_at": compensation.requested_at,
            },
        )
        .mappings()
        .one_or_none()
    )
    if restored is None:
        raise ToolExecutionError("original purchase order is not safely restorable")
    return {
        "original_purchase_order_id": cast(str, restored["purchase_order_id"]),
        "ordered_quantity": str(cast(Decimal, restored["ordered_quantity"])),
        "received_quantity": str(cast(Decimal, restored["received_quantity"])),
        "status": cast(str, restored["status"]),
        "source_version": cast(int, restored["source_version"]),
    }


def _restore_held_purchase_order(
    connection: Connection, compensation: ToolCompensation
) -> dict[str, object]:
    """Restore only the exact hold state committed by the original provider invocation."""
    result = compensation.effect_result
    if _required_result_text(result, "status") != "on_hold":
        raise ToolExecutionError("purchase-order hold is not safely restorable")
    restored = (
        connection.execute(
            RESTORE_HELD_PURCHASE_ORDER,
            {
                "purchase_order_id": _required_result_text(result, "purchase_order_id"),
                "previous_status": _required_result_text(result, "previous_status"),
                "expected_source_version": _required_result_int(result, "source_version"),
                "occurred_at": compensation.requested_at,
            },
        )
        .mappings()
        .one_or_none()
    )
    if restored is None:
        raise ToolExecutionError("purchase-order hold is not safely restorable")
    return {
        "purchase_order_id": _required_row_text(restored, "purchase_order_id"),
        "status": cast(str, restored["status"]),
        "source_version": _required_row_int(restored, "source_version"),
    }


def _send_correction_notification(
    connection: Connection, compensation: ToolCompensation
) -> dict[str, object]:
    """Send a correction to exactly the recipient of the bound original production notice."""
    original = (
        connection.execute(
            SELECT_ORIGINAL_NOTIFICATION,
            {
                "message_id": _required_result_text(compensation.effect_result, "message_id"),
                "original_idempotency_key": compensation.original_idempotency_key,
            },
        )
        .mappings()
        .one_or_none()
    )
    if original is None:
        raise ToolExecutionError("original production notification is not safely correctable")
    created = (
        connection.execute(
            INSERT_CORRECTION_NOTIFICATION,
            {
                "message_id": str(uuid4()),
                "message_key": compensation.idempotency_key,
                "sender": "enterprise-agent@example.invalid",
                "recipient": original["recipient"],
                "subject": f"Correction: {original['subject']}",
                "body": "A previous purchase-order update was reversed. Verify the current schedule.",
                "occurred_at": compensation.requested_at,
                "payload": _as_json(
                    {
                        "workflow_id": str(compensation.workflow_id),
                        "reverses_message_id": original["message_id"],
                    }
                ),
            },
        )
        .mappings()
        .one()
    )
    return {
        "message_id": cast(str, created["message_id"]),
        "recipient": cast(str, original["recipient"]),
        "reverses_message_id": cast(str, original["message_id"]),
    }


def _cancel_arrival_check(
    connection: Connection, compensation: ToolCompensation
) -> dict[str, object]:
    """Cancel only the pending arrival task that the original scheduler effect created."""
    cancelled = (
        connection.execute(
            CANCEL_ARRIVAL_CHECK,
            {
                "scheduled_task_id": _required_result_text(
                    compensation.effect_result, "scheduled_task_id"
                ),
                "workflow_id": str(compensation.workflow_id),
                "original_idempotency_key": compensation.original_idempotency_key,
                "occurred_at": compensation.requested_at,
            },
        )
        .mappings()
        .one_or_none()
    )
    if cancelled is None:
        raise ToolExecutionError("arrival check is not safely cancellable")
    return {
        "scheduled_task_id": cast(str, cancelled["task_id"]),
        "status": cast(str, cancelled["status"]),
    }


def _restore_prior_allocation(
    connection: Connection, compensation: ToolCompensation
) -> dict[str, object]:
    """Restore only a lot allocation that still exactly matches the journaled provider result."""
    result = compensation.effect_result
    quantity = _required_result_decimal(result, "quantity")
    destination_previous_quantity = _required_result_decimal(
        result, "destination_previous_quantity"
    )
    destination_source_version = _required_result_int(result, "destination_source_version")
    destination_allocation_id = _required_result_text(result, "destination_allocation_id")
    restored_destination: ProviderRow | None
    if destination_previous_quantity == Decimal(0):
        restored_destination = (
            connection.execute(
                DELETE_CREATED_ALLOCATION,
                {
                    "allocation_id": destination_allocation_id,
                    "allocated_quantity": str(quantity),
                    "source_version": destination_source_version,
                },
            )
            .mappings()
            .one_or_none()
        )
    else:
        restored_destination = _update_allocation_quantity(
            connection,
            allocation_id=destination_allocation_id,
            allocated_quantity=destination_previous_quantity,
            source_version=destination_source_version,
            occurred_at=compensation.requested_at,
        )
    if restored_destination is None:
        raise ToolExecutionError("destination allocation is not safely restorable")

    source_allocation_id = _optional_result_text(result, "source_allocation_id")
    source_previous_quantity = _optional_result_decimal(result, "source_previous_quantity")
    source_source_version = _optional_result_int(result, "source_source_version")
    if source_allocation_id is not None:
        if source_previous_quantity is None or source_source_version is None:
            raise ToolExecutionError("original tool result lacks required compensation provenance")
        restored_source = _update_allocation_quantity(
            connection,
            allocation_id=source_allocation_id,
            allocated_quantity=source_previous_quantity,
            source_version=source_source_version,
            occurred_at=compensation.requested_at,
        )
        if restored_source is None:
            raise ToolExecutionError("source allocation is not safely restorable")
    elif source_previous_quantity is not None or source_source_version is not None:
        raise ToolExecutionError("original tool result lacks required compensation provenance")

    previous_lot_allocation = _required_result_decimal(result, "previous_lot_allocated_quantity")
    expected_lot_allocation = (
        previous_lot_allocation
        if source_allocation_id is not None
        else previous_lot_allocation + quantity
    )
    restored_lot = (
        connection.execute(
            RESTORE_LOT_ALLOCATION,
            {
                "quality_lot_id": _required_result_text(result, "quality_lot_id"),
                "allocated_quantity": str(previous_lot_allocation),
                "production_order_id": _optional_result_text(
                    result, "previous_lot_production_order_id"
                ),
                "expected_allocated_quantity": str(expected_lot_allocation),
                "source_version": _required_result_int(result, "lot_source_version"),
                "occurred_at": compensation.requested_at,
            },
        )
        .mappings()
        .one_or_none()
    )
    if restored_lot is None:
        raise ToolExecutionError("quality lot allocation is not safely restorable")
    return {
        "quality_lot_id": _required_row_text(restored_lot, "quality_lot_id"),
        "allocated_quantity": _decimal_text(_decimal_value(restored_lot["allocated_quantity"])),
        "source_version": _required_row_int(restored_lot, "source_version"),
    }


def _update_allocation_quantity(
    connection: Connection,
    *,
    allocation_id: str,
    allocated_quantity: Decimal,
    source_version: int,
    occurred_at: object,
) -> ProviderRow | None:
    """CAS one normalized allocation so stale effects cannot overwrite another allocation change."""
    return cast(
        ProviderRow | None,
        connection.execute(
            UPDATE_ALLOCATION_QUANTITY,
            {
                "allocation_id": allocation_id,
                "allocated_quantity": str(allocated_quantity),
                "source_version": source_version,
                "occurred_at": occurred_at,
            },
        )
        .mappings()
        .one_or_none(),
    )


def _decimal_value(value: object) -> Decimal:
    """Convert one database numeric field without accepting malformed compensation provenance."""
    if isinstance(value, bool):
        raise ToolExecutionError("tool provider returned invalid numeric state")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ToolExecutionError("tool provider returned invalid numeric state") from error


def _decimal_text(value: Decimal) -> str:
    """Emit stable quantity text without database scale noise in journaled provider results."""
    return format(value.normalize(), "f")


def _required_row_text(row: ProviderRow, name: str) -> str:
    """Read one nonblank text field from the locked provider row."""
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError("tool provider returned incomplete state")
    return value


def _optional_row_text(row: ProviderRow, name: str) -> str | None:
    """Read a nullable text field from one provider row without inventing a target."""
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError("tool provider returned incomplete state")
    return value


def _required_row_int(row: ProviderRow, name: str) -> int:
    """Read one positive integer source version from a locked provider row."""
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolExecutionError("tool provider returned incomplete state")
    return value


def _required_result_text(result: Mapping[str, object], name: str) -> str:
    """Read one nonblank provider result field without inventing a compensation target."""
    value = result.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError("original tool result lacks required compensation provenance")
    return value


def _required_result_int(result: Mapping[str, object], name: str) -> int:
    """Read one exact integer version field that protects an optimistic restoration update."""
    value = result.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolExecutionError("original tool result lacks required compensation provenance")
    return value


def _optional_result_text(result: Mapping[str, object], name: str) -> str | None:
    """Read an explicitly nullable text provenance field without broadening a reverse target."""
    value = result.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError("original tool result lacks required compensation provenance")
    return value


def _required_result_decimal(result: Mapping[str, object], name: str) -> Decimal:
    """Read one numeric result field that will be used in a compare-and-set restoration."""
    value = result.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError("original tool result lacks required compensation provenance")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ToolExecutionError(
            "original tool result lacks required compensation provenance"
        ) from error


def _optional_result_decimal(result: Mapping[str, object], name: str) -> Decimal | None:
    """Read an explicitly nullable numeric result field for an optional source allocation."""
    value = result.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError("original tool result lacks required compensation provenance")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ToolExecutionError(
            "original tool result lacks required compensation provenance"
        ) from error


def _optional_result_int(result: Mapping[str, object], name: str) -> int | None:
    """Read an explicitly nullable source-allocation version from the original result."""
    value = result.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolExecutionError("original tool result lacks required compensation provenance")
    return value


def _as_json(values: Mapping[str, object]) -> str:
    """Serialize only JSON-compatible typed tool payloads through bound SQL parameters."""
    return json.dumps(dict(values), sort_keys=True)
