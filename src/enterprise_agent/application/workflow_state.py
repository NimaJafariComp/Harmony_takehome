"""Stage fixed workflow definitions as durable state without claiming or executing effects."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from enterprise_agent.application.bounded_tool_plan import (
    BOUNDED_TOOL_PLAN_WORKFLOW_NAME,
    BOUNDED_TOOL_PLAN_WORKFLOW_VERSION,
    bounded_tool_calls_from_plan,
)
from enterprise_agent.application.workflows import WorkflowNotDeclaredError, declared_workflow
from enterprise_agent.domain import (
    Plan,
    RunId,
    WorkflowId,
    WorkflowState,
    WorkflowStateSnapshot,
    WorkflowStatus,
    WorkflowStepId,
    WorkflowStepState,
    WorkflowStepStatus,
)
from enterprise_agent.ports import WorkflowStatePort


class WorkflowStateInitializationError(ValueError):
    """Raised when immutable plan data cannot stage an exact declared workflow."""


class WorkflowStateService:
    """Materialize one pending workflow snapshot from an immutable enter-workflow plan."""

    def __init__(self, store: WorkflowStatePort) -> None:
        """Depend on durable state storage without coupling staging to PostgreSQL."""
        self._store = store

    def stage(
        self,
        plan: Plan,
        *,
        created_at: datetime,
        workflow_id: WorkflowId | None = None,
        audit_run_id: RunId | None = None,
    ) -> WorkflowStateSnapshot:
        """Persist every fixed initial step; no approval check, claim, or external tool call occurs."""
        if (
            plan.intent != "enter_workflow"
            or plan.workflow_name is None
            or plan.workflow_version is None
        ):
            raise WorkflowStateInitializationError("plan does not name a declared workflow")
        try:
            definition = declared_workflow(plan.workflow_name, plan.workflow_version)
        except WorkflowNotDeclaredError as error:
            raise WorkflowStateInitializationError(
                "plan does not name a declared workflow"
            ) from error

        resolved_workflow_id = workflow_id or WorkflowId(str(uuid4()))
        workflow = WorkflowState(
            workflow_id=resolved_workflow_id,
            plan_id=plan.plan_id,
            definition_name=definition.name,
            definition_version=definition.version,
            status=WorkflowStatus.PENDING,
            current_step=0,
            started_at=None,
            completed_at=None,
            last_error=None,
            lease_owner=None,
            lease_expires_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
        input_snapshot = {
            "plan_hash": plan.plan_hash,
            "plan_parameters": dict(plan.parameters),
            "source_versions": dict(plan.source_versions),
        }
        if audit_run_id is not None:
            input_snapshot["audit_run_id"] = str(audit_run_id)
        steps = tuple(
            WorkflowStepState(
                step_id=WorkflowStepId(str(uuid4())),
                workflow_id=resolved_workflow_id,
                step_index=definition_step.index,
                step_name=definition_step.name.value,
                tool_name=None
                if definition_step.tool_name is None
                else definition_step.tool_name.value,
                status=WorkflowStepStatus.PENDING,
                idempotency_key=None,
                input=input_snapshot,
                result=None,
                error=None,
                attempt_count=0,
                started_at=None,
                completed_at=None,
                lease_owner=None,
                lease_expires_at=None,
                created_at=created_at,
                updated_at=created_at,
            )
            for definition_step in definition.steps
        )
        snapshot = WorkflowStateSnapshot(workflow=workflow, steps=steps)
        self._store.create(snapshot)
        return snapshot

    def stage_bounded_tool_plan(
        self,
        plan: Plan,
        *,
        created_at: datetime,
        workflow_id: WorkflowId | None = None,
        audit_run_id: RunId | None = None,
    ) -> WorkflowStateSnapshot:
        """Persist a plan-bound ordered catalog-tool workflow without executing or approving it."""
        try:
            tool_calls = bounded_tool_calls_from_plan(plan)
        except ValueError as error:
            raise WorkflowStateInitializationError(
                "plan does not name a valid bounded tool workflow"
            ) from error

        resolved_workflow_id = workflow_id or WorkflowId(str(uuid4()))
        workflow = WorkflowState(
            workflow_id=resolved_workflow_id,
            plan_id=plan.plan_id,
            definition_name=BOUNDED_TOOL_PLAN_WORKFLOW_NAME,
            definition_version=BOUNDED_TOOL_PLAN_WORKFLOW_VERSION,
            status=WorkflowStatus.PENDING,
            current_step=0,
            started_at=None,
            completed_at=None,
            last_error=None,
            lease_owner=None,
            lease_expires_at=None,
            created_at=created_at,
            updated_at=created_at,
        )
        common_input: dict[str, object] = {
            "plan_hash": plan.plan_hash,
            "plan_parameters": dict(plan.parameters),
            "source_versions": dict(plan.source_versions),
        }
        if audit_run_id is not None:
            common_input["audit_run_id"] = str(audit_run_id)
        steps = tuple(
            WorkflowStepState(
                step_id=WorkflowStepId(str(uuid4())),
                workflow_id=resolved_workflow_id,
                step_index=index,
                step_name=tool_call.tool_name.value,
                tool_name=tool_call.tool_name.value,
                status=WorkflowStepStatus.PENDING,
                idempotency_key=None,
                input={
                    **common_input,
                    "tool_input": tool_call.input.model_dump(mode="json"),
                },
                result=None,
                error=None,
                attempt_count=0,
                started_at=None,
                completed_at=None,
                lease_owner=None,
                lease_expires_at=None,
                created_at=created_at,
                updated_at=created_at,
            )
            for index, tool_call in enumerate(tool_calls, start=1)
        )
        snapshot = WorkflowStateSnapshot(workflow=workflow, steps=steps)
        self._store.create(snapshot)
        return snapshot
