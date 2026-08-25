"""Contracts for immutable, hash-bound Scenario A plan and approval records."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from enterprise_agent.application.context import AuthorizedContextBundle
from enterprise_agent.application.gate import GateDecision, GateStatus
from enterprise_agent.application.planning import EnterWorkflowRecommendation
from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    Evidence,
    EvidenceId,
    Money,
    Plan,
    PlanId,
    PlantId,
    ScenarioAStockoutTrigger,
    Scope,
    UserId,
)

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(hours=4)


def _context() -> AuthorizedContextBundle:
    """Build the source-bound identity and attention facts consumed by plan persistence."""
    actor = ActorContext(
        user_id=UserId("00000000-0000-0000-0000-000000000001"),
        role="purchasing_manager",
        scopes=frozenset(
            {
                Scope("calendar:read"),
                Scope("erp:read"),
                Scope("erp:po:reroute"),
                Scope("mail:read"),
            }
        ),
        plant_ids=frozenset({PlantId("PLANT-CHI")}),
        backup_approver_id=UserId("00000000-0000-0000-0000-000000000002"),
        approval_limits={"USD": Decimal(10000)},
    )
    trigger = ScenarioAStockoutTrigger(
        detector="stockout_detector:v1",
        part_id="part-x",
        production_order_id="production-4812",
        inventory_version=4,
        production_start_date=date(2026, 8, 27),
        detected_at=NOW,
        source_versions={
            "inventory:inventory-x": 4,
            "production_order:production-4812": 1,
        },
    )
    attention = AttentionItem(
        attention_id=AttentionId("attention-stockout"),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key=trigger.dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions=trigger.source_versions,
    )

    def evidence(record_type: str, record_id: str, source_version: int = 1) -> Evidence:
        return Evidence(
            evidence_id=EvidenceId(f"source:{record_type}:{record_id}"),
            source="source",
            record_type=record_type,
            record_id=record_id,
            source_version=source_version,
            observed_at=NOW,
            payload={},
        )

    return AuthorizedContextBundle(
        actor=actor,
        attention=attention,
        trigger=trigger,
        inventory=evidence("inventory", "inventory-x", 4),
        production_order=evidence("production_order", "production-4812"),
        original_purchase_order=evidence("purchase_order", "po-4812-y", 2),
        suppliers=(),
        shipment_update=evidence("message", "shipment-current"),
        calendar_events=(),
    )


def _recommendation() -> EnterWorkflowRecommendation:
    """Build the one structured reroute that a successful gate has already authorized."""
    return EnterWorkflowRecommendation(
        outcome="ENTER_WORKFLOW",
        workflow_name="po_reroute",
        workflow_version=1,
        supplier_id="supplier-z",
        quantity=Decimal(60),
        original_purchase_order_id="po-4812-y",
        production_order_id="production-4812",
        rationale="The approved supplier can meet production.",
    )


@dataclass
class RecordingGate:
    """Return a fixed gate result while retaining the freshness map passed by the service."""

    decision: GateDecision
    current_source_versions: Mapping[str, int] | None = None

    def evaluate(
        self,
        context: AuthorizedContextBundle,
        recommendation: EnterWorkflowRecommendation,
        *,
        current_source_versions: Mapping[str, int],
    ) -> GateDecision:
        self.current_source_versions = current_source_versions
        return self.decision


@dataclass
class MemoryPlanApprovalStore:
    """Small deterministic persistence fake that exposes the service's atomic port contract."""

    records: dict[ApprovalId, tuple[Plan, Approval]] = field(default_factory=dict)
    create_calls: int = 0
    approve_calls: int = 0

    def create_pending(self, plan: Plan, approval: Approval) -> None:
        self.create_calls += 1
        self.records[approval.approval_id] = (plan, approval)

    def load(self, approval_id: ApprovalId) -> tuple[Plan, Approval] | None:
        return self.records.get(approval_id)

    def approve(
        self,
        approval_id: ApprovalId,
        expected_plan_hash: str,
        decided_at: datetime,
    ) -> Approval | None:
        self.approve_calls += 1
        record = self.records.get(approval_id)
        if record is None:
            return None
        plan, approval = record
        if (
            approval.status is not ApprovalStatus.PENDING
            or approval.plan_hash != expected_plan_hash
        ):
            return None
        approved = replace(
            approval,
            status=ApprovalStatus.APPROVED,
            decided_at=decided_at,
        )
        self.records[approval_id] = (plan, approved)
        return approved


