"""Deterministic correlation of current supplier-risk bulletins with open production demand."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from enterprise_agent.domain import (
    ActorContext,
    AttentionRegistration,
    Evidence,
    RunId,
    ScenarioCSupplierRiskTrigger,
)
from enterprise_agent.ports import AttentionPort, ClockPort, ErpPort, EvidenceQuery, KnowledgePort

COMMITTED_PRODUCTION_STATUSES = frozenset({"in_progress", "scheduled"})
KNOWLEDGE_RECORD_TYPES = frozenset({"supplier_risk_bulletin"})
ERP_RECORD_TYPES = frozenset({"purchase_order", "production_order"})


class SupplierRiskEvidenceError(ValueError):
    """Raised when scoped source facts cannot safely prove a supplier-risk correlation."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SupplierRisk:
    """One transparent bulletin-to-open-PO-to-production-demand correlation."""

    bulletin_id: str
    purchase_order_id: str
    production_order_id: str
    supplier_id: str
    part_id: str
    production_start_date: date
    trigger: ScenarioCSupplierRiskTrigger


@dataclass(frozen=True, slots=True, kw_only=True)
class SupplierRiskDetection:
    """One persisted supplier-risk attention registration and its evidence-grounded risk."""

    risk: SupplierRisk
    registration: AttentionRegistration


class SupplierRiskDetector:
    """Read bounded evidence and register current supplier disruption risks without interpreting text."""

    def __init__(
        self,
        knowledge: KnowledgePort,
        erp: ErpPort,
        attention: AttentionPort,
        clock: ClockPort,
    ) -> None:
        """Compose read-scoped provider ports with the durable attention boundary."""
        self._knowledge = knowledge
        self._erp = erp
        self._attention = attention
        self._clock = clock

    def detect(self, actor: ActorContext, run_id: RunId) -> tuple[SupplierRiskDetection, ...]:
        """Read current facts and persist one idempotent attention item for every proven risk."""
        detected_at = self._clock.now()
        bulletin_evidence = self._knowledge.query(
            actor,
            EvidenceQuery(record_types=KNOWLEDGE_RECORD_TYPES),
        )
        erp_evidence = self._erp.query(actor, EvidenceQuery(record_types=ERP_RECORD_TYPES))
        return tuple(
            SupplierRiskDetection(
                risk=risk,
                registration=self._attention.register(risk.trigger, run_id),
            )
            for risk in self.evaluate(bulletin_evidence, erp_evidence, detected_at)
        )

    def evaluate(
        self,
        bulletin_evidence: Sequence[Evidence],
        erp_evidence: Sequence[Evidence],
        detected_at: datetime,
    ) -> tuple[SupplierRisk, ...]:
        """Return only risks proven by explicit provider fields, never by bulletin body text."""
        if detected_at.tzinfo is None:
            raise SupplierRiskEvidenceError("detected_at must be timezone-aware")

        bulletins = _current_bulletins(bulletin_evidence)
        open_purchase_orders = _open_purchase_orders(erp_evidence)
        production_orders = _committed_production_orders(erp_evidence, detected_at.date())
        risks: list[SupplierRisk] = []
        for bulletin in bulletins:
            supplier_id = _required_text(bulletin, "supplier_id")
            plant_id = _required_text(bulletin, "plant_id")
            for purchase_order in open_purchase_orders:
                if (
                    _required_text(purchase_order, "supplier_id") != supplier_id
                    or _required_text(purchase_order, "plant_id") != plant_id
                ):
                    continue
                part_id = _required_text(purchase_order, "part_id")
                for production_order, production_start_date in production_orders:
                    if (
                        _required_text(production_order, "part_id") != part_id
                        or _required_text(production_order, "plant_id") != plant_id
                    ):
                        continue
                    source_versions = {
                        str(bulletin.evidence_id): bulletin.source_version,
                        str(purchase_order.evidence_id): purchase_order.source_version,
                        str(production_order.evidence_id): production_order.source_version,
                    }
                    trigger = ScenarioCSupplierRiskTrigger(
                        detector="supplier_risk_detector:v1",
                        bulletin_id=bulletin.record_id,
                        bulletin_version=bulletin.source_version,
                        supplier_id=supplier_id,
                        purchase_order_id=purchase_order.record_id,
                        purchase_order_version=purchase_order.source_version,
                        production_order_id=production_order.record_id,
                        production_order_version=production_order.source_version,
                        part_id=part_id,
                        production_start_date=production_start_date,
                        detected_at=detected_at,
                        source_versions=source_versions,
                    )
                    risks.append(
                        SupplierRisk(
                            bulletin_id=bulletin.record_id,
                            purchase_order_id=purchase_order.record_id,
                            production_order_id=production_order.record_id,
                            supplier_id=supplier_id,
                            part_id=part_id,
                            production_start_date=production_start_date,
                            trigger=trigger,
                        )
                    )
        return tuple(risks)


