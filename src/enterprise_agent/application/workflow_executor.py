"""Fail-closed claim and declared-guard execution for approved Scenario A workflows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Protocol
from uuid import uuid4

from enterprise_agent.application.approvals import recompute_plan_hash
from enterprise_agent.application.tools import (
    CreateReplacementPOInput,
    NotifyProductionInput,
    ReduceOrCancelPOInput,
    ScheduleArrivalCheckInput,
    TerminalToolExecutionError,
    ToolAuthorizationError,
    ToolInput,
    ToolName,
    authorize_tool,
    build_compensation_idempotency_key,
    build_tool_idempotency_key,
    tool_definition,
)
from enterprise_agent.application.workflows import (
    WorkflowDefinition,
    WorkflowNotDeclaredError,
    WorkflowStepDefinition,
    declared_workflow,
)
from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalStatus,
    Plan,
    PlanId,
    ToolCompensation,
    ToolInvocation,
    ToolInvocationId,
    ToolInvocationStatus,
    WorkflowId,
    WorkflowStateSnapshot,
    WorkflowStatus,
    WorkflowStepStatus,
)
from enterprise_agent.ports import (
    IdentityPort,
    ToolCompensationPort,
    ToolExecutionPort,
    WorkflowStatePort,
)


class WorkflowExecutionRejectedError(PermissionError):
    """Raised when current approval, authorization, source, or declared state is unsafe."""


class WorkflowClaimLostError(RuntimeError):
    """Raised when another active worker owns the workflow lease."""


class WorkflowExternalStepPendingError(RuntimeError):
    """Raised when a caller attempts a local guard transition for an external declared step."""


class WorkflowToolExecutionUnavailableError(RuntimeError):
    """Raised when a declared effect has no independently authorization-enforcing tool adapter."""


class WorkflowCrashInjectedError(RuntimeError):
    """Raised only by an explicit test injector after an external effect commits locally."""


class WorkflowCrashInjectorPort(Protocol):
    """Inject a deterministic process-stop boundary without participating in normal execution."""

    def after_external_effect(self, invocation: ToolInvocation) -> None:
        """Stop immediately after the provider result exists and before workflow completion commits."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class DeterministicCrashInjector:
    """Inject one exact post-effect crash for a declared tool in a controlled restart test."""

    target_tool_name: ToolName

    def after_external_effect(self, invocation: ToolInvocation) -> None:
        """Raise only after the selected provider call has returned its durable result."""
        if invocation.tool_name == self.target_tool_name.value:
            raise WorkflowCrashInjectedError(
                "injected crash after external effect and before workflow step completion"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class StartedToolExecution:
    """A single durable started step whose external effect may now be invoked exactly by its key."""

    actor: ActorContext
    invocation: ToolInvocation
    step_index: int


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
        tool_executor: ToolExecutionPort | None = None,
        crash_injector: WorkflowCrashInjectorPort | None = None,
    ) -> None:
        """Keep authorization, approval, and durable transition concerns behind typed ports."""
        self._workflow_store = workflow_store
        self._approvals = approvals
        self._identity = identity
        self._tool_executor = tool_executor
        self._crash_injector = crash_injector

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
        if _next_step_is_started_external_tool(snapshot):
            _validate_started_tool(snapshot, definition)
        else:
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

    def begin_next_tool(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
        current_source_versions: Mapping[str, int],
    ) -> StartedToolExecution:
        """Revalidate, lease, and commit ``tool.started`` before any external effect can run."""
        if self._tool_executor is None:
            raise WorkflowToolExecutionUnavailableError("external tool executor is not configured")
        current = self._workflow_store.load(workflow_id)
        if current is None:
            raise WorkflowExecutionRejectedError("workflow does not exist")
        claimed = (
            _require_active_tool_lease(current, worker_id=worker_id, now=now)
            if _has_completed_external_tool(current)
            else self.claim(
                workflow_id,
                worker_id=worker_id,
                now=now,
                lease_expires_at=lease_expires_at,
                current_source_versions=current_source_versions,
            )
        )
        binding = self._approvals.load_for_plan(claimed.workflow.plan_id)
        if binding is None:
            raise WorkflowExecutionRejectedError("workflow has no approval binding")
        plan, _ = binding
        definition = _declared_definition(claimed, plan=plan)
        resuming = _next_step_is_started_external_tool(claimed)
        next_step = (
            _validate_started_tool(claimed, definition)
            if resuming
            else _validate_next_step(claimed, definition)
        )
        if next_step.tool_name is None:
            raise WorkflowExecutionRejectedError("workflow next step is a read-only guard")

        actor = self._identity.actor_for(plan.actor_id)
        try:
            authorize_tool(actor, next_step.tool_name)
        except ToolAuthorizationError as error:
            raise WorkflowExecutionRejectedError(
                "workflow actor no longer has the required write scope"
            ) from error
        stored_step = claimed.steps[next_step.index - 1]
        started_at = stored_step.started_at if resuming else now
        if started_at is None:
            raise WorkflowExecutionRejectedError("workflow started tool has no start timestamp")
        input_value = _tool_input_for_step(claimed, next_step, started_at=started_at)
        idempotency_key = build_tool_idempotency_key(
            claimed.workflow.workflow_id,
            next_step.index,
            next_step.tool_name,
            input_value,
        )
        if resuming:
            if stored_step.idempotency_key != idempotency_key:
                raise WorkflowExecutionRejectedError(
                    "workflow started tool idempotency key does not match its declared input"
                )
        else:
            started = self._workflow_store.start_tool_step(
                claimed.workflow.workflow_id,
                worker_id=worker_id,
                expected_step_index=next_step.index,
                idempotency_key=idempotency_key,
                started_at=started_at,
            )
            if started is None:
                raise WorkflowClaimLostError("workflow tool start transition was not acquired")
        return StartedToolExecution(
            actor=actor,
            invocation=ToolInvocation(
                invocation_id=ToolInvocationId(str(uuid4())),
                workflow_id=claimed.workflow.workflow_id,
                tool_name=next_step.tool_name.value,
                idempotency_key=idempotency_key,
                status=ToolInvocationStatus.STARTED,
                parameters=input_value.model_dump(mode="json"),
                result=None,
                attempt_count=stored_step.attempt_count if resuming else 1,
                started_at=started_at,
                completed_at=None,
            ),
            step_index=next_step.index,
        )

    def execute_started_tool(
        self,
        started: StartedToolExecution,
        *,
        worker_id: str,
        completed_at: datetime,
    ) -> WorkflowStateSnapshot:
        """Invoke an independently committed effect, then separately commit its workflow result."""
        if self._tool_executor is None:
            raise WorkflowToolExecutionUnavailableError("external tool executor is not configured")
        invocation = started.invocation
        if (
            invocation.status is not ToolInvocationStatus.STARTED
            or invocation.started_at is None
            or completed_at < invocation.started_at
        ):
            raise WorkflowExecutionRejectedError("tool invocation is not a valid started action")
        snapshot = self._workflow_store.load(invocation.workflow_id)
        if snapshot is None:
            raise WorkflowExecutionRejectedError("workflow does not exist")
        definition = _declared_definition(snapshot, plan=None)
        next_step = _validate_started_tool(snapshot, definition)
        workflow = snapshot.workflow
        if (
            next_step.index != started.step_index
            or next_step.tool_name is None
            or next_step.tool_name.value != invocation.tool_name
            or snapshot.steps[workflow.current_step].idempotency_key != invocation.idempotency_key
            or workflow.status is not WorkflowStatus.RUNNING
            or workflow.lease_owner != worker_id
            or workflow.lease_expires_at is None
            or workflow.lease_expires_at <= completed_at
        ):
            raise WorkflowClaimLostError("workflow tool execution requires the active lease")

        try:
            result = self._tool_executor.execute(started.actor, invocation)
        except TerminalToolExecutionError as error:
            failed = self._workflow_store.fail_tool_step(
                workflow.workflow_id,
                worker_id=worker_id,
                expected_step_index=next_step.index,
                idempotency_key=invocation.idempotency_key,
                error=_safe_terminal_error(error),
                failed_at=completed_at,
            )
            if failed is None:
                raise WorkflowClaimLostError(
                    "workflow terminal-failure transition was not acquired"
                ) from error
            self.compensate_failed_workflow(
                workflow.workflow_id,
                worker_id=worker_id,
                now=completed_at,
            )
            raise
        if not isinstance(result, Mapping):
            raise WorkflowExecutionRejectedError("tool result must be a mapping")
        if self._crash_injector is not None:
            self._crash_injector.after_external_effect(invocation)
        completed = self._workflow_store.complete_tool_step(
            workflow.workflow_id,
            worker_id=worker_id,
            expected_step_index=next_step.index,
            idempotency_key=invocation.idempotency_key,
            result=dict(result),
            finish_workflow=next_step.index == len(definition.steps),
            completed_at=completed_at,
        )
        if completed is None:
            raise WorkflowClaimLostError("workflow tool completion transition was not acquired")
        return completed

    def compensate_failed_workflow(
        self,
        workflow_id: WorkflowId,
        *,
        worker_id: str,
        now: datetime,
    ) -> WorkflowStateSnapshot:
        """Reverse only this failed workflow's completed effects in declared reverse order."""
        if self._tool_executor is None or not isinstance(self._tool_executor, ToolCompensationPort):
            raise WorkflowToolExecutionUnavailableError(
                "tool compensation executor is not configured"
            )
        snapshot = self._workflow_store.load(workflow_id)
        if snapshot is None:
            raise WorkflowExecutionRejectedError("workflow does not exist")
        if snapshot.workflow.status is not WorkflowStatus.FAILED:
            raise WorkflowExecutionRejectedError("workflow is not ready for compensation")
        definition = _declared_definition(
            snapshot,
            plan=None,
            allowed_statuses=frozenset({WorkflowStatus.FAILED}),
        )
        actor = self._compensation_actor(snapshot)
        compensating = self._workflow_store.begin_compensation(
            workflow_id,
            worker_id=worker_id,
            started_at=now,
        )
        if compensating is None:
            raise WorkflowClaimLostError("workflow compensation transition was not acquired")
        if compensating.workflow.status is WorkflowStatus.COMPENSATED:
            return compensating

        for step in reversed(compensating.steps):
            if step.tool_name is None or step.status is not WorkflowStepStatus.SUCCEEDED:
                continue
            if step.idempotency_key is None or step.result is None:
                raise WorkflowExecutionRejectedError(
                    "completed external step lacks compensation provenance"
                )
            declared_step = definition.steps[step.step_index - 1]
            if declared_step.tool_name is None or declared_step.tool_name.value != step.tool_name:
                raise WorkflowExecutionRejectedError(
                    "workflow stored steps do not match the declaration"
                )
            compensation = tool_definition(declared_step.tool_name).compensation
            started = self._workflow_store.start_compensation_step(
                workflow_id,
                worker_id=worker_id,
                expected_step_index=step.step_index,
                started_at=now,
            )
            if started is None:
                raise WorkflowClaimLostError("workflow compensation step was not acquired")
            result = self._tool_executor.compensate(
                actor,
                ToolCompensation(
                    workflow_id=workflow_id,
                    tool_name=step.tool_name,
                    action=compensation.value,
                    original_idempotency_key=step.idempotency_key,
                    idempotency_key=build_compensation_idempotency_key(
                        workflow_id,
                        step.step_index,
                        compensation,
                        step.idempotency_key,
                    ),
                    effect_result=step.result,
                    requested_at=now,
                ),
            )
            if not isinstance(result, Mapping):
                raise WorkflowExecutionRejectedError("tool compensation result must be a mapping")
            remaining_effects = sum(
                candidate.tool_name is not None and candidate.status is WorkflowStepStatus.SUCCEEDED
                for candidate in started.steps
            )
            completed = self._workflow_store.complete_compensation_step(
                workflow_id,
                worker_id=worker_id,
                expected_step_index=step.step_index,
                result=dict(result),
                finish_workflow=remaining_effects == 0,
                completed_at=now,
            )
            if completed is None:
                raise WorkflowClaimLostError("workflow compensation completion was not acquired")
            compensating = completed
        if compensating.workflow.status is not WorkflowStatus.COMPENSATED:
            raise WorkflowExecutionRejectedError(
                "workflow compensation did not reach a terminal state"
            )
        return compensating

    def _compensation_actor(self, snapshot: WorkflowStateSnapshot) -> ActorContext:
        """Resolve the current initiating identity without relying on expired approval freshness."""
        binding = self._approvals.load_for_plan(snapshot.workflow.plan_id)
        if binding is None:
            raise WorkflowExecutionRejectedError("workflow has no approval binding")
        plan, _ = binding
        actor = self._identity.actor_for(plan.actor_id)
        if actor.user_id != plan.actor_id:
            raise WorkflowExecutionRejectedError("workflow actor identity does not match the plan")
        return actor


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
    snapshot: WorkflowStateSnapshot,
    *,
    plan: Plan | None,
    allowed_statuses: frozenset[WorkflowStatus] = frozenset(
        {WorkflowStatus.PENDING, WorkflowStatus.RUNNING}
    ),
) -> WorkflowDefinition:
    """Validate workflow/plan identity and every stored step against the reviewed declaration."""
    workflow = snapshot.workflow
    if workflow.status not in allowed_statuses:
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


