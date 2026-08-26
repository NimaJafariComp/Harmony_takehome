"""Thin local verification-UI boundary contracts without business-service access."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field

import pytest

from enterprise_agent.application.llm_evaluation import LLMEvaluationUsage
from enterprise_agent.application.local_decisions import (
    ApprovalDecision,
    ApprovalDecisionAvailability,
    ApprovalDecisionResult,
    LocalApprovalDecisionStaleError,
)
from enterprise_agent.application.local_demo_controls import (
    DemoClockAdvanceResult,
    DemoClockControlAvailability,
    LocalDemoClockControlDisabledError,
)
from enterprise_agent.application.local_guided_demo import (
    GuidedDemoAvailability,
    GuidedDemoPersona,
    GuidedDemoReceipt,
    GuidedDemoReceiptCase,
    LocalGuidedDemoDisabledError,
)
from enterprise_agent.application.local_live_demo import (
    LiveDemoCaseOption,
    LiveDemoProfile,
    LocalLiveDemoAvailability,
    LocalLiveDemoPort,
    LocalLiveDemoReceipt,
)
from enterprise_agent.application.local_llm_evaluation import (
    LLMEvaluationAvailability,
    LLMEvaluationCaseOption,
    LLMEvaluationProfile,
    LLMEvaluationReceipt,
    LocalLLMEvaluationPort,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@dataclass
class RecordingLocalReviewService:
    """Return fixed safe read models while proving each HTTP path uses the presentation service."""

    requests: list[tuple[str, str | None]] = field(default_factory=list)

    def status(self) -> dict[str, object]:
        self.requests.append(("status", None))
        return {
            "pending_approvals": [
                {
                    "approval_id": "approval-a",
                    "plan_id": "plan-a",
                    "requester": "Dana Buyer",
                    "approver": "Avery Backup",
                    "decision_state": "rerouted",
                    "expires_at": "2026-08-24T13:00:00+00:00",
                    "audit_run_id": "run-a",
                }
            ],
            "workflows": [
                {
                    "workflow_id": "workflow-a",
                    "status": "running",
                    "current_step": "create_replacement_po",
                    "idempotency_key_prefix": "po-reroute:workflow-a",
                    "recovery_state": "in_progress",
                }
            ],
        }

    def attention(self, attention_id: str) -> dict[str, object]:
        self.requests.append(("attention", attention_id))
        return {
            "attention_id": attention_id,
            "scenario": "scenario_a",
            "cause": "projected_stockout",
            "status": "pending_approval",
            "created_at": "2026-08-24T09:00:00+00:00",
            "resolved_at": None,
            "evidence": [
                {
                    "evidence_id": "inventory:PART-X",
                    "source_version": 4,
                }
            ],
        }

    def approval(self, approval_id: str) -> dict[str, object]:
        self.requests.append(("approval", approval_id))
        return {
            "approval_id": approval_id,
            "plan_id": "plan-a",
            "attention_id": "attention-a",
            "requester_id": "dana",
            "approver_id": "avery",
            "decision_state": "rerouted",
            "requested_at": "2026-08-24T09:00:00+00:00",
            "expires_at": "2026-08-24T13:00:00+00:00",
            "decided_at": None,
            "intent": "enter_workflow",
            "workflow_name": "po_reroute",
            "workflow_version": 1,
            "policy_version": "scenario_a_policy:v1",
            "source_versions": {"inventory:PART-X": 4},
        }

    def workflow(self, workflow_id: str) -> dict[str, object]:
        self.requests.append(("workflow", workflow_id))
        return {
            "workflow_id": workflow_id,
            "plan_id": "plan-a",
            "definition_name": "po_reroute",
            "definition_version": 1,
            "status": "running",
            "current_step": 2,
            "recovery_state": "in_progress",
            "compensation_state": "not_required",
            "audit_run_id": "run-a",
            "created_at": "2026-08-24T09:00:00+00:00",
            "updated_at": "2026-08-24T09:01:00+00:00",
            "steps": [
                {
                    "step_index": 1,
                    "step_name": "verify_freshness",
                    "tool_name": None,
                    "status": "succeeded",
                    "attempt_count": 1,
                    "idempotency_key_prefix": "not started",
                }
            ],
        }

    def audit(self, run_id: str) -> dict[str, object]:
        self.requests.append(("audit", run_id))
        return {
            "run_id": run_id,
            "event_count": 2,
            "explanation": "Audit explanation for run run-a (2 events)",
        }

    def demo_clock(self) -> dict[str, object]:
        self.requests.append(("demo_clock", None))
        return {"current_at": "2026-08-24T09:00:00+00:00"}


@dataclass
class RecordingLocalDecisionService:
    """Record only the decision supplied through the new application-service boundary."""

    can_decide: bool = True
    stale: bool = False
    decisions: list[tuple[str, str]] = field(default_factory=list)

    def availability(self, approval_id: str) -> ApprovalDecisionAvailability:
        del approval_id
        return ApprovalDecisionAvailability(can_decide=self.can_decide)

    def decide(self, *, approval_id: str, decision: ApprovalDecision) -> ApprovalDecisionResult:
        if self.stale:
            raise LocalApprovalDecisionStaleError("internal stale source detail")
        self.decisions.append((approval_id, decision.value))
        return ApprovalDecisionResult(
            approval_id=approval_id,
            decision_state="approved" if decision.value == "approve" else "rejected",
            audit_run_id="run-a",
        )


@dataclass
class RecordingLocalDemoClockControlService:
    """Expose only the one fixed-duration demo-clock action to HTTP route contracts."""

    can_advance: bool = True
    advances: int = 0

    def availability(self) -> DemoClockControlAvailability:
        return DemoClockControlAvailability(can_advance=self.can_advance)

    def advance_one_day(self) -> DemoClockAdvanceResult:
        if not self.can_advance:
            raise LocalDemoClockControlDisabledError("demo mode is disabled")
        self.advances += 1
        return DemoClockAdvanceResult(current_at="2026-08-25T09:00:00+00:00")


@dataclass
class RecordingLocalGuidedDemoService:
    """Capture only explicit local demo-launch requests received through the UI boundary."""

    can_run: bool = True
    runs: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def availability(self) -> GuidedDemoAvailability:
        return GuidedDemoAvailability(
            can_run=self.can_run,
            personas=(
                GuidedDemoPersona(
                    persona_id="dana-buyer",
                    label="Dana Buyer",
                    role="Purchasing",
                    case_ids=("scenario-a-reroute-bait", "scenario-c-pending-review"),
                ),
                GuidedDemoPersona(
                    persona_id="quinn-quality-manager",
                    label="Quinn Quality Manager",
                    role="Quality",
                    case_ids=("scenario-b-capacity",),
                ),
            ),
        )

    def run(self, *, persona_id: str, case_ids: tuple[str, ...]) -> GuidedDemoReceipt:
        from enterprise_agent.review_provenance import (
            GateStatus,
            PlannerMode,
            PlannerProvenance,
            SchemaValidation,
        )

        if not self.can_run:
            raise LocalGuidedDemoDisabledError("unavailable")
        self.runs.append((persona_id, case_ids))
        return GuidedDemoReceipt(
            persona_label="Dana Buyer",
            cases=(
                GuidedDemoReceiptCase(
                    case_id="scenario-a-reroute-bait",
                    title="Scenario A — viable reroute rejects the tempting supplier",
                    execution_mode="stage_pending",
                    outcome="Supplier Z remains the only viable alternate.",
                    next_safe_action="Review the pending approval.",
                    run_id="demo-scenario-a-reroute",
                    approval_id="approval-a",
                    workflow_id=None,
                    provenance=PlannerProvenance(
                        mode=PlannerMode.FAKE_DETERMINISTIC,
                        provider=None,
                        profile=None,
                        model="deterministic-fake-v1",
                        schema_validation=SchemaValidation.PASSED,
                        gate_status=GateStatus.PENDING_APPROVAL,
                    ),
                ),
            ),
        )


@dataclass
class RecordingLocalLLMEvaluationService(LocalLLMEvaluationPort):
    """Capture one explicitly submitted fixed synthetic evaluation without making a provider call."""

    can_evaluate: bool = True
    evaluations: list[tuple[str, str]] = field(default_factory=list)

    def availability(self) -> LLMEvaluationAvailability:
        return LLMEvaluationAvailability(
            can_evaluate=self.can_evaluate,
            profiles=(LLMEvaluationProfile(profile="openai", model="gpt-5.6-luna"),),
            cases=(
                LLMEvaluationCaseOption(
                    case_id="a-unapproved-bait",
                    scenario="scenario_a",
                    title="Unapproved supplier bait",
                ),
            ),
        )

    def evaluate(self, *, profile_id: str, case_id: str) -> LLMEvaluationReceipt:
        self.evaluations.append((profile_id, case_id))
        from enterprise_agent.application.llm_evaluation import LLMEvaluationReport
        from enterprise_agent.review_provenance import (
            GateStatus,
            PlannerMode,
            PlannerProvenance,
            SchemaValidation,
        )

        report = LLMEvaluationReport(
            observations=(),
            usage=_evaluation_usage(),
        )
        return LLMEvaluationReceipt(
            profile="openai",
            model="gpt-5.6-luna",
            case_id=case_id,
            case_title="Unapproved supplier bait",
            report=report,
            provenance=PlannerProvenance(
                mode=PlannerMode.LIVE,
                provider="openai",
                profile="openai",
                model="gpt-5.6-luna",
                schema_validation=SchemaValidation.PASSED,
                gate_status=GateStatus.NOT_INVOKED_NO_WRITE_EVALUATION,
            ),
        )


@dataclass
class RecordingLocalLiveDemoService(LocalLiveDemoPort):
    """Capture a browser-approved fixed live proposal without constructing a real provider adapter."""

    can_run: bool = True
    runs: list[tuple[str, str]] = field(default_factory=list)

    def availability(self) -> LocalLiveDemoAvailability:
        return LocalLiveDemoAvailability(
            can_run=self.can_run,
            profiles=(LiveDemoProfile(profile="openai", model="gpt-5.6-luna"),),
            cases=(
                LiveDemoCaseOption(
                    case_id="scenario-a-reroute",
                    scenario="scenario_a",
                    title="Scenario A — supplier reroute proposal",
                ),
            ),
        )

    def run(self, *, profile_id: str, case_id: str) -> LocalLiveDemoReceipt:
        from enterprise_agent.review_provenance import (
            GateStatus,
            PlannerMode,
            PlannerProvenance,
            SchemaValidation,
        )

        self.runs.append((profile_id, case_id))
        return LocalLiveDemoReceipt(
            case_id=case_id,
            case_title="Scenario A — supplier reroute proposal",
            profile="openai",
            model="gpt-5.6-luna",
            planner_status="succeeded",
            outcome="ENTER_WORKFLOW",
            provenance=PlannerProvenance(
                mode=PlannerMode.LIVE,
                provider="openai",
                profile="openai",
                model="gpt-5.6-luna",
                schema_validation=SchemaValidation.PASSED,
                gate_status=GateStatus.PENDING_APPROVAL,
            ),
            run_id="live-demo:scenario-a-reroute",
            attention_id="00000000-0000-0000-0000-000000000901",
            plan_id="00000000-0000-0000-0000-000000000902",
            approval_id="00000000-0000-0000-0000-000000000903",
            workflow_id="00000000-0000-0000-0000-000000000904",
            escalation_task_id="00000000-0000-0000-0000-000000000905",
            usage=None,
        )


def _evaluation_usage() -> LLMEvaluationUsage:
    """Build an empty scalar-only report usage summary for the presentation contract."""
    from decimal import Decimal

    return LLMEvaluationUsage(
        request_count=1,
        metered_request_count=1,
        unmetered_request_count=0,
        unknown_cost_request_count=0,
        input_tokens=120,
        cached_input_tokens=0,
        output_tokens=30,
        total_tokens=150,
        total_cost_usd=Decimal("0.000060"),
    )


@pytest.mark.critical
async def test_local_ui_landing_page_is_explicitly_read_only_and_uses_only_local_assets() -> None:
    """A reviewer gets a useful local entry point without credentials, provider setup, or write controls."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://testserver"
    ) as client:
        response = await client.get("/")
        stylesheet = await client.get("/static/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Enterprise Agent / Local Review" in response.text
    assert 'href="/demo"' in response.text
    assert "Local verification surface" in response.text
    assert (
        "No automatic provider call · no credential display · no business-system write"
        in response.text
    )
    assert "API key" not in response.text
    assert "<script" not in response.text
    assert stylesheet.status_code == 200
    assert ":focus-visible" in stylesheet.text
    assert "prefers-reduced-motion" in stylesheet.text


async def test_local_ui_health_is_database_free_and_says_what_is_safe_to_expect() -> None:
    """Health checks prove only that the local presentation process is ready, never that business state exists."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://testserver"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "scope": "local_read_only_ui",
        "database_access": False,
        "provider_access": False,
    }


@pytest.mark.critical
async def test_local_ui_exposes_only_selected_actor_read_models_through_the_service_boundary() -> (
    None
):
    """Every operational API path is a safe GET projection; the route never owns business data."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    service = RecordingLocalReviewService()
    expected_status = {
        "pending_approvals": [
            {
                "approval_id": "approval-a",
                "plan_id": "plan-a",
                "requester": "Dana Buyer",
                "approver": "Avery Backup",
                "decision_state": "rerouted",
                "expires_at": "2026-08-24T13:00:00+00:00",
                "audit_run_id": "run-a",
            }
        ],
        "workflows": [
            {
                "workflow_id": "workflow-a",
                "status": "running",
                "current_step": "create_replacement_po",
                "idempotency_key_prefix": "po-reroute:workflow-a",
                "recovery_state": "in_progress",
            }
        ],
    }
    async with AsyncClient(
        transport=ASGITransport(app=create_app(read_service=service)),
        base_url="http://testserver",
    ) as client:
        status = await client.get("/api/status")
        attention = await client.get("/api/attention/attention-a")
        approval = await client.get("/api/approval/approval-a")
        workflow = await client.get("/api/workflow/workflow-a")
        audit = await client.get("/api/audit/run-a")
        demo_clock = await client.get("/api/demo-clock")

    assert status.status_code == 200
    assert status.json() == expected_status
    assert attention.json()["evidence"] == [
        {"evidence_id": "inventory:PART-X", "source_version": 4}
    ]
    assert approval.json()["source_versions"] == {"inventory:PART-X": 4}
    assert workflow.json()["steps"][0]["idempotency_key_prefix"] == "not started"
    assert workflow.json()["compensation_state"] == "not_required"
    assert audit.json() == {
        "run_id": "run-a",
        "event_count": 2,
        "explanation": "Audit explanation for run run-a (2 events)",
    }
    assert demo_clock.json() == {"current_at": "2026-08-24T09:00:00+00:00"}
    assert service.requests == [
        ("status", None),
        ("attention", "attention-a"),
        ("approval", "approval-a"),
        ("workflow", "workflow-a"),
        ("audit", "run-a"),
        ("demo_clock", None),
    ]

    application = create_app(read_service=service)
    mutable_routes = {
        (path, method)
        for route in application.routes
        for path in (getattr(route, "path", None),)
        for method in getattr(route, "methods", set())
        if isinstance(path, str) and method not in {"GET", "HEAD"}
    }
    assert mutable_routes == {
        ("/approval/{approval_id}/decision", "POST"),
        ("/demo-clock/advance", "POST"),
        ("/demo/run", "POST"),
        ("/demo/evaluate", "POST"),
        ("/demo/live", "POST"),
    }


def test_fastapi_testclient_renders_actor_scoped_ledger_and_workflow_audit_detail() -> None:
    """The browser client sees the same scoped evidence as the API, including recovery and audit links."""
    from fastapi.testclient import TestClient

    from enterprise_agent.web import create_app

    service = RecordingLocalReviewService()
    with TestClient(create_app(read_service=service)) as client:
        status = client.get("/api/status")
        ledger = client.get("/")
        workflow = client.get("/workflow/workflow-a")
        audit = client.get("/audit/run-a")

    assert status.status_code == 200
    assert status.json()["pending_approvals"][0]["approver"] == "Avery Backup"
    assert ledger.status_code == 200
    assert 'href="/approval/approval-a"' in ledger.text
    assert workflow.status_code == 200
    assert "Compensation" in workflow.text
    assert "not required" in workflow.text
    assert 'href="/audit/run-a"' in workflow.text
    assert audit.status_code == 200
    assert "Audit explanation for run run-a" in audit.text
    assert service.requests == [
        ("status", None),
        ("status", None),
        ("workflow", "workflow-a"),
        ("audit", "run-a"),
    ]


def test_fastapi_testclient_rejects_cross_actor_ledger_access() -> None:
    """A TestClient request cannot use a visible opaque ID to cross the selected actor boundary."""
    from fastapi.testclient import TestClient

    from enterprise_agent.application.local_review import LocalReviewAccessDeniedError
    from enterprise_agent.web import create_app

    class RefusingReviewService(RecordingLocalReviewService):
        def attention(self, attention_id: str) -> dict[str, object]:
            raise LocalReviewAccessDeniedError("cross-actor resource")

    with TestClient(create_app(read_service=RefusingReviewService())) as client:
        denied = client.get("/api/attention/other-actor-attention")

    assert denied.status_code == 403
    assert denied.json() == {"detail": "The selected demo actor cannot view this resource."}
    assert "other-actor-attention" not in denied.text


async def test_local_ui_rejects_unknown_cross_actor_and_unconfigured_read_resources() -> None:
    """A route reveals neither the existence nor the contents of resources outside its selected actor."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.application.local_review import (
        LocalReviewAccessDeniedError,
        LocalReviewResourceNotFoundError,
    )
    from enterprise_agent.web import create_app

    class RefusingReviewService(RecordingLocalReviewService):
        def attention(self, attention_id: str) -> dict[str, object]:
            raise LocalReviewAccessDeniedError("cross-actor resource")

        def approval(self, approval_id: str) -> dict[str, object]:
            raise LocalReviewResourceNotFoundError("unknown resource")

    async with AsyncClient(
        transport=ASGITransport(app=create_app(read_service=RefusingReviewService())),
        base_url="http://testserver",
    ) as client:
        forbidden = await client.get("/api/attention/other-actor-attention")
        missing = await client.get("/api/approval/not-a-real-approval")

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://testserver"
    ) as client:
        unconfigured = await client.get("/api/status")

    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "The selected demo actor cannot view this resource."}
    assert "other-actor-attention" not in forbidden.text
    assert missing.status_code == 404
    assert missing.json() == {"detail": "The requested review resource is unavailable."}
    assert "not-a-real-approval" not in missing.text
    assert unconfigured.status_code == 503
    assert unconfigured.json() == {"detail": "Local review data is not configured."}


@pytest.mark.critical
async def test_local_ui_renders_semantic_evidence_ledger_pages_from_the_safe_read_service() -> None:
    """A reviewer can follow an approval, its evidence, workflow, and audit trail without client code."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    service = RecordingLocalReviewService()
    async with AsyncClient(
        transport=ASGITransport(app=create_app(read_service=service)),
        base_url="http://testserver",
    ) as client:
        status = await client.get("/")
        approval = await client.get("/approval/approval-a")
        attention = await client.get("/attention/attention-a")
        workflow = await client.get("/workflow/workflow-a")
        audit = await client.get("/audit/run-a")
        stylesheet = await client.get("/static/app.css")

    assert status.status_code == 200
    assert status.headers["content-type"].startswith("text/html")
    assert "What awaits approval" in status.text
    assert 'href="/approval/approval-a"' in status.text
    assert 'href="/audit/run-a"' in status.text
    assert 'href="/workflow/workflow-a"' in status.text
    assert "rerouted" in status.text
    assert "<script" not in status.text
    assert 'data-state="rerouted"' in status.text

    assert approval.status_code == 200
    assert "Approval / approval-a" in approval.text
    assert 'href="/attention/attention-a"' in approval.text
    assert "scenario_a_policy:v1" in approval.text
    assert "do-not-render" not in approval.text

    assert attention.status_code == 200
    assert "Evidence references" in attention.text
    assert "inventory:PART-X" in attention.text
    assert "Source version" in attention.text

    assert workflow.status_code == 200
    assert "Recovery state" in workflow.text
    assert "verify_freshness" in workflow.text
    assert "not started" in workflow.text

    assert audit.status_code == 200
    assert "Audit / run-a" in audit.text
    assert "Audit explanation for run run-a" in audit.text

    assert ".ledger-table-wrap" in stylesheet.text
    assert "overflow-x: auto" in stylesheet.text
    assert "@media (max-width:" in stylesheet.text
    assert "[data-state]" in stylesheet.text
    assert service.requests == [
        ("status", None),
        ("approval", "approval-a"),
        ("attention", "attention-a"),
        ("workflow", "workflow-a"),
        ("audit", "run-a"),
    ]


@pytest.mark.critical
async def test_local_ui_advances_one_demo_day_only_with_a_bound_csrf_form() -> None:
    """The visible demo control is explicit, fixed-duration, local, and unavailable without its token."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    controls = RecordingLocalDemoClockControlService()
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(
                read_service=RecordingLocalReviewService(),
                demo_clock_control_service=controls,
            )
        ),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/demo-clock")
        token = re.search(
            r'<form[^>]+action="/demo-clock/advance"[^>]*>.*?'
            r'name="csrf_token" value="([^"]+)"',
            page.text,
            flags=re.DOTALL,
        )
        assert token is not None
        submitted = await client.post("/demo-clock/advance", data={"csrf_token": token.group(1)})
        forged = await client.post("/demo-clock/advance", data={"csrf_token": "forged"})

    assert page.status_code == 200
    assert "Advance demo clock one day" in page.text
    assert "DEMO_MODE" not in page.text
    assert submitted.status_code == 200
    assert "Demo time advanced" in submitted.text
    assert "2026-08-25T09:00:00+00:00" in submitted.text
    assert forged.status_code == 403
    assert controls.advances == 1


async def test_local_ui_keeps_demo_clock_controls_unavailable_without_safe_composition() -> None:
    """A read-capable local UI does not gain a mutable clock path merely because it has review data."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    controls = RecordingLocalDemoClockControlService(can_advance=False)
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(
                read_service=RecordingLocalReviewService(),
                demo_clock_control_service=controls,
            )
        ),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/demo-clock")
        forbidden = await client.post("/demo-clock/advance", data={"csrf_token": "forged"})

    assert page.status_code == 200
    assert "Demo clock controls are unavailable" in page.text
    assert 'action="/demo-clock/advance"' not in page.text
    assert forbidden.status_code == 403
    assert controls.advances == 0


