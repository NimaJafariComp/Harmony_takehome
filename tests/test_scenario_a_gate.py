"""Contracts for the non-executing Scenario A policy and approval gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from enterprise_agent.application.context import AuthorizedContextBundle
from enterprise_agent.application.planning import (
    EnterWorkflowRecommendation,
    ManualReviewRecommendation,
    NoActionRecommendation,
)
from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    Evidence,
    EvidenceId,
    PlantId,
    ScenarioAStockoutTrigger,
    Scope,
    UserId,
)

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)


def _evidence(
    *,
    record_type: str,
    record_id: str,
    payload: dict[str, object],
    source_version: int = 1,
) -> Evidence:
    """Build one authorized, versioned evidence record for a gate contract."""
    return Evidence(
        evidence_id=EvidenceId(f"erp:{record_type}:{record_id}"),
        source="erp",
        record_type=record_type,
        record_id=record_id,
        source_version=source_version,
        observed_at=NOW,
        payload=payload,
    )


def _actor(
    *,
    scopes: frozenset[Scope] | None = None,
    approval_limit: Decimal | None = Decimal("10000.00"),
) -> ActorContext:
    """Build the purchasing actor whose permissions and limit the gate must enforce."""
    return ActorContext(
        user_id=UserId("00000000-0000-0000-0000-000000000001"),
        role="purchasing_manager",
        scopes=scopes
        if scopes is not None
        else frozenset(
            {
                Scope("erp:read"),
                Scope("mail:read"),
                Scope("calendar:read"),
                Scope("erp:po:reroute"),
            }
        ),
        plant_ids=frozenset({PlantId("PLANT-CHI")}),
        backup_approver_id=UserId("00000000-0000-0000-0000-000000000002"),
        approval_limits={} if approval_limit is None else {"USD": approval_limit},
    )


def _context(*, actor: ActorContext | None = None) -> AuthorizedContextBundle:
    """Build a complete delayed-partial-PO context with one allowed alternate supplier."""
    trigger = ScenarioAStockoutTrigger(
        detector="stockout_detector:v1",
        part_id="part-x",
        production_order_id="production-4812",
        inventory_version=4,
        production_start_date=date(2026, 8, 27),
        detected_at=NOW,
        source_versions={
            "inventory:inventory-x": 4,
            "production_order:production-4812": 1,
        },
    )
    attention = AttentionItem(
        attention_id=AttentionId("attention-stockout"),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key=trigger.dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions=trigger.source_versions,
    )
    return AuthorizedContextBundle(
        actor=actor or _actor(),
        attention=attention,
        trigger=trigger,
        inventory=_evidence(
            record_type="inventory",
            record_id="inventory-x",
            source_version=4,
            payload={"part_id": "part-x"},
        ),
        production_order=_evidence(
            record_type="production_order",
            record_id="production-4812",
            payload={"part_id": "part-x", "start_date": date(2026, 8, 27)},
        ),
        original_purchase_order=_evidence(
            record_type="purchase_order",
            record_id="po-4812-y",
            source_version=2,
            payload={
                "part_id": "part-x",
                "supplier_id": "supplier-y",
                "status": "delayed",
                "ordered_quantity": Decimal(100),
                "received_quantity": Decimal(40),
            },
        ),
        suppliers=(
            _evidence(
                record_type="supplier",
                record_id="supplier-y",
                payload={
                    "part_id": "part-x",
                    "plant_id": "PLANT-CHI",
                    "approved": True,
                    "lead_time_days": 4,
                    "unit_price": Decimal(14),
                    "currency": "USD",
                },
            ),
            _evidence(
                record_type="supplier",
                record_id="supplier-z",
                payload={
                    "part_id": "part-x",
                    "plant_id": "PLANT-CHI",
                    "approved": True,
                    "lead_time_days": 1,
                    "unit_price": Decimal(18),
                    "currency": "USD",
                },
            ),
        ),
        shipment_update=_evidence(
            record_type="message",
            record_id="shipment-current",
            payload={"shipment_status": "delayed"},
        ),
        calendar_events=(),
    )


def _recommendation(**overrides: object) -> EnterWorkflowRecommendation:
    """Build the one exact reroute the bounded Scenario A policy may consider."""
    values: dict[str, object] = {
        "outcome": "ENTER_WORKFLOW",
        "workflow_name": "po_reroute",
        "workflow_version": 1,
        "supplier_id": "supplier-z",
        "quantity": Decimal(60),
        "original_purchase_order_id": "po-4812-y",
        "production_order_id": "production-4812",
        "rationale": "The approved alternate can meet production.",
    }
    values.update(overrides)
    return EnterWorkflowRecommendation(**values)  # type: ignore[arg-type]


def _with_original_purchase_order_payload(
    context: AuthorizedContextBundle, **overrides: object
) -> AuthorizedContextBundle:
    """Return the same context with one controlled original-PO evidence change."""
    purchase_order = context.original_purchase_order
    return replace(
        context,
        original_purchase_order=replace(
            purchase_order,
            payload={**purchase_order.payload, **overrides},
        ),
    )


def _with_supplier_payload(
    context: AuthorizedContextBundle, **overrides: object
) -> AuthorizedContextBundle:
    """Return the same context with controlled pricing metadata on the allowed supplier."""
    suppliers = tuple(
        replace(supplier, payload={**supplier.payload, **overrides})
        if supplier.record_id == "supplier-z"
        else supplier
        for supplier in context.suppliers
    )
    return replace(context, suppliers=suppliers)


@pytest.mark.critical
def test_gate_holds_a_valid_reroute_for_human_approval_without_executing_it() -> None:
    """A fully compliant proposal is only made approval-pending and binds its $1,080 value."""
    from enterprise_agent.application.gate import GateStatus, ScenarioAGate

    context = _context()
    decision = ScenarioAGate().evaluate(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
    )

    assert decision.status is GateStatus.PENDING_APPROVAL
    assert decision.approval_required is True
    assert decision.denial_reasons == ()
    assert decision.estimated_value is not None
    assert decision.estimated_value.amount == Decimal(1080)
    assert decision.estimated_value.currency == "USD"
    assert decision.candidate is not None
    assert decision.candidate.supplier_id == "supplier-z"


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"supplier_id": "supplier-y"}, "INELIGIBLE_SUPPLIER"),
        ({"original_purchase_order_id": "po-other"}, "ORIGINAL_PURCHASE_ORDER_MISMATCH"),
        ({"production_order_id": "production-other"}, "PRODUCTION_ORDER_MISMATCH"),
        ({"quantity": Decimal(59)}, "QUANTITY_MISMATCH"),
    ],
)
def test_gate_denies_workflow_parameters_outside_the_context_bound_reroute(
    overrides: dict[str, object], expected_reason: str
) -> None:
    """The LLM cannot substitute a supplier, PO, production order, or partial quantity."""
    from enterprise_agent.application.gate import GateStatus, ScenarioAGate

    context = _context()
    decision = ScenarioAGate().evaluate(
        context,
        _recommendation(**overrides),
        current_source_versions=context.source_versions,
    )

    assert decision.status is GateStatus.DENIED
    assert decision.approval_required is False
    assert expected_reason in {reason.name for reason in decision.denial_reasons}


def test_gate_denies_missing_write_scope_and_stale_evidence() -> None:
    """A reroute cannot proceed when authority is absent or any planning fact has changed."""
    from enterprise_agent.application.gate import GateDenialReason, GateStatus, ScenarioAGate

    context = _context(actor=_actor(scopes=frozenset({Scope("erp:read")})))
    current_versions = dict(context.source_versions)
    current_versions["erp:purchase_order:po-4812-y"] = 3

    decision = ScenarioAGate().evaluate(
        context,
        _recommendation(),
        current_source_versions=current_versions,
    )

    assert decision.status is GateStatus.DENIED
    assert decision.approval_required is False
    assert set(decision.denial_reasons) == {
        GateDenialReason.MISSING_REQUIRED_SCOPE,
        GateDenialReason.STALE_SOURCE_EVIDENCE,
    }


def test_gate_denies_a_reroute_that_exceeds_the_actors_currency_limit() -> None:
    """A $1,080 replacement cannot be sent for approval through a $1,000 authority."""
    from enterprise_agent.application.gate import GateDenialReason, GateStatus, ScenarioAGate

    context = _context(actor=_actor(approval_limit=Decimal(1000)))
    decision = ScenarioAGate().evaluate(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
    )

    assert decision.status is GateStatus.DENIED
    assert decision.denial_reasons == (GateDenialReason.APPROVAL_LIMIT_EXCEEDED,)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "cancelled"},
        {"ordered_quantity": "not-a-quantity"},
        {"received_quantity": Decimal(100)},
    ],
)
def test_gate_denies_closed_or_malformed_or_fully_received_original_purchase_orders(
    overrides: dict[str, object],
) -> None:
    """A non-reroutable original PO cannot become a replacement order by model suggestion."""
    from enterprise_agent.application.gate import GateDenialReason, GateStatus, ScenarioAGate

    context = _with_original_purchase_order_payload(_context(), **overrides)
    decision = ScenarioAGate().evaluate(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
    )

    assert decision.status is GateStatus.DENIED
    assert GateDenialReason.PURCHASE_ORDER_NOT_REROUTABLE in decision.denial_reasons


@pytest.mark.parametrize(
    "overrides",
    [
        {"unit_price": "not-a-price"},
        {"unit_price": Decimal("NaN")},
        {"currency": "US"},
    ],
)
def test_gate_denies_missing_or_invalid_supplier_pricing(
    overrides: dict[str, object],
) -> None:
    """A viable supplier with unusable value evidence cannot be presented for approval."""
    from enterprise_agent.application.gate import GateDenialReason, GateStatus, ScenarioAGate

    context = _with_supplier_payload(_context(), **overrides)
    decision = ScenarioAGate().evaluate(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
    )

    assert decision.status is GateStatus.DENIED
    assert decision.denial_reasons == (GateDenialReason.INVALID_SUPPLIER_PRICING,)


def test_gate_denies_reroutes_without_an_authority_limit_for_the_supplier_currency() -> None:
    """A price in an unapproved currency is never silently allowed to bypass its policy limit."""
    from enterprise_agent.application.gate import GateDenialReason, GateStatus, ScenarioAGate

    context = _context(actor=_actor(approval_limit=None))
    decision = ScenarioAGate().evaluate(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
    )

    assert decision.status is GateStatus.DENIED
    assert decision.denial_reasons == (GateDenialReason.MISSING_APPROVAL_LIMIT,)


def test_gate_denies_context_that_cannot_prove_the_original_supplier_identity() -> None:
    """Malformed PO supplier evidence causes deterministic candidate filtering to fail closed."""
    from enterprise_agent.application.gate import GateDenialReason, GateStatus, ScenarioAGate

    context = _with_original_purchase_order_payload(_context(), supplier_id="")
    decision = ScenarioAGate().evaluate(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
    )

    assert decision.status is GateStatus.DENIED
    assert GateDenialReason.INELIGIBLE_SUPPLIER in decision.denial_reasons


@pytest.mark.parametrize(
    "recommendation",
    [
        NoActionRecommendation(outcome="NO_ACTION", rationale="No reroute is necessary."),
        ManualReviewRecommendation(outcome="MANUAL_REVIEW", reason="Evidence conflicts."),
    ],
)
def test_gate_preserves_safe_non_writing_outcomes_without_requesting_approval(
    recommendation: NoActionRecommendation | ManualReviewRecommendation,
) -> None:
    """Safe outcomes stay non-executable and do not create an unnecessary approval request."""
    from enterprise_agent.application.gate import GateStatus, ScenarioAGate

    context = _context()
    decision = ScenarioAGate().evaluate(
        context,
        recommendation,
        current_source_versions=context.source_versions,
    )

    assert decision.status is GateStatus[recommendation.outcome]
    assert decision.approval_required is False
    assert decision.denial_reasons == ()
    assert decision.estimated_value is None
    assert decision.candidate is None
