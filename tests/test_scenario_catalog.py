"""Structural contracts for the reviewer-readable deterministic scenario library."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from scenario_catalog import SCENARIO_STORIES, ScenarioStory


def _declared_test_names(relative_path: str) -> set[str]:
    """Read test names only, so catalog links cannot silently rot during refactoring."""
    module = ast.parse(Path(relative_path).read_text(encoding="utf-8"))
    return {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


@pytest.mark.scenario
@pytest.mark.parametrize("story", SCENARIO_STORIES, ids=lambda story: story.pytest_id)
def test_scenario_story_is_fixed_and_linked_to_a_real_behavioral_regression(
    story: ScenarioStory,
) -> None:
    """Every named company story has a fixed mutation, observable outcome, and live test anchor."""
    assert story.base_seed_mutation
    assert story.expected_outcome
    assert story.test_nodeids
    for nodeid in story.test_nodeids:
        relative_path, separator, test_name = nodeid.partition("::")
        assert separator == "::"
        assert test_name in _declared_test_names(relative_path)


@pytest.mark.scenario
def test_scenario_story_ids_are_unique_and_cover_both_required_business_paths() -> None:
    """The library stays discoverable without a generic scenario language or random fixtures."""
    identifiers = [story.pytest_id for story in SCENARIO_STORIES]

    assert len(identifiers) == len(set(identifiers))
    assert any(identifier.startswith("scenario_a_") for identifier in identifiers)
    assert any(identifier.startswith("scenario_b_") for identifier in identifiers)