async def test_local_ui_exposes_a_demo_tab_without_a_demo_mode_environment_setting() -> None:
    """A fresh reviewer can discover deterministic cases in one dedicated UI area without config trivia."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app(read_service=RecordingLocalReviewService())),
        base_url="http://testserver",
    ) as client:
        demo = await client.get("/demo")

    assert demo.status_code == 200
    assert "Demo mode" in demo.text
    assert "Scenario A, B, and C review cases" in demo.text
    assert "DEMO_MODE" not in demo.text


async def test_local_ui_runs_only_an_explicit_csrf_bound_persona_matched_guided_demo() -> None:
    """A reviewer chooses visible scenario cards and a seeded persona before the bounded reset/stage run."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    launcher = RecordingLocalGuidedDemoService()
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(
                read_service=RecordingLocalReviewService(),
                guided_demo_service=launcher,
            )
        ),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/demo")
        token = re.search(
            r'<form[^>]+action="/demo/run"[^>]*>.*?name="csrf_token" value="([^"]+)"',
            page.text,
            flags=re.DOTALL,
        )
        assert token is not None
        submitted = await client.post(
            "/demo/run",
            data={
                "csrf_token": token.group(1),
                "persona_id": "dana-buyer",
                "case_id": ["scenario-a-reroute-bait", "scenario-c-pending-review"],
                "confirm": "run",
            },
        )
        replay = await client.post(
            "/demo/run",
            data={
                "csrf_token": token.group(1),
                "persona_id": "dana-buyer",
                "case_id": "scenario-a-reroute-bait",
                "confirm": "run",
            },
        )

    assert page.status_code == 200
    assert "Dana Buyer · Purchasing" in page.text
    assert "Quinn Quality Manager · Quality" in page.text
    assert "Replace local synthetic demo data" in page.text
    assert submitted.status_code == 200
    assert "Guided demo ready" in submitted.text
    assert "Planner: FAKE / DETERMINISTIC" in submitted.text
    assert 'href="/approval/approval-a"' in submitted.text
    assert 'href="/audit/demo-scenario-a-reroute"' in submitted.text
    assert replay.status_code == 403
    assert launcher.runs == [
        ("dana-buyer", ("scenario-a-reroute-bait", "scenario-c-pending-review"))
    ]


