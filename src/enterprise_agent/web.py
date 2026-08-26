"""Loopback-only local verification UI with an actor-scoped read-service boundary."""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from enterprise_agent.application.local_review import (
    LocalReviewAccessDeniedError,
    LocalReviewReadPort,
    LocalReviewResourceNotFoundError,
    LocalReviewUnavailableError,
    ReviewPayload,
    UnconfiguredLocalReviewService,
)
from enterprise_agent.local_review_composition import create_local_review_service

LOCAL_UI_HOST = "127.0.0.1"
LOCAL_UI_PORT = 8080
_PACKAGE_ROOT = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=_PACKAGE_ROOT / "templates")


def create_app(read_service: LocalReviewReadPort | None = None) -> FastAPI:
    """Create the local UI with an injected safe read service and no direct persistence dependency."""
    review_service = read_service if read_service is not None else UnconfiguredLocalReviewService()
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
        """Render one immutable approval record without exposing or deciding its full plan hash."""
        return render_page(request, "approval.html", approval=review_service.approval(approval_id))

    @application.get("/workflow/{workflow_id}", response_class=HTMLResponse)
    def workflow_page(request: Request, workflow_id: str) -> HTMLResponse:
        """Render declared workflow/recovery progress while keeping raw state inputs and errors hidden."""
        return render_page(request, "workflow.html", workflow=review_service.workflow(workflow_id))

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
        create_app(read_service=create_local_review_service()),
        host=LOCAL_UI_HOST,
        port=LOCAL_UI_PORT,
    )