def _current_bulletins(evidence: Sequence[Evidence]) -> tuple[Evidence, ...]:
    """Select only current knowledge facts by explicit status, never by bulletin-body content."""
    current_candidates: list[Evidence] = []
    for bulletin in evidence:
        if bulletin.record_type != "supplier_risk_bulletin":
            continue
        _require_source(bulletin, "knowledge")
        if _required_text(bulletin, "status").lower() == "active":
            _required_text(bulletin, "supplier_id")
            _required_text(bulletin, "plant_id")
            current_candidates.append(bulletin)
    return _unique_records(current_candidates, "supplier_risk_bulletin")


def _open_purchase_orders(evidence: Sequence[Evidence]) -> tuple[Evidence, ...]:
    """Select current ERP purchase orders that retain an actionable open status."""
    purchase_orders = _unique_records(evidence, "purchase_order")
    current: list[Evidence] = []
    for purchase_order in purchase_orders:
        _require_source(purchase_order, "erp")
        if _required_text(purchase_order, "status").lower() == "open":
            _required_text(purchase_order, "supplier_id")
            _required_text(purchase_order, "part_id")
            _required_text(purchase_order, "plant_id")
            current.append(purchase_order)
    return tuple(current)


def _committed_production_orders(
    evidence: Sequence[Evidence],
    as_of_date: date,
) -> tuple[tuple[Evidence, date], ...]:
    """Select committed production demand that remains current at the detection date."""
    production_orders = _unique_records(evidence, "production_order")
    committed: list[tuple[Evidence, date]] = []
    for production_order in production_orders:
        _require_source(production_order, "erp")
        if _required_text(production_order, "status").lower() not in COMMITTED_PRODUCTION_STATUSES:
            continue
        production_start_date = _required_date(production_order, "start_date")
        if production_start_date < as_of_date:
            continue
        _required_text(production_order, "part_id")
        _required_text(production_order, "plant_id")
        committed.append((production_order, production_start_date))
    return tuple(
        sorted(
            committed,
            key=lambda item: (item[1], item[0].record_id),
        )
    )


def _unique_records(evidence: Sequence[Evidence], record_type: str) -> tuple[Evidence, ...]:
    """Return stable records of one type while failing closed on duplicate identities."""
    records_by_id: dict[str, Evidence] = {}
    for record in evidence:
        if record.record_type != record_type:
            continue
        if record.record_id in records_by_id:
            raise SupplierRiskEvidenceError(
                f"multiple {record_type} records supplied for {record.record_id}"
            )
        records_by_id[record.record_id] = record
    return tuple(records_by_id[record_id] for record_id in sorted(records_by_id))


def _require_source(record: Evidence, source: str) -> None:
    """Reject evidence that does not originate from the bounded provider that owns its record type."""
    if record.source != source:
        raise SupplierRiskEvidenceError(
            f"{record.record_type} evidence must be supplied by the {source} provider"
        )


def _required_text(record: Evidence, name: str) -> str:
    """Read one identity, status, or location field without permitting blank values."""
    value = str(record.payload.get(name, "")).strip()
    if not value:
        raise SupplierRiskEvidenceError(
            f"{record.record_type} evidence lacks required field: {name}"
        )
    return value


def _required_date(record: Evidence, name: str) -> date:
    """Read a typed business date without guessing from untrusted strings."""
    value = record.payload.get(name)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise SupplierRiskEvidenceError(f"{record.record_type} evidence lacks valid date field: {name}")
