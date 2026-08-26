"""Typed Scenario B context assembly over the quality authorization boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from enterprise_agent.domain import (
    ActorContext,
    AttentionItem,
    Evidence,
    RunId,
    ScenarioBQualityHoldTrigger,
    UserId,
)
from enterprise_agent.ports import AuditPort, EvidenceQuery, IdentityPort, QualityPort

from .audit_trail import append_material_audit_event

SCENARIO_B_QUALITY_RECORD_TYPES = frozenset(
    {"quality_lot", "production_allocation", "production_impact"}
)


class MissingScenarioBContextEvidenceError(ValueError):
    """Raised when authorized quality data cannot support a bounded Scenario B proposal."""


class StaleScenarioBContextEvidenceError(ValueError):
    """Raised when a context combines current facts with an older held-allocation signal."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ScenarioBContextBundle:
    """Planner-facing Scenario B facts, restricted to one quality actor and source snapshot."""

    actor: ActorContext
    attention: AttentionItem
    trigger: ScenarioBQualityHoldTrigger
    held_lot: Evidence
    production_allocation: Evidence
    production_impact: Evidence
    alternative_lots: tuple[Evidence, ...]
    production_supervisor_id: UserId
    production_supervisor_email: str

    def __post_init__(self) -> None:
        evidence_ids = tuple(str(item.evidence_id) for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Scenario B context evidence IDs must be unique")
        if not self.production_supervisor_email.strip():
            raise ValueError("production supervisor email must not be blank")

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        """Return planner-visible quality facts in a stable provenance-preserving order."""
        return (
            self.held_lot,
            self.production_allocation,
            self.production_impact,
            *self.alternative_lots,
        )

    @property
    def source_versions(self) -> Mapping[str, int]:
        """Expose every planner-visible current source binding as an immutable mapping."""
        return MappingProxyType(
            {str(item.evidence_id): item.source_version for item in self.evidence}
        )


class ScenarioBContextAssembler:
    """Compose only authorized quality facts needed to propose a held-lot response."""

    def __init__(
        self,
        identity: IdentityPort,
        quality: QualityPort,
        *,
        audit: AuditPort | None = None,
    ) -> None:
        """Depend on the quality port rather than an unscoped ERP or users-table query."""
        self._identity = identity
        self._quality = quality
        self._audit = audit

    def assemble(
        self,
        *,
        user_id: UserId,
        attention: AttentionItem,
        trigger: ScenarioBQualityHoldTrigger,
        run_id: RunId | None = None,
    ) -> ScenarioBContextBundle:
        """Return current authorized evidence or fail before a planner receives incomplete facts."""
        _require_attention_matches_trigger(attention, trigger)
        actor = self._identity.actor_for(user_id)
        evidence = self._quality.query(
            actor,
            EvidenceQuery(record_types=SCENARIO_B_QUALITY_RECORD_TYPES),
        )
        held_lot = _select_held_lot(evidence, trigger)
        production_allocation = _select_allocation(evidence, trigger, held_lot)
        production_impact = _select_production_impact(evidence, trigger, held_lot)
        supervisor_id, supervisor_email = _select_production_supervisor(production_impact)
        context = ScenarioBContextBundle(
            actor=actor,
            attention=attention,
            trigger=trigger,
            held_lot=held_lot,
            production_allocation=production_allocation,
            production_impact=production_impact,
            alternative_lots=_select_alternative_lots(evidence, held_lot),
            production_supervisor_id=supervisor_id,
            production_supervisor_email=supervisor_email,
        )
        if self._audit is not None and run_id is not None:
            _record_context_audit(self._audit, context, run_id)
        return context


def _require_attention_matches_trigger(
    attention: AttentionItem, trigger: ScenarioBQualityHoldTrigger
) -> None:
    """Keep durable attention from being combined with another quality-hold observation."""
    if (
        attention.scenario != trigger.scenario
        or attention.cause != trigger.cause
        or attention.dedupe_key != trigger.dedupe_key
    ):
        raise StaleScenarioBContextEvidenceError(
            "attention does not match the Scenario B quality-hold detector signal"
        )
    if dict(attention.source_versions) != dict(trigger.source_versions):
        raise StaleScenarioBContextEvidenceError(
            "attention and trigger source versions do not match"
        )


def _select_held_lot(
    evidence: Sequence[Evidence], trigger: ScenarioBQualityHoldTrigger
) -> Evidence:
    """Select the exact current held lot that caused the detector observation."""
    held_lot = _single_record(evidence, "quality_lot", trigger.quality_lot_id)
    _require_payload_value(held_lot, "part_id", trigger.part_id)
    _require_payload_value(held_lot, "status", "held")
    if held_lot.source_version != trigger.quality_lot_version:
        raise StaleScenarioBContextEvidenceError("held lot source version changed after detection")
    return held_lot


def _select_allocation(
    evidence: Sequence[Evidence],
    trigger: ScenarioBQualityHoldTrigger,
    held_lot: Evidence,
) -> Evidence:
    """Select the exact current allocation that binds the held lot to impacted production."""
    allocation = _single_record(
        evidence,
        "production_allocation",
        trigger.production_allocation_id,
    )
    _require_payload_value(allocation, "quality_lot_id", held_lot.record_id)
    _require_payload_value(allocation, "production_order_id", trigger.production_order_id)
    _require_positive_quantity(allocation, "allocated_quantity")
    if allocation.source_version != trigger.production_allocation_version:
        raise StaleScenarioBContextEvidenceError(
            "production allocation source version changed after detection"
        )
    return allocation


def _select_production_impact(
    evidence: Sequence[Evidence],
    trigger: ScenarioBQualityHoldTrigger,
    held_lot: Evidence,
) -> Evidence:
    """Select the specific production impact and ensure it still belongs to the held material."""
    impact = _single_record(evidence, "production_impact", trigger.production_order_id)
    _require_payload_value(impact, "part_id", _payload_value(held_lot, "part_id"))
    if impact.source_version != trigger.production_order_version:
        raise StaleScenarioBContextEvidenceError(
            "production-impact source version changed after detection"
        )
    return impact


def _select_alternative_lots(
    evidence: Sequence[Evidence], held_lot: Evidence
) -> tuple[Evidence, ...]:
    """Retain only available released lots for the same material and permitted plant."""
    part_id = _payload_value(held_lot, "part_id")
    plant_id = _payload_value(held_lot, "plant_id")
    return tuple(
        sorted(
            (
                item
                for item in evidence
                if item.record_type == "quality_lot"
                and item.record_id != held_lot.record_id
                and _payload_value(item, "part_id") == part_id
                and _payload_value(item, "plant_id") == plant_id
                and _payload_value(item, "status").lower() == "released"
                and _available_quantity(item) > 0
            ),
            key=lambda item: item.record_id,
        )
    )


def _select_production_supervisor(production_impact: Evidence) -> tuple[UserId, str]:
    """Require the authorized recipient needed by the later production-notification proposal."""
    try:
        supervisor_id = _payload_value(production_impact, "supervisor_id")
        supervisor_email = _payload_value(production_impact, "supervisor_email")
    except MissingScenarioBContextEvidenceError as error:
        raise MissingScenarioBContextEvidenceError(
            "quality provider cannot name the production supervisor"
        ) from error
    return UserId(supervisor_id), supervisor_email


def _record_context_audit(
    audit: AuditPort,
    context: ScenarioBContextBundle,
    run_id: RunId,
) -> None:
    """Record the bounded evidence set after every quality read has completed successfully."""
    evidence_ids = tuple(item.evidence_id for item in context.evidence)
    for event_type, occurred_at in (
        ("context.gathered", context.trigger.detected_at + timedelta(microseconds=1)),
        ("evidence.observed", context.trigger.detected_at + timedelta(microseconds=2)),
    ):
        append_material_audit_event(
            audit,
            event_type=event_type,
            run_id=run_id,
            occurred_at=occurred_at,
            actor_id=context.actor.user_id,
            attention_id=context.attention.attention_id,
            evidence_ids=evidence_ids,
            payload={"evidence_count": len(evidence_ids)},
        )


def _single_record(evidence: Sequence[Evidence], record_type: str, record_id: str) -> Evidence:
    """Find exactly one required evidence item without accepting ambiguity or omission."""
    matches = tuple(
        item for item in evidence if item.record_type == record_type and item.record_id == record_id
    )
    if len(matches) != 1:
        raise MissingScenarioBContextEvidenceError(
            f"expected exactly one {record_type} record with ID {record_id}"
        )
    return matches[0]


def _payload_value(record: Evidence, name: str) -> str:
    """Read a required text payload value from an authorized provider fact."""
    value = str(record.payload.get(name, "")).strip()
    if not value:
        raise MissingScenarioBContextEvidenceError(
            f"{record.record_type} evidence lacks required field: {name}"
        )
    return value


def _require_payload_value(record: Evidence, name: str, expected: str) -> None:
    """Reject source facts that no longer correspond to the detected held-lot relationship."""
    if _payload_value(record, name).lower() != expected.lower():
        raise StaleScenarioBContextEvidenceError(
            f"{record.record_type} evidence no longer matches required field: {name}"
        )


def _require_positive_quantity(record: Evidence, name: str) -> None:
    """Reject a stale allocation that no longer has an actionable positive quantity."""
    try:
        quantity = Decimal(str(record.payload[name]))
    except (KeyError, InvalidOperation, ValueError) as error:
        raise MissingScenarioBContextEvidenceError(
            f"{record.record_type} evidence lacks valid quantity field: {name}"
        ) from error
    if quantity <= 0:
        raise StaleScenarioBContextEvidenceError(
            f"{record.record_type} evidence no longer has a positive {name}"
        )


def _available_quantity(record: Evidence, *, name: str = "quantity") -> Decimal:
    """Calculate a released lot's unallocated quantity from strict decimal provider fields."""
    try:
        quantity = Decimal(str(record.payload[name]))
        allocated = Decimal(str(record.payload.get("allocated_quantity", Decimal())))
    except (KeyError, InvalidOperation, ValueError) as error:
        raise MissingScenarioBContextEvidenceError(
            f"{record.record_type} evidence lacks valid quantity fields"
        ) from error
    return quantity - allocated
