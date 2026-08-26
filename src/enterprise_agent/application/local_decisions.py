"""Approval-only application boundary for the optional loopback verification UI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from enterprise_agent.application.approvals import PlanApprovalService, PlanNotApprovableError
from enterprise_agent.application.context import ScenarioAContextAssembler
from enterprise_agent.application.local_review import (
    AttentionReadPort,
    IdentityReadPort,
    LocalReviewAccessDeniedError,
    LocalReviewResourceNotFoundError,
    LocalReviewUnavailableError,
)
from enterprise_agent.application.quality_context import ScenarioBContextAssembler
from enterprise_agent.application.scenario_c_context import ScenarioCContextAssembler
from enterprise_agent.domain import (
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionItem,
    Evidence,
    Plan,
    PlanId,
    RunId,
    ScenarioAStockoutTrigger,
    ScenarioBQualityHoldTrigger,
    ScenarioCSupplierRiskTrigger,
    UserId,
)
from enterprise_agent.ports import (
    AuditPort,
    CalendarPort,
    ClockPort,
    ErpPort,
    EvidenceQuery,
    KnowledgePort,
    MailPort,
    PlanApprovalPort,
    QualityPort,
)


class ApprovalDecision(StrEnum):
    """The only two terminal outcomes a local reviewer may request."""

    APPROVE = "approve"
    REJECT = "reject"


class LocalApprovalDecisionConflictError(RuntimeError):
    """Raised when the approval is no longer eligible for a terminal decision."""


class LocalApprovalDecisionStaleError(LocalApprovalDecisionConflictError):
    """Raised when the exact plan or its full scenario context is no longer current."""


class PlanSourceFreshnessError(ValueError):
    """Raised when the original scenario context cannot be faithfully reconstructed."""


class LocalApprovalDecisionAccessDeniedError(LocalReviewAccessDeniedError):
    """Raised when a reviewer tries to decide a plan assigned to another current approver."""


@dataclass(frozen=True, slots=True)
class ApprovalDecisionAvailability:
    """Minimal page state that never exposes a plan hash, payload, or authorization detail."""

    can_decide: bool


@dataclass(frozen=True, slots=True)
class ApprovalDecisionResult:
    """Safe receipt emitted after the shared approval service persists one terminal outcome."""

    approval_id: str
    decision_state: str
    audit_run_id: str


class LocalApprovalDecisionPort(Protocol):
    """Narrow browser-facing contract for an approval decision, not plan or workflow mutation."""

    def availability(self, approval_id: str) -> ApprovalDecisionAvailability:
        """State whether the selected local actor is the current active approver."""
        ...

    def decide(self, *, approval_id: str, decision: ApprovalDecision) -> ApprovalDecisionResult:
        """Record one authorized approve or reject decision through the shared application service."""
        ...


class CurrentPlanSourceVersionsPort(Protocol):
    """Reconstruct current scenario evidence before the approval service evaluates freshness."""

    def current_source_versions(self, plan: Plan) -> Mapping[str, int]:
        """Return the whole current source-version map for the immutable plan's scenario context."""
        ...


class ApprovalAuditRunPort(Protocol):
    """Locate the existing run that must receive the material approval-decision audit event."""

    def latest_run_for_plan(self, plan_id: PlanId) -> RunId | None:
        """Return the current durable run identifier for the plan, if one exists."""
        ...


