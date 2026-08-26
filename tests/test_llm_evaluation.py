"""Manual no-write live-LLM evaluation-pack contracts without live provider calls in CI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.config import ProviderConfiguration
from enterprise_agent.ports import (
    LLMCostSource,
    LLMGenerationResult,
    LLMGenerationStatus,
    LLMPort,
    LLMUsage,
    PromptEnvelope,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

_LLM_ENVIRONMENT_NAMES = (
    "LLM_PROFILE",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
)
_RAW_SENTINEL = "raw-model-rationale-must-not-be-recorded"


def _clear_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a developer's ignored local profiles from affecting these contracts."""
    for name in _LLM_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


@dataclass
class _RecordingLLM:
    """Return case-configured adapter results while retaining only test-local prompts."""

    results: dict[str, LLMGenerationResult]
    prompts: list[PromptEnvelope] = field(default_factory=list)

    def generate(self, prompt: PromptEnvelope) -> LLMGenerationResult:
        """Return the fixed normalized result for one synthetic case."""
        self.prompts.append(prompt)
        return self.results[prompt.attention.cause]


def _case(case_id: str):  # type: ignore[no-untyped-def]
    """Retrieve one case by its stable public identifier."""
    from enterprise_agent.application.llm_evaluation import evaluation_cases

    return next(item for item in evaluation_cases() if item.case_id == case_id)


@pytest.mark.critical
def test_evaluation_catalogue_has_ten_fixed_sanitized_cases_across_all_scenarios() -> None:
    """The manual pack is diverse but bounded, fixed-time, and free of business-system dependencies."""
    from enterprise_agent.application.llm_evaluation import evaluation_cases

    cases = evaluation_cases()

    assert len(cases) == 10
    assert {item.scenario for item in cases} == {"scenario_a", "scenario_b", "scenario_c"}
    assert len({item.case_id for item in cases}) == len(cases)
    assert all(item.prompt.evidence for item in cases)
    assert all(item.prompt.actor.scopes == frozenset() for item in cases)
    assert all(item.prompt.actor.plant_ids == frozenset() for item in cases)
    assert all(item.prompt.purpose == "manual_synthetic_llm_evaluation" for item in cases)
    assert all(item.prompt.run_id.startswith("llm-evaluation:") for item in cases)
    assert all("@" not in repr(item.prompt) for item in cases)


@pytest.mark.critical
def test_evaluation_scores_allowed_candidate_freshness_and_rationale_without_retaining_raw_output() -> (
    None
):
    """A normalized response becomes scalar checks, never a persisted raw model recommendation."""
    from enterprise_agent.application.llm_evaluation import evaluate_cases

    case = _case("a-unapproved-bait")
    result = LLMGenerationResult.succeeded(
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
            "rationale": f"Approved EVAL-SUP-Z meets the production deadline. {_RAW_SENTINEL}",
        },
        usage=LLMUsage(
            input_tokens=120,
            cached_input_tokens=0,
            output_tokens=30,
            total_tokens=150,
            cost_usd=Decimal("0.000060"),
            cost_source=LLMCostSource.ESTIMATED,
        ),
    )

    report = evaluate_cases((case,), _RecordingLLM({case.prompt.attention.cause: result}))
    observation = report.observations[0]
    data = report.to_data()

    assert observation.status is LLMGenerationStatus.SUCCEEDED
    assert observation.observed_outcome == "ENTER_WORKFLOW"
    assert observation.checks["structured_valid"].value == "pass"
    assert observation.checks["expected_outcome"].value == "pass"
    assert observation.checks["allowed_references"].value == "pass"
    assert observation.checks["concise_explanation"].value == "pass"
    assert report.usage.input_tokens == 120
    assert report.usage.total_cost_usd == Decimal("0.000060")
    assert _RAW_SENTINEL not in repr(observation)
    assert _RAW_SENTINEL not in json.dumps(data)
    assert "rationale" not in json.dumps(data)


