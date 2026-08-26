"""Actor-scoped local-review read-model contracts independent of FastAPI and PostgreSQL."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self

import pytest

from enterprise_agent.application.local_review import (
    LocalReviewAccessDeniedError,
    LocalReviewReadService,
    LocalReviewResourceNotFoundError,
    UnconfiguredLocalReviewService,
)
from enterprise_agent.application.operator_status import (
    OperatorStatusSnapshot,
    PendingApprovalStatus,
    RecoveryState,
    WorkflowStatusSummary,
    operator_status_data,
)
from enterprise_agent.domain import (
    ActorContext,
    Approval,
    ApprovalId,
    ApprovalStatus,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    AuditEvent,
    AuditEventId,
    Plan,
    PlanId,
    RunId,
    Scope,
    UserId,
    WorkflowId,
    WorkflowState,
    WorkflowStateSnapshot,
    WorkflowStatus,
    WorkflowStepId,
    WorkflowStepState,
    WorkflowStepStatus,
)

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
ACTOR_ID = UserId("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UserId("00000000-0000-0000-0000-000000000003")
ATTENTION_ID = AttentionId("00000000-0000-0000-0000-000000000010")
PLAN_ID = PlanId("00000000-0000-0000-0000-000000000020")
APPROVAL_ID = ApprovalId("00000000-0000-0000-0000-000000000030")
WORKFLOW_ID = WorkflowId("00000000-0000-0000-0000-000000000040")
RUN_ID = RunId("run-local-review")


@dataclass
class MemoryStatusReader:
    """Return one fixed CLI-compatible summary while retaining actor scoping calls."""

    snapshot: OperatorStatusSnapshot
    requested_actor_ids: list[UserId]

    def read_status_for_actor(self, actor_id: UserId) -> OperatorStatusSnapshot:
        self.requested_actor_ids.append(actor_id)
        return self.snapshot


@dataclass
class MemoryAttentionStore:
    """Load one attention record without any transition method."""

    attention: AttentionItem | None

    def load(self, attention_id: AttentionId) -> AttentionItem | None:
        return (
            self.attention if self.attention is not None and attention_id == ATTENTION_ID else None
        )


@dataclass
class MemoryApprovalStore:
    """Expose one immutable plan/approval binding by either approved lookup key."""

    binding: tuple[Plan, Approval]

    def load(self, approval_id: ApprovalId) -> tuple[Plan, Approval] | None:
        return self.binding if approval_id == APPROVAL_ID else None

    def load_for_plan(self, plan_id: PlanId) -> tuple[Plan, Approval] | None:
        return self.binding if plan_id == PLAN_ID else None


@dataclass
class MemoryWorkflowStore:
    """Return one declared durable workflow snapshot without any executor mutation methods."""

    snapshot: WorkflowStateSnapshot | None

    def load(self, workflow_id: WorkflowId) -> WorkflowStateSnapshot | None:
        return self.snapshot if workflow_id == WORKFLOW_ID else None


@dataclass
class MemoryAuditLedger:
    """Return exactly one known immutable audit run."""

    events: tuple[AuditEvent, ...]

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        return self.events if run_id == RUN_ID else ()


@dataclass
class FixedDemoClock:
    """Read deterministic local-demo time without supporting advance."""

    current_at: datetime

    def now(self) -> datetime:
        return self.current_at


@dataclass
class MemoryIdentity:
    """Confirm either seeded test actor without returning a privileged business-system client."""

    def actor_for(self, user_id: UserId) -> ActorContext:
        if user_id not in {ACTOR_ID, OTHER_ACTOR_ID}:
            raise LookupError("unknown actor")
        return ActorContext(
            user_id=user_id,
            role="purchasing_manager",
            scopes=frozenset({Scope("erp:read")}),
            plant_ids=frozenset(),
            backup_approver_id=None,
            approval_limits={"USD": Decimal(10000)},
        )


@dataclass
class FixedAttentionAccess:
    """Control actor-bound attention visibility independently from the stored record."""

    allowed: bool

    def can_view_attention(self, actor_id: UserId, attention_id: AttentionId) -> bool:
        return self.allowed and actor_id == ACTOR_ID and attention_id == ATTENTION_ID


def _attention() -> AttentionItem:
    """Build one attention item with versioned references, not raw source payloads."""
    return AttentionItem(
        attention_id=ATTENTION_ID,
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key="scenario-a:part-x",
        status=AttentionStatus.PENDING_APPROVAL,
        created_at=NOW,
        source_versions={"inventory:part-x": 4, "message:shipment-802": 2},
    )


def _plan_and_approval() -> tuple[Plan, Approval]:
    """Build a binding whose input and hash must remain outside the review output."""
    plan = Plan(
        plan_id=PLAN_ID,
        attention_id=ATTENTION_ID,
        actor_id=ACTOR_ID,
        approver_id=ACTOR_ID,
        intent="enter_workflow",
        workflow_name="po_reroute",
        workflow_version=1,
        parameters={"supplier_id": "supplier-z", "api_key": "do-not-render"},
        source_versions={"inventory:part-x": 4},
        policy_version="scenario_a_policy:v1",
        plan_hash="sha256:plan-secret",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )
    approval = Approval(
        approval_id=APPROVAL_ID,
        plan_id=PLAN_ID,
        plan_hash=plan.plan_hash,
        requester_id=ACTOR_ID,
        approver_id=ACTOR_ID,
        status=ApprovalStatus.PENDING,
        requested_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )
    return plan, approval


def _workflow() -> WorkflowStateSnapshot:
    """Build one running workflow whose raw input, result, error, and lease owner must stay hidden."""
    workflow = WorkflowState(
        workflow_id=WORKFLOW_ID,
        plan_id=PLAN_ID,
        definition_name="po_reroute",
        definition_version=1,
        status=WorkflowStatus.RUNNING,
        current_step=1,
        started_at=NOW,
        completed_at=None,
        last_error="provider-secret",
        lease_owner="workflow-worker-secret",
        lease_expires_at=NOW + timedelta(minutes=10),
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
    )
    step = WorkflowStepState(
        step_id=WorkflowStepId("00000000-0000-0000-0000-000000000050"),
        workflow_id=WORKFLOW_ID,
        step_index=1,
        step_name="verify_freshness",
        tool_name=None,
        status=WorkflowStepStatus.SUCCEEDED,
        idempotency_key="workflow-idempotency-secret",
        input={"audit_run_id": str(RUN_ID), "raw_provider_input": "do-not-render"},
        result={"raw_provider_result": "do-not-render"},
        error="step-error-secret",
        attempt_count=1,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        lease_owner="worker-secret",
        lease_expires_at=NOW + timedelta(minutes=10),
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
    )
    return WorkflowStateSnapshot(workflow=workflow, steps=(step,))


def _status_snapshot() -> OperatorStatusSnapshot:
    """Build the stable safe shape shared with the terminal status command."""
    return OperatorStatusSnapshot(
        pending_approvals=(
            PendingApprovalStatus(
                approval_id=str(APPROVAL_ID),
                plan_id=str(PLAN_ID),
                requester="Dana Buyer",
                approver="Dana Buyer",
                decision_state="pending",
                expires_at=(NOW + timedelta(hours=4)).isoformat(),
                audit_run_id=str(RUN_ID),
            ),
        ),
        workflows=(
            WorkflowStatusSummary(
                workflow_id=str(WORKFLOW_ID),
                status="running",
                current_step="verify_freshness",
                idempotency_key_prefix="workflow-idempotency-sec",
                recovery_state=RecoveryState.IN_PROGRESS,
            ),
        ),
    )


def _audit_events(
    *,
    actor_id: UserId | None = ACTOR_ID,
    plan_id: PlanId = PLAN_ID,
    attention_id: AttentionId | None = ATTENTION_ID,
) -> tuple[AuditEvent, ...]:
    """Build safe material facts that the existing restricted audit renderer can explain."""
    return (
        AuditEvent(
            event_id=AuditEventId("00000000-0000-0000-0000-000000000060"),
            occurred_at=NOW,
            event_type="planner.recommended",
            run_id=RUN_ID,
            actor_id=actor_id,
            attention_id=attention_id,
            workflow_id=None,
            plan_id=plan_id,
            evidence_ids=(),
            payload={"outcome": "ENTER_WORKFLOW", "workflow_name": "po_reroute"},
            policy_version="scenario_a_policy:v1",
            plan_hash="sha256:plan-secret",
            idempotency_key=None,
            failure_category=None,
        ),
    )


def _service(
    *,
    actor_id: UserId = ACTOR_ID,
    attention_access: bool = True,
    audit_events: tuple[AuditEvent, ...] | None = None,
) -> tuple[LocalReviewReadService, MemoryStatusReader]:
    """Compose a fully in-memory local-review service around durable-domain-shaped fixtures."""
    status_reader = MemoryStatusReader(snapshot=_status_snapshot(), requested_actor_ids=[])
    service = LocalReviewReadService(
        actor_id=actor_id,
        operator_status=status_reader,
        attention_store=MemoryAttentionStore(attention=_attention()),
        approvals=MemoryApprovalStore(binding=_plan_and_approval()),
        workflows=MemoryWorkflowStore(snapshot=_workflow()),
        audit_ledger=MemoryAuditLedger(
            events=_audit_events() if audit_events is None else audit_events
        ),
        demo_clock_reader=FixedDemoClock(current_at=NOW),
        identity=MemoryIdentity(),
        attention_access=FixedAttentionAccess(allowed=attention_access),
    )
    return service, status_reader


@pytest.mark.critical
def test_local_review_service_reuses_cli_status_and_omits_sensitive_workflow_and_plan_data() -> (
    None
):
    """The application service is the only boundary that turns durable domain records into UI-safe JSON."""
    service, status_reader = _service()

    assert service.status() == operator_status_data(_status_snapshot())
    assert status_reader.requested_actor_ids == [ACTOR_ID]
    assert service.attention(str(ATTENTION_ID))["evidence"] == [
        {"evidence_id": "inventory:part-x", "source_version": 4},
        {"evidence_id": "message:shipment-802", "source_version": 2},
    ]

    approval = service.approval(str(APPROVAL_ID))
    assert approval["source_versions"] == {"inventory:part-x": 4}
    assert "parameters" not in approval
    assert "plan_hash" not in approval

    workflow = service.workflow(str(WORKFLOW_ID))
    assert workflow["recovery_state"] == "in_progress"
    assert workflow["compensation_state"] == "not_required"
    assert workflow["audit_run_id"] == str(RUN_ID)
    assert workflow["steps"] == [
        {
            "step_index": 1,
            "step_name": "verify_freshness",
            "tool_name": None,
            "status": "succeeded",
            "attempt_count": 1,
            "idempotency_key_prefix": "workflow-idempotency-sec",
        }
    ]
    rendered_workflow = repr(workflow)
    for secret in (
        "provider-secret",
        "workflow-worker-secret",
        "do-not-render",
        "step-error-secret",
        "workflow-idempotency-secret",
    ):
        assert secret not in rendered_workflow

    audit = service.audit(str(RUN_ID))
    assert audit == {
        "run_id": str(RUN_ID),
        "event_count": 1,
        "explanation": "Audit explanation for run run-local-review (1 events)\n"
        "2026-08-24T09:00:00+00:00 | Planner recommended ENTER_WORKFLOW using po_reroute.",
    }
    assert service.demo_clock() == {"current_at": NOW.isoformat()}


@pytest.mark.critical
def test_local_review_service_denies_cross_actor_and_mixed_audit_run_before_rendering() -> None:
    """One unauthorized resource or event blocks its entire UI response instead of leaking a partial story."""
    other_actor_service, _ = _service(actor_id=OTHER_ACTOR_ID, attention_access=False)
    mixed_run_service, _ = _service(
        audit_events=_audit_events(
            actor_id=OTHER_ACTOR_ID,
            plan_id=PlanId("00000000-0000-0000-0000-000000000099"),
            attention_id=None,
        )
    )
    spoofed_relation_service, _ = _service(
        audit_events=_audit_events(
            actor_id=ACTOR_ID,
            plan_id=PlanId("00000000-0000-0000-0000-000000000099"),
            attention_id=None,
        )
    )

    with pytest.raises(LocalReviewAccessDeniedError):
        other_actor_service.attention(str(ATTENTION_ID))
    with pytest.raises(LocalReviewAccessDeniedError):
        other_actor_service.approval(str(APPROVAL_ID))
    with pytest.raises(LocalReviewAccessDeniedError):
        mixed_run_service.audit(str(RUN_ID))
    with pytest.raises(LocalReviewAccessDeniedError):
        spoofed_relation_service.audit(str(RUN_ID))
    with pytest.raises(LocalReviewResourceNotFoundError):
        mixed_run_service.workflow("not-a-uuid")


@pytest.mark.parametrize(
    ("workflow_status", "expected_compensation_state"),
    (
        (WorkflowStatus.FAILED, "available"),
        (WorkflowStatus.COMPENSATING, "in_progress"),
        (WorkflowStatus.COMPENSATED, "completed"),
    ),
)
def test_local_review_workflow_projects_the_durable_compensation_lifecycle(
    workflow_status: WorkflowStatus,
    expected_compensation_state: str,
) -> None:
    """Recovery views name compensation from durable workflow state without rendering error payloads."""
    service, _ = _service()
    workflow_store = service.workflows
    assert isinstance(workflow_store, MemoryWorkflowStore)
    snapshot = workflow_store.snapshot
    assert snapshot is not None
    workflow_store.snapshot = replace(
        snapshot, workflow=replace(snapshot.workflow, status=workflow_status)
    )

    workflow = service.workflow(str(WORKFLOW_ID))

    assert workflow["compensation_state"] == expected_compensation_state


def test_postgres_attention_access_adapter_runs_only_the_bound_actor_authorization_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter answers a scalar permission check without loading a plan, approval, or evidence row."""
    from enterprise_agent.adapters import local_review

    observed: list[tuple[object, dict[str, str]]] = []

    class Result:
        """Return the one database-enforced authorization answer."""

        def scalar_one(self) -> bool:
            return True

    class Connection:
        """Expose a read connection that only accepts the fixed parameterized authorization query."""

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object, parameters: dict[str, str]) -> Result:
            observed.append((statement, parameters))
            return Result()

    class Engine:
        """Supply the narrow read-only connection expected by the access adapter."""

        def connect(self) -> Connection:
            return Connection()

    monkeypatch.setattr(local_review, "create_engine", lambda _url: Engine())

    assert local_review.PostgresLocalReviewAccessAdapter(
        "postgresql://read-only"
    ).can_view_attention(
        ACTOR_ID,
        ATTENTION_ID,
    )
    assert observed == [
        (
            local_review._CAN_VIEW_ATTENTION,
            {"actor_id": str(ACTOR_ID), "attention_id": str(ATTENTION_ID)},
        )
    ]


