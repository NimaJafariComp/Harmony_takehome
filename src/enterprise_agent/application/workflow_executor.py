"""Fail-closed claim and declared-guard execution for approved Scenario A workflows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from enterprise_agent.application.approvals import recompute_plan_hash
from enterprise_agent.application.tools import ToolAuthorizationError, authorize_tool
from enterprise_agent.application.workflows import (
    WorkflowDefinition,
    WorkflowNotDeclaredError,
    WorkflowStepDefinition,
    declared_workflow,
)
from enterprise_agent.domain import (
    Approval,
    ApprovalStatus,
    Plan,
    PlanId,
    WorkflowId,
    WorkflowStateSnapshot,
    WorkflowStatus,
    WorkflowStepStatus,
)
from enterprise_agent.ports import IdentityPort, WorkflowStatePort


class WorkflowExecutionRejectedError(PermissionError):
    """Raised when current approval, authorization, source, or declared state is unsafe."""


class WorkflowClaimLostError(RuntimeError):
    """Raised when another active worker owns the workflow lease."""


class WorkflowExternalStepPendingError(RuntimeError):
    """Raised when the next declared operation requires the later M4.5 tool boundary."""


class _WorkflowApprovalPort(Protocol):
    """Read the approval binding that authorizes exactly one workflow instance."""

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        """Return the immutable plan/approval record associated with a workflow plan ID."""
        ...


class ScenarioAWorkflowExecutor:
    """Claim approved declared workflows and advance only their local read-only guards."""

    def __init__(
        self,
        *,
        workflow_store: WorkflowStatePort,
        approvals: _WorkflowApprovalPort,
        identity: IdentityPort,
    ) -> None:
        """Keep authorization, approval, and durable transition concerns behind typed ports."""
        self._workflow_store = workflow_store
        self._approvals = approvals
        self._identity = identity

    def claim(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        current_source_versions: Mapping[str, int],
    ) -> WorkflowStateSnapshot:
        """Revalidate all mutable execution prerequisites before atomically taking one lease."""
        if not worker_id.strip():
            raise WorkflowExecutionRejectedError("workflow worker identity is required")
        if lease_expires_at <= now:
            raise WorkflowExecutionRejectedError("workflow lease expiry must be in the future")

        snapshot = self._workflow_store.load(workflow_id)
        if snapshot is None:
            raise WorkflowExecutionRejectedError("workflow does not exist")
        binding = self._approvals.load_for_plan(snapshot.workflow.plan_id)
        if binding is None:
            raise WorkflowExecutionRejectedError("workflow has no approval binding")
        plan, approval = binding
        definition = _revalidate_execution(
            snapshot,
            plan,
            approval,
            now=now,
            current_source_versions=current_source_versions,
            identity=self._identity,
        )
        _validate_next_step(snapshot, definition)

        claimed = self._workflow_store.claim(
            workflow_id,
            worker_id=worker_id,
            claimed_at=now,
            lease_expires_at=lease_expires_at,
        )
        if claimed is None:
            raise WorkflowClaimLostError("workflow claim was not acquired")
        return claimed

    def advance_next_guard(
        self,
        snapshot: WorkflowStateSnapshot,
        *,
        worker_id: str,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot:
        """Complete one exact next guard; effectful steps remain unavailable until M4.5."""
        definition = _declared_definition(snapshot, plan=None)
        next_step = _validate_next_step(snapshot, definition)
        if next_step.tool_name is not None:
            raise WorkflowExternalStepPendingError(
                "the next declared step requires an external tool boundary"
            )
        workflow = snapshot.workflow
        if (
            workflow.status is not WorkflowStatus.RUNNING
            or workflow.lease_owner != worker_id
            or workflow.lease_expires_at is None
            or workflow.lease_expires_at <= completed_at
        ):
            raise WorkflowClaimLostError("workflow guard transition requires the active lease")

        advanced = self._workflow_store.complete_guard_step(
            workflow.workflow_id,
            worker_id=worker_id,
            expected_step_index=next_step.index,
            completed_at=completed_at,
        )
        if advanced is None:
            raise WorkflowClaimLostError("workflow guard transition was not acquired")
        return advanced


def _revalidate_execution(
    snapshot: WorkflowStateSnapshot,
    plan: Plan,
    approval: Approval,
    *,
    now: datetime,
    current_source_versions: Mapping[str, int],
    identity: IdentityPort,
) -> WorkflowDefinition:
    """Fail closed unless immutable and live facts still authorize this exact declared workflow."""
    if (
        approval.status is not ApprovalStatus.APPROVED
        or approval.plan_id != plan.plan_id
        or approval.plan_hash != plan.plan_hash
        or approval.requester_id != plan.actor_id
        or approval.approver_id != plan.approver_id
        or now >= approval.expires_at
        or now >= plan.expires_at
    ):
        raise WorkflowExecutionRejectedError("workflow approval is not currently approved")
    if recompute_plan_hash(plan) != plan.plan_hash:
        raise WorkflowExecutionRejectedError("workflow approval binding does not match the plan")
    if dict(current_source_versions) != dict(plan.source_versions):
        raise WorkflowExecutionRejectedError("workflow source evidence is stale")

    definition = _declared_definition(snapshot, plan=plan)
    actor = identity.actor_for(plan.actor_id)
    if actor.user_id != plan.actor_id:
        raise WorkflowExecutionRejectedError("workflow actor identity does not match the plan")
    for step in definition.steps:
        if step.tool_name is None:
            continue
        try:
            authorize_tool(actor, step.tool_name)
        except ToolAuthorizationError as error:
            raise WorkflowExecutionRejectedError(
                "workflow actor no longer has the required write scope"
            ) from error
    return definition


def _declared_definition(
    snapshot: WorkflowStateSnapshot, *, plan: Plan | None
) -> WorkflowDefinition:
    """Validate workflow/plan identity and every stored step against the reviewed declaration."""
    workflow = snapshot.workflow
    if workflow.status not in {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}:
        raise WorkflowExecutionRejectedError("workflow is not runnable")
    if (workflow.lease_owner is None) != (workflow.lease_expires_at is None):
        raise WorkflowExecutionRejectedError("workflow lease state is invalid")
    if plan is not None and (
        workflow.plan_id != plan.plan_id
        or plan.intent != "enter_workflow"
        or plan.workflow_name != workflow.definition_name
        or plan.workflow_version != workflow.definition_version
    ):
        raise WorkflowExecutionRejectedError("workflow plan does not match its declaration")
    try:
        definition = declared_workflow(workflow.definition_name, workflow.definition_version)
    except WorkflowNotDeclaredError as error:
        raise WorkflowExecutionRejectedError("workflow has no declared definition") from error
    if not 0 <= workflow.current_step <= len(definition.steps):
        raise WorkflowExecutionRejectedError("workflow step cursor is invalid")
    if len(snapshot.steps) != len(definition.steps):
        raise WorkflowExecutionRejectedError("workflow stored steps do not match the declaration")

    expected_input = None
    if plan is not None:
        expected_input = {
            "plan_hash": plan.plan_hash,
            "plan_parameters": dict(plan.parameters),
            "source_versions": dict(plan.source_versions),
        }
    for stored_step, declared_step in zip(snapshot.steps, definition.steps, strict=True):
        if (
            stored_step.workflow_id != workflow.workflow_id
            or stored_step.step_index != declared_step.index
            or stored_step.step_name != declared_step.name.value
            or stored_step.tool_name
            != (None if declared_step.tool_name is None else declared_step.tool_name.value)
            or (expected_input is not None and dict(stored_step.input) != expected_input)
        ):
            raise WorkflowExecutionRejectedError(
                "workflow stored steps do not match the declaration"
            )
    return definition


def _validate_next_step(
    snapshot: WorkflowStateSnapshot, definition: WorkflowDefinition
) -> WorkflowStepDefinition:
    """Return only the exact cursor-selected pending declared step, rejecting skips and replays."""
    workflow = snapshot.workflow
    if workflow.current_step >= len(definition.steps):
        raise WorkflowExecutionRejectedError("workflow has no remaining declared step")
    next_step = definition.steps[workflow.current_step]
    stored_step = snapshot.steps[workflow.current_step]
    if stored_step.status is not WorkflowStepStatus.PENDING:
        raise WorkflowExecutionRejectedError("workflow next step is not pending")
    if any(
        step.status is not WorkflowStepStatus.SUCCEEDED
        for step in snapshot.steps[: workflow.current_step]
    ):
        raise WorkflowExecutionRejectedError("workflow has an incomplete prior step")
    if any(
        step.status is not WorkflowStepStatus.PENDING
        for step in snapshot.steps[workflow.current_step :]
    ):
        raise WorkflowExecutionRejectedError("workflow has an invalid future step")
    return next_step
