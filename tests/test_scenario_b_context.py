"""Contracts for authorized Scenario B quality-hold context assembly."""

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
    AuditEvent,
    Evidence,
    EvidenceId,
    PlantId,
    RunId,
    ScenarioBQualityHoldTrigger,
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
    """Build one quality-provider fact for Scenario B context contracts."""
    return Evidence(
        evidence_id=EvidenceId(f"quality:{record_type}:{record_id}"),
        source="quality",
        record_type=record_type,
        record_id=record_id,
        source_version=source_version,
        observed_at=NOW,
        payload=payload,
    )


def held_lot(*, source_version: int = 3) -> Evidence:
    """Build the exact held lot bound by the detector trigger."""
    return evidence(
        record_type="quality_lot",
        record_id="lot-held",
        source_version=source_version,
        payload={
            "lot_number": "LOT-QUALITY-HELD",
            "part_id": "part-quality",
            "plant_id": "PLANT-CHI",
            "quantity": Decimal(80),
            "allocated_quantity": Decimal(80),
            "status": "held",
            "production_order_id": "production-q7001",
        },
    )


def released_lot(
    *,
    record_id: str = "lot-good",
    part_id: str = "part-quality",
    available_quantity: str = "120",
) -> Evidence:
    """Build one visible alternative lot, optionally a wrong-part filtering trap."""
    return evidence(
        record_type="quality_lot",
        record_id=record_id,
        source_version=1,
        payload={
            "lot_number": record_id.upper(),
            "part_id": part_id,
            "plant_id": "PLANT-CHI",
            "quantity": Decimal(available_quantity),
            "allocated_quantity": Decimal(0),
            "status": "released",
            "production_order_id": None,
        },
    )


def allocation() -> Evidence:
    """Build the current allocation that attaches the held lot to production Q-7001."""
    return evidence(
        record_type="production_allocation",
        record_id="allocation-held",
        source_version=3,
        payload={
            "quality_lot_id": "lot-held",
            "production_order_id": "production-q7001",
            "allocated_quantity": Decimal(80),
        },
    )


def production_impact(*, include_recipient: bool = True) -> Evidence:
    """Build the impact projection, including the permitted production-supervisor recipient."""
    payload: dict[str, object] = {
        "part_id": "part-quality",
        "plant_id": "PLANT-CHI",
        "required_quantity": Decimal(80),
        "start_date": date(2026, 8, 27),
        "status": "scheduled",
    }
    if include_recipient:
        payload.update(
            {
                "supervisor_id": "00000000-0000-0000-0000-000000000004",
                "supervisor_email": "priya.production@example.com",
            }
        )
    return evidence(
        record_type="production_impact",
        record_id="production-q7001",
        source_version=1,
        payload=payload,
    )


def trigger() -> ScenarioBQualityHoldTrigger:
    """Build one immutable signal that binds every material held-lot source version."""
    return ScenarioBQualityHoldTrigger(
        detector="quality_hold_detector:v1",
        part_id="part-quality",
        quality_lot_id="lot-held",
        quality_lot_version=3,
        production_allocation_id="allocation-held",
        production_allocation_version=3,
        production_order_id="production-q7001",
        production_order_version=1,
        production_start_date=date(2026, 8, 27),
        detected_at=NOW,
        source_versions={
            "quality_lot:lot-held": 3,
            "production_allocation:allocation-held": 3,
            "production_impact:production-q7001": 1,
        },
    )


def attention(signal: ScenarioBQualityHoldTrigger) -> AttentionItem:
    """Build the durable attention item registered by that exact quality-hold signal."""
    return AttentionItem(
        attention_id=AttentionId("attention-quality-held"),
        scenario="scenario_b",
        cause="quality_hold",
        dedupe_key=signal.dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions=signal.source_versions,
    )


@dataclass
class RecordingIdentity:
    """Resolve only the quality principal requested by context assembly."""

    requests: list[UserId] = field(default_factory=list)

    def actor_for(self, user_id: UserId) -> ActorContext:
        """Return Quinn's authoritative quality-only identity context."""
        self.requests.append(user_id)
        return QUINN


@dataclass
class RecordingQualityProvider:
    """Return fixed authorized quality facts and retain the requested record types."""

    records: tuple[Evidence, ...]
    queries: list[EvidenceQuery] = field(default_factory=list)

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        """Prove application code uses the resolved quality actor at the port boundary."""
        assert actor == QUINN
        self.queries.append(query)
        return self.records


@dataclass
class RecordingAudit:
    """Collect material context events without a database dependency."""

    events: list[AuditEvent] = field(default_factory=list)

    def append(self, event: AuditEvent) -> None:
        """Keep every event for direct context-audit assertions."""
        self.events.append(event)

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        """Satisfy the read side of the audit protocol for this isolated fake."""
        return tuple(event for event in self.events if event.run_id == run_id)


