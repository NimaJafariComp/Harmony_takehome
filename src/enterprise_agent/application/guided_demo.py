"""Local-only, deterministic operator stories for the guided terminal demo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from enterprise_agent.adapters import (
    PostgresAttentionAdapter,
    PostgresAuditAdapter,
    PostgresCalendarAdapter,
    PostgresDemoClock,
    PostgresErpAdapter,
    PostgresIdentityAdapter,
    PostgresMailAdapter,
    PostgresPlanApprovalAdapter,
)
from enterprise_agent.application.approvals import PlanApprovalService
from enterprise_agent.application.candidates import (
    SupplierCandidateFilter,
    SupplierExclusionReason,
)
from enterprise_agent.application.context import ScenarioAContextAssembler
from enterprise_agent.application.planning import (
    EnterWorkflowRecommendation,
    FakeLLMPort,
    validate_scenario_a_recommendation,
)
from enterprise_agent.application.scenario_c_demo import (
    ScenarioCPendingRun,
    stage_scenario_c_pending,
)
from enterprise_agent.application.stockout import StockoutDetector
from enterprise_agent.domain import ApprovalId, AttentionId, PlanId, RunId, UserId
from enterprise_agent.ports import LLMMessage, PromptEnvelope
from enterprise_agent.review_provenance import (
    GateStatus,
    PlannerMode,
    PlannerProvenance,
    SchemaValidation,
)
from enterprise_agent.seed import (
    ID_DANA,
    ID_LOT_GOOD,
    ID_LOT_NO_COVER,
    ID_PO_4812_Y,
    ID_PRODUCTION_4812,
    ID_PRODUCTION_Q7001,
    ID_PRODUCTION_Q7002,
    ID_SUPPLIER_BAIT,
    ID_SUPPLIER_Z,
    reset_database,
    seed_database,
)


class DemoCaseSelectionError(ValueError):
    """Raised before a reset when an operator asks for an invalid demo selection."""


class GuidedDemoExecutionError(ValueError):
    """Raised when the fixed seed no longer proves the deterministic demo contract."""


class DemoExecutionMode(StrEnum):
    """Describe whether a guided case stages a real pending plan or displays a safe fixture."""

    STAGE_PENDING = "stage_pending"
    FIXTURE = "fixture"


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoIdentifier:
    """One copyable seeded, staged, or fixture identifier shown by the terminal adapter."""

    label: str
    value: str
    included: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedDemoCase:
    """One concise business story with a deterministic, explicitly bounded outcome."""

    case_id: str
    title: str
    execution_mode: DemoExecutionMode
    phase: str
    outcome: str
    next_safe_action: str
    identifiers: tuple[DemoIdentifier, ...]

    @property
    def planner_provenance(self) -> PlannerProvenance:
        """Expose explicit fake-planner and gate facts without claiming a fixture executed a planner."""
        if self.execution_mode is DemoExecutionMode.STAGE_PENDING:
            return PlannerProvenance(
                mode=PlannerMode.FAKE_DETERMINISTIC,
                provider=None,
                profile=None,
                model="deterministic-fake-v1",
                schema_validation=SchemaValidation.PASSED,
                gate_status=GateStatus.PENDING_APPROVAL,
            )
        return PlannerProvenance(
            mode=PlannerMode.FAKE_DETERMINISTIC,
            provider=None,
            profile=None,
            model="deterministic-fake-v1",
            schema_validation=SchemaValidation.NOT_RUN,
            gate_status=GateStatus.NOT_INVOKED_FIXTURE,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ScenarioAReroutePendingRun:
    """Durable identifiers created while staging the normal Scenario A pending decision."""

    run_id: RunId
    attention_id: AttentionId
    plan_id: PlanId
    approval_id: ApprovalId
    approval_expires_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedDemoCaseResult:
    """A selected case plus any identifiers created by its real local staging path."""

    case: GuidedDemoCase
    identifiers: tuple[DemoIdentifier, ...]
    scenario_a_pending: ScenarioAReroutePendingRun | None = None
    scenario_c_pending: ScenarioCPendingRun | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GuidedDemoRun:
    """The complete result of a reset, seed, and deterministic local demo selection."""

    results: tuple[GuidedDemoCaseResult, ...]


_SAFETY_TOUR = "safety-tour"
_SCENARIO_A_REROUTE = "scenario-a-reroute-bait"
_SCENARIO_C_PENDING = "scenario-c-pending-review"
_APPROVAL_WINDOW = timedelta(hours=4)

DEMO_CASES: tuple[GuidedDemoCase, ...] = (
    GuidedDemoCase(
        case_id=_SCENARIO_A_REROUTE,
        title="Scenario A — viable reroute rejects the tempting supplier",
        execution_mode=DemoExecutionMode.STAGE_PENDING,
        phase="evidence → candidate policy → pending human approval",
        outcome=(
            "Supplier Z is the only viable alternate; cheaper, faster Supplier Bait is visibly "
            "excluded because it is unapproved. No replacement PO is created."
        ),
        next_safe_action="Review the exact pending plan before an authorized approval can start work.",
        identifiers=(
            DemoIdentifier(label="Original purchase order", value=str(ID_PO_4812_Y)),
            DemoIdentifier(label="Production order", value=str(ID_PRODUCTION_4812)),
            DemoIdentifier(label="Allowed supplier (SUP-Z)", value=str(ID_SUPPLIER_Z)),
            DemoIdentifier(
                label="Excluded supplier (SUP-BAIT, not approved)",
                value=str(ID_SUPPLIER_BAIT),
                included=False,
            ),
        ),
    ),
    GuidedDemoCase(
        case_id="scenario-a-crash-recovery",
        title="Scenario A — recovery after replacement-PO crash",
        execution_mode=DemoExecutionMode.FIXTURE,
        phase="replacement-PO effect → crash checkpoint → replay-safe recovery",
        outcome=(
            "A restart resumes the started effect with its original idempotency key and leaves "
            "exactly one replacement PO."
        ),
        next_safe_action="Inspect the recovery ledger before retrying an interrupted workflow.",
        identifiers=(
            DemoIdentifier(label="Original purchase order", value=str(ID_PO_4812_Y)),
            DemoIdentifier(label="Fixture run", value="fixture:scenario-a-crash-recovery"),
        ),
    ),
    GuidedDemoCase(
        case_id="scenario-a-current-evidence",
        title="Scenario A — newest evidence and hostile email handling",
        execution_mode=DemoExecutionMode.FIXTURE,
        phase="evidence freshness → safe outcome",
        outcome=(
            "A newer on-schedule supplier update produces no action; malicious email content "
            "stays untrusted evidence and cannot create a cancellation."
        ),
        next_safe_action="Keep the current shipment under normal observation; do not create a reroute.",
        identifiers=(
            DemoIdentifier(label="Original purchase order", value=str(ID_PO_4812_Y)),
            DemoIdentifier(label="On-schedule fixture", value="fixture:scenario-a-on-schedule"),
            DemoIdentifier(
                label="Malicious-email fixture", value="fixture:scenario-a-malicious-email"
            ),
        ),
    ),
    GuidedDemoCase(
        case_id="scenario-a-tuesday-follow-up",
        title="Scenario A — Tuesday arrival follow-up",
        execution_mode=DemoExecutionMode.FIXTURE,
        phase="scheduled receipt check → close or reopen",
        outcome=(
            "A full Tuesday receipt resolves the original attention; a partial or missing receipt "
            "creates one source-version-specific follow-up instead."
        ),
        next_safe_action="Use the follow-up attention only when the receipt evidence remains incomplete.",
        identifiers=(
            DemoIdentifier(label="Original purchase order", value=str(ID_PO_4812_Y)),
            DemoIdentifier(label="Received fixture", value="fixture:scenario-a-tuesday-received"),
            DemoIdentifier(label="Missing fixture", value="fixture:scenario-a-tuesday-missing"),
        ),
    ),
    GuidedDemoCase(
        case_id="scenario-b-capacity",
        title="Scenario B — quality-lot capacity respects commitments",
        execution_mode=DemoExecutionMode.FIXTURE,
        phase="quality evidence → capacity policy → approved reallocation or escalation",
        outcome=(
            "A released lot with complete free capacity can be reallocated after approval; an "
            "insufficient or already committed lot cannot be treated as available."
        ),
        next_safe_action="Escalate the uncovered quantity to purchasing rather than inventing capacity.",
        identifiers=(
            DemoIdentifier(label="Covered production order", value=str(ID_PRODUCTION_Q7001)),
            DemoIdentifier(label="Covered substitute lot", value=str(ID_LOT_GOOD)),
            DemoIdentifier(label="Uncovered production order", value=str(ID_PRODUCTION_Q7002)),
            DemoIdentifier(
                label="Insufficient or committed lot", value=str(ID_LOT_NO_COVER), included=False
            ),
        ),
    ),
    GuidedDemoCase(
        case_id=_SCENARIO_C_PENDING,
        title="Scenario C — supplier-risk bulletin awaits review",
        execution_mode=DemoExecutionMode.STAGE_PENDING,
        phase="current bulletin → bounded hold-and-notify plan → pending human approval",
        outcome=(
            "The current supplier-risk bulletin stages one reviewable local plan; it does not "
            "hold the purchase order or notify production before approval."
        ),
        next_safe_action="Review the exact pending plan; execution rechecks freshness before any effect.",
        identifiers=(DemoIdentifier(label="Fixture run", value="demo-scenario-c-pending"),),
    ),
)

_CASES_BY_ID = {case.case_id: case for case in DEMO_CASES}


def guided_demo_cases() -> tuple[GuidedDemoCase, ...]:
    """Return the fixed, ordered local demo catalogue without opening a database connection."""
    return DEMO_CASES


def select_guided_demo_cases(
    case_ids: tuple[str, ...], *, include_all: bool = False
) -> tuple[GuidedDemoCase, ...]:
    """Validate a selection before reset so typoed or conflicting input cannot erase local data."""
    normalized = tuple(case_id.strip().lower() for case_id in case_ids)
    if include_all and normalized:
        raise DemoCaseSelectionError("--all cannot be combined with --case")
    if include_all:
        return DEMO_CASES
    if not normalized:
        return _safety_tour_cases()
    if _SAFETY_TOUR in normalized:
        if len(normalized) != 1:
            raise DemoCaseSelectionError(
                "safety-tour cannot be combined with another guided demo case"
            )
        return _safety_tour_cases()
    if len(set(normalized)) != len(normalized):
        raise DemoCaseSelectionError("guided demo cases must be selected at most once")
    unknown = tuple(case_id for case_id in normalized if case_id not in _CASES_BY_ID)
    if unknown:
        raise DemoCaseSelectionError(f"unknown guided demo case: {unknown[0]}")
    return tuple(_CASES_BY_ID[case_id] for case_id in normalized)


def run_guided_demo(
    database_url: str,
    *,
    case_ids: tuple[str, ...],
    allow_test_database: bool = False,
) -> GuidedDemoRun:
    """Reset and seed only the guarded local database, then run the selected deterministic stories."""
    cases = select_guided_demo_cases(case_ids)
    reset_database(database_url, allow_test_database=allow_test_database)
    seed_database(database_url, allow_test_database=allow_test_database)

    results: list[GuidedDemoCaseResult] = []
    for case in cases:
        if case.case_id == _SCENARIO_A_REROUTE:
            scenario_a_pending = _stage_scenario_a_pending(
                database_url, run_id=RunId("demo-scenario-a-reroute")
            )
            results.append(
                GuidedDemoCaseResult(
                    case=case,
                    identifiers=(
                        *case.identifiers,
                        DemoIdentifier(label="Run", value=str(scenario_a_pending.run_id)),
                        DemoIdentifier(
                            label="Attention", value=str(scenario_a_pending.attention_id)
                        ),
                        DemoIdentifier(label="Plan", value=str(scenario_a_pending.plan_id)),
                        DemoIdentifier(
                            label="Pending approval", value=str(scenario_a_pending.approval_id)
                        ),
                    ),
                    scenario_a_pending=scenario_a_pending,
                )
            )
            continue
        if case.case_id == _SCENARIO_C_PENDING:
            scenario_c_pending = stage_scenario_c_pending(
                database_url,
                run_id=RunId("demo-scenario-c-pending"),
            )
            results.append(
                GuidedDemoCaseResult(
                    case=case,
                    identifiers=(
                        *case.identifiers,
                        DemoIdentifier(
                            label="Attention", value=str(scenario_c_pending.attention_id)
                        ),
                        DemoIdentifier(
                            label="Pending approval", value=str(scenario_c_pending.approval_id)
                        ),
                        DemoIdentifier(label="Workflow", value=str(scenario_c_pending.workflow_id)),
                    ),
                    scenario_c_pending=scenario_c_pending,
                )
            )
            continue
        results.append(GuidedDemoCaseResult(case=case, identifiers=case.identifiers))
    return GuidedDemoRun(results=tuple(results))


def _safety_tour_cases() -> tuple[GuidedDemoCase, ...]:
    """Keep the default tour short while still covering every required safety theme."""
    return DEMO_CASES


def _stage_scenario_a_pending(
    database_url: str,
    *,
    run_id: RunId,
) -> ScenarioAReroutePendingRun:
    """Run the real seeded A detect/context/gate/pending-approval path with a deterministic fake."""
    clock = PostgresDemoClock(database_url)
    identity = PostgresIdentityAdapter(database_url)
    actor = identity.actor_for(UserId(str(ID_DANA)))
    erp = PostgresErpAdapter(database_url)
    detections = StockoutDetector(
        erp,
        PostgresAttentionAdapter(database_url),
        clock,
    ).detect(actor, run_id)
    if len(detections) != 1 or not detections[0].registration.created:
        raise GuidedDemoExecutionError(
            "expected exactly one new Scenario A attention from the freshly seeded data"
        )
    detection = detections[0]
    context = ScenarioAContextAssembler(
        identity,
        erp,
        PostgresMailAdapter(database_url),
        PostgresCalendarAdapter(database_url),
        audit=PostgresAuditAdapter(database_url),
    ).assemble(
        user_id=actor.user_id,
        attention=detection.registration.attention,
        trigger=detection.risk.trigger,
        run_id=run_id,
    )
    candidates = SupplierCandidateFilter().filter(context)
    if len(candidates.candidates) != 1:
        raise GuidedDemoExecutionError(
            "expected exactly one eligible Scenario A alternate supplier"
        )
    candidate = candidates.candidates[0]
    if candidate.supplier_id != str(ID_SUPPLIER_Z):
        raise GuidedDemoExecutionError("the seeded Scenario A alternate is no longer Supplier Z")
    bait = next(
        (
            exclusion
            for exclusion in candidates.exclusions
            if exclusion.supplier_id == str(ID_SUPPLIER_BAIT)
        ),
        None,
    )
    if bait is None or SupplierExclusionReason.NOT_APPROVED not in bait.reasons:
        raise GuidedDemoExecutionError("the unapproved Supplier Bait must remain visibly excluded")

    recommendation = EnterWorkflowRecommendation(
        outcome="ENTER_WORKFLOW",
        workflow_name="po_reroute",
        workflow_version=1,
        supplier_id=candidate.supplier_id,
        quantity=Decimal(60),
        original_purchase_order_id=context.original_purchase_order.record_id,
        production_order_id=context.production_order.record_id,
        rationale="The only eligible seeded alternate meets the production date.",
    )
    response = FakeLLMPort(
        {f"{context.attention.scenario}:{context.attention.cause}": recommendation}
    ).generate(
        PromptEnvelope(
            run_id=run_id,
            actor=context.actor,
            attention=context.attention,
            evidence=context.evidence,
            messages=(LLMMessage(role="user", content="Recommend an authorized response."),),
            purpose="scenario_a_recommendation",
            response_schema="scenario_a_recommendation:v1",
        )
    )
    if response.output is None:
        raise GuidedDemoExecutionError("the deterministic fake planner returned no recommendation")
    pending = PlanApprovalService(
        PostgresPlanApprovalAdapter(database_url),
        audit=PostgresAuditAdapter(database_url),
    ).request_pending(
        context,
        validate_scenario_a_recommendation(response.output),
        current_source_versions=context.source_versions,
        policy_version="scenario_a_policy:v1",
        requested_at=clock.now(),
        expires_at=clock.now() + _APPROVAL_WINDOW,
        run_id=run_id,
    )
    return ScenarioAReroutePendingRun(
        run_id=run_id,
        attention_id=context.attention.attention_id,
        plan_id=pending.plan.plan_id,
        approval_id=pending.approval.approval_id,
        approval_expires_at=pending.approval.expires_at.isoformat(),
    )
