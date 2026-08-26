"""Loopback-only local verification UI with no business-service dependency."""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

LOCAL_UI_HOST = "127.0.0.1"
LOCAL_UI_PORT = 8080
_PACKAGE_ROOT = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=_PACKAGE_ROOT / "templates")


def create_app() -> FastAPI:
    """Create the deliberately thin local UI without importing any application, adapter, or config service."""
    application = FastAPI(
        title="Enterprise Agent Local Review",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.mount("/static", StaticFiles(directory=_PACKAGE_ROOT / "static"), name="static")

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

    return application


app = create_app()


def main() -> None:
    """Run the optional review surface on loopback, never a network-facing default host."""
    uvicorn.run(app, host=LOCAL_UI_HOST, port=LOCAL_UI_PORT)
