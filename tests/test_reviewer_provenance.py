"""Contracts for reviewer-visible fake/live planner provenance."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.contract]


def test_live_no_write_provenance_is_explicit_and_secret_free() -> None:
    """A live evaluation names its mode and adapter facts without carrying a credential or prompt."""
    from enterprise_agent.review_provenance import (
        GateStatus,
        PlannerMode,
        PlannerProvenance,
        SchemaValidation,
    )

    provenance = PlannerProvenance(
        mode=PlannerMode.LIVE,
        provider="openai",
        profile="openai",
        model="gpt-5.6-luna",
        schema_validation=SchemaValidation.PASSED,
        gate_status=GateStatus.NOT_INVOKED_NO_WRITE_EVALUATION,
    )

    assert provenance.to_data() == {
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
    assert "api_key" not in repr(provenance)
    assert "prompt" not in repr(provenance).lower()


def test_deterministic_demo_provenance_distinguishes_staged_and_fixture_cases() -> None:
    """Fake-planner fixtures cannot be confused with a validated, gated pending plan."""
    from enterprise_agent.application.guided_demo import DemoExecutionMode, guided_demo_cases
    from enterprise_agent.review_provenance import GateStatus, SchemaValidation

    staged = next(
        case
        for case in guided_demo_cases()
        if case.execution_mode is DemoExecutionMode.STAGE_PENDING
    ).planner_provenance
    fixture = next(
        case for case in guided_demo_cases() if case.execution_mode is DemoExecutionMode.FIXTURE
    ).planner_provenance

    assert staged.mode_label == "FAKE / DETERMINISTIC"
    assert staged.provider is None
    assert staged.profile is None
    assert staged.model == "deterministic-fake-v1"
    assert staged.schema_validation is SchemaValidation.PASSED
    assert staged.gate_status is GateStatus.PENDING_APPROVAL
    assert fixture.schema_validation is SchemaValidation.NOT_RUN
    assert fixture.gate_status is GateStatus.NOT_INVOKED_FIXTURE


@pytest.mark.parametrize(
    ("status", "label"),
    (
        ("NOT_INVOKED_PLANNER_FAILURE", "Not invoked (planner response was not usable)"),
        ("NOT_INVOKED_MANUAL_REVIEW", "Not invoked (manual review proposed)"),
        ("NOT_INVOKED_NO_ACTION", "Not invoked (no action proposed)"),
        ("DENIED", "Denied by deterministic policy"),
    ),
)
def test_live_demo_non_executable_paths_have_specific_visible_gate_labels(
    status: str, label: str
) -> None:
    """A live result cannot falsely appear approval-ready when validation or policy stopped it."""
    from enterprise_agent.review_provenance import GateStatus

    assert GateStatus[status].label == label
