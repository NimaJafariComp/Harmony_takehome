"""Loopback-only local verification UI with an actor-scoped read-service boundary."""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
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

    @application.exception_handler(LocalReviewAccessDeniedError)
    async def local_review_access_denied(
        request: Request, error: LocalReviewAccessDeniedError
    ) -> JSONResponse:
        """Hide cross-actor resource existence and detail behind one stable response."""
        del request, error
        return JSONResponse(
            status_code=403,
            content={"detail": "The selected demo actor cannot view this resource."},
        )

    @application.exception_handler(LocalReviewResourceNotFoundError)
    async def local_review_resource_not_found(
        request: Request, error: LocalReviewResourceNotFoundError
    ) -> JSONResponse:
        """Reject absent and malformed opaque resource IDs without echoing caller input."""
        del request, error
        return JSONResponse(
            status_code=404,
            content={"detail": "The requested review resource is unavailable."},
        )

    @application.exception_handler(LocalReviewUnavailableError)
    async def local_review_unavailable(
        request: Request, error: LocalReviewUnavailableError
    ) -> JSONResponse:
        """Fail closed when the optional local reader cannot be composed safely."""
        del request, error
        return JSONResponse(
            status_code=503,
            content={"detail": "Local review data is not configured."},
        )

    @application.get("/", response_class=HTMLResponse)
    def local_review(request: Request) -> HTMLResponse:
        """Render the safe entry point for the optional local evidence-review surface."""
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"start_command": "uv run enterprise-agent-ui"},
        )

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