@pytest.mark.critical
def test_evaluation_scores_latest_evidence_injection_and_ambiguity_as_explicit_policy_checks() -> (
    None
):
    """The high-value messy-company cases are independently visible in a provider-neutral scorecard."""
    from enterprise_agent.application.llm_evaluation import evaluate_cases

    latest = _case("a-newest-on-schedule")
    injection = _case("a-hostile-email")
    ambiguity = _case("b-multiple-unranked-lots")
    results = {
        latest.prompt.attention.cause: LLMGenerationResult.succeeded(
            provider="claude",
            model="claude-sonnet-5",
            output={
                "outcome": "NO_ACTION",
                "rationale": "The newest on-schedule update removes the production risk.",
            },
        ),
        injection.prompt.attention.cause: LLMGenerationResult.succeeded(
            provider="claude",
            model="claude-sonnet-5",
            output={
                "outcome": "MANUAL_REVIEW",
                "reason": "The hostile email is untrusted and the supplier evidence conflicts.",
            },
        ),
        ambiguity.prompt.attention.cause: LLMGenerationResult.succeeded(
            provider="claude",
            model="claude-sonnet-5",
            output={
                "outcome": "MANUAL_REVIEW",
                "reason": "Two released lots are unranked, so a human must choose.",
            },
        ),
    }

    observations = evaluate_cases(
        (latest, injection, ambiguity), _RecordingLLM(results)
    ).observations

    assert observations[0].checks["newest_evidence"].value == "pass"
    assert observations[1].checks["prompt_injection_resistance"].value == "pass"
    assert observations[2].checks["manual_review_under_ambiguity"].value == "pass"
    assert all(observation.passed for observation in observations)


def test_evaluation_failed_or_invalid_adapter_results_are_visible_without_output_or_raw_error_data() -> (
    None
):
    """A provider failure cannot be mistaken for an evaluated recommendation or leak transport content."""
    from enterprise_agent.application.llm_evaluation import evaluate_cases

    case = _case("c-current-bulletin")
    failed = LLMGenerationResult.failed(
        provider="openrouter",
        model="nvidia/nemotron-3-ultra-550b-a55b:free",
        status=LLMGenerationStatus.INVALID_RESPONSE,
    )

    observation = evaluate_cases(
        (case,), _RecordingLLM({case.prompt.attention.cause: failed})
    ).observations[0]

    assert observation.status is LLMGenerationStatus.INVALID_RESPONSE
    assert observation.observed_outcome is None
    assert observation.checks["structured_valid"].value == "fail"
    assert observation.passed is False
    assert "error" not in repr(observation).lower()


def test_evaluation_selection_rejects_duplicate_unknown_and_implicit_execution_sets() -> None:
    """Live cost is bounded by a named case set rather than an accidental default run."""
    from enterprise_agent.application.llm_evaluation import (
        EvaluationCaseSelectionError,
        select_evaluation_cases,
    )

    with pytest.raises(EvaluationCaseSelectionError, match="select at least one"):
        select_evaluation_cases((), include_all=False)
    with pytest.raises(EvaluationCaseSelectionError, match="at most once"):
        select_evaluation_cases(("a-approved-reroute", "a-approved-reroute"), include_all=False)
    with pytest.raises(EvaluationCaseSelectionError, match="unknown"):
        select_evaluation_cases(("not-a-case",), include_all=False)

    all_cases = select_evaluation_cases((), include_all=True)

    assert len(all_cases) == 10


def test_explicit_profile_configuration_can_use_a_locally_stored_nonactive_profile() -> None:
    """A manual evaluation chooses one named configured provider without changing LLM_PROFILE."""
    from enterprise_agent.config import load_provider_profile

    configuration = load_provider_profile(
        "claude",
        {
            "LLM_PROFILE": "openai",
            "ANTHROPIC_API_KEY": "claude-evaluation-secret",
            "ANTHROPIC_MODEL": "claude-sonnet-5",
        },
    )

    assert configuration == ProviderConfiguration(
        profile="claude", model="claude-sonnet-5", api_key="claude-evaluation-secret"
    )


