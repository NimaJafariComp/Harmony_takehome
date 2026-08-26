"""Pure recovery-status contracts for the local operator read model."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest

from enterprise_agent.application.operator_status import RecoveryState, recovery_state_for
from enterprise_agent.domain import WorkflowStatus

pytestmark = pytest.mark.unit


def test_recovery_status_never_exposes_a_raw_error_and_distinguishes_reclaimable_work() -> None:
    """Operators get an actionable recovery state without being shown internal failure details."""
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)

    assert (
        recovery_state_for(
            WorkflowStatus.RUNNING,
            lease_expires_at=now - timedelta(minutes=1),
            now=now,
        )
        is RecoveryState.RECLAIMABLE
    )
    assert (
        recovery_state_for(
            WorkflowStatus.FAILED,
            lease_expires_at=None,
            now=now,
        )
        is RecoveryState.RECOVERY_REQUIRED
    )
    assert (
        recovery_state_for(
            WorkflowStatus.PENDING,
            lease_expires_at=None,
            now=now,
        )
        is RecoveryState.APPROVAL_REQUIRED
    )


@pytest.mark.parametrize(
    ("status", "lease_expires_at", "expected"),
    (
        (WorkflowStatus.RUNNING, datetime(2026, 8, 26, 13, tzinfo=UTC), RecoveryState.IN_PROGRESS),
        (WorkflowStatus.COMPENSATING, None, RecoveryState.COMPENSATING),
        (WorkflowStatus.SUCCEEDED, None, RecoveryState.NOT_REQUIRED),
    ),
)
def test_recovery_status_covers_active_compensation_and_terminal_workflows(
    status: WorkflowStatus,
    lease_expires_at: datetime | None,
    expected: RecoveryState,
) -> None:
    """Every durable workflow state maps to a short safe operator category."""
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)

    assert recovery_state_for(status, lease_expires_at=lease_expires_at, now=now) is expected
    assert RecoveryState.APPROVAL_REQUIRED.label == "approval required"


def test_postgres_status_projection_maps_only_safe_fields_from_reader_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter leaves plan inputs, tool results, and error text out of its in-process projection."""
    from enterprise_agent.adapters import operator_status

    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    approval_rows = (
        {
            "approval_id": "approval-1",
            "plan_id": "plan-1",
            "requester": "Dana Buyer",
            "approver": "Avery Backup",
            "decision_state": "rerouted",
            "expires_at": now,
            "audit_run_id": "run-status-1",
        },
    )
    workflow_rows = (
        {
            "workflow_id": "workflow-1",
            "status": "running",
            "current_step": 2,
            "lease_expires_at": now - timedelta(minutes=1),
            "step_name": "create replacement purchase order",
            "idempotency_key": "replacement-po-idempotency-key-123",
        },
    )

    class Result:
        """Minimal SQLAlchemy-result substitute for the three fixed read-only queries."""

        def __init__(
            self,
            *,
            scalar: datetime | None = None,
            rows: tuple[dict[str, object], ...] = (),
        ) -> None:
            self._scalar = scalar
            self._rows = rows

        def scalar_one_or_none(self) -> datetime | None:
            return self._scalar

        def mappings(self) -> Result:
            return self

        def all(self) -> tuple[dict[str, object], ...]:
            return self._rows

    class Connection:
        """Return only hard-coded rows and reject any unexpected SQL operation."""

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object) -> Result:
            if statement is operator_status._SELECT_DEMO_CLOCK:
                return Result(scalar=now)
            if statement is operator_status._SELECT_PENDING_APPROVALS:
                return Result(rows=approval_rows)
            if statement is operator_status._SELECT_WORKFLOWS:
                return Result(rows=workflow_rows)
            raise AssertionError("unexpected statement")

    class Engine:
        """Expose only a read connection; there is intentionally no write transaction method."""

        def connect(self) -> Connection:
            return Connection()

    monkeypatch.setattr(operator_status, "create_engine", lambda _url: Engine())

    snapshot = operator_status.PostgresOperatorStatusAdapter("postgresql://read-only").read_status()

    assert snapshot.pending_approvals[0].approval_id == "approval-1"
    assert snapshot.pending_approvals[0].audit_run_id == "run-status-1"
    assert snapshot.workflows[0].current_step == "create replacement purchase order"
    assert snapshot.workflows[0].idempotency_key_prefix == "replacement-po-idempoten"
    assert snapshot.workflows[0].recovery_state is RecoveryState.RECLAIMABLE


def _compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the real read-model contract in its Compose network with useful failure diagnostics."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.integration
@pytest.mark.scenario
def test_operator_status_reads_staged_a_and_c_without_mutating_the_ledger(
    disposable_database: str,
) -> None:
    """The overview exposes copyable pending state and recovery category through a read-only query."""
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
        "from enterprise_agent.adapters.operator_status import PostgresOperatorStatusAdapter\n"
        "from enterprise_agent.application.guided_demo import run_guided_demo\n"
        "database_url = environ['DATABASE_URL']\n"
        "run_guided_demo(\n"
        "    database_url,\n"
        "    case_ids=('scenario-a-reroute-bait', 'scenario-c-pending-review'),\n"
        "    allow_test_database=True,\n"
        ")\n"
        "engine = create_engine(database_url)\n"
        "with engine.connect() as connection:\n"
        "    before_events = connection.execute(text('SELECT COUNT(*) FROM audit_events')).scalar_one()\n"
        "snapshot = PostgresOperatorStatusAdapter(database_url).read_status()\n"
        "with engine.connect() as connection:\n"
        "    after_events = connection.execute(text('SELECT COUNT(*) FROM audit_events')).scalar_one()\n"
        "assert before_events == after_events\n"
        "assert len(snapshot.pending_approvals) == 2\n"
        "assert {item.audit_run_id for item in snapshot.pending_approvals} == {\n"
        "    'demo-scenario-a-reroute', 'demo-scenario-c-pending'\n"
        "}\n"
        "assert len(snapshot.workflows) == 1\n"
        "workflow = snapshot.workflows[0]\n"
        "assert workflow.status == 'pending'\n"
        "assert workflow.current_step == 'awaiting approval'\n"
        "assert workflow.idempotency_key_prefix == 'not started'\n"
        "assert workflow.recovery_state.value == 'approval_required'\n"
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
