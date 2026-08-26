"""Contracts for proactive Scenario B quality-hold detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionRegistration,
    AttentionStatus,
    AttentionTrigger,
    Evidence,
    EvidenceId,
    PlantId,
    RunId,
    Scope,
    UserId,
)
from enterprise_agent.ports import EvidenceQuery

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
QUINN = ActorContext(
    user_id=UserId("00000000-0000-0000-0000-000000000003"),
    role="quality_manager",
    scopes=frozenset({Scope("quality:lot:read")}),
    plant_ids=frozenset({PlantId("PLANT-CHI")}),
    backup_approver_id=None,
    approval_limits={"USD": Decimal("5000.00")},
)


def evidence(
    *,
    record_type: str,
    record_id: str,
    source_version: int,
    payload: dict[str, object],
) -> Evidence:
    """Build one versioned quality-provider fact for detector contracts."""
    return Evidence(
        evidence_id=EvidenceId(f"quality:{record_type}:{record_id}"),
        source="quality",
        record_type=record_type,
        record_id=record_id,
        source_version=source_version,
        observed_at=NOW,
        payload=payload,
    )


def quality_lot(
    *,
    record_id: str = "lot-held",
    status: str = "held",
    part_id: str = "part-quality",
    plant_id: str = "PLANT-CHI",
) -> Evidence:
    """Build a lot whose status determines whether it enters the quality detector."""
    return evidence(
        record_type="quality_lot",
        record_id=record_id,
        source_version=3,
        payload={
            "lot_number": record_id.upper(),
            "part_id": part_id,
            "plant_id": plant_id,
            "quantity": Decimal(80),
            "status": status,
            "allocated_quantity": Decimal(80),
            "production_order_id": "production-q7001",
        },
    )


def allocation(
    *,
    record_id: str = "allocation-held",
    quality_lot_id: str = "lot-held",
    production_order_id: str = "production-q7001",
    quantity: str = "80",
) -> Evidence:
    """Build the allocation that proves a held lot will be consumed by production."""
    return evidence(
        record_type="production_allocation",
        record_id=record_id,
        source_version=3,
        payload={
            "quality_lot_id": quality_lot_id,
            "production_order_id": production_order_id,
            "allocated_quantity": Decimal(quantity),
        },
    )


def production_impact(
    *,
    record_id: str = "production-q7001",
    start_date: date = date(2026, 8, 27),
    status: str = "scheduled",
) -> Evidence:
    """Build the authorized impact projection for one production order."""
    return evidence(
        record_type="production_impact",
        record_id=record_id,
        source_version=1,
        payload={
            "part_id": "part-quality",
            "plant_id": "PLANT-CHI",
            "required_quantity": Decimal(80),
            "start_date": start_date,
            "status": status,
            "supervisor_id": "00000000-0000-0000-0000-000000000004",
            "supervisor_email": "priya.production@example.com",
        },
    )


@dataclass
class RecordingQualityProvider:
    """Return fixed quality evidence while retaining the exact provider request."""

    records: tuple[Evidence, ...]
    queries: list[EvidenceQuery] = field(default_factory=list)

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        """Return only the controlled facts assigned to the resolved quality actor."""
        assert actor == QUINN
        self.queries.append(query)
        return self.records


@dataclass(frozen=True)
class FixedClock:
    """Expose one deterministic business instant without consulting wall time."""

    current: datetime

    def now(self) -> datetime:
        """Return the injected scheduled-detector instant."""
        return self.current


@dataclass
class RecordingAttention:
    """Capture quality triggers and emulate one durable open attention registration."""

    triggers: list[AttentionTrigger] = field(default_factory=list)

    def register(self, trigger: AttentionTrigger, run_id: RunId) -> AttentionRegistration:
        """Record the detector result without requiring a concrete persistence adapter."""
        del run_id
        self.triggers.append(trigger)
        return AttentionRegistration(
            attention=AttentionItem(
                attention_id=AttentionId(f"attention-quality-{len(self.triggers)}"),
                scenario="scenario_b",
                cause="quality_hold",
                dedupe_key=trigger.dedupe_key,
                status=AttentionStatus.OPEN,
                created_at=NOW,
                source_versions=trigger.source_versions,
            ),
            created=True,
        )

    def transition(
        self,
        attention: AttentionItem,
        target: AttentionStatus,
        run_id: RunId,
        occurred_at: datetime,
    ) -> AttentionItem:
        """Satisfy the attention port; detector tests do not mutate registered state."""
        del target, run_id, occurred_at
        return attention


def test_detector_registers_a_held_lot_allocated_within_the_three_day_horizon() -> None:
    """The quality agent turns one current held allocation into source-bound durable attention."""
    from enterprise_agent.application.quality_hold import QualityHoldDetector

    quality = RecordingQualityProvider(
        (
            quality_lot(),
            quality_lot(record_id="lot-good", status="released"),
            allocation(),
            production_impact(),
        )
    )
    attention = RecordingAttention()

    detections = QualityHoldDetector(quality, attention, FixedClock(NOW)).detect(
        QUINN, RunId("run-quality-held")
    )

    assert len(detections) == 1
    assert detections[0].risk.quality_lot_id == "lot-held"
    assert detections[0].risk.production_allocation_id == "allocation-held"
    assert detections[0].risk.production_order_id == "production-q7001"
    assert detections[0].risk.days_until_consumption == 3
    assert detections[0].risk.allocated_quantity == Decimal(80)
    assert attention.triggers[0].source_versions == {
        "production_allocation:allocation-held": 3,
        "production_impact:production-q7001": 1,
        "quality_lot:lot-held": 3,
    }
    assert quality.queries == [
        EvidenceQuery(
            record_types=frozenset({"quality_lot", "production_allocation", "production_impact"})
        )
    ]


def test_detector_ignores_released_unallocated_and_out_of_horizon_lots() -> None:
    """Only a positive allocation on a held lot whose production is imminent becomes attention."""
    from enterprise_agent.application.quality_hold import QualityHoldDetector

    quality = RecordingQualityProvider(
        (
            quality_lot(record_id="lot-released", status="released"),
            allocation(record_id="allocation-released", quality_lot_id="lot-released"),
            production_impact(),
            quality_lot(record_id="lot-unallocated"),
            quality_lot(record_id="lot-late"),
            allocation(
                record_id="allocation-late",
                quality_lot_id="lot-late",
                production_order_id="production-late",
            ),
            production_impact(
                record_id="production-late",
                start_date=NOW.date() + timedelta(days=4),
            ),
        )
    )

    detections = QualityHoldDetector(quality, RecordingAttention(), FixedClock(NOW)).detect(
        QUINN, RunId("run-quality-filtered")
    )

    assert detections == ()