@dataclass(slots=True)
class LocalApprovalDecisionService:
    """Authorize one local decision, recheck source freshness, and delegate its write to policy code."""

    actor_id: UserId
    approvals: PlanApprovalPort
    freshness: CurrentPlanSourceVersionsPort
    clock: ClockPort
    audit: AuditPort
    audit_runs: ApprovalAuditRunPort

    def availability(self, approval_id: str) -> ApprovalDecisionAvailability:
        """Show controls only when the selected actor remains the current active approver."""
        _, approval = self._load_binding(approval_id)
        return ApprovalDecisionAvailability(
            can_decide=(
                approval.approver_id == self.actor_id
                and approval.status in {ApprovalStatus.PENDING, ApprovalStatus.REROUTED}
            )
        )

    def decide(self, *, approval_id: str, decision: ApprovalDecision) -> ApprovalDecisionResult:
        """Use the existing compare-and-swap approval service after all local checks pass."""
        plan, approval = self._load_binding(approval_id)
        if approval.approver_id != self.actor_id:
            raise LocalApprovalDecisionAccessDeniedError(
                "selected actor is not the current approver"
            )
        if approval.status not in {ApprovalStatus.PENDING, ApprovalStatus.REROUTED}:
            raise LocalApprovalDecisionConflictError("approval is no longer active")

        audit_run_id = self.audit_runs.latest_run_for_plan(plan.plan_id)
        if audit_run_id is None:
            raise LocalApprovalDecisionConflictError("approval has no durable audit run")

        service = PlanApprovalService(self.approvals, audit=self.audit)
        try:
            if decision is ApprovalDecision.APPROVE:
                current_source_versions = self.freshness.current_source_versions(plan)
                if dict(current_source_versions) != dict(plan.source_versions):
                    raise LocalApprovalDecisionStaleError("plan source evidence is stale")
                recorded = service.approve(
                    approval_id=approval.approval_id,
                    expected_plan_hash=plan.plan_hash,
                    decider_id=self.actor_id,
                    current_source_versions=current_source_versions,
                    decided_at=self.clock.now(),
                    run_id=_run_id_or_conflict(audit_run_id),
                )
            else:
                recorded = service.reject(
                    approval_id=approval.approval_id,
                    expected_plan_hash=plan.plan_hash,
                    decider_id=self.actor_id,
                    decided_at=self.clock.now(),
                    run_id=_run_id_or_conflict(audit_run_id),
                )
        except LocalApprovalDecisionStaleError:
            raise
        except PlanSourceFreshnessError as error:
            raise LocalApprovalDecisionStaleError("plan source evidence is stale") from error
        except PlanNotApprovableError as error:
            if "stale" in str(error).lower():
                raise LocalApprovalDecisionStaleError("plan source evidence is stale") from error
            raise LocalApprovalDecisionConflictError("approval could not be recorded") from error

        return ApprovalDecisionResult(
            approval_id=str(recorded.approval_id),
            decision_state=recorded.status.value,
            audit_run_id=audit_run_id,
        )

    def _load_binding(self, value: str) -> tuple[Plan, Approval]:
        """Reject malformed or missing opaque IDs before they can reach a persistence adapter."""
        approval_id = _approval_id_or_not_found(value)
        binding = self.approvals.load(approval_id)
        if binding is None:
            raise LocalReviewResourceNotFoundError("approval is unavailable")
        return binding


class UnconfiguredLocalApprovalDecisionService:
    """Fail closed when the optional local decision boundary has no safe composition."""

    def availability(self, approval_id: str) -> ApprovalDecisionAvailability:
        """Reject decision-control discovery before inspecting a caller-provided record ID."""
        del approval_id
        raise LocalReviewUnavailableError("local approval decisions are not configured")

    def decide(self, *, approval_id: str, decision: ApprovalDecision) -> ApprovalDecisionResult:
        """Reject every decision while the local writer and freshness readers are unavailable."""
        del approval_id, decision
        raise LocalReviewUnavailableError("local approval decisions are not configured")