def test_local_review_composition_uses_only_local_database_and_actor_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UI composition root ignores LLM profiles and creates no review service for invalid local settings."""
    from enterprise_agent import local_review_composition

    database_url = "postgresql+psycopg://local/demo"
    constructed_urls: list[str] = []

    class StubAdapter:
        """Record that each selected adapter receives only the local database URL."""

        def __init__(self, configured_database_url: str) -> None:
            constructed_urls.append(configured_database_url)

    monkeypatch.setattr(
        local_review_composition,
        "load_local_environment",
        lambda _path: {"DATABASE_URL": database_url, "LOCAL_REVIEW_ACTOR_ID": str(ACTOR_ID)},
    )
    for adapter_name in (
        "PostgresAttentionAdapter",
        "PostgresAuditAdapter",
        "PostgresDemoClock",
        "PostgresIdentityAdapter",
        "PostgresLocalReviewAccessAdapter",
        "PostgresOperatorStatusAdapter",
        "PostgresPlanApprovalAdapter",
        "PostgresWorkflowStateAdapter",
    ):
        monkeypatch.setattr(local_review_composition, adapter_name, StubAdapter)

    configured = local_review_composition.create_local_review_service()

    assert isinstance(configured, LocalReviewReadService)
    assert configured.actor_id == ACTOR_ID
    assert constructed_urls == [database_url] * 8

    monkeypatch.setattr(local_review_composition, "load_local_environment", lambda _path: {})
    assert isinstance(
        local_review_composition.create_local_review_service(),
        UnconfiguredLocalReviewService,
    )


def _compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the real local-review contract in the Compose network with useful diagnostics on failure."""
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
@pytest.mark.scenario
def test_postgres_local_review_service_reads_only_dana_bound_demo_records(
    disposable_database: str,
) -> None:
    """The real UI service matches CLI status and denies a different actor without mutating the ledger."""
    _compose(
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
        "from os import environ\n"
        "from sqlalchemy import create_engine, text\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter, PostgresAuditAdapter, PostgresDemoClock,\n"
        "    PostgresIdentityAdapter, PostgresLocalReviewAccessAdapter,\n"
        "    PostgresOperatorStatusAdapter, PostgresPlanApprovalAdapter,\n"
        "    PostgresWorkflowStateAdapter,\n"
        ")\n"
        "from enterprise_agent.application.guided_demo import run_guided_demo\n"
        "from enterprise_agent.application.local_review import (\n"
        "    LocalReviewAccessDeniedError, LocalReviewReadService,\n"
        ")\n"
        "from enterprise_agent.application.operator_status import operator_status_data\n"
        "from enterprise_agent.domain import UserId\n"
        "from enterprise_agent.seed import ID_DANA, ID_QUINN\n"
        "database_url = environ['DATABASE_URL']\n"
        "run_guided_demo(\n"
        "    database_url,\n"
        "    case_ids=('scenario-a-reroute-bait', 'scenario-c-pending-review'),\n"
        "    allow_test_database=True,\n"
        ")\n"
        "def reader(actor_id):\n"
        "    return LocalReviewReadService(\n"
        "        actor_id=actor_id,\n"
        "        operator_status=PostgresOperatorStatusAdapter(database_url),\n"
        "        attention_store=PostgresAttentionAdapter(database_url),\n"
        "        approvals=PostgresPlanApprovalAdapter(database_url),\n"
        "        workflows=PostgresWorkflowStateAdapter(database_url),\n"
        "        audit_ledger=PostgresAuditAdapter(database_url),\n"
        "        demo_clock_reader=PostgresDemoClock(database_url),\n"
        "        identity=PostgresIdentityAdapter(database_url),\n"
        "        attention_access=PostgresLocalReviewAccessAdapter(database_url),\n"
        "    )\n"
        "dana = reader(UserId(str(ID_DANA)))\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    events_before = connection.execute(text('SELECT COUNT(*) FROM audit_events')).scalar_one()\n"
        "status = dana.status()\n"
        "cli_status = operator_status_data(\n"
        "    PostgresOperatorStatusAdapter(database_url).read_status_for_actor(UserId(str(ID_DANA)))\n"
        ")\n"
        "assert status == cli_status\n"
        "assert len(status['pending_approvals']) == 2\n"
        "approval_summary = status['pending_approvals'][0]\n"
        "approval = dana.approval(approval_summary['approval_id'])\n"
        "attention = dana.attention(approval['attention_id'])\n"
        "assert attention['attention_id'] == approval['attention_id']\n"
        "assert attention['evidence']\n"
        "assert dana.audit(approval_summary['audit_run_id'])['event_count'] > 0\n"
        "assert status['workflows']\n"
        "assert dana.workflow(status['workflows'][0]['workflow_id'])['steps']\n"
        "assert dana.demo_clock()['current_at']\n"
        "try:\n"
        "    reader(UserId(str(ID_QUINN))).approval(approval_summary['approval_id'])\n"
        "except LocalReviewAccessDeniedError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('cross-actor approval was visible through local review')\n"
        "with engine.connect() as connection:\n"
        "    events_after = connection.execute(text('SELECT COUNT(*) FROM audit_events')).scalar_one()\n"
        "assert events_after == events_before\n"
    )
    _compose(
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
