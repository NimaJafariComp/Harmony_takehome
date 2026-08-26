"""Scenario-neutral immutable declarations for approval-gated, catalog-backed tool plans."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast
from uuid import uuid4

from enterprise_agent.application.approvals import recompute_plan_hash
from enterprise_agent.application.gate import GateDecision, GateDenialReason, GateStatus
from enterprise_agent.application.tools import (
    ToolAuthorizationError,
    ToolInput,
    ToolName,
    authorize_tool,
    tool_definition,
)
from enterprise_agent.domain import ActorContext, AttentionId, Plan, PlanId, UserId

BOUNDED_TOOL_PLAN_INTENT = "bounded_tool_plan"
BOUNDED_TOOL_PLAN_WORKFLOW_NAME = "bounded_tool_plan"
BOUNDED_TOOL_PLAN_WORKFLOW_VERSION = 1


class BoundedToolPlanError(ValueError):
    """Raised when an immutable bounded-tool plan is malformed or not policy-safe."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundedToolCall:
    """One reviewed catalog invocation with its exact immutable typed input."""

    tool_name: ToolName
    input: ToolInput

    def __post_init__(self) -> None:
        definition = tool_definition(self.tool_name)
        if type(self.input) is not definition.input_model:
            raise BoundedToolPlanError(f"tool input schema does not match {definition.name.value}")

    def serialized(self) -> dict[str, object]:
        """Return the canonical JSON-safe representation persisted inside the reviewed plan."""
        return {
            "tool_name": self.tool_name.value,
            "input": self.input.model_dump(mode="json"),
        }


class BoundedToolPlanGate:
    """Fail closed unless current evidence and actor scope permit every selected catalog tool."""

    def evaluate(
        self,
        actor: ActorContext,
        tool_calls: Sequence[BoundedToolCall],
        *,
        source_versions: Mapping[str, int],
        current_source_versions: Mapping[str, int],
    ) -> GateDecision:
        """Return a shared gate decision before a bounded write plan can request approval."""
        if not tool_calls:
            raise BoundedToolPlanError("bounded tool plan must contain at least one tool")

        reasons: list[GateDenialReason] = []
        if dict(source_versions) != dict(current_source_versions):
            reasons.append(GateDenialReason.STALE_SOURCE_EVIDENCE)
        for tool_call in tool_calls:
            try:
                authorize_tool(actor, tool_call.tool_name)
            except ToolAuthorizationError:
                if GateDenialReason.MISSING_REQUIRED_SCOPE not in reasons:
                    reasons.append(GateDenialReason.MISSING_REQUIRED_SCOPE)

        if reasons:
            return GateDecision(
                status=GateStatus.DENIED,
                approval_required=False,
                denial_reasons=tuple(reasons),
                estimated_value=None,
                candidate=None,
            )
        return GateDecision(
            status=GateStatus.PENDING_APPROVAL,
            approval_required=True,
            denial_reasons=(),
            estimated_value=None,
            candidate=None,
        )


def build_bounded_tool_plan(
    *,
    attention_id: AttentionId,
    actor_id: UserId,
    approver_id: UserId,
    tool_calls: Sequence[BoundedToolCall],
    source_versions: Mapping[str, int],
    policy_version: str,
    created_at: datetime,
    expires_at: datetime,
) -> Plan:
    """Build one hash-bound plan that contains the only ordered external effects it may execute."""
    if not tool_calls:
        raise BoundedToolPlanError("bounded tool plan must contain at least one tool")
    if expires_at <= created_at:
        raise BoundedToolPlanError("plan expiry must be after its creation time")
    if not policy_version.strip():
        raise BoundedToolPlanError("policy version is required")
    _validate_source_versions(source_versions)

    plan = Plan(
        plan_id=PlanId(str(uuid4())),
        attention_id=attention_id,
        actor_id=actor_id,
        approver_id=approver_id,
        intent=BOUNDED_TOOL_PLAN_INTENT,
        workflow_name=BOUNDED_TOOL_PLAN_WORKFLOW_NAME,
        workflow_version=BOUNDED_TOOL_PLAN_WORKFLOW_VERSION,
        parameters={"tool_calls": [tool_call.serialized() for tool_call in tool_calls]},
        source_versions=dict(source_versions),
        policy_version=policy_version,
        plan_hash="",
        created_at=created_at,
        expires_at=expires_at,
    )
    return replace(plan, plan_hash=recompute_plan_hash(plan))


def bounded_tool_calls_from_plan(plan: Plan) -> tuple[BoundedToolCall, ...]:
    """Decode and revalidate only the immutable catalog calls bound by a bounded-tool plan."""
    if (
        plan.intent != BOUNDED_TOOL_PLAN_INTENT
        or plan.workflow_name != BOUNDED_TOOL_PLAN_WORKFLOW_NAME
        or plan.workflow_version != BOUNDED_TOOL_PLAN_WORKFLOW_VERSION
    ):
        raise BoundedToolPlanError("plan is not a bounded tool plan")
    if set(plan.parameters) != {"tool_calls"}:
        raise BoundedToolPlanError("bounded tool plan parameters are invalid")
    serialized_calls = plan.parameters["tool_calls"]
    if not isinstance(serialized_calls, (list, tuple)) or not serialized_calls:
        raise BoundedToolPlanError("bounded tool plan must contain at least one tool")

    tool_calls: list[BoundedToolCall] = []
    for serialized_call in serialized_calls:
        if not isinstance(serialized_call, Mapping) or set(serialized_call) != {
            "tool_name",
            "input",
        }:
            raise BoundedToolPlanError("bounded tool plan call is invalid")
        raw_name = serialized_call["tool_name"]
        raw_input = serialized_call["input"]
        if not isinstance(raw_name, str) or not isinstance(raw_input, Mapping):
            raise BoundedToolPlanError("bounded tool plan call is invalid")
        try:
            tool_name = ToolName(raw_name)
            definition = tool_definition(tool_name)
            input_value = definition.input_model.model_validate(dict(raw_input))
        except ValueError as error:
            raise BoundedToolPlanError("bounded tool plan call is invalid") from error
        tool_calls.append(BoundedToolCall(tool_name=tool_name, input=cast(ToolInput, input_value)))
    return tuple(tool_calls)


def _validate_source_versions(source_versions: Mapping[str, int]) -> None:
    """Reject empty, non-positive, or non-text source bindings before they become approval facts."""
    if not source_versions:
        raise BoundedToolPlanError("bounded tool plan requires source versions")
    for source_id, version in source_versions.items():
        if (
            not str(source_id).strip()
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
        ):
            raise BoundedToolPlanError("bounded tool plan source versions are invalid")