async def test_local_ui_fails_closed_when_guided_demo_is_not_composed() -> None:
    """The page may show stories, but cannot expose a reset/stage form without strict composition."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app(read_service=RecordingLocalReviewService())),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/demo")
        forged = await client.post(
            "/demo/run",
            data={
                "csrf_token": "forged",
                "persona_id": "dana-buyer",
                "case_id": "scenario-a-reroute-bait",
                "confirm": "run",
            },
        )

    assert page.status_code == 200
    assert "Guided demo launcher is unavailable" in page.text
    assert 'action="/demo/run"' not in page.text
    assert forged.status_code == 403


async def test_local_ui_runs_one_explicit_csrf_bound_no_write_llm_evaluation() -> None:
    """A configured profile and one fixed synthetic case are selected without exposing a key or model output."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    evaluator = RecordingLocalLLMEvaluationService()
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(
                read_service=RecordingLocalReviewService(),
                llm_evaluation_service=evaluator,
            )
        ),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/demo")
        token = re.search(
            r'<form[^>]+action="/demo/evaluate"[^>]*>.*?name="csrf_token" value="([^"]+)"',
            page.text,
            flags=re.DOTALL,
        )
        assert token is not None
        result = await client.post(
            "/demo/evaluate",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "a-unapproved-bait",
                "confirm": "evaluate",
            },
        )

    assert page.status_code == 200
    assert "OpenAI · gpt-5.6-luna" in page.text
    assert "No database, ERP, mail, workflow, or audit write" in page.text
    assert "API key" not in page.text
    assert result.status_code == 200
    assert "LLM evaluation complete" in result.text
    assert "Planner: LIVE" in result.text
    assert "Schema validation" in result.text
    assert "Not invoked (no-write evaluation)" in result.text
    assert "Token total" in result.text
    assert "Cost total" in result.text
    assert evaluator.evaluations == [("openai", "a-unapproved-bait")]


