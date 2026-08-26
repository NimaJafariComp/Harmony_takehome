"""Thin local verification-UI boundary contracts without business-service access."""

from __future__ import annotations

import inspect

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]


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
    assert "Read-only local verification surface" in response.text
    assert "No provider call · no credential display · no business-system write" in response.text
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
    """Starting the optional review server never exposes it on a network interface by default."""
    from enterprise_agent import web

    observed: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int) -> None:
        observed.update(app=app, host=host, port=port)

    monkeypatch.setattr("enterprise_agent.web.uvicorn.run", fake_run)

    web.main()

    assert observed["app"] is web.app
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 8080
