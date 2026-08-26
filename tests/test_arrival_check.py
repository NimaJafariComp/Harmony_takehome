"""Tuesday receipt-check contracts for durable Scenario A follow-up."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from enterprise_agent.application.arrival_check import (
    ArrivalCheckError,
    ArrivalCheckOutcome,
    TuesdayArrivalCheckService,
)
from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionRegistration,
    AttentionStatus,
    AuditEvent,
    Evidence,
    EvidenceId,
    RunId,
    ScheduledTask,
    ScheduledTaskId,
    ScheduledTaskStatus,
    Scope,
    UserId,
)
from enterprise_agent.ports import EvidenceQuery

MONDAY = datetime(2026, 8, 24, 9, tzinfo=UTC)
TUESDAY = MONDAY + timedelta(days=1)
DANA = UserId("00000000-0000-0000-0000-000000000001")
ORIGINAL_ATTENTION_ID = AttentionId("00000000-0000-0000-0000-000000000601")
REPLACEMENT_PURCHASE_ORDER_ID = "00000000-0000-0000-0000-000000000499"


def original_attention() -> AttentionItem:
    """Create the current stockout attention item whose replacement-arrival outcome is checked."""
    return AttentionItem(
        attention_id=ORIGINAL_ATTENTION_ID,
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key="scenario_a:stockout:v1:original",
        status=AttentionStatus.OPEN,
        created_at=MONDAY,
        source_versions={"inventory:inventory-x": 4},
    )


def purchase_order(
    *,
    received_quantity: str,
    ordered_quantity: str = "60",
    source_version: int = 3,
) -> Evidence:
    """Return the exact current ERP receipt evidence for the replacement purchase order."""
    return Evidence(
        evidence_id=EvidenceId("erp:purchase_order:replacement"),
        source="erp",
        record_type="purchase_order",
        record_id=REPLACEMENT_PURCHASE_ORDER_ID,
        source_version=source_version,
        observed_at=TUESDAY,
        payload={
            "ordered_quantity": ordered_quantity,
            "received_quantity": received_quantity,
            "status": "open",
        },
    )


def claimed_arrival_task(*, payload: dict[str, object] | None = None) -> ScheduledTask:
    """Build one valid, safely leased Tuesday arrival check with immutable causal binding."""
    return ScheduledTask(
        task_id=ScheduledTaskId("00000000-0000-0000-0000-000000000a01"),
        task_type="arrival_check",
        due_at=TUESDAY,
        status=ScheduledTaskStatus.CLAIMED,
        idempotency_key="arrival-check:replacement-po",
        payload=(
            {
                "purchase_order_id": REPLACEMENT_PURCHASE_ORDER_ID,
                "original_attention_id": str(ORIGINAL_ATTENTION_ID),
                "actor_id": str(DANA),
            }
            if payload is None
            else payload
        ),
        attempt_count=1,
        lease_expires_at=TUESDAY + timedelta(minutes=5),
        completed_at=None,
    )


def dana() -> ActorContext:
    """Build Dana's scope-limited actor identity for the ERP read that proves receipt state."""
    return ActorContext(
        user_id=DANA,
        role="purchasing_manager",
        scopes=frozenset({Scope("erp:read")}),
        plant_ids=frozenset(),
        backup_approver_id=None,
        approval_limits={},
    )


@dataclass
class RecordingIdentity:
    """Return only Dana's current identity for the arrival-check contracts."""

    def actor_for(self, user_id: UserId) -> ActorContext:
        assert user_id == DANA
        return dana()


