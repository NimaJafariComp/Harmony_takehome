"""Contracts for the repository's standard developer commands."""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.contract


def run_make(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run Make without hiding its output when a target is unavailable."""
    return subprocess.run(
        ["make", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_makefile_exposes_all_standard_validation_targets() -> None:
    """Every documented developer command has a valid Make target."""
    result = run_make(
        "-n",
        "format-check",
        "lint",
        "typecheck",
        "test",
        "test-critical",
        "verify",
        "migrate",
        "seed",
        "demo",
        "llm-smoke",
    )

    assert result.returncode == 0, result.stderr


def test_llm_smoke_target_passes_an_explicit_provider_selection_to_the_cli() -> None:
    """The documented manual target selects one profile without running a provider during validation."""
    result = run_make("-n", "llm-smoke", "LLM_PROFILE=openai")

    assert result.returncode == 0, result.stderr
    assert 'LLM_PROFILE="openai" uv run enterprise-agent llm-smoke' in result.stdout


def test_critical_test_target_executes_the_critical_suite() -> None:
    """The focused safety suite can run independently of the full suite."""
    result = run_make("test-critical")

    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
