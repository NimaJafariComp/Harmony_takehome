"""Non-executing Scenario A policy gate for bounded reroute recommendations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from enterprise_agent.application.candidates import SupplierCandidate, SupplierCandidateFilter
from enterprise_agent.application.context import (
    ACTIVE_OUTSTANDING_PURCHASE_ORDER_STATUSES,
    AuthorizedContextBundle,
)
from enterprise_agent.application.planning import (
    EnterWorkflowRecommendation,
    ManualReviewRecommendation,
    NoActionRecommendation,
    ScenarioARecommendation,
)
from enterprise_agent.domain import Money, Scope

_REQUIRED_CONTEXT_SCOPES = frozenset(
    {Scope("erp:read"), Scope("mail:read"), Scope("calendar:read")}
)
_REQUIRED_REROUTE_SCOPE = Scope("erp:po:reroute")
_ON_SCHEDULE_SHIPMENT_STATUSES = frozenset({"on_schedule", "on_track"})


class GateStatus(StrEnum):
    """The only results a gate may return before any workflow execution exists."""

    NO_ACTION = "no_action"
    MANUAL_REVIEW = "manual_review"
    PENDING_APPROVAL = "pending_approval"
    DENIED = "denied"


class GateDenialReason(StrEnum):
    """Stable, auditable reasons why a proposed reroute may not request approval."""

    MISSING_REQUIRED_SCOPE = "missing_required_scope"
    STALE_SOURCE_EVIDENCE = "stale_source_evidence"
    ORIGINAL_PURCHASE_ORDER_MISMATCH = "original_purchase_order_mismatch"
    PRODUCTION_ORDER_MISMATCH = "production_order_mismatch"
    QUANTITY_MISMATCH = "quantity_mismatch"
    PURCHASE_ORDER_NOT_REROUTABLE = "purchase_order_not_reroutable"
    INELIGIBLE_SUPPLIER = "ineligible_supplier"
    INVALID_SUPPLIER_PRICING = "invalid_supplier_pricing"
    MISSING_APPROVAL_LIMIT = "missing_approval_limit"
    APPROVAL_LIMIT_EXCEEDED = "approval_limit_exceeded"


@dataclass(frozen=True, slots=True, kw_only=True)
class GateDecision:
    """An immutable gate result that intentionally contains no executable tool request."""

    status: GateStatus
    approval_required: bool
    denial_reasons: tuple[GateDenialReason, ...]
    estimated_value: Money | None
    candidate: SupplierCandidate | None


class ScenarioAGate:
    """Validate a fixed reroute proposal against current, authorized Scenario A evidence."""

    def evaluate(
        self,
        context: AuthorizedContextBundle,
        recommendation: ScenarioARecommendation,
        *,
        current_source_versions: Mapping[str, int],
    ) -> GateDecision:
        """Hold valid writes for approval, denying policy violations without executing anything."""
        if isinstance(recommendation, NoActionRecommendation):
            return _safe_outcome(GateStatus.NO_ACTION)
        if isinstance(recommendation, ManualReviewRecommendation):
            return _safe_outcome(GateStatus.MANUAL_REVIEW)
        assert isinstance(recommendation, EnterWorkflowRecommendation)

        shipment_outcome = _shipment_update_outcome(context)
        if shipment_outcome is not None:
            return _safe_outcome(shipment_outcome)

        reasons: list[GateDenialReason] = []
        if not _current_evidence_matches(context, current_source_versions):
            reasons.append(GateDenialReason.STALE_SOURCE_EVIDENCE)
        if not _has_required_reroute_scopes(context):
            reasons.append(GateDenialReason.MISSING_REQUIRED_SCOPE)
        if recommendation.original_purchase_order_id != context.original_purchase_order.record_id:
            reasons.append(GateDenialReason.ORIGINAL_PURCHASE_ORDER_MISMATCH)
        if recommendation.production_order_id != context.production_order.record_id:
            reasons.append(GateDenialReason.PRODUCTION_ORDER_MISMATCH)

        outstanding_quantity = _outstanding_quantity(context)
        if outstanding_quantity is None:
            reasons.append(GateDenialReason.PURCHASE_ORDER_NOT_REROUTABLE)
        elif recommendation.quantity != outstanding_quantity:
            reasons.append(GateDenialReason.QUANTITY_MISMATCH)

        candidate = _eligible_candidate(context, recommendation.supplier_id)
        if candidate is None:
            reasons.append(GateDenialReason.INELIGIBLE_SUPPLIER)

        estimated_value = _estimated_value(candidate, recommendation.quantity)
        if candidate is not None and estimated_value is None:
            reasons.append(GateDenialReason.INVALID_SUPPLIER_PRICING)
        if estimated_value is not None:
            approval_limit = context.actor.approval_limit_for(estimated_value.currency)
            if approval_limit is None:
                reasons.append(GateDenialReason.MISSING_APPROVAL_LIMIT)
            elif estimated_value.amount > approval_limit:
                reasons.append(GateDenialReason.APPROVAL_LIMIT_EXCEEDED)

        if reasons:
            return GateDecision(
                status=GateStatus.DENIED,
                approval_required=False,
                denial_reasons=tuple(reasons),
                estimated_value=estimated_value,
                candidate=candidate,
            )

        return GateDecision(
            status=GateStatus.PENDING_APPROVAL,
            approval_required=True,
            denial_reasons=(),
            estimated_value=estimated_value,
            candidate=candidate,
        )


def _safe_outcome(status: GateStatus) -> GateDecision:
    """Return one safe, non-writing model outcome without invoking write policy checks."""
    return GateDecision(
        status=status,
        approval_required=False,
        denial_reasons=(),
        estimated_value=None,
        candidate=None,
    )


def _shipment_update_outcome(context: AuthorizedContextBundle) -> GateStatus | None:
    """Honor a current on-schedule update only when its delivery date coherently meets production."""
    details = context.shipment_update.payload.get("payload", context.shipment_update.payload)
    if not isinstance(details, Mapping):
        return GateStatus.MANUAL_REVIEW
    shipment_status = str(details.get("shipment_status", "")).strip().lower()
    if shipment_status not in _ON_SCHEDULE_SHIPMENT_STATUSES:
        return None
    try:
        expected_receipt = date.fromisoformat(str(details["expected_receipt_date"]))
        production_start = context.trigger.production_start_date
    except (KeyError, TypeError, ValueError):
        return GateStatus.MANUAL_REVIEW
    return (
        GateStatus.NO_ACTION if expected_receipt <= production_start else GateStatus.MANUAL_REVIEW
    )


def _current_evidence_matches(
    context: AuthorizedContextBundle, current_source_versions: Mapping[str, int]
) -> bool:
    """Fail closed unless every fact used for planning still has its exact source version."""
    return dict(context.source_versions) == dict(current_source_versions)


def _has_required_reroute_scopes(context: AuthorizedContextBundle) -> bool:
    """Require the original read authority and the dedicated reroute write capability."""
    required_scopes = _REQUIRED_CONTEXT_SCOPES | {_REQUIRED_REROUTE_SCOPE}
    return required_scopes.issubset(context.actor.scopes)


def _outstanding_quantity(context: AuthorizedContextBundle) -> Decimal | None:
    """Return a positive reroutable remainder only for a currently active purchase order."""
    purchase_order = context.original_purchase_order
    status = str(purchase_order.payload.get("status", "")).strip().lower()
    if status not in ACTIVE_OUTSTANDING_PURCHASE_ORDER_STATUSES:
        return None
    try:
        ordered = Decimal(str(purchase_order.payload["ordered_quantity"]))
        received = Decimal(str(purchase_order.payload["received_quantity"]))
    except (KeyError, InvalidOperation, ValueError):
        return None
    outstanding = ordered - received
    if not outstanding.is_finite() or outstanding <= 0:
        return None
    return outstanding


def _eligible_candidate(
    context: AuthorizedContextBundle, supplier_id: str
) -> SupplierCandidate | None:
    """Recompute deterministic eligibility so a caller cannot forge a candidate result."""
    try:
        candidates = SupplierCandidateFilter().filter(context).candidates
    except ValueError:
        return None
    return next(
        (candidate for candidate in candidates if candidate.supplier_id == supplier_id), None
    )


def _estimated_value(candidate: SupplierCandidate | None, quantity: Decimal) -> Money | None:
    """Calculate replacement value only from a viable supplier's valid ERP price and currency."""
    if candidate is None:
        return None
    payload = candidate.evidence.payload
    try:
        unit_price = Decimal(str(payload["unit_price"]))
        currency = str(payload["currency"])
    except (KeyError, InvalidOperation, ValueError):
        return None
    if not unit_price.is_finite() or unit_price <= 0:
        return None
    try:
        return Money(amount=quantity * unit_price, currency=currency)
    except ValueError:
        return None