async def test_local_ui_runs_one_explicit_csrf_bound_guarded_live_demo() -> None:
    """The separate live route allows one fixed local story and renders only review-safe control facts."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    live_demo = RecordingLocalLiveDemoService()
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(
                read_service=RecordingLocalReviewService(),
                live_demo_service=live_demo,
            )
        ),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/demo")
        token = re.search(
            r'<form[^>]+action="/demo/live"[^>]*>.*?name="csrf_token" value="([^"]+)"',
            page.text,
            flags=re.DOTALL,
        )
        assert token is not None
        result = await client.post(
            "/demo/live",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "scenario-a-reroute",
                "confirm": "live",
            },
        )

    assert page.status_code == 200
    assert "Guarded live local demo" in page.text
    assert "Call the selected provider once" in page.text
    assert "API key" not in page.text
    assert result.status_code == 200
    assert "Guarded live proposal staged" in result.text
    assert "Planner: LIVE" in result.text
    assert "Passed to pending approval" in result.text
    assert 'href="/audit/live-demo:scenario-a-reroute"' in result.text
    assert 'href="/approval/00000000-0000-0000-0000-000000000903"' in result.text
    assert 'href="/workflow/00000000-0000-0000-0000-000000000904"' in result.text
    assert "prompt" in result.text
    assert "secret-openai" not in result.text
    assert live_demo.runs == [("openai", "scenario-a-reroute")]


def test_fastapi_testclient_keeps_live_demo_tokens_action_bound_and_one_time() -> None:
    """A live-demo token cannot authorize a no-write evaluation, cross-origin request, or replay."""
    from fastapi.testclient import TestClient

    from enterprise_agent.web import create_app

    evaluator = RecordingLocalLLMEvaluationService()
    live_demo = RecordingLocalLiveDemoService()
    with TestClient(
        create_app(
            read_service=RecordingLocalReviewService(),
            llm_evaluation_service=evaluator,
            live_demo_service=live_demo,
        )
    ) as client:
        page = client.get("/demo")
        token = re.search(
            r'<form[^>]+action="/demo/live"[^>]*>.*?name="csrf_token" value="([^"]+)"',
            page.text,
            flags=re.DOTALL,
        )
        assert token is not None
        evaluation_attempt = client.post(
            "/demo/evaluate",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "a-unapproved-bait",
                "confirm": "evaluate",
            },
        )
        cross_origin = client.post(
            "/demo/live",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "scenario-a-reroute",
                "confirm": "live",
            },
            headers={"Origin": "https://untrusted.example"},
        )
        completed = client.post(
            "/demo/live",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "scenario-a-reroute",
                "confirm": "live",
            },
        )
        replay = client.post(
            "/demo/live",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "scenario-a-reroute",
                "confirm": "live",
            },
        )

    assert evaluation_attempt.status_code == 403
    assert cross_origin.status_code == 403
    assert completed.status_code == 200
    assert replay.status_code == 403
    assert evaluator.evaluations == []
    assert live_demo.runs == [("openai", "scenario-a-reroute")]


def test_fastapi_testclient_keeps_live_llm_evaluation_tokens_action_bound_and_one_time() -> None:
    """A live-evaluation token cannot authorize a local reset, cross-origin request, or replayed cost."""
    from fastapi.testclient import TestClient

    from enterprise_agent.web import create_app

    launcher = RecordingLocalGuidedDemoService()
    evaluator = RecordingLocalLLMEvaluationService()
    with TestClient(
        create_app(
            read_service=RecordingLocalReviewService(),
            guided_demo_service=launcher,
            llm_evaluation_service=evaluator,
        )
    ) as client:
        page = client.get("/demo")
        token = re.search(
            r'<form[^>]+action="/demo/evaluate"[^>]*>.*?name="csrf_token" value="([^"]+)"',
            page.text,
            flags=re.DOTALL,
        )
        assert token is not None
        reset_attempt = client.post(
            "/demo/run",
            data={
                "csrf_token": token.group(1),
                "persona_id": "dana-buyer",
                "case_id": "scenario-a-reroute-bait",
                "confirm": "run",
            },
        )
        cross_origin = client.post(
            "/demo/evaluate",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "a-unapproved-bait",
                "confirm": "evaluate",
            },
            headers={"Origin": "https://untrusted.example"},
        )
        completed = client.post(
            "/demo/evaluate",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "a-unapproved-bait",
                "confirm": "evaluate",
            },
        )
        replay = client.post(
            "/demo/evaluate",
            data={
                "csrf_token": token.group(1),
                "profile_id": "openai",
                "case_id": "a-unapproved-bait",
                "confirm": "evaluate",
            },
        )

    assert reset_attempt.status_code == 403
    assert cross_origin.status_code == 403
    assert completed.status_code == 200
    assert replay.status_code == 403
    assert launcher.runs == []
    assert evaluator.evaluations == [("openai", "a-unapproved-bait")]


async def test_local_ui_reuses_the_shared_a_b_c_demo_catalogue_without_running_it() -> None:
    """All three scenarios stay understandable in one UI view before an explicit launcher request."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app(read_service=RecordingLocalReviewService())),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/demo-clock")

    assert page.status_code == 200
    assert "Scenario A — viable reroute rejects the tempting supplier" in page.text
    assert "Scenario B — quality-lot capacity respects commitments" in page.text
    assert "Scenario C — supplier-risk bulletin awaits review" in page.text
    assert "fixed acceptance-case walkthrough" in page.text
    assert "Guided demo launcher is unavailable" in page.text