@dataclass(slots=True)
class CurrentPlanSourceVersionsService:
    """Reassemble the original scenario through its existing scoped provider and context services."""

    identity: IdentityReadPort
    attentions: AttentionReadPort
    erp: ErpPort
    quality: QualityPort
    knowledge: KnowledgePort
    mail: MailPort
    calendar: CalendarPort

    def current_source_versions(self, plan: Plan) -> Mapping[str, int]:
        """Return a complete current context map or fail closed when scenario evidence changed."""
        attention = self.attentions.load(plan.attention_id)
        if attention is None:
            raise PlanSourceFreshnessError("plan attention is unavailable")
        try:
            actor = self.identity.actor_for(plan.actor_id)
        except LookupError as error:
            raise PlanSourceFreshnessError("plan actor is unavailable") from error

        try:
            match attention.scenario:
                case "scenario_a":
                    return self._scenario_a_versions(plan, attention, actor.user_id)
                case "scenario_b":
                    return self._scenario_b_versions(plan, attention, actor.user_id)
                case "scenario_c":
                    return self._scenario_c_versions(plan, attention, actor.user_id)
                case _:
                    raise PlanSourceFreshnessError("plan scenario is not supported")
        except (KeyError, TypeError, ValueError) as error:
            raise PlanSourceFreshnessError("plan scenario evidence is unavailable") from error

    def _scenario_a_versions(
        self, plan: Plan, attention: AttentionItem, actor_id: UserId
    ) -> Mapping[str, int]:
        """Rebuild Scenario A's exact ERP, mail, and calendar context before approval."""
        inventory_id = _bare_source_id(attention.source_versions, "inventory")
        production_order_id = _required_plan_parameter(plan, "production_order_id")
        evidence = self.erp.query(
            self.identity.actor_for(actor_id),
            EvidenceQuery(
                record_types=frozenset({"inventory", "production_order"}),
                record_ids=frozenset({inventory_id, production_order_id}),
            ),
        )
        inventory = _single_evidence(evidence, "inventory", inventory_id)
        production_order = _single_evidence(evidence, "production_order", production_order_id)
        trigger = ScenarioAStockoutTrigger(
            detector="stockout_detector:v1",
            part_id=_required_payload_text(inventory, "part_id"),
            production_order_id=production_order_id,
            inventory_version=_bare_source_version(attention.source_versions, "inventory"),
            production_start_date=_required_date(production_order, "start_date"),
            detected_at=attention.created_at,
            source_versions=attention.source_versions,
        )
        context = ScenarioAContextAssembler(
            self.identity,
            self.erp,
            self.mail,
            self.calendar,
        ).assemble(user_id=actor_id, attention=attention, trigger=trigger)
        return context.source_versions

    def _scenario_b_versions(
        self, plan: Plan, attention: AttentionItem, actor_id: UserId
    ) -> Mapping[str, int]:
        """Rebuild Scenario B's held-lot context, including every current alternative lot."""
        held_lot_id = _bare_source_id(attention.source_versions, "quality_lot")
        allocation_id = _bare_source_id(attention.source_versions, "production_allocation")
        production_order_id = _bare_source_id(attention.source_versions, "production_impact")
        evidence = self.quality.query(
            self.identity.actor_for(actor_id),
            EvidenceQuery(
                record_types=frozenset(
                    {"quality_lot", "production_allocation", "production_impact"}
                ),
                record_ids=frozenset({held_lot_id, allocation_id, production_order_id}),
            ),
        )
        held_lot = _single_evidence(evidence, "quality_lot", held_lot_id)
        allocation = _single_evidence(evidence, "production_allocation", allocation_id)
        production_impact = _single_evidence(evidence, "production_impact", production_order_id)
        trigger = ScenarioBQualityHoldTrigger(
            detector="quality_hold_detector:v1",
            part_id=_required_payload_text(held_lot, "part_id"),
            quality_lot_id=held_lot_id,
            quality_lot_version=_bare_source_version(attention.source_versions, "quality_lot"),
            production_allocation_id=allocation_id,
            production_allocation_version=_bare_source_version(
                attention.source_versions, "production_allocation"
            ),
            production_order_id=production_order_id,
            production_order_version=_bare_source_version(
                attention.source_versions, "production_impact"
            ),
            production_start_date=_required_date(production_impact, "start_date"),
            detected_at=attention.created_at,
            source_versions=attention.source_versions,
        )
        if _required_payload_text(allocation, "production_order_id") != production_order_id:
            raise PlanSourceFreshnessError("quality allocation no longer matches production")
        context = ScenarioBContextAssembler(self.identity, self.quality).assemble(
            user_id=actor_id,
            attention=attention,
            trigger=trigger,
        )
        return context.source_versions

    def _scenario_c_versions(
        self, plan: Plan, attention: AttentionItem, actor_id: UserId
    ) -> Mapping[str, int]:
        """Rebuild Scenario C's current bulletin, PO, and committed-production correlation."""
        bulletin_id = _evidence_source_id(
            attention.source_versions, "knowledge:supplier_risk_bulletin"
        )
        purchase_order_id = _evidence_source_id(attention.source_versions, "erp:purchase_order")
        production_order_id = _evidence_source_id(attention.source_versions, "erp:production_order")
        actor = self.identity.actor_for(actor_id)
        bulletin = _single_evidence(
            self.knowledge.query(
                actor,
                EvidenceQuery(
                    record_types=frozenset({"supplier_risk_bulletin"}),
                    record_ids=frozenset({bulletin_id}),
                ),
            ),
            "supplier_risk_bulletin",
            bulletin_id,
        )
        erp_evidence = self.erp.query(
            actor,
            EvidenceQuery(
                record_types=frozenset({"purchase_order", "production_order"}),
                record_ids=frozenset({purchase_order_id, production_order_id}),
            ),
        )
        purchase_order = _single_evidence(erp_evidence, "purchase_order", purchase_order_id)
        production_order = _single_evidence(erp_evidence, "production_order", production_order_id)
        trigger = ScenarioCSupplierRiskTrigger(
            detector="supplier_risk_detector:v1",
            bulletin_id=bulletin_id,
            bulletin_version=_evidence_source_version(
                attention.source_versions, "knowledge:supplier_risk_bulletin"
            ),
            supplier_id=_required_payload_text(bulletin, "supplier_id"),
            purchase_order_id=purchase_order_id,
            purchase_order_version=_evidence_source_version(
                attention.source_versions, "erp:purchase_order"
            ),
            production_order_id=production_order_id,
            production_order_version=_evidence_source_version(
                attention.source_versions, "erp:production_order"
            ),
            part_id=_required_payload_text(purchase_order, "part_id"),
            production_start_date=_required_date(production_order, "start_date"),
            detected_at=attention.created_at,
            source_versions=attention.source_versions,
        )
        context = ScenarioCContextAssembler(self.identity, self.knowledge, self.erp).assemble(
            user_id=actor_id,
            attention=attention,
            trigger=trigger,
        )
        return context.source_versions


