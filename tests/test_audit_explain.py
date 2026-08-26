"""Read-only Scenario A reconstruction contracts over the append-only audit ledger."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from enterprise_agent.application.audit_explain import AuditExplainer, AuditExplanationError
from enterprise_agent.domain import AuditEvent, AuditEventId, RunId

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)
RUN_ID = RunId("run-scenario-a-explain")


def event(
    *,
    event_id: str,
    event_type: str,
    offset_seconds: int,
    payload: dict[str, object] | None = None,
    run_id: RunId = RUN_ID,
) -> AuditEvent:
    """Build one audit-only scenario fact without any live provider dependency."""
    return AuditEvent(
        event_id=AuditEventId(event_id),
        occurred_at=NOW + timedelta(seconds=offset_seconds),
        event_type=event_type,
        run_id=run_id,
        actor_id=None,
        attention_id=None,
        workflow_id=None,
        plan_id=None,
        evidence_ids=(),
        payload={} if payload is None else payload,
        policy_version="scenario_a_policy:v1",
        plan_hash="sha256:plan",
        idempotency_key=None,
        failure_category=None,
    )


@dataclass
class RecordingAudit:
    """Audit-only fake that proves the explainer needs no ERP, workflow, or provider ports."""

    events: tuple[AuditEvent, ...]
    requested_run_ids: list[RunId]

    def append(self, event: AuditEvent) -> None:
        raise AssertionError(f"the read-only explainer must not append {event.event_type}")

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        self.requested_run_ids.append(run_id)
        return self.events


@pytest.mark.unit
def test_audit_explain_reconstructs_a_chronological_human_scenario_story() -> None:
    """A reviewer can read detection through Tuesday outcome from audit events alone."""
    audit = RecordingAudit(
        events=(
            event(
                event_id="event-1",
                event_type="attention.detected",
                offset_seconds=0,
                payload={"part_id": "part-101", "production_order_id": "order-301"},
            ),
            event(
                event_id="event-2",
                event_type="context.gathered",
                offset_seconds=1,
                payload={"evidence_count": 6},
            ),
            event(
                event_id="event-3",
                event_type="planner.recommended",
                offset_seconds=2,
                payload={"outcome": "ENTER_WORKFLOW", "workflow_name": "po_reroute"},
            ),
            event(
                event_id="event-4",
                event_type="gate.allowed",
                offset_seconds=3,
                payload={"estimated_value": "240.00"},
            ),
            event(
                event_id="event-5",
                event_type="approval.approved",
                offset_seconds=4,
                payload={"approver_id": "avery"},
            ),
            event(
                event_id="event-6",
                event_type="workflow.step_completed",
                offset_seconds=5,
                payload={"step_name": "create_replacement_po", "result": "replacement-created"},
            ),
            event(
                event_id="event-7",
                event_type="schedule.created",
                offset_seconds=6,
                payload={"task_type": "arrival_check", "due_at": "2026-08-26T09:00:00+00:00"},
            ),
            event(
                event_id="event-8",
                event_type="followup.resolved",
                offset_seconds=7,
                payload={"purchase_order_id": "replacement-499"},
            ),
        ),
        requested_run_ids=[],
    )

    explanation = AuditExplainer(audit).explain(RUN_ID)

    assert audit.requested_run_ids == [RUN_ID]
    assert explanation.run_id == RUN_ID
    assert explanation.event_count == 8
    assert explanation.render().splitlines() == [
        "Audit explanation for run run-scenario-a-explain (8 events)",
        "2026-08-25T09:00:00+00:00 | Detected stockout risk for part part-101 affecting production order order-301.",
        "2026-08-25T09:00:01+00:00 | Gathered 6 authorized evidence records for planning.",
        "2026-08-25T09:00:02+00:00 | Planner recommended ENTER_WORKFLOW using po_reroute.",
        "2026-08-25T09:00:03+00:00 | Gate allowed the proposed action at estimated value 240.00.",
        "2026-08-25T09:00:04+00:00 | Approval was approved by avery.",
        "2026-08-25T09:00:05+00:00 | Workflow completed create_replacement_po: replacement-created.",
        "2026-08-25T09:00:06+00:00 | Scheduled arrival_check for 2026-08-26T09:00:00+00:00.",
        "2026-08-25T09:00:07+00:00 | Confirmed receipt for replacement PO replacement-499 and resolved the follow-up.",
    ]


@pytest.mark.unit
def test_audit_explain_rejects_empty_or_cross_run_ledger_data() -> None:
    """Missing or corrupt histories cannot be represented as a completed scenario explanation."""
    empty = RecordingAudit(events=(), requested_run_ids=[])
    mixed = RecordingAudit(
        events=(
            event(event_id="event-current", event_type="attention.detected", offset_seconds=0),
            event(
                event_id="event-other",
                event_type="tool.succeeded",
                offset_seconds=1,
                run_id=RunId("another-run"),
            ),
        ),
        requested_run_ids=[],
    )

    with pytest.raises(AuditExplanationError, match="no audit events"):
        AuditExplainer(empty).explain(RUN_ID)
    with pytest.raises(AuditExplanationError, match="different run"):
        AuditExplainer(mixed).explain(RUN_ID)


@pytest.mark.unit
def test_audit_explain_never_renders_sensitive_or_unrecognized_payload_content() -> None:
    """An explanation reports known safe fields only, even if a corrupt in-memory event has secrets."""
    audit = RecordingAudit(
        events=(
            event(
                event_id="event-sensitive",
                event_type="tool.succeeded",
                offset_seconds=0,
                payload={
                    "tool_name": "create_replacement_po",
                    "api_key": "must-not-appear",
                    "raw_response": "must-not-appear",
                    "unexpected": "must-not-appear",
                },
            ),
        ),
        requested_run_ids=[],
    )

    rendered = AuditExplainer(audit).explain(RUN_ID).render()

    assert "Created" in rendered
    assert "create_replacement_po" in rendered
    assert "must-not-appear" not in rendered


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event_type", "payload", "expected_description"),
    [
        (
            "attention.deduplicated",
            {},
            "Deduplicated a repeated stockout detection without creating duplicate work.",
        ),
        ("evidence.observed", {"evidence_count": -1}, "Recorded unknown evidence references."),
        (
            "gate.denied",
            {"reason": "stale evidence"},
            "Gate denied the proposed action: stale evidence.",
        ),
        ("approval.requested", {"approver_id": "avery"}, "Requested approval from avery."),
        (
            "approval.rerouted",
            {"approver_id": " "},
            "Rerouted approval to backup approver unknown.",
        ),
        ("approval.rejected", {"approver_id": "avery"}, "Approval was rejected by avery."),
        ("workflow.started", {"workflow_name": 42}, "Started workflow 42."),
        (
            "workflow.step_started",
            {"step_name": "create_replacement_po"},
            "Workflow started create_replacement_po.",
        ),
        ("workflow.failed", {}, "Workflow failed: unknown."),
        (
            "tool.started",
            {"tool_name": "create_replacement_po"},
            "Started tool create_replacement_po.",
        ),
        ("tool.succeeded", {"tool_name": "notify_production"}, "Tool notify_production succeeded."),
        (
            "tool.failed",
            {"tool_name": "create_replacement_po", "failure_category": "timeout"},
            "Tool create_replacement_po failed: timeout.",
        ),
        (
            "compensation.started",
            {"tool_name": "create_replacement_po"},
            "Started compensation for create_replacement_po.",
        ),
        (
            "compensation.completed",
            {"tool_name": "create_replacement_po"},
            "Completed compensation for create_replacement_po.",
        ),
        ("schedule.fired", {"task_type": "arrival_check"}, "Fired scheduled arrival_check work."),
        (
            "followup.reopened",
            {"purchase_order_id": "replacement-499"},
            "Receipt is still missing for replacement PO replacement-499; reopened the follow-up.",
        ),
        (
            "attention.status_changed",
            {"from_status": "open", "to_status": "resolved"},
            "Changed attention status from open to resolved.",
        ),
    ],
)
def test_audit_explain_renders_every_remaining_controlled_event_type(
    event_type: str,
    payload: dict[str, object],
    expected_description: str,
) -> None:
    """Every persisted event type has a bounded, reviewer-readable description."""
    audit = RecordingAudit(
        events=(
            event(
                event_id=f"event-{event_type}",
                event_type=event_type,
                offset_seconds=0,
                payload=payload,
            ),
        ),
        requested_run_ids=[],
    )

    rendered = AuditExplainer(audit).explain(RUN_ID).render()

    assert rendered.endswith(expected_description)


@pytest.mark.unit
def test_audit_explain_rejects_unsupported_event_types_and_ambiguous_event_times() -> None:
    """A reconstruction fails rather than narrating unknown history or local wall-clock time."""
    unsupported = RecordingAudit(
        events=(event(event_id="event-unsupported", event_type="unknown.event", offset_seconds=0),),
        requested_run_ids=[],
    )
    naive = RecordingAudit(
        events=(
            replace(
                event(event_id="event-naive", event_type="workflow.started", offset_seconds=0),
                occurred_at=datetime(2026, 8, 25, 9, tzinfo=UTC).replace(tzinfo=None),
            ),
        ),
        requested_run_ids=[],
    )

    with pytest.raises(AuditExplanationError, match="unsupported audit event type"):
        AuditExplainer(unsupported).explain(RUN_ID)
    with pytest.raises(AuditExplanationError, match="must include a timezone"):
        AuditExplainer(naive).explain(RUN_ID)


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one Compose command and retain diagnostics for audit-only integration failures."""
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
def test_postgres_audit_explain_reconstructs_only_the_persisted_run_ledger(
    disposable_database: str,
) -> None:
    """The production reconstruction performs no live-system lookup beyond the audit port's ledger read."""
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
        "from os import environ\n"
        "from enterprise_agent.adapters import PostgresAuditAdapter\n"
        "from enterprise_agent.application.audit_explain import AuditExplainer\n"
        "from enterprise_agent.domain import AuditEvent, AuditEventId, RunId\n"
        "now = datetime(2026, 8, 25, 9, tzinfo=UTC)\n"
        "run_id = RunId('run-audit-explain-integration')\n"
        "audit = PostgresAuditAdapter(environ['DATABASE_URL'])\n"
        "for event_id, offset, event_type, payload in (('00000000-0000-0000-0000-000000000911', 0, 'attention.detected', {'part_id': 'part-101', 'production_order_id': 'order-301'}), ('00000000-0000-0000-0000-000000000912', 1, 'tool.succeeded', {'tool_name': 'create_replacement_po'}), ('00000000-0000-0000-0000-000000000913', 2, 'followup.reopened', {'purchase_order_id': 'replacement-499'})):\n"
        "    audit.append(AuditEvent(event_id=AuditEventId(event_id), occurred_at=now + timedelta(seconds=offset), event_type=event_type, run_id=run_id, actor_id=None, attention_id=None, workflow_id=None, plan_id=None, evidence_ids=(), payload=payload, policy_version=None, plan_hash=None, idempotency_key=None, failure_category=None))\n"
        "rendered = AuditExplainer(audit).explain(run_id).render()\n"
        "assert 'Detected stockout risk for part part-101 affecting production order order-301.' in rendered\n"
        "assert 'Created replacement PO using create_replacement_po.' in rendered\n"
        "assert 'Receipt is still missing for replacement PO replacement-499; reopened the follow-up.' in rendered\n"
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