def _validate_started_tool(
    snapshot: WorkflowStateSnapshot, definition: WorkflowDefinition
) -> WorkflowStepDefinition:
    """Return only the exact cursor-selected external step already committed as running."""
    workflow = snapshot.workflow
    if workflow.current_step >= len(definition.steps):
        raise WorkflowExecutionRejectedError("workflow has no remaining declared step")
    next_step = definition.steps[workflow.current_step]
    stored_step = snapshot.steps[workflow.current_step]
    if next_step.tool_name is None or stored_step.status is not WorkflowStepStatus.RUNNING:
        raise WorkflowExecutionRejectedError("workflow next step is not a started external tool")
    if stored_step.idempotency_key is None:
        raise WorkflowExecutionRejectedError("workflow started tool has no idempotency key")
    if any(
        step.status is not WorkflowStepStatus.SUCCEEDED
        for step in snapshot.steps[: workflow.current_step]
    ):
        raise WorkflowExecutionRejectedError("workflow has an incomplete prior step")
    if any(
        step.status is not WorkflowStepStatus.PENDING
        for step in snapshot.steps[workflow.current_step + 1 :]
    ):
        raise WorkflowExecutionRejectedError("workflow has an invalid future step")
    return next_step


def _next_step_is_started_external_tool(snapshot: WorkflowStateSnapshot) -> bool:
    """Identify the only restartable state: the exact current external step was durably started."""
    workflow = snapshot.workflow
    return (
        workflow.current_step < len(snapshot.steps)
        and snapshot.steps[workflow.current_step].tool_name is not None
        and snapshot.steps[workflow.current_step].status is WorkflowStepStatus.RUNNING
    )