def test_context_selects_only_released_same_part_alternatives_and_the_authorized_recipient() -> (
    None
):
    """The planner receives bound held/allocation/impact evidence and no unrelated lot data."""
    from enterprise_agent.application.quality_context import ScenarioBContextAssembler

    signal = trigger()
    identity = RecordingIdentity()
    quality = RecordingQualityProvider(
        (
            held_lot(),
            released_lot(),
            released_lot(record_id="lot-noise", part_id="part-noise"),
            allocation(),
            production_impact(),
        )
    )
    audit = RecordingAudit()

    context = ScenarioBContextAssembler(identity, quality, audit=audit).assemble(
        user_id=QUINN.user_id,
        attention=attention(signal),
        trigger=signal,
        run_id=RunId("run-quality-context"),
    )

    assert context.held_lot.record_id == "lot-held"
    assert context.production_allocation.record_id == "allocation-held"
    assert context.production_impact.record_id == "production-q7001"
    assert [item.record_id for item in context.alternative_lots] == ["lot-good"]
    assert context.production_supervisor_id == UserId("00000000-0000-0000-0000-000000000004")
    assert context.production_supervisor_email == "priya.production@example.com"
    assert context.source_versions == {
        "quality:quality_lot:lot-held": 3,
        "quality:production_allocation:allocation-held": 3,
        "quality:production_impact:production-q7001": 1,
        "quality:quality_lot:lot-good": 1,
    }
    assert identity.requests == [QUINN.user_id]
    assert quality.queries == [
        EvidenceQuery(
            record_types=frozenset({"quality_lot", "production_allocation", "production_impact"})
        )
    ]
    assert [event.event_type for event in audit.events] == ["context.gathered", "evidence.observed"]
    assert all(event.run_id == RunId("run-quality-context") for event in audit.events)


def test_context_rejects_stale_held_lot_evidence_before_planning() -> None:
    """A later hold change cannot be combined with an attention item from an older signal."""
    from enterprise_agent.application.quality_context import (
        ScenarioBContextAssembler,
        StaleScenarioBContextEvidenceError,
    )

    signal = trigger()
    quality = RecordingQualityProvider(
        (held_lot(source_version=4), released_lot(), allocation(), production_impact())
    )

    with pytest.raises(StaleScenarioBContextEvidenceError, match="held lot source version"):
        ScenarioBContextAssembler(RecordingIdentity(), quality).assemble(
            user_id=QUINN.user_id,
            attention=attention(signal),
            trigger=signal,
        )


def test_context_fails_closed_when_the_quality_provider_cannot_name_a_supervisor() -> None:
    """The planner cannot propose a notification when the authorized recipient is absent."""
    from enterprise_agent.application.quality_context import (
        MissingScenarioBContextEvidenceError,
        ScenarioBContextAssembler,
    )

    signal = trigger()
    quality = RecordingQualityProvider(
        (held_lot(), released_lot(), allocation(), production_impact(include_recipient=False))
    )

    with pytest.raises(MissingScenarioBContextEvidenceError, match="production supervisor"):
        ScenarioBContextAssembler(RecordingIdentity(), quality).assemble(
            user_id=QUINN.user_id,
            attention=attention(signal),
            trigger=signal,
        )


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
def test_seeded_quality_detector_and_context_use_only_quality_scoped_evidence(
    disposable_database: str,
) -> None:
    """Both seeded held-lot paths retain only released alternatives and a named supervisor."""
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
        "from decimal import Decimal\n"
        "from os import environ\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresDemoClock,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresQualityAdapter,\n"
        ")\n"
        "from enterprise_agent.application.quality_context import ScenarioBContextAssembler\n"
        "from enterprise_agent.application.quality_hold import QualityHoldDetector\n"
        "from enterprise_agent.domain import RunId, UserId\n"
        "from enterprise_agent.seed import (\n"
        "    ID_LOT_GOOD,\n"
        "    ID_PRODUCTION_Q7001,\n"
        "    ID_PRODUCTION_Q7002,\n"
        "    reset_database,\n"
        "    seed_database,\n"
        ")\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "actor = identity.actor_for(UserId('00000000-0000-0000-0000-000000000003'))\n"
        "assert 'quality:lot:read' in actor.scopes\n"
        "assert 'erp:read' not in actor.scopes\n"
        "quality = PostgresQualityAdapter(database_url)\n"
        "detections = QualityHoldDetector(quality, PostgresAttentionAdapter(database_url), PostgresDemoClock(database_url)).detect(actor, RunId('run-seeded-quality-hold'))\n"
        "assert len(detections) == 2\n"
        "by_order = {detection.risk.production_order_id: detection for detection in detections}\n"
        "assert set(by_order) == {str(ID_PRODUCTION_Q7001), str(ID_PRODUCTION_Q7002)}\n"
        "assembler = ScenarioBContextAssembler(identity, quality)\n"
        "contexts = {\n"
        "    order_id: assembler.assemble(\n"
        "        user_id=actor.user_id,\n"
        "        attention=detection.registration.attention,\n"
        "        trigger=detection.risk.trigger,\n"
        "    )\n"
        "    for order_id, detection in by_order.items()\n"
        "}\n"
        "for context in contexts.values():\n"
        "    assert {item.source for item in context.evidence} == {'quality'}\n"
        "    assert [item.record_id for item in context.alternative_lots] == [str(ID_LOT_GOOD)]\n"
        "    assert context.production_supervisor_email == 'priya.production@example.com'\n"
        "good_quantity = Decimal(str(contexts[str(ID_PRODUCTION_Q7001)].alternative_lots[0].payload['quantity']))\n"
        "assert good_quantity >= by_order[str(ID_PRODUCTION_Q7001)].risk.allocated_quantity\n"
        "assert good_quantity < by_order[str(ID_PRODUCTION_Q7002)].risk.allocated_quantity\n"
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
