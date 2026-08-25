"""Deterministic, provider-neutral detection of Scenario A inventory shortfalls."""

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
    ScenarioAStockoutTrigger,
)
from enterprise_agent.ports import AttentionPort, ErpPort, EvidenceQuery

COMMITTED_PRODUCTION_STATUSES = frozenset({"in_progress", "scheduled"})


@dataclass(frozen=True, slots=True, kw_only=True)
class StockoutRisk:
    """The transparent calculation that justifies one Scenario A attention trigger."""

    part_id: str
    production_order_id: str
    inventory_record_id: str
    production_start_date: date
    available_quantity: Decimal
    safety_stock_quantity: Decimal
    committed_demand: Decimal
    projected_available: Decimal
    shortfall: Decimal
    trigger: ScenarioAStockoutTrigger


@dataclass(frozen=True, slots=True, kw_only=True)
class StockoutDetection:
    """One persisted attention registration paired with the calculation that created it."""

    risk: StockoutRisk
    registration: AttentionRegistration


@dataclass(frozen=True, slots=True, kw_only=True)
class _InventorySnapshot:
    """The one current inventory record for a visible part and plant."""

    evidence: Evidence
    part_id: str
    available_quantity: Decimal
    safety_stock_quantity: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class _ProductionDemand:
    """One current committed production-order demand record for a visible part."""

    evidence: Evidence
    part_id: str
    required_quantity: Decimal
    production_start_date: date


class StockoutDetector:
    """Read scoped ERP evidence, calculate material risk, and register only positive shortfalls."""

    def __init__(self, erp: ErpPort, attention: AttentionPort) -> None:
        """Compose provider and durable-attention boundaries without depending on their adapters."""
        self._erp = erp
        self._attention = attention

    def detect(
        self, actor: ActorContext, run_id: RunId, detected_at: datetime
    ) -> tuple[StockoutDetection, ...]:
        """Persist one attention item per current at-risk production order visible to the actor."""
        evidence = self._erp.query(
            actor,
            EvidenceQuery(record_types=frozenset({"inventory", "production_order"})),
        )
        return tuple(
            StockoutDetection(
                risk=risk,
                registration=self._attention.register(risk.trigger, run_id),
            )
            for risk in self.evaluate(evidence, detected_at)
        )

    def evaluate(
        self, evidence: Sequence[Evidence], detected_at: datetime
    ) -> tuple[StockoutRisk, ...]:
        """Return transparent risk calculations without writing any application state."""
        inventories = _inventory_by_part(evidence)
        demands_by_part = _committed_demands_by_part(evidence, detected_at.date())
        risks: list[StockoutRisk] = []

        for part_id, demands in demands_by_part.items():
            inventory = inventories.get(part_id)
            if inventory is None:
                continue
            for target in demands:
                committed_demands = tuple(
                    demand
                    for demand in demands
                    if demand.production_start_date <= target.production_start_date
                )
                committed_demand = sum(
                    (demand.required_quantity for demand in committed_demands), start=Decimal()
                )
                projected_available = (
                    inventory.available_quantity
                    - inventory.safety_stock_quantity
                    - committed_demand
                )
                shortfall = max(Decimal(), -projected_available)
                if shortfall <= 0:
                    continue

                source_versions = {
                    f"inventory:{inventory.evidence.record_id}": inventory.evidence.source_version,
                    **{
                        f"production_order:{demand.evidence.record_id}": demand.evidence.source_version
                        for demand in committed_demands
                    },
                }
                trigger = ScenarioAStockoutTrigger(
                    detector="stockout_detector:v1",
                    part_id=part_id,
                    production_order_id=target.evidence.record_id,
                    inventory_version=inventory.evidence.source_version,
                    production_start_date=target.production_start_date,
                    detected_at=detected_at,
                    source_versions=source_versions,
                )
                risks.append(
                    StockoutRisk(
                        part_id=part_id,
                        production_order_id=target.evidence.record_id,
                        inventory_record_id=inventory.evidence.record_id,
                        production_start_date=target.production_start_date,
                        available_quantity=inventory.available_quantity,
                        safety_stock_quantity=inventory.safety_stock_quantity,
                        committed_demand=committed_demand,
                        projected_available=projected_available,
                        shortfall=shortfall,
                        trigger=trigger,
                    )
                )
        return tuple(risks)


def _inventory_by_part(evidence: Sequence[Evidence]) -> dict[str, _InventorySnapshot]:
    """Index the one scoped inventory snapshot for every part represented in the evidence."""
    inventories: dict[str, _InventorySnapshot] = {}
    for record in evidence:
        if record.record_type != "inventory":
            continue
        part_id = _required_identifier(record.payload, "part_id")
        if part_id in inventories:
            raise ValueError(f"multiple inventory records supplied for part {part_id}")
        inventories[part_id] = _InventorySnapshot(
            evidence=record,
            part_id=part_id,
            available_quantity=_required_decimal(record.payload, "available_quantity"),
            safety_stock_quantity=_required_decimal(record.payload, "safety_stock_quantity"),
        )
    return inventories


def _committed_demands_by_part(
    evidence: Sequence[Evidence], as_of_date: date
) -> dict[str, tuple[_ProductionDemand, ...]]:
    """Group only current, committed production demand by its required part."""
    demands: defaultdict[str, list[_ProductionDemand]] = defaultdict(list)
    for record in evidence:
        if record.record_type != "production_order":
            continue
        status = _required_identifier(record.payload, "status").lower()
        production_start_date = _required_date(record.payload, "start_date")
        if status not in COMMITTED_PRODUCTION_STATUSES or production_start_date < as_of_date:
            continue
        demand = _ProductionDemand(
            evidence=record,
            part_id=_required_identifier(record.payload, "part_id"),
            required_quantity=_required_decimal(record.payload, "required_quantity"),
            production_start_date=production_start_date,
        )
        demands[demand.part_id].append(demand)
    return {
        part_id: tuple(
            sorted(
                part_demands,
                key=lambda demand: (demand.production_start_date, demand.evidence.record_id),
            )
        )
        for part_id, part_demands in demands.items()
    }


def _required_identifier(payload: Mapping[str, object], name: str) -> str:
    """Read a non-empty identifier or status string from trusted provider evidence."""
    value = str(payload.get(name, "")).strip()
    if not value:
        raise ValueError(f"missing required evidence field: {name}")
    return value


def _required_decimal(payload: Mapping[str, object], name: str) -> Decimal:
    """Read a non-negative amount without silently accepting malformed provider evidence."""
    try:
        value = Decimal(str(payload[name]))
    except (KeyError, InvalidOperation, ValueError) as error:
        raise ValueError(f"missing or invalid decimal evidence field: {name}") from error
    if value < 0:
        raise ValueError(f"decimal evidence field must be non-negative: {name}")
    return value


def _required_date(payload: Mapping[str, object], name: str) -> date:
    """Read one production date from typed provider evidence without string guessing."""
    value = payload.get(name)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError(f"missing or invalid date evidence field: {name}")