def _has_completed_external_tool(snapshot: WorkflowStateSnapshot) -> bool:
    """Avoid comparing immutable pre-effect source versions after this workflow changed them itself."""
    return any(
        step.tool_name is not None and step.status is WorkflowStepStatus.SUCCEEDED
        for step in snapshot.steps
    )


def _require_active_tool_lease(
    snapshot: WorkflowStateSnapshot, *, worker_id: str, now: datetime
) -> WorkflowStateSnapshot:
    """Continue a valid owner lease after prior workflow effects changed their own ERP sources."""
    workflow = snapshot.workflow
    if (
        workflow.status is not WorkflowStatus.RUNNING
        or workflow.lease_owner != worker_id
        or workflow.lease_expires_at is None
        or workflow.lease_expires_at <= now
    ):
        raise WorkflowClaimLostError("workflow tool start requires the active lease")
    return snapshot


def _tool_input_for_step(
    snapshot: WorkflowStateSnapshot,
    step: WorkflowStepDefinition,
    *,
    started_at: datetime,
) -> ToolInput:
    """Build only the reviewed typed tool input for the next exact Scenario A effect."""
    parameters_value = snapshot.steps[step.index - 1].input.get("plan_parameters")
    if not isinstance(parameters_value, Mapping):
        raise WorkflowExecutionRejectedError("workflow stored tool parameters are invalid")
    parameters = dict(parameters_value)
    if step.tool_name is ToolName.CREATE_REPLACEMENT_PO:
        return CreateReplacementPOInput.model_validate(
            {
                "original_purchase_order_id": _required_parameter(
                    parameters, "original_purchase_order_id"
                ),
                "supplier_id": _required_parameter(parameters, "supplier_id"),
                "production_order_id": _required_parameter(parameters, "production_order_id"),
                "quantity": _required_parameter(parameters, "quantity"),
            }
        )
    if step.tool_name is ToolName.REDUCE_OR_CANCEL_PO:
        return ReduceOrCancelPOInput.model_validate(
            {
                "original_purchase_order_id": _required_parameter(
                    parameters, "original_purchase_order_id"
                ),
                "quantity": _required_parameter(parameters, "quantity"),
            }
        )
    replacement_purchase_order_id = _completed_replacement_purchase_order_id(snapshot)
    if step.tool_name is ToolName.NOTIFY_PRODUCTION:
        original_purchase_order_id = _required_parameter(parameters, "original_purchase_order_id")
        return NotifyProductionInput.model_validate(
            {
                "production_order_id": _required_parameter(parameters, "production_order_id"),
                "message": (
                    f"Replacement purchase order {replacement_purchase_order_id} was created; "
                    f"original purchase order {original_purchase_order_id} was reduced or cancelled."
                ),
            }
        )
    if step.tool_name is ToolName.SCHEDULE_ARRIVAL_CHECK:
        return ScheduleArrivalCheckInput(
            purchase_order_id=replacement_purchase_order_id,
            due_at=_next_tuesday_at_nine(started_at),
        )
    raise WorkflowExecutionRejectedError("workflow references an unsupported external tool")


