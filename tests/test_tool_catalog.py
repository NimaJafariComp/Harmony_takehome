"""Contracts for the closed, authorized tool catalog used by declared workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from enterprise_agent.domain import ActorContext, PlantId, Scope, UserId, WorkflowId

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)


def actor_with(*scopes: Scope) -> ActorContext:
    """Build one principal whose write authority can vary by test."""
    return ActorContext(
        user_id=UserId("00000000-0000-0000-0000-000000000001"),
        role="purchasing_manager",
        scopes=frozenset(scopes),
        plant_ids=frozenset({PlantId("PLANT-CHI")}),
        backup_approver_id=None,
        approval_limits={},
    )


@pytest.mark.critical
def test_catalog_declares_exactly_the_six_approved_tools() -> None:
    """Workflow code can select only the reviewed tools and their declared authority."""
    from enterprise_agent.application.tools import (
        TOOL_CATALOG,
        CompensationAction,
        ToolName,
    )

    assert set(TOOL_CATALOG) == set(ToolName)
    assert {name: definition.required_scopes for name, definition in TOOL_CATALOG.items()} == {
        ToolName.CREATE_REPLACEMENT_PO: frozenset({Scope("erp:po:create")}),
        ToolName.REDUCE_OR_CANCEL_PO: frozenset({Scope("erp:po:cancel")}),
        ToolName.NOTIFY_PRODUCTION: frozenset({Scope("production:notify")}),
        ToolName.SCHEDULE_ARRIVAL_CHECK: frozenset({Scope("scheduler:write")}),
        ToolName.REALLOCATE_LOT: frozenset({Scope("erp:lot:write")}),
        ToolName.FLAG_SHORTAGE_TO_PURCHASING: frozenset({Scope("production:notify")}),
    }
    assert {name: definition.compensation for name, definition in TOOL_CATALOG.items()} == {
        ToolName.CREATE_REPLACEMENT_PO: CompensationAction.CANCEL_CREATED_REPLACEMENT_PO,
        ToolName.REDUCE_OR_CANCEL_PO: CompensationAction.RESTORE_ORIGINAL_PURCHASE_ORDER,
        ToolName.NOTIFY_PRODUCTION: CompensationAction.SEND_CORRECTION_NOTIFICATION,
        ToolName.SCHEDULE_ARRIVAL_CHECK: CompensationAction.CANCEL_ARRIVAL_CHECK,
        ToolName.REALLOCATE_LOT: CompensationAction.RESTORE_PRIOR_ALLOCATION,
        ToolName.FLAG_SHORTAGE_TO_PURCHASING: CompensationAction.SEND_CORRECTION_NOTIFICATION,
    }


def test_tool_inputs_are_closed_and_reject_non_actionable_values() -> None:
    """Untrusted workflow arguments cannot add fields or create zero-quantity effects."""
    from enterprise_agent.application.tools import (
        CreateReplacementPOInput,
        ReallocateLotInput,
        ScheduleArrivalCheckInput,
    )

    replacement = CreateReplacementPOInput(
        original_purchase_order_id="po-4812-y",
        supplier_id="supplier-z",
        production_order_id="production-4812",
        quantity=Decimal(60),
    )
    scheduled_check = ScheduleArrivalCheckInput(purchase_order_id="po-4812-y", due_at=NOW)
    allocation = ReallocateLotInput(
        quality_lot_id="lot-1",
        from_production_order_id="production-4812",
        to_production_order_id="production-4813",
        quantity=Decimal(1),
    )

    assert replacement.quantity == Decimal(60)
    assert scheduled_check.due_at == NOW
    assert allocation.to_production_order_id == "production-4813"
    with pytest.raises(ValidationError):
        CreateReplacementPOInput.model_validate(
            {
                "original_purchase_order_id": "po-4812-y",
                "supplier_id": "supplier-z",
                "production_order_id": "production-4812",
                "quantity": Decimal(0),
                "unapproved_argument": "do this too",
            }
        )
    with pytest.raises(ValidationError):
        ReallocateLotInput(
            quality_lot_id="lot-1",
            from_production_order_id="production-4812",
            to_production_order_id="production-4812",
            quantity=Decimal(1),
        )
    with pytest.raises(ValidationError):
        ScheduleArrivalCheckInput(purchase_order_id="po-4812-y", due_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "name",
    [
        "create_replacement_po",
        "reduce_or_cancel_po",
        "notify_production",
        "schedule_arrival_check",
        "reallocate_lot",
        "flag_shortage_to_purchasing",
    ],
)
def test_tool_authorization_requires_every_declared_scope(name: str) -> None:
    """Tool-level authorization stands even when a caller claims workflow approval."""
    from enterprise_agent.application.tools import (
        TOOL_CATALOG,
        ToolAuthorizationError,
        ToolName,
        authorize_tool,
    )

    tool_name = ToolName(name)
    required_scope = next(iter(TOOL_CATALOG[tool_name].required_scopes))

    assert authorize_tool(actor_with(required_scope), tool_name).name is tool_name
    with pytest.raises(ToolAuthorizationError):
        authorize_tool(actor_with(), tool_name)


@pytest.mark.critical
def test_tool_idempotency_key_is_stable_for_a_workflow_step_and_bound_to_its_input() -> None:
    """A retry reaches the same effect; changed intent or a different step cannot collide."""
    from enterprise_agent.application.tools import (
        CreateReplacementPOInput,
        ToolName,
        build_tool_idempotency_key,
    )

    workflow_id = WorkflowId("00000000-0000-0000-0000-000000000901")
    input_value = CreateReplacementPOInput(
        original_purchase_order_id="po-4812-y",
        supplier_id="supplier-z",
        production_order_id="production-4812",
        quantity=Decimal(60),
    )
    changed_input = input_value.model_copy(update={"quantity": Decimal(59)})

    first = build_tool_idempotency_key(workflow_id, 3, ToolName.CREATE_REPLACEMENT_PO, input_value)
    second = build_tool_idempotency_key(workflow_id, 3, ToolName.CREATE_REPLACEMENT_PO, input_value)

    assert first == second
    assert first != build_tool_idempotency_key(
        workflow_id, 3, ToolName.CREATE_REPLACEMENT_PO, changed_input
    )
    assert first != build_tool_idempotency_key(
        workflow_id, 4, ToolName.CREATE_REPLACEMENT_PO, input_value
    )
    assert first != build_tool_idempotency_key(
        WorkflowId("00000000-0000-0000-0000-000000000902"),
        3,
        ToolName.CREATE_REPLACEMENT_PO,
        input_value,
    )
    assert "po-4812-y" not in first
    assert len(first) <= 255


def test_tool_idempotency_rejects_an_undeclared_input_for_the_tool() -> None:
    """A workflow cannot pair an allowed tool name with a schema from another tool."""
    from enterprise_agent.application.tools import (
        CreateReplacementPOInput,
        NotifyProductionInput,
        ToolName,
        build_tool_idempotency_key,
    )

    workflow_id = WorkflowId("00000000-0000-0000-0000-000000000901")
    with pytest.raises(TypeError, match="input schema"):
        build_tool_idempotency_key(
            workflow_id,
            3,
            ToolName.CREATE_REPLACEMENT_PO,
            NotifyProductionInput(
                production_order_id="production-4812",
                message="A replacement purchase order is pending.",
            ),
        )
    with pytest.raises(ValueError, match="step index"):
        build_tool_idempotency_key(
            workflow_id,
            0,
            ToolName.CREATE_REPLACEMENT_PO,
            CreateReplacementPOInput(
                original_purchase_order_id="po-4812-y",
                supplier_id="supplier-z",
                production_order_id="production-4812",
                quantity=Decimal(60),
            ),
        )


def test_catalog_rejects_a_tool_name_outside_the_allowlist() -> None:
    """A caller cannot turn a string into an undeclared capability at runtime."""
    from enterprise_agent.application.tools import ToolNotDeclaredError, tool_definition

    with pytest.raises(ToolNotDeclaredError, match="not declared"):
        tool_definition("arbitrary_http_call")