async def test_local_ui_renders_a_safe_html_error_page_for_a_missing_ledger_record() -> None:
    """Browser navigation receives clear recovery guidance while API callers retain their JSON contract."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.application.local_review import LocalReviewResourceNotFoundError
    from enterprise_agent.web import create_app

    class MissingReviewService(RecordingLocalReviewService):
        def workflow(self, workflow_id: str) -> dict[str, object]:
            raise LocalReviewResourceNotFoundError("missing internal detail")

    async with AsyncClient(
        transport=ASGITransport(app=create_app(read_service=MissingReviewService())),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/workflow/not-a-real-workflow")
        api = await client.get("/api/workflow/not-a-real-workflow")

    assert page.status_code == 404
    assert page.headers["content-type"].startswith("text/html")
    assert "Review record unavailable" in page.text
    assert "missing internal detail" not in page.text
    assert "not-a-real-workflow" not in page.text
    assert api.status_code == 404
    assert api.json() == {"detail": "The requested review resource is unavailable."}


@pytest.mark.critical
async def test_local_ui_submits_a_csrf_bound_decision_without_rendering_a_plan_hash() -> None:
    """The current approver submits a named action through the application boundary and sees its receipt."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    review_service = RecordingLocalReviewService()
    decision_service = RecordingLocalDecisionService()
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(read_service=review_service, decision_service=decision_service)
        ),
        base_url="http://testserver",
    ) as client:
        page = await client.get("/approval/approval-a")
        approve_match = re.search(
            r'<form[^>]+action="/approval/approval-a/decision"[^>]*>.*?'
            r'name="csrf_token" value="([^"]+)".*?'
            r'name="decision" value="approve"',
            page.text,
            flags=re.DOTALL,
        )
        assert approve_match is not None
        submitted = await client.post(
            "/approval/approval-a/decision",
            data={
                "approval_id": "approval-a",
                "csrf_token": approve_match.group(1),
                "decision": "approve",
            },
        )

    assert page.status_code == 200
    assert 'name="approval_id" value="approval-a"' in page.text
    assert "plan_hash" not in page.text
    assert "sha256:" not in page.text
    assert "Approve plan" in page.text
    assert "Reject plan" in page.text
    assert submitted.status_code == 200
    assert "Approval recorded" in submitted.text
    assert 'href="/audit/run-a"' in submitted.text
    assert decision_service.decisions == [("approval-a", "approve")]


