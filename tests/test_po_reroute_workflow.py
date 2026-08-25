"""Contracts for the one fixed, versioned Scenario A workflow declaration."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from enterprise_agent.application.planning import EnterWorkflowRecommendation


@pytest.mark.critical
def test_po_reroute_v1_declares_exactly_the_reviewed_six_steps_in_order() -> None:
    """Scenario A has one executable sequence, not a model-authored action list."""
    from enterprise_agent.application.workflows import (
        PO_REROUTE_V1,
        WorkflowStepName,
        declared_workflow,
    )

    from enterprise_agent.application.tools import ToolName

    definition = declared_workflow("po_reroute", 1)

    assert definition is PO_REROUTE_V1
    assert [(step.index, step.name, step.tool_name) for step in definition.steps] == [
        (1, WorkflowStepName.CONFIRM_ALTERNATE_SUPPLIER_APPROVED, None),
        (2, WorkflowStepName.CONFIRM_ALTERNATE_LEAD_TIME, None),
        (3, WorkflowStepName.CREATE_REPLACEMENT_PO, ToolName.CREATE_REPLACEMENT_PO),
        (4, WorkflowStepName.REDUCE_OR_CANCEL_ORIGINAL_PO, ToolName.REDUCE_OR_CANCEL_PO),
        (5, WorkflowStepName.NOTIFY_PRODUCTION, ToolName.NOTIFY_PRODUCTION),
        (6, WorkflowStepName.SCHEDULE_ARRIVAL_CHECK, ToolName.SCHEDULE_ARRIVAL_CHECK),
    ]


def test_validated_recommendations_always_resolve_to_the_same_static_definition() -> None:
    """Supplier and quantity may change within policy, but no recommendation controls steps."""
    from enterprise_agent.application.workflows import PO_REROUTE_V1, workflow_for_recommendation

    recommendation = EnterWorkflowRecommendation(
        outcome="ENTER_WORKFLOW",
        workflow_name="po_reroute",
        workflow_version=1,
        supplier_id="supplier-z",
        quantity=Decimal(60),
        original_purchase_order_id="po-4812-y",
        production_order_id="production-4812",
        rationale="The approved alternate can meet the production date.",
    )
    changed_parameters = recommendation.model_copy(
        update={"supplier_id": "supplier-q", "quantity": Decimal(59)}
    )

    assert workflow_for_recommendation(recommendation) is PO_REROUTE_V1
    assert workflow_for_recommendation(changed_parameters) is PO_REROUTE_V1


def test_only_the_declared_workflow_name_and_version_can_be_resolved() -> None:
    """No unknown name or unreviewed version becomes executable by lookup."""
    from enterprise_agent.application.workflows import WorkflowNotDeclaredError, declared_workflow

    with pytest.raises(WorkflowNotDeclaredError, match="not declared"):
        declared_workflow("po_reroute", 2)
    with pytest.raises(WorkflowNotDeclaredError, match="not declared"):
        declared_workflow("model_authored_workflow", 1)


def test_workflow_definition_is_immutable() -> None:
    """Callers cannot mutate a reviewed definition after it has been loaded."""
    from enterprise_agent.application.workflows import PO_REROUTE_V1

    with pytest.raises(FrozenInstanceError):
        PO_REROUTE_V1.steps = ()  # type: ignore[misc]
