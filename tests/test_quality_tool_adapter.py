"""Contracts for the scoped, fresh Scenario B tool-provider effects."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from enterprise_agent.application.tools import (
    CompensationAction,
    FlagShortageToPurchasingInput,
    ReallocateLotInput,
    ToolAuthorizationError,
    ToolName,
)
from enterprise_agent.domain import (
    ActorContext,
    PlantId,
    Scope,
    ToolCompensation,
    ToolInvocation,
    ToolInvocationId,
    ToolInvocationStatus,
    UserId,
    WorkflowId,
)

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
WORKFLOW_ID = WorkflowId("00000000-0000-0000-0000-000000000901")
QUINN = ActorContext(
    user_id=UserId("00000000-0000-0000-0000-000000000003"),
    role="quality_manager",
    scopes=frozenset({Scope("erp:lot:write"), Scope("production:notify")}),
    plant_ids=frozenset({PlantId("PLANT-CHI")}),
    backup_approver_id=None,
    approval_limits={},
)


def invocation(name: ToolName, parameters: dict[str, object]) -> ToolInvocation:
    """Build one started quality action after the future approval boundary has released it."""
    return ToolInvocation(
        invocation_id=ToolInvocationId("00000000-0000-0000-0000-000000000911"),
        workflow_id=WORKFLOW_ID,
        tool_name=name.value,
        idempotency_key=f"tool:v1:{name.value}:quality",
        status=ToolInvocationStatus.STARTED,
        parameters=parameters,
        result=None,
        attempt_count=1,
        started_at=NOW,
        completed_at=None,
    )


def compensation(effect_result: dict[str, object]) -> ToolCompensation:
    """Build the catalog-declared reverse request for a reallocation effect."""
    return ToolCompensation(
        workflow_id=WORKFLOW_ID,
        tool_name=ToolName.REALLOCATE_LOT.value,
        action=CompensationAction.RESTORE_PRIOR_ALLOCATION.value,
        original_idempotency_key="tool:v1:reallocate_lot:quality",
        idempotency_key="compensation:v1:restore_prior_allocation:quality",
        effect_result=effect_result,
        requested_at=NOW,
    )


def mapping_result(
    *,
    one: dict[str, object] | None = None,
    one_or_none: dict[str, object] | None = None,
    rows: list[dict[str, object]] | None = None,
) -> MagicMock:
    """Return a minimal SQLAlchemy result double for one provider statement."""
    result = MagicMock()
    result.mappings.return_value.one.return_value = one
    result.mappings.return_value.one_or_none.return_value = one_or_none
    result.mappings.return_value.all.return_value = [] if rows is None else rows
    return result


def test_public_tool_adapter_validates_the_two_scenario_b_tool_scopes() -> None:
    """Scenario B uses the catalog journal without inheriting purchasing's authority."""
    from enterprise_agent.adapters import PostgresToolAdapter
    from enterprise_agent.adapters.tools import _validated_input

    reallocation = invocation(
        ToolName.REALLOCATE_LOT,
        {
            "quality_lot_id": "00000000-0000-0000-0000-000000000602",
            "from_production_order_id": None,
            "to_production_order_id": "00000000-0000-0000-0000-000000000302",
            "quantity": "80",
        },
    )
    shortage = invocation(
        ToolName.FLAG_SHORTAGE_TO_PURCHASING,
        {
            "production_order_id": "00000000-0000-0000-0000-000000000303",
            "part_id": "00000000-0000-0000-0000-000000000102",
            "shortage_quantity": "80",
        },
    )

    assert PostgresToolAdapter.__name__ == "PostgresToolAdapter"
    assert isinstance(_validated_input(QUINN, reallocation), ReallocateLotInput)
    assert isinstance(_validated_input(QUINN, shortage), FlagShortageToPurchasingInput)
    with pytest.raises(ToolAuthorizationError, match="required scope"):
        _validated_input(
            ActorContext(
                user_id=QUINN.user_id,
                role=QUINN.role,
                scopes=frozenset({Scope("quality:lot:read")}),
                plant_ids=QUINN.plant_ids,
                backup_approver_id=None,
                approval_limits={},
            ),
            reallocation,
        )


