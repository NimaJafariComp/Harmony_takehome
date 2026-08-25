"""Contracts for assembling Scenario A context from authorized provider evidence."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    DateRange,
    Evidence,
    EvidenceId,
    PlantId,
    ScenarioAStockoutTrigger,
    Scope,
    UserId,
)
from enterprise_agent.ports import EvidenceQuery

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
DANA = ActorContext(
    user_id=UserId("00000000-0000-0000-0000-000000000001"),
    role="purchasing_manager",
    scopes=frozenset({Scope("erp:read"), Scope("mail:read"), Scope("calendar:read")}),
    plant_ids=frozenset({PlantId("PLANT-CHI")}),
    backup_approver_id=UserId("00000000-0000-0000-0000-000000000002"),
    approval_limits={"USD": Decimal("10000.00")},
)


def evidence(
    *,
    source: str,
    record_type: str,
    record_id: str,
    source_version: int = 1,
    observed_at: datetime = NOW,
    payload: dict[str, object],
) -> Evidence:
    """Build one timestamped versioned provider fact for context-assembly contracts."""
    return Evidence(
        evidence_id=EvidenceId(f"{source}:{record_type}:{record_id}"),
        source=source,
        record_type=record_type,
        record_id=record_id,
        source_version=source_version,
        observed_at=observed_at,
        payload=payload,
    )


def scenario_trigger(*, inventory_version: int = 4) -> ScenarioAStockoutTrigger:
    """Build the stable Scenario A signal that binds a context to its risk evidence."""
    return ScenarioAStockoutTrigger(
        detector="stockout_detector:v1",
        part_id="part-x",
        production_order_id="production-4812",
        inventory_version=inventory_version,
        production_start_date=date(2026, 8, 27),
        detected_at=NOW,
        source_versions={
            "inventory:inventory-x": inventory_version,
            "production_order:production-4812": 1,
        },
    )


def attention(trigger: ScenarioAStockoutTrigger) -> AttentionItem:
    """Build the durable attention item produced by the exact supplied detector signal."""
    return AttentionItem(
        attention_id=AttentionId("attention-stockout"),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key=trigger.dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions=trigger.source_versions,
    )


def erp_records() -> tuple[Evidence, ...]:
    """Return the stockout facts, original PO, and visible supplier facts for Dana."""
    return (
        evidence(
            source="erp",
            record_type="inventory",
            record_id="inventory-x",
            source_version=4,
            payload={
                "part_id": "part-x",
                "plant_id": "PLANT-CHI",
                "available_quantity": Decimal(30),
                "safety_stock_quantity": Decimal(20),
            },
        ),
        evidence(
            source="erp",
            record_type="production_order",
            record_id="production-4812",
            payload={
                "part_id": "part-x",
                "start_date": date(2026, 8, 27),
                "status": "scheduled",
                "required_quantity": Decimal(100),
            },
        ),
        evidence(
            source="erp",
            record_type="purchase_order",
            record_id="po-4812-y",
            source_version=2,
            payload={
                "part_id": "part-x",
                "supplier_id": "supplier-y",
                "plant_id": "PLANT-CHI",
                "ordered_quantity": Decimal(100),
                "received_quantity": Decimal(40),
                "status": "delayed",
            },
        ),
        evidence(
            source="erp",
            record_type="supplier",
            record_id="supplier-y",
            payload={
                "part_id": "part-x",
                "plant_id": "PLANT-CHI",
                "approved": True,
                "lead_time_days": 4,
            },
        ),
        evidence(
            source="erp",
            record_type="supplier",
            record_id="supplier-w",
            payload={
                "part_id": "part-noise",
                "plant_id": "PLANT-CHI",
                "approved": True,
                "lead_time_days": 1,
            },
        ),
    )


def shipment_update(
    *,
    record_id: str,
    received_at: datetime,
    details: dict[str, object],
    purchase_order_id: str = "po-4812-y",
    supplier_id: str = "supplier-y",
) -> Evidence:
    """Build a mailbox-visible supplier shipment update with its nested provider payload."""
    return evidence(
        source="mail",
        record_type="message",
        record_id=record_id,
        observed_at=received_at,
        payload={
            "message_key": f"shipment-update-{record_id}",
            "purchase_order_id": purchase_order_id,
            "supplier_id": supplier_id,
            "payload": details,
        },
    )


@dataclass
class RecordingProvider:
    """Return fixed authorized evidence and record the exact narrow query requested."""

    records: tuple[Evidence, ...]
    queries: list[EvidenceQuery] = field(default_factory=list)

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        """Accept the actor already resolved by the identity boundary without widening access."""
        assert actor == DANA
        self.queries.append(query)
        return self.records


@dataclass
class RecordingIdentity:
    """Resolve one authoritative actor context while recording its requested identity."""

    actor: ActorContext
    requests: list[UserId] = field(default_factory=list)

    def actor_for(self, user_id: UserId) -> ActorContext:
        """Return the provider-owned actor rather than trusting caller-provided scopes."""
        self.requests.append(user_id)
        return self.actor


def test_context_uses_authorized_evidence_and_newest_current_shipment_update() -> None:
    """The planner-facing bundle retains only current relevant facts and their provenance."""
    from enterprise_agent.application.context import ScenarioAContextAssembler

    trigger = scenario_trigger()
    identity = RecordingIdentity(DANA)
    erp = RecordingProvider(erp_records())
    old_message = shipment_update(
        record_id="old",
        received_at=datetime(2026, 8, 24, 8, tzinfo=UTC),
        details={"shipment_status": "delayed", "superseded_by": "shipment-update-new"},
    )
    current_message = shipment_update(
        record_id="new",
        received_at=NOW,
        details={"shipment_status": "delayed", "expected_receipt_date": "2026-08-25"},
    )
    mail = RecordingProvider((old_message, current_message))
    calendar_event = evidence(
        source="calendar",
        record_type="calendar_event",
        record_id="dana-ooo",
        observed_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
        payload={"event_type": "out_of_office"},
    )
    calendar = RecordingProvider((calendar_event,))

    context = ScenarioAContextAssembler(identity, erp, mail, calendar).assemble(
        user_id=DANA.user_id,
        attention=attention(trigger),
        trigger=trigger,
    )

    assert context.actor == DANA
    assert context.inventory.record_id == "inventory-x"
    assert context.production_order.record_id == "production-4812"
    assert context.original_purchase_order.record_id == "po-4812-y"
    assert context.shipment_update.record_id == "new"
    assert context.calendar_events == (calendar_event,)
    assert [supplier.record_id for supplier in context.suppliers] == ["supplier-w", "supplier-y"]
    assert {item.record_id for item in context.evidence} == {
        "inventory-x",
        "production-4812",
        "po-4812-y",
        "supplier-y",
        "supplier-w",
        "new",
        "dana-ooo",
    }
    assert context.source_versions["erp:purchase_order:po-4812-y"] == 2
    assert context.source_observed_at["mail:message:new"] == NOW
    assert identity.requests == [DANA.user_id]
    assert erp.queries == [
        EvidenceQuery(
            record_types=frozenset({"inventory", "production_order", "purchase_order", "supplier"})
        )
    ]
    assert mail.queries == [EvidenceQuery(record_types=frozenset({"message"}))]
    assert calendar.queries == [
        EvidenceQuery(
            record_types=frozenset({"calendar_event"}),
            date_range=DateRange(start=date(2026, 8, 24), end=date(2026, 8, 25)),
        )
    ]


def test_context_fails_closed_when_no_current_valid_shipment_update_exists() -> None:
    """A superseded or misattributed email cannot become operational supplier truth."""
    from enterprise_agent.application.context import (
        MissingScenarioAContextEvidenceError,
        ScenarioAContextAssembler,
    )

    trigger = scenario_trigger()
    only_superseded_message = shipment_update(
        record_id="old",
        received_at=NOW,
        details={"shipment_status": "delayed", "superseded_by": "shipment-update-new"},
    )
    misattributed_message = shipment_update(
        record_id="wrong-supplier",
        received_at=datetime(2026, 8, 24, 10, tzinfo=UTC),
        details={"shipment_status": "delayed"},
        supplier_id="supplier-z",
    )

    assembler = ScenarioAContextAssembler(
        RecordingIdentity(DANA),
        RecordingProvider(erp_records()),
        RecordingProvider((only_superseded_message, misattributed_message)),
        RecordingProvider(()),
    )

    with pytest.raises(MissingScenarioAContextEvidenceError, match="current shipment update"):
        assembler.assemble(
            user_id=DANA.user_id,
            attention=attention(trigger),
            trigger=trigger,
        )


def test_context_refuses_attention_with_different_source_versions_before_provider_reads() -> None:
    """A context cannot mix a durable attention item with a different detector snapshot."""
    from enterprise_agent.application.context import (
        ScenarioAContextAssembler,
        StaleAttentionEvidenceError,
    )

    trigger = scenario_trigger()
    mismatched_attention = AttentionItem(
        attention_id=AttentionId("attention-stale"),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key=trigger.dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions={
            "inventory:inventory-x": 5,
            "production_order:production-4812": 1,
        },
    )
    identity = RecordingIdentity(DANA)
    erp = RecordingProvider(erp_records())

    with pytest.raises(StaleAttentionEvidenceError, match="source versions"):
        ScenarioAContextAssembler(
            identity,
            erp,
            RecordingProvider(()),
            RecordingProvider(()),
        ).assemble(user_id=DANA.user_id, attention=mismatched_attention, trigger=trigger)

    assert identity.requests == []
    assert erp.queries == []


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
def test_seeded_context_selects_current_supplier_update_and_authorized_facts(
    disposable_database: str,
) -> None:
    """The seeded Scenario A yields only the current PO shipment evidence for planning."""
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
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresCalendarAdapter,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter,\n"
        ")\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.domain import RunId, UserId\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "actor = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "detection = StockoutDetector(PostgresErpAdapter(database_url), PostgresAttentionAdapter(database_url)).detect(actor, RunId('run-seeded-context'), datetime(2026, 8, 24, 9, tzinfo=UTC))[0]\n"
        "context = ScenarioAContextAssembler(identity, PostgresErpAdapter(database_url), PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "assert context.production_order.payload['order_number'] == '4812'\n"
        "assert context.original_purchase_order.payload['po_number'] == 'PO-4812-Y'\n"
        "assert context.shipment_update.payload['message_key'] == 'shipment-update-po-4812-y-v2'\n"
        "assert context.shipment_update.observed_at == datetime(2026, 8, 24, 9, tzinfo=UTC)\n"
        "assert {item.payload.get('message_key') for item in context.evidence if item.source == 'mail'} == {'shipment-update-po-4812-y-v2'}\n"
        "assert context.source_versions['erp:purchase_order:00000000-0000-0000-0000-000000000401'] == 2\n"
        "assert context.calendar_events[0].payload['event_type'] == 'out_of_office'\n"
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
