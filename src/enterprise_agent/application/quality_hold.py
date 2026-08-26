"""Deterministic, provider-neutral detection of imminent Scenario B quality-hold impacts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from enterprise_agent.domain import (
    ActorContext,
    AttentionRegistration,
    Evidence,
    RunId,
    ScenarioBQualityHoldTrigger,
)
from enterprise_agent.ports import AttentionPort, ClockPort, EvidenceQuery, QualityPort

QUALITY_HOLD_HORIZON_DAYS = 3
COMMITTED_PRODUCTION_STATUSES = frozenset({"in_progress", "scheduled"})
QUALITY_RECORD_TYPES = frozenset({"quality_lot", "production_allocation", "production_impact"})


class QualityHoldEvidenceError(ValueError):
    """Raised when quality-provider evidence cannot safely prove one held allocation risk."""


@dataclass(frozen=True, slots=True, kw_only=True)
class QualityHoldRisk:
    """The transparent calculation that justifies one Scenario B quality-hold attention item."""

    quality_lot_id: str
    production_allocation_id: str
    production_order_id: str
    part_id: str
    production_start_date: date
    days_until_consumption: int
    allocated_quantity: Decimal
    trigger: ScenarioBQualityHoldTrigger


@dataclass(frozen=True, slots=True, kw_only=True)
class QualityHoldDetection:
    """One persisted attention registration paired with its held-allocation risk calculation."""

    risk: QualityHoldRisk
    registration: AttentionRegistration


class QualityHoldDetector:
    """Register held lots allocated to scheduled production no more than three days away."""

    def __init__(self, quality: QualityPort, attention: AttentionPort, clock: ClockPort) -> None:
        """Compose only the quality provider, attention store, and injectable business clock."""
        self._quality = quality
        self._attention = attention
        self._clock = clock

    def detect(self, actor: ActorContext, run_id: RunId) -> tuple[QualityHoldDetection, ...]:
        """Read quality-scoped evidence and persist one attention item per imminent held allocation."""
        detected_at = self._clock.now()
        evidence = self._quality.query(
            actor,
            EvidenceQuery(record_types=QUALITY_RECORD_TYPES),
        )
        return tuple(
            QualityHoldDetection(
                risk=risk,
                registration=self._attention.register(risk.trigger, run_id),
            )
            for risk in self.evaluate(evidence, detected_at)
        )

    def evaluate(
        self, evidence: Sequence[Evidence], detected_at: datetime
    ) -> tuple[QualityHoldRisk, ...]:
        """Evaluate current provider evidence without writing attention state."""
        allocations_by_lot = _allocations_by_lot(evidence)
        impacts = _impacts_by_id(evidence)
        risks: list[QualityHoldRisk] = []

        for lot in sorted(
            (item for item in evidence if item.record_type == "quality_lot"),
            key=lambda item: item.record_id,
        ):
            if _required_text(lot, "status").lower() != "held":
                continue
            part_id = _required_text(lot, "part_id")
            for allocation in allocations_by_lot.get(lot.record_id, ()):
                allocated_quantity = _required_decimal(allocation, "allocated_quantity")
                if allocated_quantity <= 0:
                    continue
                production_order_id = _required_text(allocation, "production_order_id")
                impact = impacts.get(production_order_id)
                if impact is None:
                    raise QualityHoldEvidenceError(
                        "held-lot allocation lacks a matching production-impact record"
                    )
                _require_same_value(impact, "part_id", part_id)
                if _required_text(impact, "status").lower() not in COMMITTED_PRODUCTION_STATUSES:
                    continue
                production_start_date = _required_date(impact, "start_date")
                days_until_consumption = (production_start_date - detected_at.date()).days
                if not 0 <= days_until_consumption <= QUALITY_HOLD_HORIZON_DAYS:
                    continue
                source_versions = {
                    f"quality_lot:{lot.record_id}": lot.source_version,
                    f"production_allocation:{allocation.record_id}": allocation.source_version,
                    f"production_impact:{impact.record_id}": impact.source_version,
                }
                trigger = ScenarioBQualityHoldTrigger(
                    detector="quality_hold_detector:v1",
                    part_id=part_id,
                    quality_lot_id=lot.record_id,
                    quality_lot_version=lot.source_version,
                    production_allocation_id=allocation.record_id,
                    production_allocation_version=allocation.source_version,
                    production_order_id=impact.record_id,
                    production_order_version=impact.source_version,
                    production_start_date=production_start_date,
                    detected_at=detected_at,
                    source_versions=source_versions,
                )
                risks.append(
                    QualityHoldRisk(
                        quality_lot_id=lot.record_id,
                        production_allocation_id=allocation.record_id,
                        production_order_id=impact.record_id,
                        part_id=part_id,
                        production_start_date=production_start_date,
                        days_until_consumption=days_until_consumption,
                        allocated_quantity=allocated_quantity,
                        trigger=trigger,
                    )
                )
        return tuple(risks)


def _allocations_by_lot(evidence: Sequence[Evidence]) -> Mapping[str, tuple[Evidence, ...]]:
    """Group quality-owned allocation facts by held-lot identity without losing duplicates."""
    allocations: defaultdict[str, list[Evidence]] = defaultdict(list)
    for record in evidence:
        if record.record_type == "production_allocation":
            allocations[_required_text(record, "quality_lot_id")].append(record)
    return {
        lot_id: tuple(sorted(items, key=lambda item: item.record_id))
        for lot_id, items in allocations.items()
    }


def _impacts_by_id(evidence: Sequence[Evidence]) -> Mapping[str, Evidence]:
    """Index one visible production-impact projection per production order."""
    impacts: dict[str, Evidence] = {}
    for record in evidence:
        if record.record_type != "production_impact":
            continue
        if record.record_id in impacts:
            raise QualityHoldEvidenceError(
                f"multiple production-impact records supplied for {record.record_id}"
            )
        impacts[record.record_id] = record
    return impacts


def _required_text(record: Evidence, name: str) -> str:
    """Read one non-empty identity, status, or location value from provider evidence."""
    value = str(record.payload.get(name, "")).strip()
    if not value:
        raise QualityHoldEvidenceError(
            f"{record.record_type} evidence lacks required field: {name}"
        )
    return value


def _required_decimal(record: Evidence, name: str) -> Decimal:
    """Read a non-negative allocation quantity without accepting malformed provider evidence."""
    try:
        value = Decimal(str(record.payload[name]))
    except (KeyError, InvalidOperation, ValueError) as error:
        raise QualityHoldEvidenceError(
            f"{record.record_type} evidence lacks valid decimal field: {name}"
        ) from error
    if value < 0:
        raise QualityHoldEvidenceError(f"allocation quantity must be non-negative: {name}")
    return value


def _required_date(record: Evidence, name: str) -> date:
    """Read one production date without provider-specific string parsing."""
    value = record.payload.get(name)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise QualityHoldEvidenceError(f"{record.record_type} evidence lacks valid date field: {name}")


def _require_same_value(record: Evidence, name: str, expected: str) -> None:
    """Reject an allocation whose visible production impact belongs to another material part."""
    if _required_text(record, name) != expected:
        raise QualityHoldEvidenceError(
            f"{record.record_type} evidence does not match the held lot {name}"
        )
