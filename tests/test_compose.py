"""Docker Compose contract tests."""

import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.contract


def rendered_compose_configuration() -> dict[str, Any]:
    """Render Compose configuration exactly as Docker will interpret it."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "tools",
            "-f",
            "docker-compose.yml",
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    return cast(dict[str, Any], json.loads(result.stdout))


def test_compose_defines_private_durable_postgresql_service() -> None:
    """The harness database is healthy, persistent, and not host-exposed."""
    configuration = rendered_compose_configuration()
    service = configuration["services"]["db"]
    volume = service["volumes"][0]

    assert service["image"].startswith("postgres:16")
    assert "ports" not in service
    assert volume["source"] == "postgres_data"
    assert volume["target"] == "/var/lib/postgresql/data"
    assert volume["type"] == "volume"
    assert service["healthcheck"]["test"] == [
        "CMD-SHELL",
        "pg_isready -U enterprise_agent -d enterprise_agent",
    ]
    assert configuration["networks"]["agent_backend"]["internal"] is True
    assert configuration["volumes"]["postgres_data"] is not None


def test_example_database_url_targets_compose_service() -> None:
    """The documented application connection targets the internal DB hostname."""
    contents = Path(".env.example").read_text(encoding="utf-8")

    assert (
        "DATABASE_URL=postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/enterprise_agent"
        in contents
    )


def test_optional_ui_reaches_only_the_private_database_and_loopback_host_port() -> None:
    """The review UI can use Compose service discovery without publishing the database."""
    configuration = rendered_compose_configuration()
    service = configuration["services"]["ui"]

    assert service["profiles"] == ["tools"]
    assert service["environment"]["DATABASE_URL"].endswith("@db:5432/enterprise_agent")
    assert service["environment"]["ENTERPRISE_AGENT_UI_BIND_HOST"] == "0.0.0.0"
    assert service["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "8080",
            "target": 8080,
        }
    ]
