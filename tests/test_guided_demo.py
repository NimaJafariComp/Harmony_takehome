"""Contracts for the local-only guided deterministic demo catalogue."""

import pytest

from enterprise_agent.application.guided_demo import (
    DemoCaseSelectionError,
    DemoExecutionMode,
    select_guided_demo_cases,
)


pytestmark = pytest.mark.unit


def test_safety_tour_selects_every_required_company_story_with_honest_execution_labels() -> None:
    """The default tour covers the assignment's messy cases without implying a live LLM ran."""
    cases = select_guided_demo_cases(("safety-tour",))

    assert [case.case_id for case in cases] == [
        "scenario-a-reroute-bait",
        "scenario-a-crash-recovery",
        "scenario-a-current-evidence",
        "scenario-a-tuesday-follow-up",
        "scenario-b-capacity",
        "scenario-c-pending-review",
    ]
    assert cases[0].execution_mode is DemoExecutionMode.STAGE_PENDING
    assert cases[-1].execution_mode is DemoExecutionMode.STAGE_PENDING
    assert all(case.planner_label == "deterministic fake planner" for case in cases)
    assert "unapproved" in cases[0].outcome.lower()
    assert "exactly one" in cases[1].outcome.lower()
    assert "malicious" in cases[2].outcome.lower()
    assert "missing" in cases[3].outcome.lower()
    assert "committed" in cases[4].outcome.lower()


def test_demo_case_selection_rejects_unknown_or_conflicting_aliases_before_any_write() -> None:
    """A typo or an ambiguous tour request fails while selection is still entirely read-only."""
    with pytest.raises(DemoCaseSelectionError, match="unknown guided demo case"):
        select_guided_demo_cases(("not-a-case",))

    with pytest.raises(DemoCaseSelectionError, match="cannot be combined"):
        select_guided_demo_cases(("safety-tour", "scenario-b-capacity"))
