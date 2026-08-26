"""Loopback-only local verification UI with an actor-scoped read-service boundary."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from os import environ
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from enterprise_agent.application.local_decisions import (
    ApprovalDecision,
    LocalApprovalDecisionConflictError,
    LocalApprovalDecisionPort,
    LocalApprovalDecisionStaleError,
    UnconfiguredLocalApprovalDecisionService,
)
from enterprise_agent.application.local_demo_catalogue import (
    LocalDemoCataloguePort,
    LocalDemoCatalogueService,
)
from enterprise_agent.application.local_demo_controls import (
    LocalDemoClockControlDisabledError,
    LocalDemoClockControlPort,
    LocalDemoClockControlUnavailableError,
    UnconfiguredLocalDemoClockControlService,
)
from enterprise_agent.application.local_guided_demo import (
    GuidedDemoSelectionError,
    LocalGuidedDemoDisabledError,
    LocalGuidedDemoPort,
    LocalGuidedDemoUnavailableError,
    UnconfiguredLocalGuidedDemoService,
)
from enterprise_agent.application.local_live_demo import (
    LocalLiveDemoDisabledError,
    LocalLiveDemoPort,
    LocalLiveDemoSelectionError,
    LocalLiveDemoUnavailableError,
    UnconfiguredLocalLiveDemoService,
)
from enterprise_agent.application.local_llm_evaluation import (
    LocalLLMEvaluationPort,
    LocalLLMEvaluationSelectionError,
    LocalLLMEvaluationUnavailableError,
    UnconfiguredLocalLLMEvaluationService,
)
from enterprise_agent.application.local_review import (
    LocalReviewAccessDeniedError,
    LocalReviewReadPort,
    LocalReviewResourceNotFoundError,
    LocalReviewUnavailableError,
    ReviewPayload,
    UnconfiguredLocalReviewService,
)
from enterprise_agent.local_review_composition import (
    create_local_approval_decision_service,
    create_local_demo_clock_control_service,
    create_local_guided_demo_service,
    create_local_live_demo_service,
    create_local_llm_evaluation_service,
    create_local_review_service,
)

LOCAL_UI_HOST = "127.0.0.1"
LOCAL_UI_PORT = 8080
_CONTAINER_UI_HOST = "0.0.0.0"
_UI_BIND_HOST_SETTING = "ENTERPRISE_AGENT_UI_BIND_HOST"
_PACKAGE_ROOT = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=_PACKAGE_ROOT / "templates")
_CSRF_COOKIE_NAME = "enterprise_agent_local_csrf"
_MAX_DECISION_FORM_BYTES = 4096
_MAX_GUIDED_DEMO_FORM_BYTES = 4096
_MAX_LIVE_DEMO_FORM_BYTES = 4096


class _DecisionRequestError(ValueError):
    """Raised when a browser request cannot prove its local page origin and intended decision."""


def create_app(
    read_service: LocalReviewReadPort | None = None,
    decision_service: LocalApprovalDecisionPort | None = None,
    demo_clock_control_service: LocalDemoClockControlPort | None = None,
    demo_catalogue_service: LocalDemoCataloguePort | None = None,
    guided_demo_service: LocalGuidedDemoPort | None = None,
    llm_evaluation_service: LocalLLMEvaluationPort | None = None,
    live_demo_service: LocalLiveDemoPort | None = None,
) -> FastAPI:
    """Create a loopback UI with injected read and approval-decision service boundaries."""
    review_service = read_service if read_service is not None else UnconfiguredLocalReviewService()
    decisions = (
        decision_service
        if decision_service is not None
        else UnconfiguredLocalApprovalDecisionService()
    )
    demo_clock_controls = (
        demo_clock_control_service
        if demo_clock_control_service is not None
        else UnconfiguredLocalDemoClockControlService()
    )
    demo_catalogue = (
        demo_catalogue_service
        if demo_catalogue_service is not None
        else LocalDemoCatalogueService()
    )
    guided_demo = (
        guided_demo_service
        if guided_demo_service is not None
        else UnconfiguredLocalGuidedDemoService()
    )
    llm_evaluation = (
        llm_evaluation_service
        if llm_evaluation_service is not None
        else UnconfiguredLocalLLMEvaluationService()
    )
    live_demo = (
        live_demo_service if live_demo_service is not None else UnconfiguredLocalLiveDemoService()
    )
    csrf_signing_key = secrets.token_bytes(32)
    application = FastAPI(
        title="Enterprise Agent Local Review",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.mount("/static", StaticFiles(directory=_PACKAGE_ROOT / "static"), name="static")

    def render_page(
        request: Request,
        name: str,
        *,
        status_code: int = 200,
        **context: object,
    ) -> HTMLResponse:
        """Render one server-owned page without handing templates a persistence or provider dependency."""
        return _TEMPLATES.TemplateResponse(
            request=request,
            name=name,
            context={"start_command": "uv run enterprise-agent-ui", **context},
            status_code=status_code,
        )

    def review_error(
        request: Request,
        *,
        status_code: int,
        title: str,
        message: str,
    ) -> Response:
        """Keep API error envelopes stable while browser navigation receives non-sensitive guidance."""
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=status_code, content={"detail": message})
        return render_page(
            request,
            "error.html",
            status_code=status_code,
            error_title=title,
            error_message=message,
        )

    def new_decision_csrf(*, approval_id: str) -> tuple[str, dict[ApprovalDecision, str]]:
        """Create a fresh cookie value and action-bound fields without putting a plan hash in HTML."""
        session = secrets.token_urlsafe(32)
        tokens = {
            decision: _csrf_token(
                csrf_signing_key,
                session=session,
                approval_id=approval_id,
                decision=decision,
            )
            for decision in ApprovalDecision
        }
        return session, tokens

    async def read_decision_form(request: Request, *, approval_id: str) -> ApprovalDecision:
        """Parse a deliberately tiny URL-encoded form and reject unbound, cross-origin submissions."""
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise _DecisionRequestError("unsupported decision request")
        body = await request.body()
        if not body or len(body) > _MAX_DECISION_FORM_BYTES:
            raise _DecisionRequestError("invalid decision request")
        try:
            values = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=3,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise _DecisionRequestError("invalid decision request") from error
        if set(values) != {"approval_id", "csrf_token", "decision"}:
            raise _DecisionRequestError("invalid decision request")
        submitted_approval_id = _one_form_value(values, "approval_id")
        csrf_token = _one_form_value(values, "csrf_token")
        submitted_decision = _one_form_value(values, "decision")
        if submitted_approval_id != approval_id:
            raise _DecisionRequestError("approval identity mismatch")
        try:
            decision = ApprovalDecision(submitted_decision)
        except ValueError as error:
            raise _DecisionRequestError("unsupported decision") from error
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if origin is not None and not hmac.compare_digest(origin, expected_origin):
            raise _DecisionRequestError("cross-origin decision request")
        session = request.cookies.get(_CSRF_COOKIE_NAME)
        if session is None:
            raise _DecisionRequestError("missing csrf cookie")
        expected_token = _csrf_token(
            csrf_signing_key,
            session=session,
            approval_id=approval_id,
            decision=decision,
        )
        if not hmac.compare_digest(csrf_token, expected_token):
            raise _DecisionRequestError("invalid csrf token")
        return decision

    async def read_demo_clock_form(request: Request) -> None:
        """Require a same-origin one-field form whose token cannot authorize any other action."""
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise _DecisionRequestError("unsupported demo-clock request")
        body = await request.body()
        if not body or len(body) > _MAX_DECISION_FORM_BYTES:
            raise _DecisionRequestError("invalid demo-clock request")
        try:
            values = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=1,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise _DecisionRequestError("invalid demo-clock request") from error
        if set(values) != {"csrf_token"}:
            raise _DecisionRequestError("invalid demo-clock request")
        csrf_token = _one_form_value(values, "csrf_token")
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if origin is not None and not hmac.compare_digest(origin, expected_origin):
            raise _DecisionRequestError("cross-origin demo-clock request")
        session = request.cookies.get(_CSRF_COOKIE_NAME)
        if session is None or not hmac.compare_digest(
            csrf_token,
            _demo_clock_csrf_token(csrf_signing_key, session=session),
        ):
            raise _DecisionRequestError("invalid demo-clock token")

    async def read_guided_demo_form(request: Request) -> tuple[str, tuple[str, ...]]:
        """Accept only a confirmed, same-origin seeded-persona and case selection."""
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise _DecisionRequestError("unsupported guided-demo request")
        body = await request.body()
        if not body or len(body) > _MAX_GUIDED_DEMO_FORM_BYTES:
            raise _DecisionRequestError("invalid guided-demo request")
        try:
            values = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=8,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise _DecisionRequestError("invalid guided-demo request") from error
        if set(values) != {"csrf_token", "persona_id", "case_id", "confirm"}:
            raise _DecisionRequestError("invalid guided-demo request")
        csrf_token = _one_form_value(values, "csrf_token")
        persona_id = _one_form_value(values, "persona_id")
        confirmation = _one_form_value(values, "confirm")
        case_ids = tuple(values["case_id"])
        if confirmation != "run" or not case_ids or any(not case_id for case_id in case_ids):
            raise _DecisionRequestError("invalid guided-demo request")
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if origin is not None and not hmac.compare_digest(origin, expected_origin):
            raise _DecisionRequestError("cross-origin guided-demo request")
        session = request.cookies.get(_CSRF_COOKIE_NAME)
        if session is None or not hmac.compare_digest(
            csrf_token,
            _guided_demo_csrf_token(csrf_signing_key, session=session),
        ):
            raise _DecisionRequestError("invalid guided-demo token")
        return persona_id, case_ids

    async def read_llm_evaluation_form(request: Request) -> tuple[str, str]:
        """Accept only a confirmed, same-origin configured-profile and fixed-case evaluation request."""
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise _DecisionRequestError("unsupported LLM evaluation request")
        body = await request.body()
        if not body or len(body) > _MAX_GUIDED_DEMO_FORM_BYTES:
            raise _DecisionRequestError("invalid LLM evaluation request")
        try:
            values = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=4,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise _DecisionRequestError("invalid LLM evaluation request") from error
        if set(values) != {"csrf_token", "profile_id", "case_id", "confirm"}:
            raise _DecisionRequestError("invalid LLM evaluation request")
        csrf_token = _one_form_value(values, "csrf_token")
        profile_id = _one_form_value(values, "profile_id")
        case_id = _one_form_value(values, "case_id")
        confirmation = _one_form_value(values, "confirm")
        if confirmation != "evaluate":
            raise _DecisionRequestError("invalid LLM evaluation request")
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if origin is not None and not hmac.compare_digest(origin, expected_origin):
            raise _DecisionRequestError("cross-origin LLM evaluation request")
        session = request.cookies.get(_CSRF_COOKIE_NAME)
        if session is None or not hmac.compare_digest(
            csrf_token,
            _llm_evaluation_csrf_token(csrf_signing_key, session=session),
        ):
            raise _DecisionRequestError("invalid LLM evaluation token")
        return profile_id, case_id

    async def read_live_demo_form(request: Request) -> tuple[str, str]:
        """Accept one confirmed same-origin profile and fixed local live-demo case only."""
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
        if content_type != "application/x-www-form-urlencoded":
            raise _DecisionRequestError("unsupported live-demo request")
        body = await request.body()
        if not body or len(body) > _MAX_LIVE_DEMO_FORM_BYTES:
            raise _DecisionRequestError("invalid live-demo request")
        try:
            values = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=4,
            )
        except (UnicodeDecodeError, ValueError) as error:
            raise _DecisionRequestError("invalid live-demo request") from error
        if set(values) != {"csrf_token", "profile_id", "case_id", "confirm"}:
            raise _DecisionRequestError("invalid live-demo request")
        csrf_token = _one_form_value(values, "csrf_token")
        profile_id = _one_form_value(values, "profile_id")
        case_id = _one_form_value(values, "case_id")
        if _one_form_value(values, "confirm") != "live":
            raise _DecisionRequestError("invalid live-demo request")
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if origin is not None and not hmac.compare_digest(origin, expected_origin):
            raise _DecisionRequestError("cross-origin live-demo request")
        session = request.cookies.get(_CSRF_COOKIE_NAME)
        if session is None or not hmac.compare_digest(
            csrf_token,
            _live_demo_csrf_token(csrf_signing_key, session=session),
        ):
            raise _DecisionRequestError("invalid live-demo token")
        return profile_id, case_id

    @application.exception_handler(LocalReviewAccessDeniedError)
    async def local_review_access_denied(
        request: Request, error: LocalReviewAccessDeniedError
    ) -> Response:
        """Hide cross-actor resource existence and detail behind one stable response."""
        del error
        return review_error(
            request,
            status_code=403,
            title="Review access denied",
            message="The selected demo actor cannot view this resource.",
        )

    @application.exception_handler(LocalReviewResourceNotFoundError)
    async def local_review_resource_not_found(
        request: Request, error: LocalReviewResourceNotFoundError
    ) -> Response:
        """Reject absent and malformed opaque resource IDs without echoing caller input."""
        del error
        return review_error(
            request,
            status_code=404,
            title="Review record unavailable",
            message="The requested review resource is unavailable.",
        )

    @application.exception_handler(LocalReviewUnavailableError)
    async def local_review_unavailable(
        request: Request, error: LocalReviewUnavailableError
    ) -> Response:
        """Fail closed when the optional local reader cannot be composed safely."""
        del error
        return review_error(
            request,
            status_code=503,
            title="Local review unavailable",
            message="Local review data is not configured.",
        )

    @application.get("/", response_class=HTMLResponse)
    def local_review(request: Request) -> HTMLResponse:
        """Render the safe status ledger, keeping an unconfigured imported app database-free."""
        status = (
            None
            if isinstance(review_service, UnconfiguredLocalReviewService)
            else review_service.status()
        )
        return render_page(request, "status.html", status=status)

    @application.get("/attention/{attention_id}", response_class=HTMLResponse)
    def attention_page(request: Request, attention_id: str) -> HTMLResponse:
        """Render one safe evidence-reference page through the injected actor-scoped reader."""
        return render_page(
            request, "attention.html", attention=review_service.attention(attention_id)
        )

    @application.get("/approval/{approval_id}", response_class=HTMLResponse)
    def approval_page(request: Request, approval_id: str) -> HTMLResponse:
        """Render one immutable approval plus action controls only for the current active approver."""
        approval = review_service.approval(approval_id)
        can_decide = False
        if not isinstance(decisions, UnconfiguredLocalApprovalDecisionService):
            can_decide = decisions.availability(approval_id).can_decide
        csrf_session: str | None = None
        csrf_tokens: dict[ApprovalDecision, str] | None = None
        if can_decide:
            csrf_session, csrf_tokens = new_decision_csrf(approval_id=approval_id)
        response = render_page(
            request,
            "approval.html",
            approval=approval,
            can_decide=can_decide,
            csrf_tokens=csrf_tokens,
        )
        if csrf_session is not None:
            response.set_cookie(
                _CSRF_COOKIE_NAME,
                csrf_session,
                max_age=600,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        return response

    @application.post("/approval/{approval_id}/decision", response_class=HTMLResponse)
    async def approval_decision_page(request: Request, approval_id: str) -> Response:
        """Record one CSRF-bound local decision through the shared application approval service."""
        try:
            decision = await read_decision_form(request, approval_id=approval_id)
        except _DecisionRequestError:
            return review_error(
                request,
                status_code=403,
                title="Decision request expired",
                message="Reload the approval record before choosing a decision.",
            )
        try:
            result = decisions.decide(approval_id=approval_id, decision=decision)
        except LocalApprovalDecisionStaleError:
            return review_error(
                request,
                status_code=409,
                title="Approval needs a fresh review",
                message=(
                    "The plan changed or its supporting evidence is no longer current. "
                    "Reload the review queue."
                ),
            )
        except LocalApprovalDecisionConflictError:
            return review_error(
                request,
                status_code=409,
                title="Decision could not be recorded",
                message="This approval is no longer available for a decision. Reload the review queue.",
            )
        return render_page(request, "decision.html", result=result)

    @application.get("/workflow/{workflow_id}", response_class=HTMLResponse)
    def workflow_page(request: Request, workflow_id: str) -> HTMLResponse:
        """Render declared workflow/recovery progress while keeping raw state inputs and errors hidden."""
        return render_page(request, "workflow.html", workflow=review_service.workflow(workflow_id))

    @application.get("/demo", response_class=HTMLResponse)
    @application.get("/demo-clock", response_class=HTMLResponse, include_in_schema=False)
    def demo_clock_page(request: Request) -> HTMLResponse:
        """Render the discoverable local demo page plus its one bounded time control."""
        demo_clock = review_service.demo_clock()
        controls = demo_clock_controls.availability()
        guided_demo_availability = guided_demo.availability()
        llm_evaluation_availability = llm_evaluation.availability()
        live_demo_availability = live_demo.availability()
        csrf_session: str | None = None
        csrf_token: str | None = None
        guided_demo_csrf_token: str | None = None
        llm_evaluation_csrf_token: str | None = None
        live_demo_csrf_token: str | None = None
        if (
            controls.can_advance
            or guided_demo_availability.can_run
            or llm_evaluation_availability.can_evaluate
            or live_demo_availability.can_run
        ):
            csrf_session = secrets.token_urlsafe(32)
        if csrf_session is not None and controls.can_advance:
            csrf_token = _demo_clock_csrf_token(csrf_signing_key, session=csrf_session)
        if csrf_session is not None and guided_demo_availability.can_run:
            guided_demo_csrf_token = _guided_demo_csrf_token(csrf_signing_key, session=csrf_session)
        if csrf_session is not None and llm_evaluation_availability.can_evaluate:
            llm_evaluation_csrf_token = _llm_evaluation_csrf_token(
                csrf_signing_key, session=csrf_session
            )
        if csrf_session is not None and live_demo_availability.can_run:
            live_demo_csrf_token = _live_demo_csrf_token(csrf_signing_key, session=csrf_session)
        response = render_page(
            request,
            "demo_clock.html",
            demo_clock=demo_clock,
            demo_cases=demo_catalogue.cases(),
            can_advance=controls.can_advance,
            csrf_token=csrf_token,
            guided_demo_availability=guided_demo_availability,
            guided_demo_csrf_token=guided_demo_csrf_token,
            llm_evaluation_availability=llm_evaluation_availability,
            llm_evaluation_csrf_token=llm_evaluation_csrf_token,
            live_demo_availability=live_demo_availability,
            live_demo_csrf_token=live_demo_csrf_token,
        )
        if csrf_session is not None:
            response.set_cookie(
                _CSRF_COOKIE_NAME,
                csrf_session,
                max_age=600,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        return response

    @application.post("/demo/run", response_class=HTMLResponse)
    async def run_guided_demo_page(request: Request) -> Response:
        """Reset, seed, and stage only a confirmed strict-local deterministic demo selection."""
        try:
            persona_id, case_ids = await read_guided_demo_form(request)
        except _DecisionRequestError:
            return review_error(
                request,
                status_code=403,
                title="Guided demo request expired",
                message="Reload Demo mode before selecting a local guided scenario.",
            )
        try:
            receipt = guided_demo.run(persona_id=persona_id, case_ids=case_ids)
        except LocalGuidedDemoDisabledError:
            return review_error(
                request,
                status_code=403,
                title="Guided demo launcher is unavailable",
                message="Start the local deterministic demo database, then reload Demo mode.",
            )
        except GuidedDemoSelectionError:
            return review_error(
                request,
                status_code=400,
                title="Guided demo selection is invalid",
                message="Choose one listed persona and one or more compatible scenario cards.",
            )
        except LocalGuidedDemoUnavailableError:
            return review_error(
                request,
                status_code=503,
                title="Guided demo could not be prepared",
                message="Start the local deterministic demo database, then retry from Demo mode.",
            )
        response = render_page(request, "guided_demo_result.html", receipt=receipt)
        response.delete_cookie(_CSRF_COOKIE_NAME, path="/")
        return response

    @application.post("/demo/evaluate", response_class=HTMLResponse)
    async def run_llm_evaluation_page(request: Request) -> Response:
        """Evaluate one fixed synthetic case through one selected profile and no-write adapter only."""
        try:
            profile_id, case_id = await read_llm_evaluation_form(request)
        except _DecisionRequestError:
            return review_error(
                request,
                status_code=403,
                title="LLM evaluation request expired",
                message="Reload Demo mode before selecting a configured profile and synthetic case.",
            )
        try:
            receipt = llm_evaluation.evaluate(profile_id=profile_id, case_id=case_id)
        except LocalLLMEvaluationSelectionError:
            return review_error(
                request,
                status_code=400,
                title="LLM evaluation selection is invalid",
                message="Choose one listed configured profile and one fixed synthetic case.",
            )
        except LocalLLMEvaluationUnavailableError:
            return review_error(
                request,
                status_code=503,
                title="LLM evaluation is unavailable",
                message="Configure a supported local provider profile, then reload Demo mode.",
            )
        response = render_page(request, "llm_evaluation_result.html", receipt=receipt)
        response.delete_cookie(_CSRF_COOKIE_NAME, path="/")
        return response

    @application.post("/demo/live", response_class=HTMLResponse)
    async def run_live_demo_page(request: Request) -> Response:
        """Run one confirmed local A/B/C provider proposal through the existing guarded control plane."""
        try:
            profile_id, case_id = await read_live_demo_form(request)
        except _DecisionRequestError:
            return review_error(
                request,
                status_code=403,
                title="Live demo request expired",
                message="Reload Demo mode before selecting a configured profile and fixed local scenario.",
            )
        try:
            receipt = live_demo.run(profile_id=profile_id, case_id=case_id)
        except LocalLiveDemoDisabledError:
            return review_error(
                request,
                status_code=403,
                title="Live local demo is unavailable",
                message="Use the exact local synthetic demo database, then reload Demo mode.",
            )
        except LocalLiveDemoSelectionError:
            return review_error(
                request,
                status_code=400,
                title="Live demo selection is invalid",
                message="Choose one listed configured profile and one fixed Scenario A, B, or C story.",
            )
        except LocalLiveDemoUnavailableError:
            return review_error(
                request,
                status_code=503,
                title="Live local demo could not be prepared",
                message="Confirm local provider configuration and demo database availability, then retry.",
            )
        response = render_page(request, "live_demo_result.html", receipt=receipt)
        response.delete_cookie(_CSRF_COOKIE_NAME, path="/")
        return response

    @application.post("/demo-clock/advance", response_class=HTMLResponse)
    async def advance_demo_clock_page(request: Request) -> Response:
        """Advance one persisted local-demo day after an action-specific CSRF check."""
        try:
            await read_demo_clock_form(request)
        except _DecisionRequestError:
            return review_error(
                request,
                status_code=403,
                title="Demo clock request expired",
                message="Reload the demo clock before advancing local demo time.",
            )
        try:
            result = demo_clock_controls.advance_one_day()
        except LocalDemoClockControlDisabledError:
            return review_error(
                request,
                status_code=403,
                title="Demo clock controls are unavailable",
                message="Start the local deterministic demo database, then reload the demo page.",
            )
        except LocalDemoClockControlUnavailableError:
            return review_error(
                request,
                status_code=503,
                title="Demo clock unavailable",
                message="Start the local deterministic demo before advancing its clock.",
            )
        return render_page(request, "demo_clock_result.html", result=result)

    @application.get("/audit/{run_id}", response_class=HTMLResponse)
    def audit_page(request: Request, run_id: str) -> HTMLResponse:
        """Render one already-authorized audit explanation from the injected immutable ledger reader."""
        return render_page(request, "audit.html", audit=review_service.audit(run_id))

    @application.get("/health")
    def health() -> dict[str, bool | str]:
        """Report presentation-process readiness without connecting to a business or provider system."""
        return {
            "status": "ready",
            "scope": "local_read_only_ui",
            "database_access": False,
            "provider_access": False,
        }

    @application.get("/api/status")
    def status() -> ReviewPayload:
        """Read the same safe operator summary as the CLI for the selected local demo actor."""
        return review_service.status()

    @application.get("/api/attention/{attention_id}")
    def attention(attention_id: str) -> ReviewPayload:
        """Read one authorized attention record with versioned references, never raw source content."""
        return review_service.attention(attention_id)

    @application.get("/api/approval/{approval_id}")
    def approval(approval_id: str) -> ReviewPayload:
        """Read one authorized immutable approval summary with no approve or reject operation."""
        return review_service.approval(approval_id)

    @application.get("/api/workflow/{workflow_id}")
    def workflow(workflow_id: str) -> ReviewPayload:
        """Read safe workflow and recovery facts without loading tool inputs, results, or errors."""
        return review_service.workflow(workflow_id)

    @application.get("/api/audit/{run_id}")
    def audit(run_id: str) -> ReviewPayload:
        """Read a fully authorized audit-only explanation from the immutable ledger."""
        return review_service.audit(run_id)

    @application.get("/api/demo-clock")
    def demo_clock() -> ReviewPayload:
        """Read local deterministic demo time; advancing it remains outside this read-only milestone."""
        return review_service.demo_clock()

    return application


app = create_app()


def main() -> None:
    """Run the optional review surface on loopback, never a network-facing default host."""
    uvicorn.run(
        create_app(
            read_service=create_local_review_service(),
            decision_service=create_local_approval_decision_service(),
            demo_clock_control_service=create_local_demo_clock_control_service(),
            guided_demo_service=create_local_guided_demo_service(),
            llm_evaluation_service=create_local_llm_evaluation_service(),
            live_demo_service=create_local_live_demo_service(),
        ),
        host=_ui_bind_host(),
        port=LOCAL_UI_PORT,
    )


def _ui_bind_host(environment: Mapping[str, str] | None = None) -> str:
    """Allow only the Compose bridge bind override; local launches stay loopback-only."""
    values = environ if environment is None else environment
    requested_host = values.get(_UI_BIND_HOST_SETTING, LOCAL_UI_HOST).strip()
    if requested_host == _CONTAINER_UI_HOST:
        return _CONTAINER_UI_HOST
    return LOCAL_UI_HOST


def _csrf_token(
    signing_key: bytes,
    *,
    session: str,
    approval_id: str,
    decision: ApprovalDecision,
) -> str:
    """Sign a cookie-bound action token without placing a plan hash in the browser response."""
    payload = f"{session}\x1f{approval_id}\x1f{decision.value}".encode()
    return hmac.new(signing_key, payload, hashlib.sha256).hexdigest()


def _demo_clock_csrf_token(signing_key: bytes, *, session: str) -> str:
    """Sign a token that applies only to the one-day local-demo clock operation."""
    return hmac.new(
        signing_key, f"{session}\x1fdemo-clock.advance-one-day".encode(), hashlib.sha256
    ).hexdigest()


def _guided_demo_csrf_token(signing_key: bytes, *, session: str) -> str:
    """Sign a token that applies only to the local deterministic reset-and-stage action."""
    return hmac.new(
        signing_key, f"{session}\x1fguided-demo.run".encode(), hashlib.sha256
    ).hexdigest()


def _llm_evaluation_csrf_token(signing_key: bytes, *, session: str) -> str:
    """Sign a token that applies only to one synthetic no-write provider evaluation action."""
    return hmac.new(
        signing_key, f"{session}\x1fllm-evaluation.run".encode(), hashlib.sha256
    ).hexdigest()


def _live_demo_csrf_token(signing_key: bytes, *, session: str) -> str:
    """Sign a token that applies only to one locally guarded live A/B/C proposal request."""
    return hmac.new(signing_key, f"{session}\x1flive-demo.run".encode(), hashlib.sha256).hexdigest()


def _one_form_value(values: dict[str, list[str]], name: str) -> str:
    """Require exactly one small scalar form value instead of accepting duplicate browser fields."""
    value = values.get(name)
    if value is None or len(value) != 1 or not value[0]:
        raise _DecisionRequestError("missing form value")
    return value[0]
