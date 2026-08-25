"""Closed, typed catalog for every effectful tool a declared workflow may use."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from json import dumps
from types import MappingProxyType
from typing import Annotated, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from enterprise_agent.domain import ActorContext, Scope, WorkflowId

NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveQuantity = Annotated[Decimal, Field(gt=0)]


class ToolName(StrEnum):
    """The exhaustive allowlist of reviewed effectful tools."""

    CREATE_REPLACEMENT_PO = "create_replacement_po"
    REDUCE_OR_CANCEL_PO = "reduce_or_cancel_po"
    NOTIFY_PRODUCTION = "notify_production"
    SCHEDULE_ARRIVAL_CHECK = "schedule_arrival_check"
    REALLOCATE_LOT = "reallocate_lot"
    FLAG_SHORTAGE_TO_PURCHASING = "flag_shortage_to_purchasing"


class CompensationAction(StrEnum):
    """Declarative rollback action assigned to each effect before it can execute."""

    CANCEL_CREATED_REPLACEMENT_PO = "cancel_created_replacement_po"
    RESTORE_ORIGINAL_PURCHASE_ORDER = "restore_original_purchase_order"
    SEND_CORRECTION_NOTIFICATION = "send_correction_notification"
    CANCEL_ARRIVAL_CHECK = "cancel_arrival_check"
    RESTORE_PRIOR_ALLOCATION = "restore_prior_allocation"


class ToolInputModel(BaseModel):
    """Base schema that rejects additional unreviewed tool arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CreateReplacementPOInput(ToolInputModel):
    """Create the approved alternate purchase order for a bounded reroute."""

    original_purchase_order_id: NonBlankText
    supplier_id: NonBlankText
    production_order_id: NonBlankText
    quantity: PositiveQuantity


class ReduceOrCancelPOInput(ToolInputModel):
    """Reduce or close only the source purchase order by an actionable quantity."""

    original_purchase_order_id: NonBlankText
    quantity: PositiveQuantity


class NotifyProductionInput(ToolInputModel):
    """Notify production of a bounded workflow impact."""

    production_order_id: NonBlankText
    message: NonBlankText


class ScheduleArrivalCheckInput(ToolInputModel):
    """Create the explicit, timezone-aware arrival-check task."""

    purchase_order_id: NonBlankText
    due_at: datetime

    @field_validator("due_at")
    @classmethod
    def _require_timezone(cls, due_at: datetime) -> datetime:
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            raise ValueError("due_at must include a timezone")
        return due_at


class ReallocateLotInput(ToolInputModel):
    """Move a good quality lot between distinct production-order allocations."""

    quality_lot_id: NonBlankText
    from_production_order_id: NonBlankText
    to_production_order_id: NonBlankText
    quantity: PositiveQuantity

    @model_validator(mode="after")
    def _require_distinct_orders(self) -> Self:
        if self.from_production_order_id == self.to_production_order_id:
            raise ValueError("source and destination production orders must differ")
        return self


class FlagShortageToPurchasingInput(ToolInputModel):
    """Escalate a specific production shortfall to purchasing."""

    production_order_id: NonBlankText
    part_id: NonBlankText
    shortage_quantity: PositiveQuantity


ToolInput = (
    CreateReplacementPOInput
    | ReduceOrCancelPOInput
    | NotifyProductionInput
    | ScheduleArrivalCheckInput
    | ReallocateLotInput
    | FlagShortageToPurchasingInput
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolDefinition:
    """Reviewed capability metadata, deliberately separate from a tool implementation."""

    name: ToolName
    input_model: type[ToolInputModel]
    required_scopes: frozenset[Scope]
    compensation: CompensationAction


class ToolNotDeclaredError(ValueError):
    """Raised when a caller names a capability outside the closed catalog."""


class ToolAuthorizationError(PermissionError):
    """Raised when the initiating actor lacks a tool's declared write authority."""


_TOOL_CATALOG: dict[ToolName, ToolDefinition] = {
    ToolName.CREATE_REPLACEMENT_PO: ToolDefinition(
        name=ToolName.CREATE_REPLACEMENT_PO,
        input_model=CreateReplacementPOInput,
        required_scopes=frozenset({Scope("erp:po:create")}),
        compensation=CompensationAction.CANCEL_CREATED_REPLACEMENT_PO,
    ),
    ToolName.REDUCE_OR_CANCEL_PO: ToolDefinition(
        name=ToolName.REDUCE_OR_CANCEL_PO,
        input_model=ReduceOrCancelPOInput,
        required_scopes=frozenset({Scope("erp:po:cancel")}),
        compensation=CompensationAction.RESTORE_ORIGINAL_PURCHASE_ORDER,
    ),
    ToolName.NOTIFY_PRODUCTION: ToolDefinition(
        name=ToolName.NOTIFY_PRODUCTION,
        input_model=NotifyProductionInput,
        required_scopes=frozenset({Scope("production:notify")}),
        compensation=CompensationAction.SEND_CORRECTION_NOTIFICATION,
    ),
    ToolName.SCHEDULE_ARRIVAL_CHECK: ToolDefinition(
        name=ToolName.SCHEDULE_ARRIVAL_CHECK,
        input_model=ScheduleArrivalCheckInput,
        required_scopes=frozenset({Scope("scheduler:write")}),
        compensation=CompensationAction.CANCEL_ARRIVAL_CHECK,
    ),
    ToolName.REALLOCATE_LOT: ToolDefinition(
        name=ToolName.REALLOCATE_LOT,
        input_model=ReallocateLotInput,
        required_scopes=frozenset({Scope("erp:lot:write")}),
        compensation=CompensationAction.RESTORE_PRIOR_ALLOCATION,
    ),
    ToolName.FLAG_SHORTAGE_TO_PURCHASING: ToolDefinition(
        name=ToolName.FLAG_SHORTAGE_TO_PURCHASING,
        input_model=FlagShortageToPurchasingInput,
        required_scopes=frozenset({Scope("production:notify")}),
        compensation=CompensationAction.SEND_CORRECTION_NOTIFICATION,
    ),
}

TOOL_CATALOG: Mapping[ToolName, ToolDefinition] = cast(
    Mapping[ToolName, ToolDefinition], MappingProxyType(_TOOL_CATALOG)
)


def tool_definition(name: ToolName | str) -> ToolDefinition:
    """Return the declared metadata for an allowed tool, rejecting all other names."""
    try:
        return TOOL_CATALOG[ToolName(name)]
    except ValueError as error:
        raise ToolNotDeclaredError(f"Tool is not declared: {name}") from error


def authorize_tool(actor: ActorContext, name: ToolName | str) -> ToolDefinition:
    """Verify the initiating actor has every write scope that the tool declares."""
    definition = tool_definition(name)
    if not definition.required_scopes.issubset(actor.scopes):
        raise ToolAuthorizationError(f"Actor lacks required scope for {definition.name.value}")
    return definition


def build_tool_idempotency_key(
    workflow_id: WorkflowId,
    step_index: int,
    name: ToolName | str,
    input_value: ToolInput,
) -> str:
    """Derive one opaque, retry-stable key from the declared workflow step and input."""
    if step_index < 1:
        raise ValueError("step index must be positive")

    definition = tool_definition(name)
    if type(input_value) is not definition.input_model:
        raise TypeError(f"input schema does not match {definition.name.value}")

    canonical_input = dumps(
        input_value.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = sha256(canonical_input.encode("utf-8")).hexdigest()[:24]
    return f"tool:v1:{workflow_id}:{step_index}:{definition.name.value}:{digest}"