def _required_parameter(parameters: Mapping[str, object], name: str) -> object:
    """Read one reviewed immutable plan parameter without silently inventing a default."""
    try:
        return parameters[name]
    except KeyError as error:
        raise WorkflowExecutionRejectedError(
            "workflow stored tool parameters are invalid"
        ) from error


def _completed_replacement_purchase_order_id(snapshot: WorkflowStateSnapshot) -> str:
    """Read the prior replacement result required by the notification and Tuesday check only."""
    replacement_step = snapshot.steps[2]
    if (
        replacement_step.status is not WorkflowStepStatus.SUCCEEDED
        or replacement_step.result is None
        or not isinstance(
            replacement_purchase_order_id := replacement_step.result.get(
                "replacement_purchase_order_id"
            ),
            str,
        )
        or not replacement_purchase_order_id.strip()
    ):
        raise WorkflowExecutionRejectedError(
            "workflow replacement purchase order result is unavailable"
        )
    return replacement_purchase_order_id


def _next_tuesday_at_nine(now: datetime) -> datetime:
    """Schedule the next explicit Tuesday receipt check in the supplied business timezone."""
    days_until_tuesday = (1 - now.weekday()) % 7
    if days_until_tuesday == 0:
        days_until_tuesday = 7
    return datetime.combine(
        (now + timedelta(days=days_until_tuesday)).date(),
        time(9),
        tzinfo=now.tzinfo,
    )


def _safe_terminal_error(error: TerminalToolExecutionError) -> str:
    """Persist a bounded operational failure category without leaking arbitrary provider detail."""
    return str(error).strip()[:500] or "terminal tool execution failed"