def _pending_decision() -> GateDecision:
    """Return the sole gate outcome permitted to create a write-capable approval record."""
    return GateDecision(
        status=GateStatus.PENDING_APPROVAL,
        approval_required=True,
        denial_reasons=(),
        estimated_value=Money(amount=Decimal(1080), currency="USD"),
        candidate=None,
    )


def _service() -> tuple[object, MemoryPlanApprovalStore, RecordingGate]:
    """Build the application service with deterministic control-plane collaborators."""
    from enterprise_agent.application.approvals import ScenarioAApprovalService

    store = MemoryPlanApprovalStore()
    gate = RecordingGate(_pending_decision())
    return ScenarioAApprovalService(store, gate=gate), store, gate


def test_service_persists_a_pending_plan_and_approval_bound_to_the_full_intent_hash() -> None:
    """Every material workflow input and source version is frozen before human approval."""
    from enterprise_agent.application.approvals import recompute_plan_hash

    service, store, gate = _service()
    context = _context()
    result = service.request_pending(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
        policy_version="scenario_a_policy:v1",
        requested_at=NOW,
        expires_at=EXPIRES_AT,
    )

    assert gate.current_source_versions == context.source_versions
    assert store.create_calls == 1
    assert result.approval.status is ApprovalStatus.PENDING
    assert result.approval.plan_hash == result.plan.plan_hash
    assert result.plan.approver_id == context.actor.user_id
    assert result.plan.parameters == {
        "estimated_value_amount": "1080",
        "estimated_value_currency": "USD",
        "original_purchase_order_id": "po-4812-y",
        "production_order_id": "production-4812",
        "quantity": "60",
        "supplier_id": "supplier-z",
    }
    assert recompute_plan_hash(result.plan) == result.plan.plan_hash
    assert recompute_plan_hash(replace(result.plan, policy_version="scenario_a_policy:v2")) != (
        result.plan.plan_hash
    )


def test_service_refuses_to_persist_a_plan_when_the_gate_did_not_hold_it_for_approval() -> None:
    """A denied or non-writing recommendation cannot be converted into a durable write intent."""
    from enterprise_agent.application.approvals import (
        PlanNotApprovableError,
        ScenarioAApprovalService,
    )

    store = MemoryPlanApprovalStore()
    gate = RecordingGate(
        GateDecision(
            status=GateStatus.DENIED,
            approval_required=False,
            denial_reasons=(),
            estimated_value=None,
            candidate=None,
        )
    )
    service = ScenarioAApprovalService(store, gate=gate)
    context = _context()

    with pytest.raises(PlanNotApprovableError, match="pending approval"):
        service.request_pending(
            context,
            _recommendation(),
            current_source_versions=context.source_versions,
            policy_version="scenario_a_policy:v1",
            requested_at=NOW,
            expires_at=EXPIRES_AT,
        )

    assert store.create_calls == 0


def test_service_approves_only_an_unexpired_hash_and_source_version_match() -> None:
    """The approval decision is a compare-and-swap over the exact approved plan snapshot."""
    from enterprise_agent.application.approvals import ScenarioAApprovalService

    service, store, _ = _service()
    context = _context()
    result = service.request_pending(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
        policy_version="scenario_a_policy:v1",
        requested_at=NOW,
        expires_at=EXPIRES_AT,
    )

    approval = service.approve(
        approval_id=result.approval.approval_id,
        expected_plan_hash=result.plan.plan_hash,
        current_source_versions=context.source_versions,
        decided_at=NOW + timedelta(minutes=1),
    )

    assert store.approve_calls == 1
    assert approval.status is ApprovalStatus.APPROVED
    assert approval.decided_at == NOW + timedelta(minutes=1)


@pytest.mark.parametrize(
    ("expected_plan_hash", "current_source_versions", "decided_at"),
    [
        ("sha256:wrong", {"source:inventory:inventory-x": 4}, NOW + timedelta(minutes=1)),
        ("from_plan", {"source:inventory:inventory-x": 5}, NOW + timedelta(minutes=1)),
        ("from_plan", {"source:inventory:inventory-x": 4}, EXPIRES_AT),
    ],
)
def test_service_rejects_hash_mismatch_stale_evidence_or_expiry_before_approval(
    expected_plan_hash: str,
    current_source_versions: Mapping[str, int],
    decided_at: datetime,
) -> None:
    """A plan is unapprovable as soon as its approved intent, evidence, or validity window changes."""
    from enterprise_agent.application.approvals import PlanNotApprovableError

    service, store, _ = _service()
    context = _context()
    result = service.request_pending(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
        policy_version="scenario_a_policy:v1",
        requested_at=NOW,
        expires_at=EXPIRES_AT,
    )

    with pytest.raises(PlanNotApprovableError):
        service.approve(
            approval_id=result.approval.approval_id,
            expected_plan_hash=(
                result.plan.plan_hash if expected_plan_hash == "from_plan" else expected_plan_hash
            ),
            current_source_versions=current_source_versions,
            decided_at=decided_at,
        )

    assert store.approve_calls == 0