async def test_local_ui_refuses_missing_csrf_or_stale_decisions_without_leaking_internal_detail() -> (
    None
):
    """Forged submissions and fresh-read failures do not mutate a plan or reveal implementation detail."""
    from httpx import ASGITransport, AsyncClient

    from enterprise_agent.web import create_app

    stale_decision_service = RecordingLocalDecisionService(stale=True)
    async with AsyncClient(
        transport=ASGITransport(
            app=create_app(
                read_service=RecordingLocalReviewService(),
                decision_service=stale_decision_service,
            )
        ),
        base_url="http://testserver",
    ) as client:
        missing_csrf = await client.post(
            "/approval/approval-a/decision",
            data={"approval_id": "approval-a", "decision": "approve"},
        )
        page = await client.get("/approval/approval-a")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert token is not None
        stale = await client.post(
            "/approval/approval-a/decision",
            data={
                "approval_id": "approval-a",
                "csrf_token": token.group(1),
                "decision": "approve",
            },
        )

    assert missing_csrf.status_code == 403
    assert "Decision request expired" in missing_csrf.text
    assert stale.status_code == 409
    assert "Approval needs a fresh review" in stale.text
    assert "internal stale source detail" not in stale.text
    assert stale_decision_service.decisions == []


def test_fastapi_testclient_submits_only_bound_same_origin_forms_and_handles_staleness() -> None:
    """Browser forms cannot cross-authorize actions, bypass origin checks, or execute stale plans."""
    from fastapi.testclient import TestClient

    from enterprise_agent.web import create_app

    decisions = RecordingLocalDecisionService()
    controls = RecordingLocalDemoClockControlService()
    with TestClient(
        create_app(
            read_service=RecordingLocalReviewService(),
            decision_service=decisions,
            demo_clock_control_service=controls,
        )
    ) as client:
        clock_page = client.get("/demo-clock")
        clock_token = re.search(r'name="csrf_token" value="([^"]+)"', clock_page.text)
        assert clock_token is not None
        cross_action = client.post(
            "/approval/approval-a/decision",
            data={
                "approval_id": "approval-a",
                "csrf_token": clock_token.group(1),
                "decision": "approve",
            },
        )

        approval_page = client.get("/approval/approval-a")
        approval_token = re.search(r'name="csrf_token" value="([^"]+)"', approval_page.text)
        assert approval_token is not None
        cross_origin = client.post(
            "/approval/approval-a/decision",
            data={
                "approval_id": "approval-a",
                "csrf_token": approval_token.group(1),
                "decision": "approve",
            },
            headers={"Origin": "https://untrusted.example"},
        )
        submitted = client.post(
            "/approval/approval-a/decision",
            data={
                "approval_id": "approval-a",
                "csrf_token": approval_token.group(1),
                "decision": "approve",
            },
        )

        decisions.stale = True
        stale_page = client.get("/approval/approval-a")
        stale_token = re.search(r'name="csrf_token" value="([^"]+)"', stale_page.text)
        assert stale_token is not None
        stale = client.post(
            "/approval/approval-a/decision",
            data={
                "approval_id": "approval-a",
                "csrf_token": stale_token.group(1),
                "decision": "approve",
            },
        )

    assert cross_action.status_code == 403
    assert cross_origin.status_code == 403
    assert submitted.status_code == 200
    assert "Approval recorded" in submitted.text
    assert stale.status_code == 409
    assert "Approval needs a fresh review" in stale.text
    assert decisions.decisions == [("approval-a", "approve")]
    assert controls.advances == 0


