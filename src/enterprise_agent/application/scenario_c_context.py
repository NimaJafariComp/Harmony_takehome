"""Fresh Scenario C context assembly over separate knowledge and ERP authorization boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from enterprise_agent.domain import (
    ActorContext,
    AttentionItem,
    Evidence,
    ScenarioCSupplierRiskTrigger,
    UserId,
)
from enterprise_agent.ports import ErpPort, EvidenceQuery, IdentityPort, KnowledgePort

from .supplier_risk import COMMITTED_PRODUCTION_STATUSES


class MissingScenarioCContextEvidenceError(ValueError):
    """Raised when current authorized evidence cannot support a bounded supplier-risk context."""


class StaleScenarioCContextEvidenceError(ValueError):
    """Raised when an attention signal no longer matches current supplier-risk evidence."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ScenarioCContextBundle:
    """Planner-facing current facts for one supplier bulletin and its affected demand."""

    actor: ActorContext
    attention: AttentionItem
    trigger: ScenarioCSupplierRiskTrigger
    bulletin: Evidence
    purchase_order: Evidence
    production_order: Evidence

    def __post_init__(self) -> None:
        evidence_ids = tuple(str(item.evidence_id) for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Scenario C context evidence IDs must be unique")

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        """Return source-separated facts in stable provenance order without interpreting body text."""
        return (self.bulletin, self.purchase_order, self.production_order)

    @property
    def source_versions(self) -> Mapping[str, int]:
        """Expose immutable current evidence versions for subsequent freshness gates."""
        return MappingProxyType(
            {str(item.evidence_id): item.source_version for item in self.evidence}
        )


class ScenarioCContextAssembler:
    """Re-read exact provider-owned evidence before a future Scenario C planner uses it."""

    def __init__(self, identity: IdentityPort, knowledge: KnowledgePort, erp: ErpPort) -> None:
        """Compose identity, knowledge, and ERP read boundaries with no write authority."""
        self._identity = identity
        self._knowledge = knowledge
        self._erp = erp

    def assemble(
        self,
        *,
        user_id: UserId,
        attention: AttentionItem,
        trigger: ScenarioCSupplierRiskTrigger,
    ) -> ScenarioCContextBundle:
        """Return exact current facts or fail closed if any source binding has become stale."""
        _require_attention_matches_trigger(attention, trigger)
        actor = self._identity.actor_for(user_id)
        bulletin_evidence = self._knowledge.query(
            actor,
            EvidenceQuery(
                record_types=frozenset({"supplier_risk_bulletin"}),
                record_ids=frozenset({trigger.bulletin_id}),
            ),
        )
        erp_evidence = self._erp.query(
            actor,
            EvidenceQuery(
                record_types=frozenset({"purchase_order", "production_order"}),
                record_ids=frozenset({trigger.purchase_order_id, trigger.production_order_id}),
            ),
        )
        bulletin = _select_bulletin(bulletin_evidence, trigger)
        purchase_order = _select_purchase_order(erp_evidence, trigger, bulletin)
        production_order = _select_production_order(erp_evidence, trigger, purchase_order)
        context = ScenarioCContextBundle(
            actor=actor,
            attention=attention,
            trigger=trigger,
            bulletin=bulletin,
            purchase_order=purchase_order,
            production_order=production_order,
        )
        if dict(context.source_versions) != dict(trigger.source_versions):
            raise StaleScenarioCContextEvidenceError(
                "source versions changed after supplier-risk detection"
            )
        return context


def _require_attention_matches_trigger(
    attention: AttentionItem,
    trigger: ScenarioCSupplierRiskTrigger,
) -> None:
    """Prevent a durable attention item from being combined with another risk observation."""
    if (
        attention.scenario != trigger.scenario
        or attention.cause != trigger.cause
        or attention.dedupe_key != trigger.dedupe_key
    ):
        raise StaleScenarioCContextEvidenceError(
            "attention does not match the Scenario C supplier-risk detector signal"
        )
    if dict(attention.source_versions) != dict(trigger.source_versions):
        raise StaleScenarioCContextEvidenceError(
            "attention and trigger source versions do not match"
        )


def _select_bulletin(
    evidence: Sequence[Evidence], trigger: ScenarioCSupplierRiskTrigger
) -> Evidence:
    """Select the exact active bulletin without parsing its opaque body field."""
    bulletin = _single_record(evidence, "supplier_risk_bulletin", trigger.bulletin_id)
    _require_source(bulletin, "knowledge")
    _require_payload_value(bulletin, "status", "active")
    _require_payload_value(bulletin, "supplier_id", trigger.supplier_id)
    if bulletin.source_version != trigger.bulletin_version:
        raise StaleScenarioCContextEvidenceError(
            "supplier-risk bulletin version changed after detection"
        )
    return bulletin


def _select_purchase_order(
    evidence: Sequence[Evidence],
    trigger: ScenarioCSupplierRiskTrigger,
    bulletin: Evidence,
) -> Evidence:
    """Select the exact still-open PO and preserve its supplier/plant relationship."""
    purchase_order = _single_record(evidence, "purchase_order", trigger.purchase_order_id)
    _require_source(purchase_order, "erp")
    _require_payload_value(purchase_order, "status", "open")
    _require_payload_value(purchase_order, "supplier_id", trigger.supplier_id)
    _require_payload_value(
        purchase_order,
        "plant_id",
        _payload_value(bulletin, "plant_id"),
    )
    _require_payload_value(purchase_order, "part_id", trigger.part_id)
    if purchase_order.source_version != trigger.purchase_order_version:
        raise StaleScenarioCContextEvidenceError("purchase-order version changed after detection")
    return purchase_order


def _select_production_order(
    evidence: Sequence[Evidence],
    trigger: ScenarioCSupplierRiskTrigger,
    purchase_order: Evidence,
) -> Evidence:
    """Select the exact current production demand corresponding to the affected PO material."""
    production_order = _single_record(evidence, "production_order", trigger.production_order_id)
    _require_source(production_order, "erp")
    if _payload_value(production_order, "status").lower() not in COMMITTED_PRODUCTION_STATUSES:
        raise StaleScenarioCContextEvidenceError("production order is no longer committed")
    _require_payload_value(production_order, "part_id", trigger.part_id)
    _require_payload_value(
        production_order,
        "plant_id",
        _payload_value(purchase_order, "plant_id"),
    )
    production_start_date = _required_date(production_order, "start_date")
    if production_start_date != trigger.production_start_date:
        raise StaleScenarioCContextEvidenceError("production start date changed after detection")
    if production_start_date < trigger.detected_at.date():
        raise StaleScenarioCContextEvidenceError("production order is no longer future demand")
    if production_order.source_version != trigger.production_order_version:
        raise StaleScenarioCContextEvidenceError("production-order version changed after detection")
    return production_order


def _single_record(evidence: Sequence[Evidence], record_type: str, record_id: str) -> Evidence:
    """Find exactly one required source fact and reject ambiguity or omission."""
    matches = tuple(
        item for item in evidence if item.record_type == record_type and item.record_id == record_id
    )
    if len(matches) != 1:
        raise MissingScenarioCContextEvidenceError(
            f"expected exactly one {record_type} record with ID {record_id}"
        )
    return matches[0]


def _require_source(record: Evidence, expected_source: str) -> None:
    """Ensure a context fact came from the source boundary that owns its record type."""
    if record.source != expected_source:
        raise MissingScenarioCContextEvidenceError(
            f"{record.record_type} evidence must be supplied by the {expected_source} provider"
        )


def _payload_value(record: Evidence, name: str) -> str:
    """Read one non-empty structured field without parsing untrusted bulletin prose."""
    value = str(record.payload.get(name, "")).strip()
    if not value:
        raise MissingScenarioCContextEvidenceError(
            f"{record.record_type} evidence lacks required field: {name}"
        )
    return value


def _require_payload_value(record: Evidence, name: str, expected: str) -> None:
    """Reject a current record whose structured relationship no longer matches the trigger."""
    if _payload_value(record, name).lower() != expected.lower():
        raise StaleScenarioCContextEvidenceError(
            f"{record.record_type} evidence no longer matches required field: {name}"
        )


def _required_date(record: Evidence, name: str) -> date:
    """Read a typed production date without accepting ambiguous text date formats."""
    value = record.payload.get(name)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise MissingScenarioCContextEvidenceError(
        f"{record.record_type} evidence lacks valid date field: {name}"
    )