def test_service_rejects_a_loaded_plan_whose_immutable_content_no_longer_matches_its_hash() -> None:
    """Even a bypassed database constraint cannot turn a mutated persisted plan into an approval."""
    from enterprise_agent.application.approvals import PlanNotApprovableError

    service, store, _ = _service()
    context = _context()
    result = service.request_pending(
        context,
        _recommendation(),
        current_source_versions=context.source_versions,
        policy_version="scenario_a_policy:v1",
        requested_at=NOW,
        expires_at=EXPIRES_AT,
    )
    stored_plan, stored_approval = store.records[result.approval.approval_id]
    store.records[result.approval.approval_id] = (
        replace(stored_plan, parameters={"supplier_id": "supplier-tampered"}),
        stored_approval,
    )

    with pytest.raises(PlanNotApprovableError, match="hash"):
        service.approve(
            approval_id=result.approval.approval_id,
            expected_plan_hash=result.plan.plan_hash,
            current_source_versions=context.source_versions,
            decided_at=NOW + timedelta(minutes=1),
        )

    assert store.approve_calls == 0


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and retain diagnostics if it fails."""
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
def test_postgres_records_are_hash_bound_immutable_and_approval_cas_is_durable(
    disposable_database: str,
) -> None:
    """PostgreSQL prevents direct plan tampering and persists one valid approval decision."""
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
        "from datetime import UTC, datetime, timedelta\n"
        "from decimal import Decimal\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from sqlalchemy.exc import IntegrityError\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresCalendarAdapter,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter,\n"
        "    PostgresPlanApprovalAdapter,\n"
        ")\n"
        "from enterprise_agent.application.approvals import ScenarioAApprovalService\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.planning import EnterWorkflowRecommendation\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.domain import ApprovalStatus, RunId, UserId\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "now = datetime(2026, 8, 24, 9, tzinfo=UTC)\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "actor = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "erp = PostgresErpAdapter(database_url)\n"
        "detection = StockoutDetector(erp, PostgresAttentionAdapter(database_url)).detect(actor, RunId('run-plan-approval'), now)[0]\n"
        "context = ScenarioAContextAssembler(identity, erp, PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "recommendation = EnterWorkflowRecommendation(outcome='ENTER_WORKFLOW', workflow_name='po_reroute', workflow_version=1, supplier_id='00000000-0000-0000-0000-000000000202', quantity=Decimal(60), original_purchase_order_id=context.original_purchase_order.record_id, production_order_id=context.production_order.record_id, rationale='Approved alternate meets the production date.')\n"
        "service = ScenarioAApprovalService(PostgresPlanApprovalAdapter(database_url))\n"
        "result = service.request_pending(context, recommendation, current_source_versions=context.source_versions, policy_version='scenario_a_policy:v1', requested_at=now, expires_at=now + timedelta(hours=4))\n"
        "assert result.approval.plan_hash == result.plan.plan_hash\n"
        "approved = service.approve(approval_id=result.approval.approval_id, expected_plan_hash=result.plan.plan_hash, current_source_versions=context.source_versions, decided_at=now + timedelta(minutes=1))\n"
        "assert approved.status is ApprovalStatus.APPROVED\n"
        "try:\n"
        "    with create_engine(database_url).begin() as connection:\n"
        "        connection.execute(text(\"UPDATE plans SET intent = 'tampered' WHERE id = CAST(:plan_id AS UUID)\"), {'plan_id': str(result.plan.plan_id)})\n"
        "except IntegrityError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('plans must reject direct mutation')\n"
        "with create_engine(database_url).connect() as connection:\n"
        "    assert connection.execute(text('SELECT COUNT(*) FROM plans')).scalar_one() == 1\n"
        "    assert connection.execute(text(\"SELECT status FROM approvals WHERE id = CAST(:approval_id AS UUID)\"), {'approval_id': str(result.approval.approval_id)}).scalar_one() == 'approved'\n"
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