@dataclass
class RecordingErp:
    """Return a fixed exact-PO evidence set and retain the provider-owned query boundary."""

    evidence: tuple[Evidence, ...]
    queries: list[tuple[ActorContext, EvidenceQuery]] = field(default_factory=list)

    def query(self, actor: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        self.queries.append((actor, query))
        return self.evidence


@dataclass
class RecordingAudit:
    """Collect the durable task's audit event without coupling the service to PostgreSQL."""

    events: list[AuditEvent]

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        return tuple(event for event in self.events if event.run_id == run_id)


@dataclass
class MemoryArrivalAttentionStore:
    """Persist lifecycle changes and deduplicate one source-version-specific follow-up in memory."""

    records: dict[AttentionId, AttentionItem]
    followups: dict[str, AttentionItem] = field(default_factory=dict)

    def load(self, attention_id: AttentionId) -> AttentionItem | None:
        return self.records.get(attention_id)

    def transition(
        self,
        attention: AttentionItem,
        target: AttentionStatus,
        run_id: RunId,
        occurred_at: datetime,
    ) -> AttentionItem:
        updated = replace(attention, status=target, resolved_at=occurred_at)
        self.records[attention.attention_id] = updated
        return updated

    def register_arrival_followup(
        self,
        *,
        original_attention: AttentionItem,
        purchase_order: Evidence,
        run_id: RunId,
        detected_at: datetime,
    ) -> AttentionRegistration:
        source_versions = {
            f"purchase_order:{purchase_order.record_id}": purchase_order.source_version
        }
        dedupe_key = (
            f"scenario_a:arrival_check:v1:{original_attention.attention_id}:"
            f"{purchase_order.record_id}:{purchase_order.source_version}"
        )
        existing = self.followups.get(dedupe_key)
        if existing is not None:
            return AttentionRegistration(attention=existing, created=False)
        followup = AttentionItem(
            attention_id=AttentionId("00000000-0000-0000-0000-000000000602"),
            scenario="scenario_a",
            cause="arrival_check",
            dedupe_key=dedupe_key,
            status=AttentionStatus.OPEN,
            created_at=detected_at,
            source_versions=source_versions,
        )
        self.records[followup.attention_id] = followup
        self.followups[dedupe_key] = followup
        return AttentionRegistration(attention=followup, created=True)


def arrival_service(
    evidence: tuple[Evidence, ...],
) -> tuple[TuesdayArrivalCheckService, MemoryArrivalAttentionStore, RecordingErp]:
    """Construct the Tuesday receipt checker and its bounded provider fakes."""

    attention = MemoryArrivalAttentionStore({ORIGINAL_ATTENTION_ID: original_attention()})
    erp = RecordingErp(evidence)
    return (
        TuesdayArrivalCheckService(erp=erp, identity=RecordingIdentity(), attention=attention),
        attention,
        erp,
    )


@pytest.mark.critical
def test_tuesday_full_receipt_resolves_the_original_attention() -> None:
    """A full explicit receipt satisfies the follow-up and closes only its causal attention item."""
    service, attention, erp = arrival_service((purchase_order(received_quantity="60"),))

    result = service.handle_claimed_task(
        claimed_arrival_task(),
        checked_at=TUESDAY,
        run_id=RunId("run-arrival-received"),
    )

    assert result.outcome is ArrivalCheckOutcome.RESOLVED
    assert result.attention is not None
    assert result.attention.attention_id == ORIGINAL_ATTENTION_ID
    assert result.attention.status is AttentionStatus.RESOLVED
    assert attention.followups == {}
    assert erp.queries == [
        (
            dana(),
            EvidenceQuery(
                record_types=frozenset({"purchase_order"}),
                record_ids=frozenset({REPLACEMENT_PURCHASE_ORDER_ID}),
            ),
        )
    ]


def test_tuesday_task_uses_its_durable_audit_run_correlation_when_fired() -> None:
    """A restarted worker needs no caller-memory run ID to preserve the scheduled audit story."""
    attention = MemoryArrivalAttentionStore({ORIGINAL_ATTENTION_ID: original_attention()})
    audit = RecordingAudit(events=[])
    run_id = RunId("run-arrival-task-audit")
    service = TuesdayArrivalCheckService(
        erp=RecordingErp((purchase_order(received_quantity="60"),)),
        identity=RecordingIdentity(),
        attention=attention,
        audit=audit,
    )
    task = claimed_arrival_task(
        payload={
            "purchase_order_id": REPLACEMENT_PURCHASE_ORDER_ID,
            "original_attention_id": str(ORIGINAL_ATTENTION_ID),
            "actor_id": str(DANA),
            "audit_run_id": str(run_id),
        }
    )

    result = service.handle_claimed_task(task, checked_at=TUESDAY)

    assert result.outcome is ArrivalCheckOutcome.RESOLVED
    assert [event.event_type for event in audit.events] == ["schedule.fired"]
    assert audit.events[0].run_id == run_id


@pytest.mark.critical
def test_partial_or_missing_receipt_opens_one_source_version_specific_follow_up() -> None:
    """A partial receipt never masquerades as arrival and retrying the task cannot duplicate follow-up."""
    service, attention, _ = arrival_service((purchase_order(received_quantity="20"),))
    task = claimed_arrival_task()

    first = service.handle_claimed_task(
        task,
        checked_at=TUESDAY,
        run_id=RunId("run-arrival-missing"),
    )
    retry = service.handle_claimed_task(
        task,
        checked_at=TUESDAY,
        run_id=RunId("run-arrival-retry"),
    )

    assert first.outcome is ArrivalCheckOutcome.REOPENED
    assert first.followup is not None
    assert first.followup.cause == "arrival_check"
    assert first.followup.source_versions == {f"purchase_order:{REPLACEMENT_PURCHASE_ORDER_ID}": 3}
    assert first.followup.dedupe_key != original_attention().dedupe_key
    assert retry.outcome is ArrivalCheckOutcome.REOPENED
    assert retry.followup == first.followup
    assert len(attention.followups) == 1
    assert attention.records[ORIGINAL_ATTENTION_ID].status is AttentionStatus.OPEN


def test_newer_purchase_order_update_creates_a_distinct_arrival_follow_up() -> None:
    """A later authoritative PO version is causal new evidence, not a duplicate of an older check."""
    service, attention, erp = arrival_service((purchase_order(received_quantity="0"),))
    task = claimed_arrival_task()

    first = service.handle_claimed_task(
        task,
        checked_at=TUESDAY,
        run_id=RunId("run-arrival-version-3"),
    )
    erp.evidence = (purchase_order(received_quantity="20", source_version=4),)
    newer = service.handle_claimed_task(
        task,
        checked_at=TUESDAY,
        run_id=RunId("run-arrival-version-4"),
    )

    assert first.followup is not None
    assert newer.followup is not None
    assert first.followup.dedupe_key != newer.followup.dedupe_key
    assert newer.followup.source_versions == {f"purchase_order:{REPLACEMENT_PURCHASE_ORDER_ID}": 4}
    assert len(attention.followups) == 2


def test_missing_or_terminal_original_attention_cannot_query_or_mutate_erp_state() -> None:
    """Stale scheduler work is harmless if its causal attention item disappeared or already finished."""
    missing_service, missing_attention, missing_erp = arrival_service(
        (purchase_order(received_quantity="60"),)
    )
    missing_attention.records.clear()

    missing = missing_service.handle_claimed_task(
        claimed_arrival_task(),
        checked_at=TUESDAY,
        run_id=RunId("run-arrival-missing-attention"),
    )

    terminal_service, terminal_attention, terminal_erp = arrival_service(
        (purchase_order(received_quantity="60"),)
    )
    terminal_attention.records[ORIGINAL_ATTENTION_ID] = replace(
        original_attention(), status=AttentionStatus.RESOLVED, resolved_at=TUESDAY
    )
    terminal = terminal_service.handle_claimed_task(
        claimed_arrival_task(),
        checked_at=TUESDAY,
        run_id=RunId("run-arrival-terminal"),
    )

    assert missing.outcome is ArrivalCheckOutcome.MISSING_ATTENTION
    assert terminal.outcome is ArrivalCheckOutcome.NOT_ACTIONABLE
    assert missing_erp.queries == []
    assert terminal_erp.queries == []


@pytest.mark.parametrize(
    ("task", "checked_at", "evidence", "error_fragment"),
    [
        (
            replace(
                claimed_arrival_task(), status=ScheduledTaskStatus.PENDING, lease_expires_at=None
            ),
            TUESDAY,
            (purchase_order(received_quantity="60"),),
            "scheduler lease",
        ),
        (
            replace(claimed_arrival_task(), due_at=TUESDAY + timedelta(seconds=1)),
            TUESDAY,
            (purchase_order(received_quantity="60"),),
            "not due",
        ),
        (
            replace(claimed_arrival_task(), due_at=TUESDAY.replace(tzinfo=None)),
            TUESDAY,
            (purchase_order(received_quantity="60"),),
            "task due time",
        ),
        (
            replace(
                claimed_arrival_task(),
                payload={
                    "purchase_order_id": REPLACEMENT_PURCHASE_ORDER_ID,
                    "original_attention_id": str(ORIGINAL_ATTENTION_ID),
                },
            ),
            TUESDAY,
            (purchase_order(received_quantity="60"),),
            "lacks actor_id",
        ),
        (
            claimed_arrival_task(),
            TUESDAY,
            (),
            "one current purchase-order",
        ),
        (
            claimed_arrival_task(),
            TUESDAY,
            (purchase_order(received_quantity="0", ordered_quantity="0"),),
            "zero ordered quantity",
        ),
        (
            claimed_arrival_task(),
            TUESDAY,
            (purchase_order(received_quantity="-1"),),
            "invalid received_quantity",
        ),
        (
            claimed_arrival_task(),
            TUESDAY,
            (purchase_order(received_quantity="NaN"),),
            "invalid received_quantity",
        ),
        (
            claimed_arrival_task(),
            TUESDAY,
            (purchase_order(received_quantity="not-a-number"),),
            "invalid received_quantity",
        ),
    ],
)
def test_arrival_check_rejects_unsafe_scheduler_or_erp_evidence(
    task: ScheduledTask,
    checked_at: datetime,
    evidence: tuple[Evidence, ...],
    error_fragment: str,
) -> None:
    """The worker fails closed rather than fabricating an arrival decision from unsafe data."""
    service, _, _ = arrival_service(evidence)

    with pytest.raises(ArrivalCheckError, match=error_fragment):
        service.handle_claimed_task(
            task,
            checked_at=checked_at,
            run_id=RunId("run-arrival-unsafe"),
        )


@pytest.mark.parametrize(
    ("task", "checked_at", "error_fragment"),
    [
        (
            replace(claimed_arrival_task(), task_type="approval_escalation"),
            TUESDAY,
            "not an arrival check",
        ),
        (
            replace(claimed_arrival_task(), lease_expires_at=TUESDAY),
            TUESDAY,
            "lease has expired",
        ),
        (
            replace(
                claimed_arrival_task(),
                payload={"purchase_order_id": REPLACEMENT_PURCHASE_ORDER_ID},
            ),
            TUESDAY,
            "lacks original_attention_id",
        ),
        (claimed_arrival_task(), TUESDAY.replace(tzinfo=None), "check time"),
    ],
)
def test_arrival_check_rejects_invalid_scheduler_state_or_causal_binding(
    task: ScheduledTask,
    checked_at: datetime,
    error_fragment: str,
) -> None:
    """Only a due, leased, fully bound arrival task can influence attention state."""
    service, _, _ = arrival_service((purchase_order(received_quantity="60"),))

    with pytest.raises(ArrivalCheckError, match=error_fragment):
        service.handle_claimed_task(
            task, checked_at=checked_at, run_id=RunId("run-arrival-invalid")
        )


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and preserve diagnostics for the real durable follow-up contract."""
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
def test_postgres_tuesday_tasks_resolve_with_receipt_and_reopen_without_one(
    disposable_database: str,
) -> None:
    """Claimed durable tasks survive to Tuesday and choose distinct resolve versus re-entry outcomes."""
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
        "from datetime import timedelta\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter, PostgresDemoClock, PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter, PostgresSchedulerAdapter,\n"
        ")\n"
        "from enterprise_agent.application.arrival_check import ArrivalCheckOutcome, TuesdayArrivalCheckService\n"
        "from enterprise_agent.domain import RunId, ScenarioAStockoutTrigger, ScheduledTask, ScheduledTaskId, ScheduledTaskStatus, UserId\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "clock = PostgresDemoClock(database_url)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "dana = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "attention = PostgresAttentionAdapter(database_url)\n"
        "def trigger(version):\n"
        "    return ScenarioAStockoutTrigger(detector='stockout_detector:v1', part_id='00000000-0000-0000-0000-000000000101', production_order_id='00000000-0000-0000-0000-000000000301', inventory_version=version, production_start_date=clock.now().date() + timedelta(days=3), detected_at=clock.now(), source_versions={'inventory:00000000-0000-0000-0000-000000000501': version})\n"
        "received_original = attention.register(trigger(4), RunId('run-arrival-received')).attention\n"
        "missing_original = attention.register(trigger(5), RunId('run-arrival-missing')).attention\n"
        "received_po = '00000000-0000-0000-0000-000000000498'\n"
        "missing_po = '00000000-0000-0000-0000-000000000497'\n"
        "with create_engine(database_url).begin() as connection:\n"
        "    for po_id, po_number, quantity in ((received_po, 'RPL-RECEIVED', '60'), (missing_po, 'RPL-MISSING', '0')):\n"
        "        connection.execute(text(\"INSERT INTO purchase_orders (id, po_number, part_id, supplier_id, plant_id, ordered_quantity, received_quantity, status, expected_receipt_date, source_version, created_at, updated_at) VALUES (CAST(:po_id AS UUID), :po_number, CAST('00000000-0000-0000-0000-000000000101' AS UUID), CAST('00000000-0000-0000-0000-000000000202' AS UUID), 'PLANT-CHI', 60, CAST(:received_quantity AS NUMERIC), 'open', :expected_receipt_date, 1, :now, :now)\"), {'po_id': po_id, 'po_number': po_number, 'received_quantity': quantity, 'expected_receipt_date': clock.now().date() + timedelta(days=1), 'now': clock.now()})\n"
        "tuesday = clock.advance(timedelta(days=1))\n"
        "scheduler = PostgresSchedulerAdapter(database_url, clock)\n"
        "for task_id, key, original, po_id in (('00000000-0000-0000-0000-000000000a11', 'arrival-check:received', received_original, received_po), ('00000000-0000-0000-0000-000000000a12', 'arrival-check:missing', missing_original, missing_po)):\n"
        "    scheduler.schedule(ScheduledTask(task_id=ScheduledTaskId(task_id), task_type='arrival_check', due_at=tuesday, status=ScheduledTaskStatus.PENDING, idempotency_key=key, payload={'purchase_order_id': po_id, 'original_attention_id': str(original.attention_id), 'actor_id': str(dana.user_id)}, attempt_count=0, lease_expires_at=None, completed_at=None))\n"
        "service = TuesdayArrivalCheckService(erp=PostgresErpAdapter(database_url), identity=identity, attention=attention)\n"
        "outcomes = {}\n"
        "for task in scheduler.claim_due(tuesday, limit=10):\n"
        "    result = service.handle_claimed_task(task, checked_at=tuesday, run_id=RunId(f'run-task-{task.task_id}'))\n"
        "    outcomes[task.payload['purchase_order_id']] = result\n"
        "    scheduler.mark_succeeded(task.task_id, tuesday)\n"
        "assert outcomes[received_po].outcome is ArrivalCheckOutcome.RESOLVED\n"
        "assert outcomes[missing_po].outcome is ArrivalCheckOutcome.REOPENED\n"
        "assert attention.load(received_original.attention_id).status.value == 'resolved'\n"
        "followup = outcomes[missing_po].followup\n"
        "assert followup is not None and followup.cause == 'arrival_check' and followup.status.value == 'open'\n"
        "assert followup.source_versions == {f'purchase_order:{missing_po}': 1}\n"
        "with create_engine(database_url).connect() as connection:\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM scheduled_tasks WHERE status = 'succeeded' AND task_type = 'arrival_check'\")).scalar_one() == 2\n"
        "    assert connection.execute(text(\"SELECT COUNT(*) FROM attention_items WHERE cause = 'arrival_check'\")).scalar_one() == 1\n"
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