def test_cli_lists_cases_without_loading_configuration_or_constructing_a_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery is free, database-free, and cannot accidentally spend a provider credit."""
    _clear_llm_environment(monkeypatch)

    def should_not_construct_provider(_: ProviderConfiguration) -> LLMPort:
        raise AssertionError("listing evaluation cases must not construct a provider")

    monkeypatch.setattr(cli, "create_no_write_adapter", should_not_construct_provider)

    result = CliRunner().invoke(cli.app, ["--output", "json", "llm-evaluate", "--list"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "succeeded"
    assert len(payload["data"]["cases"]) == 10
    assert "API_KEY" not in result.output


def test_cli_text_catalogue_and_invalid_list_combination_remain_safe_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyboard-oriented listing and its argument refusal share the no-provider safety boundary."""
    _clear_llm_environment(monkeypatch)

    text_result = CliRunner().invoke(cli.app, ["--no-color", "llm-evaluate", "--list"])
    invalid_result = CliRunner().invoke(
        cli.app,
        ["--output", "json", "llm-evaluate", "--list", "--execute"],
    )

    assert text_result.exit_code == 0
    assert "Manual live-LLM evaluation" in text_result.stdout
    assert "Listing is local and makes no provider request." in text_result.stdout
    assert "a-unapproved-bait [scenario_a]" in text_result.stdout
    assert invalid_result.exit_code == 2
    assert json.loads(invalid_result.stdout)["error"] == {
        "code": "invalid_arguments",
        "message": "Run enterprise-agent llm-evaluate --list by itself.",
    }


def test_cli_requires_profile_case_and_explicit_execute_before_any_live_adapter_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No environment profile or provider request is implicit for this manual paid evaluation path."""
    _clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-evaluation-secret")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    calls: list[ProviderConfiguration] = []

    def recording_provider(configuration: ProviderConfiguration) -> LLMPort:
        calls.append(configuration)
        raise AssertionError("the adapter must not be constructed before --execute")

    monkeypatch.setattr(cli, "create_no_write_adapter", recording_provider)

    missing_profile = CliRunner().invoke(cli.app, ["llm-evaluate", "--case", "a-approved-reroute"])
    missing_execute = CliRunner().invoke(
        cli.app,
        ["llm-evaluate", "--profile", "openai", "--case", "a-approved-reroute"],
    )

    assert missing_profile.exit_code == 1
    assert "--profile is required" in missing_profile.stderr
    assert missing_execute.exit_code == 1
    assert "--execute is required" in missing_execute.stderr
    assert calls == []
    assert "openai-evaluation-secret" not in missing_profile.output + missing_execute.output


def test_cli_evaluation_emits_only_sanitized_scorecard_and_never_persists_a_provider_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The deliberate live command has one explicit profile and one output-only synthetic evaluation."""
    _clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-evaluation-secret")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    constructed: list[ProviderConfiguration] = []

    @dataclass
    class _PassingAdapter:
        prompts: list[PromptEnvelope] = field(default_factory=list)

        def generate(self, prompt: PromptEnvelope) -> LLMGenerationResult:
            self.prompts.append(prompt)
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
                    "rationale": f"Approved supplier meets the deadline. {_RAW_SENTINEL}",
                },
            )

    adapter = _PassingAdapter()

    def create_adapter(configuration: ProviderConfiguration) -> LLMPort:
        constructed.append(configuration)
        return adapter

    monkeypatch.setattr(cli, "create_no_write_adapter", create_adapter)

    result = CliRunner().invoke(
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

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert constructed == [
        ProviderConfiguration(
            profile="openai", model="gpt-5.6-luna", api_key="openai-evaluation-secret"
        )
    ]
    assert len(adapter.prompts) == 1
    assert payload["status"] == "succeeded"
    assert payload["data"]["profile"] == "openai"
    assert payload["data"]["model"] == "gpt-5.6-luna"
    assert payload["data"]["observations"][0]["observed_outcome"] == "ENTER_WORKFLOW"
    assert payload["data"]["observations"][0]["checks"]["allowed_references"] == "pass"
    assert "openai-evaluation-secret" not in result.output
    assert _RAW_SENTINEL not in result.output
    assert "rationale" not in result.output


def test_cli_evaluation_text_scorecard_keeps_the_same_scalar_safety_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Terminal-oriented reviewers receive result, checks, and metering without the model's response text."""
    _clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "text-evaluation-secret")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")

    class _PassingAdapter:
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
                    "rationale": _RAW_SENTINEL,
                },
            )

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

    assert result.exit_code == 1
    assert "Profile: openai · model: gpt-5.6-luna" in result.stdout
    assert "a-unapproved-bait: expected=ENTER_WORKFLOW; observed=ENTER_WORKFLOW" in result.stdout
    assert "concise_explanation=fail" in result.stdout
    assert "requests=1, metered=0, unmetered=1" in result.stdout
    assert "text-evaluation-secret" not in result.output
    assert _RAW_SENTINEL not in result.output
