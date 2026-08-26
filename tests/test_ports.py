"""Ports-and-adapters boundary contracts."""

from datetime import UTC, datetime

import pytest

from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    AuditEvent,
    AuditEventId,
    Evidence,
    EvidenceId,
    RunId,
    ScheduledTask,
    ScheduledTaskId,
    ScheduledTaskStatus,
    UserId,
)
from enterprise_agent.ports import (
    AuditPort,
    CalendarPort,
    ClockPort,
    ErpPort,
    EvidenceQuery,
    IdentityPort,
    KnowledgePort,
    LLMMessage,
    LLMPort,
    MailPort,
    PromptEnvelope,
    SchedulerPort,
    StructuredLLMResponse,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)


def actor() -> ActorContext:
    """Return the scoped actor shared by the fake port implementations."""
    return ActorContext(
        user_id=UserId("dana"),
        role="purchasing_manager",
        scopes=frozenset(),
        plant_ids=frozenset(),
        backup_approver_id=None,
        approval_limits={},
    )


def evidence() -> Evidence:
    """Return one versioned provider fact."""
    return Evidence(
        evidence_id=EvidenceId("evidence-1"),
        source="erp",
        record_type="purchase_order",
        record_id="po-1",
        source_version=1,
        observed_at=NOW,
        payload={},
    )


def knowledge_evidence() -> Evidence:
    """Return one provider-owned bulletin fact without conflating it with ERP evidence."""
    return Evidence(
        evidence_id=EvidenceId("knowledge:bulletin-1"),
        source="knowledge",
        record_type="supplier_risk_bulletin",
        record_id="bulletin-1",
        source_version=2,
        observed_at=NOW,
        payload={"status": "active"},
    )


class FakeEvidenceProvider:
    """In-memory implementation shared by the three read-only providers."""

    def query(self, actor_context: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        assert actor_context.user_id == UserId("dana")
        assert query.record_types == frozenset({"purchase_order"})
        return (evidence(),)


class FakeKnowledgeProvider:
    """In-memory scoped knowledge provider for the future Scenario C boundary."""

    def query(self, actor_context: ActorContext, query: EvidenceQuery) -> tuple[Evidence, ...]:
        assert actor_context.user_id == UserId("dana")
        assert query.record_types == frozenset({"supplier_risk_bulletin"})
        return (knowledge_evidence(),)


class FakeIdentity:
    """In-memory identity provider."""

    def actor_for(self, user_id: UserId) -> ActorContext:
        assert user_id == UserId("dana")
        return actor()


class FakeClock:
    """Fixed test clock."""

    def now(self) -> datetime:
        return NOW


class FakeAudit:
    """Append-only in-memory audit port."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        return tuple(event for event in self.events if event.run_id == run_id)


class FakeScheduler:
    """In-memory scheduler port."""

    def __init__(self) -> None:
        self.tasks: list[ScheduledTask] = []

    def schedule(self, task: ScheduledTask) -> None:
        self.tasks.append(task)

    def claim_due(self, now: datetime, limit: int) -> tuple[ScheduledTask, ...]:
        return tuple(task for task in self.tasks if task.due_at <= now)[:limit]

    def mark_succeeded(self, task_id: ScheduledTaskId, completed_at: datetime) -> None:
        assert task_id == ScheduledTaskId("task-1")
        assert completed_at == NOW


class FakeLLM:
    """Structured-output fake that never receives a provider-specific dependency."""

    def generate(self, prompt: PromptEnvelope) -> StructuredLLMResponse:
        assert prompt.purpose == "stockout_recommendation"
        return StructuredLLMResponse(
            provider="fake",
            model="fake-model",
            output={"outcome": "manual_review"},
        )


def test_application_depends_on_explicit_provider_and_control_plane_ports() -> None:
    """Every planned adapter boundary is structural, typed, and independently fakeable."""
    evidence_provider = FakeEvidenceProvider()
    knowledge_provider = FakeKnowledgeProvider()
    identity = FakeIdentity()
    clock = FakeClock()
    audit = FakeAudit()
    scheduler = FakeScheduler()
    llm = FakeLLM()
    query = EvidenceQuery(record_types=frozenset({"purchase_order"}))
    knowledge_query = EvidenceQuery(record_types=frozenset({"supplier_risk_bulletin"}))
    planned_attention = AttentionItem(
        attention_id=AttentionId("attention-1"),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key="stockout:po-1",
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions={},
    )
    prompt = PromptEnvelope(
        run_id=RunId("run-1"),
        actor=actor(),
        attention=planned_attention,
        evidence=(evidence(),),
        messages=(LLMMessage(role="system", content="Return structured output."),),
        purpose="stockout_recommendation",
        response_schema="scenario_a_recommendation",
    )
    task = ScheduledTask(
        task_id=ScheduledTaskId("task-1"),
        task_type="arrival_check",
        due_at=NOW,
        status=ScheduledTaskStatus.PENDING,
        idempotency_key="arrival-check:po-1",
        payload={},
        attempt_count=0,
        lease_expires_at=None,
        completed_at=None,
    )
    event = AuditEvent(
        event_id=AuditEventId("event-1"),
        occurred_at=NOW,
        event_type="attention.detected",
        run_id=RunId("run-1"),
        actor_id=UserId("dana"),
        attention_id=planned_attention.attention_id,
        workflow_id=None,
        plan_id=None,
        evidence_ids=(),
        payload={},
        policy_version=None,
        plan_hash=None,
        idempotency_key=None,
        failure_category=None,
    )

    assert isinstance(evidence_provider, ErpPort)
    assert isinstance(evidence_provider, MailPort)
    assert isinstance(evidence_provider, CalendarPort)
    assert isinstance(knowledge_provider, KnowledgePort)
    assert isinstance(identity, IdentityPort)
    assert isinstance(clock, ClockPort)
    assert isinstance(audit, AuditPort)
    assert isinstance(scheduler, SchedulerPort)
    assert isinstance(llm, LLMPort)
    assert evidence_provider.query(actor(), query) == (evidence(),)
    assert knowledge_provider.query(actor(), knowledge_query) == (knowledge_evidence(),)
    assert identity.actor_for(UserId("dana")) == actor()
    assert clock.now() == NOW

    audit.append(event)
    scheduler.schedule(task)
    scheduler.mark_succeeded(task.task_id, NOW)

    assert audit.events_for_run(RunId("run-1")) == (event,)
    assert scheduler.claim_due(NOW, limit=1) == (task,)
    assert llm.generate(prompt).output == {"outcome": "manual_review"}
    assert query.date_range is None
