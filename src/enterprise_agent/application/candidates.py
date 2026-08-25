"""Deterministic, auditable filtering of Scenario A replacement-PO suppliers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from enterprise_agent.application.context import AuthorizedContextBundle
from enterprise_agent.domain import Evidence


class SupplierExclusionReason(StrEnum):
    """Explicit policy reasons a visible supplier cannot be offered to the planner."""

    ORIGINAL_SUPPLIER = "original_supplier"
    NOT_APPROVED = "not_approved"
    WRONG_PART = "wrong_part"
    WRONG_PLANT = "wrong_plant"
    INVALID_LEAD_TIME = "invalid_lead_time"
    LEAD_TIME_TOO_LONG = "lead_time_too_long"


@dataclass(frozen=True, slots=True, kw_only=True)
class SupplierCandidate:
    """One pre-authorized alternate supplier that deterministic policy has allowed."""

    supplier_id: str
    evidence: Evidence
    lead_time_days: int
    arrival_date: date


@dataclass(frozen=True, slots=True, kw_only=True)
class SupplierExclusion:
    """One visible supplier and every deterministic reason it was not a valid alternate."""

    supplier_id: str
    evidence: Evidence
    reasons: tuple[SupplierExclusionReason, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SupplierCandidateResult:
    """The full planner input boundary, retaining accepted candidates and rejected evidence."""

    candidates: tuple[SupplierCandidate, ...]
    exclusions: tuple[SupplierExclusion, ...]

    @property
    def allowed_supplier_ids(self) -> frozenset[str]:
        """Return the only supplier IDs that later planning may legally select."""
        return frozenset(candidate.supplier_id for candidate in self.candidates)


class SupplierCandidateFilter:
    """Apply Scenario A supplier, plant, timing, and alternate-source policy before planning."""

    def filter(self, context: AuthorizedContextBundle) -> SupplierCandidateResult:
        """Allow only authorized viable alternates and preserve every exclusion for the audit path."""
        candidates: list[SupplierCandidate] = []
        exclusions: list[SupplierExclusion] = []
        original_supplier_id = _required_supplier_id(context.original_purchase_order)
        production_start_date = context.trigger.production_start_date
        detected_on = context.trigger.detected_at.date()

        for supplier in sorted(context.suppliers, key=lambda item: item.record_id):
            reasons, lead_time_days = _exclusion_reasons(
                supplier=supplier,
                original_supplier_id=original_supplier_id,
                part_id=context.trigger.part_id,
                plant_ids=context.actor.plant_ids,
                detected_on=detected_on,
                production_start_date=production_start_date,
            )
            if reasons:
                exclusions.append(
                    SupplierExclusion(
                        supplier_id=supplier.record_id,
                        evidence=supplier,
                        reasons=tuple(reasons),
                    )
                )
                continue

            assert lead_time_days is not None
            candidates.append(
                SupplierCandidate(
                    supplier_id=supplier.record_id,
                    evidence=supplier,
                    lead_time_days=lead_time_days,
                    arrival_date=detected_on + timedelta(days=lead_time_days),
                )
            )

        return SupplierCandidateResult(candidates=tuple(candidates), exclusions=tuple(exclusions))


def _exclusion_reasons(
    *,
    supplier: Evidence,
    original_supplier_id: str,
    part_id: str,
    plant_ids: frozenset[str],
    detected_on: date,
    production_start_date: date,
) -> tuple[list[SupplierExclusionReason], int | None]:
    """Evaluate every independent supplier policy so audit can explain all denials at once."""
    reasons: list[SupplierExclusionReason] = []
    if supplier.record_id == original_supplier_id:
        reasons.append(SupplierExclusionReason.ORIGINAL_SUPPLIER)
    if supplier.payload.get("approved") is not True:
        reasons.append(SupplierExclusionReason.NOT_APPROVED)
    if _string_payload_value(supplier, "part_id") != part_id:
        reasons.append(SupplierExclusionReason.WRONG_PART)
    if _string_payload_value(supplier, "plant_id") not in plant_ids:
        reasons.append(SupplierExclusionReason.WRONG_PLANT)

    lead_time_days = _lead_time_days(supplier)
    if lead_time_days is None:
        reasons.append(SupplierExclusionReason.INVALID_LEAD_TIME)
    elif detected_on + timedelta(days=lead_time_days) > production_start_date:
        reasons.append(SupplierExclusionReason.LEAD_TIME_TOO_LONG)
    return reasons, lead_time_days


def _required_supplier_id(purchase_order: Evidence) -> str:
    """Read the current PO's supplier ID from the already-authorized context evidence."""
    value = _string_payload_value(purchase_order, "supplier_id")
    if not value:
        raise ValueError("original purchase-order evidence lacks supplier ID")
    return value


def _string_payload_value(evidence: Evidence, name: str) -> str:
    """Normalize an identifier-like ERP payload value without trusting its original type."""
    return str(evidence.payload.get(name, "")).strip()


def _lead_time_days(supplier: Evidence) -> int | None:
    """Accept only a non-negative integer lead time from the typed ERP supplier record."""
    value = supplier.payload.get("lead_time_days")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
