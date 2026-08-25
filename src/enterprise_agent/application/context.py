"""Typed Scenario A context assembly over authorization-enforcing provider ports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from enterprise_agent.domain import (
    ActorContext,
    AttentionItem,
    DateRange,
    Evidence,
    ScenarioAStockoutTrigger,
    UserId,
)
from enterprise_agent.ports import (
    CalendarPort,
    ErpPort,
    EvidenceQuery,
    IdentityPort,
    MailPort,
)


class MissingScenarioAContextEvidenceError(ValueError):
    """Raised when authorized providers cannot prove every fact needed to plan Scenario A."""


class StaleAttentionEvidenceError(ValueError):
    """Raised when durable attention no longer binds the detector's source snapshot."""


ACTIVE_OUTSTANDING_PURCHASE_ORDER_STATUSES = frozenset({"delayed", "open", "partial"})
SCENARIO_A_ERP_RECORD_TYPES = frozenset(
    {"inventory", "production_order", "purchase_order", "supplier"}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizedContextBundle:
    """Planner-facing Scenario A facts, restricted to one actor and source-bound evidence."""

    actor: ActorContext
    attention: AttentionItem
    trigger: ScenarioAStockoutTrigger
    inventory: Evidence
    production_order: Evidence
    original_purchase_order: Evidence
    suppliers: tuple[Evidence, ...]
    shipment_update: Evidence
    calendar_events: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        """Reject ambiguous evidence identities instead of losing provenance in a mapping."""
        evidence_ids = tuple(str(item.evidence_id) for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("authorized context evidence IDs must be unique")

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        """Return every planner-visible fact in a deterministic provenance-preserving order."""
        return (
            self.inventory,
            self.production_order,
            self.original_purchase_order,
            *self.suppliers,
            self.shipment_update,
            *self.calendar_events,
        )

    @property
    def source_versions(self) -> Mapping[str, int]:
        """Expose immutable source-version bindings for every planner-visible fact."""
        return MappingProxyType(
            {str(item.evidence_id): item.source_version for item in self.evidence}
        )

    @property
    def source_observed_at(self) -> Mapping[str, datetime]:
        """Expose immutable source timestamps without allowing mutation of provider evidence."""
        return MappingProxyType({str(item.evidence_id): item.observed_at for item in self.evidence})


class ScenarioAContextAssembler:
    """Compose separately authorized identity, ERP, mail, and calendar facts for Scenario A."""

    def __init__(
        self,
        identity: IdentityPort,
        erp: ErpPort,
        mail: MailPort,
        calendar: CalendarPort,
    ) -> None:
        """Depend only on provider ports so context assembly cannot bypass authorization SQL."""
        self._identity = identity
        self._erp = erp
        self._mail = mail
        self._calendar = calendar

    def assemble(
        self,
        *,
        user_id: UserId,
        attention: AttentionItem,
        trigger: ScenarioAStockoutTrigger,
    ) -> AuthorizedContextBundle:
        """Return current authorized facts or fail before a planner can receive incomplete truth."""
        _require_attention_matches_trigger(attention, trigger)
        actor = self._identity.actor_for(user_id)
        erp_evidence = self._erp.query(
            actor,
            EvidenceQuery(record_types=SCENARIO_A_ERP_RECORD_TYPES),
        )

        inventory = _select_inventory(erp_evidence, trigger)
        production_order = _select_production_order(erp_evidence, trigger)
        original_purchase_order = _select_original_purchase_order(erp_evidence, trigger.part_id)
        shipment_update = _select_current_shipment_update(
            self._mail.query(actor, EvidenceQuery(record_types=frozenset({"message"}))),
            original_purchase_order,
        )
        calendar_events = tuple(
            sorted(
                self._calendar.query(
                    actor,
                    EvidenceQuery(
                        record_types=frozenset({"calendar_event"}),
                        date_range=DateRange(
                            start=trigger.detected_at.date(),
                            end=trigger.detected_at.date() + timedelta(days=1),
                        ),
                    ),
                ),
                key=lambda item: (item.observed_at, item.record_id),
            )
        )

        return AuthorizedContextBundle(
            actor=actor,
            attention=attention,
            trigger=trigger,
            inventory=inventory,
            production_order=production_order,
            original_purchase_order=original_purchase_order,
            suppliers=tuple(
                sorted(
                    (item for item in erp_evidence if item.record_type == "supplier"),
                    key=lambda item: item.record_id,
                )
            ),
            shipment_update=shipment_update,
            calendar_events=calendar_events,
        )


def _require_attention_matches_trigger(
    attention: AttentionItem, trigger: ScenarioAStockoutTrigger
) -> None:
    """Keep durable attention from being combined with a different detector observation."""
    if attention.scenario != "scenario_a" or attention.dedupe_key != trigger.dedupe_key:
        raise StaleAttentionEvidenceError("attention does not match the Scenario A detector signal")
    if dict(attention.source_versions) != dict(trigger.source_versions):
        raise StaleAttentionEvidenceError("attention and trigger source versions do not match")


def _select_inventory(evidence: Sequence[Evidence], trigger: ScenarioAStockoutTrigger) -> Evidence:
    """Select the exact versioned inventory snapshot that caused the stockout signal."""
    record_id = _source_record_id(trigger.source_versions, "inventory")
    inventory = _single_record(evidence, "inventory", record_id)
    _require_payload_value(inventory, "part_id", trigger.part_id)
    if inventory.source_version != trigger.inventory_version:
        raise StaleAttentionEvidenceError("inventory source version changed after detection")
    return inventory


def _select_production_order(
    evidence: Sequence[Evidence], trigger: ScenarioAStockoutTrigger
) -> Evidence:
    """Select the exact production order and version that the detector marked at risk."""
    production_order = _single_record(evidence, "production_order", trigger.production_order_id)
    _require_payload_value(production_order, "part_id", trigger.part_id)
    expected_version = trigger.source_versions.get(
        f"production_order:{trigger.production_order_id}"
    )
    if expected_version is None or production_order.source_version != expected_version:
        raise StaleAttentionEvidenceError("production-order source version changed after detection")
    return production_order


def _select_original_purchase_order(evidence: Sequence[Evidence], part_id: str) -> Evidence:
    """Select exactly one current outstanding PO for the threatened part or fail closed."""
    candidates = tuple(
        item
        for item in evidence
        if item.record_type == "purchase_order"
        and _payload_value(item, "part_id") == part_id
        and _payload_value(item, "status").lower() in ACTIVE_OUTSTANDING_PURCHASE_ORDER_STATUSES
        and _outstanding_quantity(item) > 0
    )
    if len(candidates) != 1:
        raise MissingScenarioAContextEvidenceError(
            "expected exactly one active outstanding purchase order for the threatened part"
        )
    return candidates[0]


def _select_current_shipment_update(
    evidence: Sequence[Evidence], original_purchase_order: Evidence
) -> Evidence:
    """Choose the newest non-superseded structured update tied to the original PO and supplier."""
    supplier_id = _payload_value(original_purchase_order, "supplier_id")
    candidates = tuple(
        item
        for item in evidence
        if item.record_type == "message"
        and _payload_value(item, "purchase_order_id") == original_purchase_order.record_id
        and _payload_value(item, "supplier_id") == supplier_id
        and _is_current_shipment_update(item)
    )
    if not candidates:
        raise MissingScenarioAContextEvidenceError(
            "no current shipment update exists for the original purchase order"
        )
    return max(candidates, key=lambda item: (item.observed_at, item.record_id))


def _is_current_shipment_update(message: Evidence) -> bool:
    """Accept only a structured update with shipment status and no declared replacement."""
    details = message.payload.get("payload")
    if not isinstance(details, Mapping):
        return False
    shipment_status = str(details.get("shipment_status", "")).strip()
    return bool(shipment_status) and not details.get("superseded_by")


def _source_record_id(source_versions: Mapping[str, int], record_type: str) -> str:
    """Read exactly one typed record ID from the detector's immutable source-version map."""
    prefix = f"{record_type}:"
    record_ids = tuple(
        source.removeprefix(prefix) for source in source_versions if source.startswith(prefix)
    )
    if len(record_ids) != 1 or not record_ids[0]:
        raise StaleAttentionEvidenceError(
            f"detector signal must bind exactly one {record_type} source record"
        )
    return record_ids[0]


def _single_record(evidence: Sequence[Evidence], record_type: str, record_id: str) -> Evidence:
    """Find exactly one typed record without accepting missing or duplicate provider evidence."""
    matches = tuple(
        item for item in evidence if item.record_type == record_type and item.record_id == record_id
    )
    if len(matches) != 1:
        raise MissingScenarioAContextEvidenceError(
            f"expected exactly one {record_type} record with ID {record_id}"
        )
    return matches[0]


def _payload_value(record: Evidence, name: str) -> str:
    """Read a required non-empty identity attribute from typed provider evidence."""
    value = str(record.payload.get(name, "")).strip()
    if not value:
        raise MissingScenarioAContextEvidenceError(
            f"{record.record_type} evidence lacks required field: {name}"
        )
    return value


def _require_payload_value(record: Evidence, name: str, expected: str) -> None:
    """Ensure selected provider evidence still belongs to the detector's threatened part."""
    if _payload_value(record, name) != expected:
        raise StaleAttentionEvidenceError(
            f"{record.record_type} evidence no longer matches the detector's threatened part"
        )


def _outstanding_quantity(purchase_order: Evidence) -> Decimal:
    """Calculate open quantity while rejecting malformed monetary-style ERP quantity fields."""
    try:
        ordered = Decimal(str(purchase_order.payload["ordered_quantity"]))
        received = Decimal(str(purchase_order.payload["received_quantity"]))
    except (KeyError, InvalidOperation, ValueError) as error:
        raise MissingScenarioAContextEvidenceError(
            "purchase-order evidence lacks valid ordered and received quantities"
        ) from error
    return ordered - received
