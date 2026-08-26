"""CLI/JSON contracts for visible fake and live planner provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.ports import LLMGenerationResult, PromptEnvelope

pytestmark = [pytest.mark.unit, pytest.mark.contract]


@dataclass
class _PassingAdapter:
    """Return the fixed synthetic Scenario A recommendation without contacting a provider."""

    def generate(self, _: PromptEnvelope) -> LLMGenerationResult:
        return LLMGenerationResult.succeeded(
            provider="openai",
            model="gpt-5.6-luna",
            output={
                "outcome": "ENTER_WORKFLOW",
                "workflow_name": "po_reroute",
                "workflow_version": 1,
                "supplier_id": "EVAL-SUP-Z",
                "quantity": "90",
                "original_purchase_order_id": "EVAL-PO-A2",
                "production_order_id": "EVAL-PROD-A2",
                "rationale": "Approved alternate meets the deadline with current authorized evidence.",
            },
        )


def test_cli_json_marks_guided_demo_as_fake_and_live_evaluation_as_no_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """JSON never leaves a reviewer to infer planner mode, schema status, or the gate boundary."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "provenance-test-secret")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setattr(cli, "create_no_write_adapter", lambda _: _PassingAdapter())

    demo = CliRunner().invoke(cli.app, ["--output", "json", "demo", "--list"])
    evaluation = CliRunner().invoke(
        cli.app,
        [
            "--output",
            "json",
            "llm-evaluate",
            "--profile",
            "openai",
            "--case",
            "a-unapproved-bait",
            "--execute",
        ],
    )

    assert demo.exit_code == 0
    demo_payload = json.loads(demo.stdout)
    assert demo_payload["data"]["cases"][0]["planner"] == {
        "mode": "fake_deterministic",
        "label": "FAKE / DETERMINISTIC",
        "provider": None,
        "profile": None,
        "model": "deterministic-fake-v1",
        "schema_validation": "passed",
        "schema_validation_label": "Passed",
        "gate_status": "pending_approval",
        "gate_label": "Passed to pending approval",
    }

    assert evaluation.exit_code == 0
    evaluation_payload = json.loads(evaluation.stdout)
    assert evaluation_payload["data"]["planner"] == {
        "mode": "live",
        "label": "LIVE",
        "provider": "openai",
        "profile": "openai",
        "model": "gpt-5.6-luna",
        "schema_validation": "passed",
        "schema_validation_label": "Passed",
        "gate_status": "not_invoked_no_write_evaluation",
        "gate_label": "Not invoked (no-write evaluation)",
    }
    assert "provenance-test-secret" not in demo.output + evaluation.output


def test_cli_text_evaluation_prints_live_mode_and_no_write_gate_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plain terminal output preserves provenance when color is disabled."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "provenance-text-secret")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setattr(cli, "create_no_write_adapter", lambda _: _PassingAdapter())

    result = CliRunner().invoke(
        cli.app,
        [
            "--no-color",
            "llm-evaluate",
            "--profile",
            "openai",
            "--case",
            "a-unapproved-bait",
            "--execute",
        ],
    )

    assert result.exit_code == 0
    assert "Planner: LIVE" in result.stdout
    assert "Schema validation" in result.stdout
    assert "Not invoked (no-write evaluation)" in result.stdout
    assert "provenance-text-secret" not in result.output