def _approval_id_or_not_found(value: str) -> ApprovalId:
    """Normalize one opaque path value before it reaches a UUID-backed approval adapter."""
    try:
        return ApprovalId(str(UUID(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise LocalReviewResourceNotFoundError("approval is unavailable") from error


def _run_id_or_conflict(value: RunId) -> RunId:
    """Reject blank audit-run references without disclosing persistence implementation detail."""
    normalized = value.strip()
    if not normalized:
        raise LocalApprovalDecisionConflictError("approval has no durable audit run")
    return RunId(normalized)


def _bare_source_id(source_versions: Mapping[str, int], record_type: str) -> str:
    """Return the one source ID recorded by the Scenario A/B detector for a typed record."""
    prefix = f"{record_type}:"
    matches = tuple(
        source.removeprefix(prefix) for source in source_versions if source.startswith(prefix)
    )
    if len(matches) != 1 or not matches[0]:
        raise PlanSourceFreshnessError("required detector source is unavailable")
    return matches[0]


def _bare_source_version(source_versions: Mapping[str, int], record_type: str) -> int:
    """Return the positive version associated with a Scenario A/B detector source."""
    source_id = _bare_source_id(source_versions, record_type)
    version = source_versions[f"{record_type}:{source_id}"]
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise PlanSourceFreshnessError("detector source version is invalid")
    return version


def _evidence_source_id(source_versions: Mapping[str, int], prefix: str) -> str:
    """Return one record ID from a full provider-owned Scenario C evidence identity."""
    expected = f"{prefix}:"
    matches = tuple(
        source.removeprefix(expected) for source in source_versions if source.startswith(expected)
    )
    if len(matches) != 1 or not matches[0]:
        raise PlanSourceFreshnessError("required context source is unavailable")
    return matches[0]


def _evidence_source_version(source_versions: Mapping[str, int], prefix: str) -> int:
    """Return one positive version bound to a full Scenario C provider evidence identity."""
    record_id = _evidence_source_id(source_versions, prefix)
    version = source_versions[f"{prefix}:{record_id}"]
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise PlanSourceFreshnessError("context source version is invalid")
    return version


def _single_evidence(evidence: Sequence[Evidence], record_type: str, record_id: str) -> Evidence:
    """Require one current provider fact before reconstructing a scenario trigger."""
    matches = tuple(
        item for item in evidence if item.record_type == record_type and item.record_id == record_id
    )
    if len(matches) != 1:
        raise PlanSourceFreshnessError("required current evidence is unavailable")
    return matches[0]


def _required_plan_parameter(plan: Plan, name: str) -> str:
    """Read a stable opaque plan parameter without trusting any browser-supplied value."""
    value = str(plan.parameters.get(name, "")).strip()
    if not value:
        raise PlanSourceFreshnessError("required immutable plan field is unavailable")
    return value


def _required_payload_text(evidence: Evidence, name: str) -> str:
    """Read one structured provider field needed to reconstruct a typed trigger."""
    value = str(evidence.payload.get(name, "")).strip()
    if not value:
        raise PlanSourceFreshnessError("required evidence field is unavailable")
    return value


def _required_date(evidence: Evidence, name: str) -> date:
    """Require a provider-owned date value rather than parsing an untrusted display string."""
    value = evidence.payload.get(name)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise PlanSourceFreshnessError("required evidence date is unavailable")
