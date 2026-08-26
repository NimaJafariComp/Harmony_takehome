"""Named deterministic business stories used by the required-scenario regression selection.

This remains intentionally small and test-only.  Each entry names a fixed mutation of
the seeded company and anchors it to the focused test that proves the expected safety
outcome; it is not a runtime scenario engine or a generic fixture DSL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScenarioStory:
    """One reviewer-readable company situation with an explicit behavioral oracle."""

    pytest_id: str
    base_seed_mutation: str
    expected_outcome: str
    test_nodeids: tuple[str, ...]


SCENARIO_STORIES: tuple[ScenarioStory, ...] = (
    ScenarioStory(
        pytest_id="scenario_a_normal_reroute",
        base_seed_mutation="none; deterministic Scenario A seed",
        expected_outcome="one pending approval and no ERP write before approval",
        test_nodeids=(
            "tests/test_safe_planning.py::test_seeded_scenario_a_creates_only_one_pending_approval_and_no_erp_writes",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_unapproved_bait",
        base_seed_mutation="add cheaper, faster, same-part/plant Supplier Bait with approved=false",
        expected_outcome="supplier is visible as an exclusion, never an eligible reroute",
        test_nodeids=(
            "tests/test_seed.py::test_scenario_a_seed_contains_a_cheaper_faster_but_unapproved_supplier_bait",
            "tests/test_supplier_candidates.py::test_seeded_filter_exposes_only_supplier_z_as_the_allowed_alternate",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_approved_but_too_slow",
        base_seed_mutation="use approved alternate whose lead time misses production start",
        expected_outcome="candidate and write-time guard both reject the supplier",
        test_nodeids=(
            "tests/test_tool_adapter.py::test_create_replacement_tool_rejects_an_approved_supplier_that_is_still_too_slow",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_authority_limit",
        base_seed_mutation="lower Dana's USD authority below the replacement value",
        expected_outcome="gate denies the reroute without inventing an escalation hierarchy",
        test_nodeids=(
            "tests/test_scenario_a_gate.py::test_gate_denies_a_reroute_that_exceeds_the_actors_currency_limit",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_available_approver",
        base_seed_mutation="Dana remains available or answers before the end-of-day task",
        expected_outcome="approval remains with Dana and is never rerouted",
        test_nodeids=(
            "tests/test_approval_routing.py::test_routing_leaves_answered_or_available_approvals_with_the_original_approver",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_out_of_office_backup",
        base_seed_mutation="Dana is out tomorrow at end of day with capable backup Avery",
        expected_outcome="one durable end-of-day reroute goes to the designated backup",
        test_nodeids=(
            "tests/test_approval_routing.py::test_end_of_day_routing_schedules_once_and_reroutes_only_for_next_day_absence",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_current_on_schedule_update",
        base_seed_mutation="replace delayed shipment update with a newer on-schedule arrival before production",
        expected_outcome="NO_ACTION, no approval, and no write path",
        test_nodeids=(
            "tests/test_scenario_a_gate.py::test_newer_on_schedule_supplier_update_prevents_a_reroute",
            "tests/test_scenario_a_gate.py::test_on_schedule_update_cannot_create_an_approval_or_write_path",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_malicious_email",
        base_seed_mutation="add email text instructing cancellation of every open PO",
        expected_outcome="text remains untrusted evidence and invalid output cannot create an action",
        test_nodeids=(
            "tests/test_provider_contracts.py::test_malicious_email_remains_untrusted_data_and_cannot_create_an_action",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_changed_purchase_order",
        base_seed_mutation="increment the source version of the original PO after recommendation",
        expected_outcome="gate blocks approval and effects because the plan is stale",
        test_nodeids=(
            "tests/test_scenario_a_gate.py::test_gate_denies_missing_write_scope_and_stale_evidence",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_crash_after_replacement_po",
        base_seed_mutation="inject a crash after the replacement-PO external effect",
        expected_outcome="restart reuses the started key and creates exactly one replacement PO",
        test_nodeids=(
            "tests/test_workflow_executor.py::test_crash_after_replacement_effect_restarts_with_the_same_started_key",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_tuesday_received",
        base_seed_mutation="record a full receipt for the replacement PO on Tuesday",
        expected_outcome="original attention item resolves",
        test_nodeids=(
            "tests/test_arrival_check.py::test_tuesday_full_receipt_resolves_the_original_attention",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_a_tuesday_missing",
        base_seed_mutation="record only partial or no receipt for the replacement PO on Tuesday",
        expected_outcome="one source-version-specific follow-up is opened",
        test_nodeids=(
            "tests/test_arrival_check.py::test_partial_or_missing_receipt_opens_one_source_version_specific_follow_up",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_b_sufficient_cover",
        base_seed_mutation="released substitute lot covers the held lot's full production allocation",
        expected_outcome="approved reallocation and notification execute once",
        test_nodeids=(
            "tests/test_scenario_b_execution.py::test_seeded_scenario_b_requires_approval_executes_each_path_once_and_explains_it",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_b_insufficient_or_ambiguous_cover",
        base_seed_mutation="leave only partial capacity or two unranked substitute lots",
        expected_outcome="no approval or workflow is created for a partial or arbitrary reallocation",
        test_nodeids=(
            "tests/test_scenario_b_context.py::test_quality_control_refuses_reallocation_that_is_not_one_unambiguous_full_cover",
        ),
    ),
    ScenarioStory(
        pytest_id="scenario_b_quality_scope_and_state_change",
        base_seed_mutation="use Quality-only Quinn, then release the formerly held lot",
        expected_outcome="purchasing write is denied and the old hold context is stale",
        test_nodeids=(
            "tests/test_tool_catalog.py::test_quality_manager_cannot_create_a_replacement_purchase_order",
            "tests/test_scenario_b_context.py::test_quality_release_after_recommendation_invalidates_the_old_hold_context",
        ),
    ),
)