def test_reallocate_lot_rechecks_current_lot_and_destination_before_it_writes() -> None:
    """A released same-part lot is allocated only while quantity, status, and production remain current."""
    from enterprise_agent.adapters import tools

    request = invocation(
        ToolName.REALLOCATE_LOT,
        {
            "quality_lot_id": "lot-good",
            "from_production_order_id": None,
            "to_production_order_id": "production-q7001",
            "quantity": "80",
        },
    )
    connection = MagicMock()
    connection.execute.side_effect = [
        mapping_result(
            one_or_none={
                "quality_lot_id": "lot-good",
                "part_id": "part-quality",
                "plant_id": "PLANT-CHI",
                "quantity": Decimal("120"),
                "status": "released",
                "production_order_id": None,
                "allocated_quantity": Decimal("0"),
                "source_version": 1,
            }
        ),
        mapping_result(
            one_or_none={
                "production_order_id": "production-q7001",
                "part_id": "part-quality",
                "plant_id": "PLANT-CHI",
                "status": "scheduled",
            }
        ),
        mapping_result(one_or_none=None),
        mapping_result(
            one={
                "allocation_id": "allocation-good-q7001",
                "allocated_quantity": Decimal("80"),
                "source_version": 1,
            }
        ),
        mapping_result(one_or_none={"source_version": 2}),
    ]

    result = tools._execute_effect(
        connection,
        request,
        ReallocateLotInput.model_validate(dict(request.parameters)),
    )

    assert result == {
        "quality_lot_id": "lot-good",
        "to_production_order_id": "production-q7001",
        "quantity": "80",
        "previous_lot_allocated_quantity": "0",
        "previous_lot_production_order_id": None,
        "lot_source_version": 2,
        "destination_allocation_id": "allocation-good-q7001",
        "destination_previous_quantity": "0",
        "destination_source_version": 1,
        "source_allocation_id": None,
        "source_previous_quantity": None,
        "source_source_version": None,
    }
    assert connection.execute.call_args_list[3].args[1]["allocated_quantity"] == "80"
    assert connection.execute.call_args_list[4].args[1]["allocated_quantity"] == "80"


def test_reallocate_lot_fails_closed_when_current_capacity_or_destination_is_stale() -> None:
    """A plan cannot spend a held, undersized, wrong-part, or non-runnable current resource."""
    from enterprise_agent.adapters import tools
    from enterprise_agent.adapters.tools import ToolExecutionError

    request = invocation(
        ToolName.REALLOCATE_LOT,
        {
            "quality_lot_id": "lot-good",
            "to_production_order_id": "production-q7001",
            "quantity": "80",
        },
    )
    connection = MagicMock()
    connection.execute.return_value = mapping_result(
        one_or_none={
            "quality_lot_id": "lot-good",
            "part_id": "part-quality",
            "plant_id": "PLANT-CHI",
            "quantity": Decimal("40"),
            "status": "released",
            "production_order_id": None,
            "allocated_quantity": Decimal("0"),
            "source_version": 1,
        }
    )

    with pytest.raises(ToolExecutionError, match="not currently reallocatable"):
        tools._execute_effect(
            connection,
            request,
            ReallocateLotInput.model_validate(dict(request.parameters)),
        )

    assert connection.execute.call_count == 1


def test_shortage_tool_rechecks_the_exact_current_shortfall_and_notifies_purchasing() -> None:
    """The escalation is sent only when the production/part shortfall remains exactly actionable."""
    from enterprise_agent.adapters import tools

    request = invocation(
        ToolName.FLAG_SHORTAGE_TO_PURCHASING,
        {
            "production_order_id": "production-q7002",
            "part_id": "part-quality",
            "shortage_quantity": "80",
        },
    )
    recipient = MagicMock()
    recipient.scalar_one_or_none.return_value = "dana.buyer@example.com"
    connection = MagicMock()
    connection.execute.side_effect = [
        mapping_result(
            one_or_none={
                "production_order_id": "production-q7002",
                "part_id": "part-quality",
                "plant_id": "PLANT-CHI",
                "required_quantity": Decimal("200"),
                "status": "scheduled",
            }
        ),
        mapping_result(
            rows=[
                {"quantity": Decimal("120"), "allocated_quantity": Decimal("0")},
            ]
        ),
        recipient,
        mapping_result(one={"message_id": "shortage-message-1"}),
    ]

    result = tools._execute_effect(
        connection,
        request,
        FlagShortageToPurchasingInput.model_validate(dict(request.parameters)),
    )

    assert result == {
        "message_id": "shortage-message-1",
        "recipient": "dana.buyer@example.com",
        "production_order_id": "production-q7002",
        "part_id": "part-quality",
        "shortage_quantity": "80",
    }
    assert connection.execute.call_args_list[3].args[1]["recipient"] == "dana.buyer@example.com"


def test_reallocation_compensation_uses_the_effect_versions_and_prior_state() -> None:
    """A reverse action changes only the allocation result still owned by its original effect."""
    from enterprise_agent.adapters import tools

    effect_result = {
        "quality_lot_id": "lot-good",
        "to_production_order_id": "production-q7001",
        "quantity": "80",
        "previous_lot_allocated_quantity": "0",
        "previous_lot_production_order_id": None,
        "lot_source_version": 2,
        "destination_allocation_id": "allocation-good-q7001",
        "destination_previous_quantity": "0",
        "destination_source_version": 1,
        "source_allocation_id": None,
        "source_previous_quantity": None,
        "source_source_version": None,
    }
    connection = MagicMock()
    connection.execute.side_effect = [
        mapping_result(one_or_none={"allocation_id": "allocation-good-q7001"}),
        mapping_result(
            one_or_none={
                "quality_lot_id": "lot-good",
                "allocated_quantity": Decimal("0"),
                "source_version": 3,
            }
        ),
    ]

    result = tools._execute_compensation_effect(connection, compensation(effect_result))

    assert result == {
        "quality_lot_id": "lot-good",
        "allocated_quantity": "0",
        "source_version": 3,
    }
    assert connection.execute.call_args_list[0].args[1]["source_version"] == 1
    assert connection.execute.call_args_list[1].args[1]["source_version"] == 2
