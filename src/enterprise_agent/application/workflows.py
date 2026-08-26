"""Immutable, versioned workflow declarations independent of execution and persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import cast

from enterprise_agent.application.planning import EnterWorkflowRecommendation
from enterprise_agent.application.tools import ToolName


class WorkflowStepName(StrEnum):
    """The reviewed operations that can appear in a declared workflow definition."""

    CONFIRM_ALTERNATE_SUPPLIER_APPROVED = "confirm_alternate_supplier_approved"
    CONFIRM_ALTERNATE_LEAD_TIME = "confirm_alternate_lead_time"
    CREATE_REPLACEMENT_PO = "create_replacement_po"
    REDUCE_OR_CANCEL_ORIGINAL_PO = "reduce_or_cancel_original_po"
    PLACE_PURCHASE_ORDER_HOLD = "place_purchase_order_hold"
    NOTIFY_PRODUCTION = "notify_production"
    SCHEDULE_ARRIVAL_CHECK = "schedule_arrival_check"
    REALLOCATE_LOT = "reallocate_lot"
    FLAG_SHORTAGE_TO_PURCHASING = "flag_shortage_to_purchasing"


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowStepDefinition:
    """One ordered guard or catalog-backed effect within a reviewed workflow."""

    index: int
    name: WorkflowStepName
    tool_name: ToolName | None


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowDefinition:
    """An immutable, versioned sequence that an executor may later run in order."""

    name: str
    version: int
    steps: tuple[WorkflowStepDefinition, ...]


class WorkflowNotDeclaredError(ValueError):
    """Raised when a caller requests a workflow outside the reviewed registry."""


PO_REROUTE_V1 = WorkflowDefinition(
    name="po_reroute",
    version=1,
    steps=(
        WorkflowStepDefinition(
            index=1,
            name=WorkflowStepName.CONFIRM_ALTERNATE_SUPPLIER_APPROVED,
            tool_name=None,
        ),
        WorkflowStepDefinition(
            index=2,
            name=WorkflowStepName.CONFIRM_ALTERNATE_LEAD_TIME,
            tool_name=None,
        ),
        WorkflowStepDefinition(
            index=3,
            name=WorkflowStepName.CREATE_REPLACEMENT_PO,
            tool_name=ToolName.CREATE_REPLACEMENT_PO,
        ),
        WorkflowStepDefinition(
            index=4,
            name=WorkflowStepName.REDUCE_OR_CANCEL_ORIGINAL_PO,
            tool_name=ToolName.REDUCE_OR_CANCEL_PO,
        ),
        WorkflowStepDefinition(
            index=5,
            name=WorkflowStepName.NOTIFY_PRODUCTION,
            tool_name=ToolName.NOTIFY_PRODUCTION,
        ),
        WorkflowStepDefinition(
            index=6,
            name=WorkflowStepName.SCHEDULE_ARRIVAL_CHECK,
            tool_name=ToolName.SCHEDULE_ARRIVAL_CHECK,
        ),
    ),
)

_DECLARED_WORKFLOWS: dict[tuple[str, int], WorkflowDefinition] = {
    (PO_REROUTE_V1.name, PO_REROUTE_V1.version): PO_REROUTE_V1,
}
DECLARED_WORKFLOWS: Mapping[tuple[str, int], WorkflowDefinition] = cast(
    Mapping[tuple[str, int], WorkflowDefinition], MappingProxyType(_DECLARED_WORKFLOWS)
)


def declared_workflow(name: str, version: int) -> WorkflowDefinition:
    """Resolve a reviewed workflow by its exact immutable name and version."""
    try:
        return DECLARED_WORKFLOWS[(name, version)]
    except KeyError as error:
        raise WorkflowNotDeclaredError(f"Workflow is not declared: {name}:v{version}") from error


def workflow_for_recommendation(
    recommendation: EnterWorkflowRecommendation,
) -> WorkflowDefinition:
    """Resolve the static declaration named by an already validated Scenario A recommendation."""
    return declared_workflow(recommendation.workflow_name, recommendation.workflow_version)
