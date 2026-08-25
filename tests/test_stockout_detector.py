"""Contracts for proactive Scenario A stockout detection."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import pytest

from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionRegistration,
    AttentionStatus,
    Evidence,
    EvidenceId,
    PlantId,
    RunId,
    Scope,
)
from enterprise_agent.ports import EvidenceQuery

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
ACTOR = ActorContext(
    user_id=cast("UserId", "00000000-0000-0000-0000-000000000001"),
    role="purchasing_manager",
    scopes=frozenset({Scope("erp:read")}),
    plant_ids=frozenset({PlantId("PLANT-CHI")}),
    backup_approver_id=None,
    approval_limits={"USD": Decimal("10000.00")},
)


def inventory(
    *,
    record_id: str = "inventory-x",
    part_id: str = "part-x",
    available: str = "100",
    safety_stock: str = "20",
    source_version: int = 4,
) -> Evidence:
    """Build one authorized inventory snapshot for detector tests."""
    return Evidence(
        evidence_id=EvidenceId(f"erp:inventory:{record_id}"),
        source="erp",
        record_type="inventory",
        record_id=record_id,
        source_version=source_version,
        observed_at=NOW,
        payload={
            "part_id": part_id,
            "available_quantity": Decimal(available),
            "safety_stock_quantity": Decimal(safety_stock),
        },
    )


def production_order(
    *,
    record_id: str,
    part_id: str = "part-x",
    required_quantity: str,
    start_date: date,
    status: str = "scheduled",
    source_version: int = 1,
) -> Evidence:
    """Build one authorized production-demand snapshot for detector tests."""
    return Evidence(
        evidence_id=EvidenceId(f"erp:production_order:{record_id}"),
        source="erp",
        record_type="production_order",
        record_id=record_id,
        source_version=source_version,
        observed_at=NOW,
        payload={
            "part_id": part_id,
            "required_quantity": Decimal(required_quantity),
            "start_date": start_date,
            "status": status,
        },
    )


@dataclass
class FakeErp:
    """Return only the fixed scoped evidence used by this detector contract."""

    evidence: tuple[Evidence, ...]
    queries: list[EvidenceQuery] = field(default_factory=list)

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        """Record the narrow read request without emulating a database."""
        del actor
        self.queries.append(query)
        return self.evidence


@dataclass
class RecordingAttention:
    """Capture emitted triggers while returning a minimal durable registration result."""

    triggers: list[object] = field(default_factory=list)

    def register(self, trigger: object, run_id: RunId) -> AttentionRegistration:
        """Record a trigger and return its open attention representation."""
        del run_id
        self.triggers.append(trigger)
        return AttentionRegistration(
            attention=AttentionItem(
                attention_id=AttentionId(f"attention-{len(self.triggers)}"),
                scenario="scenario_a",
                cause="projected_stockout",
                dedupe_key=cast("str", trigger.dedupe_key),
                status=AttentionStatus.OPEN,
                created_at=NOW,
                source_versions=cast("dict[str, int]", trigger.source_versions),
            ),
            created=True,
        )


def test_detector_includes_safety_stock_and_all_committed_demand_before_start() -> None:
    """A target order is at risk when on-hand stock cannot cover safety and committed demand."""
    from enterprise_agent.application.stockout import StockoutDetector

    erp = FakeErp(
        (
            inventory(available="100", safety_stock="20"),
            production_order(
                record_id="production-earlier",
                required_quantity="40",
                start_date=date(2026, 8, 26),
            ),
            production_order(
                record_id="production-4812",
                required_quantity="50",
                start_date=date(2026, 8, 27),
            ),
        )
    )
    attention = RecordingAttention()

    detections = StockoutDetector(erp, attention).detect(ACTOR, RunId("run-stockout"), NOW)

    assert len(detections) == 1
    assert detections[0].risk.production_order_id == "production-4812"
    assert detections[0].risk.committed_demand == Decimal("90")
    assert detections[0].risk.projected_available == Decimal("-10")
    assert detections[0].risk.shortfall == Decimal("10")
    assert attention.triggers[0].source_versions == {
        "inventory:inventory-x": 4,
        "production_order:production-earlier": 1,
        "production_order:production-4812": 1,
    }
    assert erp.queries == [EvidenceQuery(record_types=frozenset({"inventory", "production_order"}))]


def test_detector_emits_no_attention_when_projected_available_is_not_negative() -> None:
    """No detector signal becomes durable work unless the calculated shortfall is positive."""
    from enterprise_agent.application.stockout import StockoutDetector

    erp = FakeErp(
        (
            inventory(available="200", safety_stock="20"),
            production_order(
                record_id="production-4812",
                required_quantity="100",
                start_date=date(2026, 8, 27),
            ),
        )
    )
    attention = RecordingAttention()

    detections = StockoutDetector(erp, attention).detect(ACTOR, RunId("run-no-risk"), NOW)

    assert detections == ()
    assert attention.triggers == []


def test_detector_excludes_past_cancelled_and_other_part_demand() -> None:
    """Only current committed demand for the inventory part affects its risk calculation."""
    from enterprise_agent.application.stockout import StockoutDetector

    erp = FakeErp(
        (
            inventory(available="90", safety_stock="10"),
            production_order(
                record_id="production-target",
                required_quantity="50",
                start_date=date(2026, 8, 27),
            ),
            production_order(
                record_id="production-cancelled",
                required_quantity="100",
                start_date=date(2026, 8, 26),
                status="cancelled",
            ),
            production_order(
                record_id="production-past",
                required_quantity="100",
                start_date=date(2026, 8, 23),
            ),
            production_order(
                record_id="production-other-part",
                part_id="part-y",
                required_quantity="100",
                start_date=date(2026, 8, 26),
            ),
        )
    )
    attention = RecordingAttention()

    detections = StockoutDetector(erp, attention).detect(ACTOR, RunId("run-filtered"), NOW)

    assert detections == ()
    assert attention.triggers == []


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and expose diagnostics if it fails."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.critical
@pytest.mark.integration
def test_seeded_detector_emits_one_stockout_attention_from_scoped_erp_evidence(
    disposable_database: str,
) -> None:
    """The seeded Scenario A creates one real attention item with its 90-unit shortfall."""
    compose(
        "--profile",
        "tools",
        "run",
        "--build",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "alembic",
        "upgrade",
        "head",
    )
    command = (
        "from datetime import UTC, datetime\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        ")\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.domain import RunId, UserId\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "actor = PostgresIdentityAdapter(database_url).actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "detections = StockoutDetector(PostgresErpAdapter(database_url), PostgresAttentionAdapter(database_url)).detect(actor, RunId('run-seeded-stockout'), datetime(2026, 8, 24, 9, tzinfo=UTC))\n"
        "assert len(detections) == 1\n"
        "assert detections[0].risk.shortfall == 90\n"
        "assert detections[0].registration.created is True\n"
        "with create_engine(database_url).connect() as connection:\n"
        "    row = connection.execute(text('SELECT COUNT(*), MIN(status) FROM attention_items')).one()\n"
        "assert row == (1, 'open')\n"
    )
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "python",
        "-c",
        command,
    )