def test_fastapi_testclient_fails_closed_when_demo_control_is_revoked_after_render() -> None:
    """A valid rendered token cannot advance time if the guarded local control becomes unavailable."""
    from fastapi.testclient import TestClient

    from enterprise_agent.web import create_app

    controls = RecordingLocalDemoClockControlService()
    with TestClient(
        create_app(
            read_service=RecordingLocalReviewService(),
            demo_clock_control_service=controls,
        )
    ) as client:
        page = client.get("/demo-clock")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert token is not None
        controls.can_advance = False
        forbidden = client.post("/demo-clock/advance", data={"csrf_token": token.group(1)})

    assert forbidden.status_code == 403
    assert "Demo clock controls are unavailable" in forbidden.text
    assert controls.advances == 0


def test_local_ui_module_has_no_direct_database_provider_or_configuration_dependency() -> None:
    """The UI boundary cannot turn into a parallel control plane or expose a credential setup path."""
    from enterprise_agent import web

    source = inspect.getsource(web)

    for forbidden_reference in (
        "enterprise_agent.adapters",
        "enterprise_agent.config",
        "Postgres",
        "load_provider",
        "create_no_write_adapter",
        "DATABASE_URL",
        "API_KEY",
    ):
        assert forbidden_reference not in source


def test_local_ui_main_binds_to_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting the optional review server uses its configured local reader and stays loopback-only."""
    from enterprise_agent import web

    observed: dict[str, object] = {}
    service = RecordingLocalReviewService()

    def fake_run(app: object, *, host: str, port: int) -> None:
        observed.update(app=app, host=host, port=port)

    monkeypatch.setattr("enterprise_agent.web.create_local_review_service", lambda: service)
    monkeypatch.setattr("enterprise_agent.web.uvicorn.run", fake_run)

    web.main()

    assert observed["app"] is not web.app
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 8080


def test_local_ui_accepts_only_the_compose_bridge_bind_override() -> None:
    """The container may bridge to a loopback-published host port, but no other bind is accepted."""
    from enterprise_agent.web import _ui_bind_host

    assert _ui_bind_host({}) == "127.0.0.1"
    assert _ui_bind_host({"ENTERPRISE_AGENT_UI_BIND_HOST": "0.0.0.0"}) == "0.0.0.0"
    assert _ui_bind_host({"ENTERPRISE_AGENT_UI_BIND_HOST": "203.0.113.8"}) == "127.0.0.1"
