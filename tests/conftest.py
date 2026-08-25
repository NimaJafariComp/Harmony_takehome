"""Shared fixtures for tests that require the local Compose database."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from uuid import uuid4

import pytest


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and retain diagnostics for fixture failures."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture
def disposable_database() -> Iterator[str]:
    """Create and remove one isolated PostgreSQL database for an integration test."""
    database_name = f"enterprise_agent_test_{uuid4().hex}"
    quoted_database_name = f'"{database_name}"'

    compose("up", "-d", "--wait", "db")
    compose(
        "exec",
        "-T",
        "db",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "enterprise_agent",
        "-d",
        "postgres",
        "-c",
        f"CREATE DATABASE {quoted_database_name}",
    )

    try:
        yield f"postgresql+psycopg://enterprise_agent:enterprise_agent@db:5432/{database_name}"
    finally:
        compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "enterprise_agent",
            "-d",
            "postgres",
            "-c",
            f"DROP DATABASE IF EXISTS {quoted_database_name} WITH (FORCE)",
        )
