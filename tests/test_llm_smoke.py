"""Manual non-sensitive LLM smoke-command contracts without live provider calls."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.config import ProviderConfiguration
from enterprise_agent.ports import LLMGenerationResult, LLMGenerationStatus

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


def _clear_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent the developer's local profile from affecting a smoke-command contract."""
    for name in _LLM_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.critical
def test_fixed_smoke_prompt_has_no_business_evidence_or_data_source_dependencies() -> None:
    """The manual probe carries a fixed harmless request only, never seeded business context."""
    from enterprise_agent.smoke import FIXED_SMOKE_MESSAGE, smoke_prompt

    prompt = smoke_prompt()

    assert prompt.evidence == ()
    assert prompt.actor.scopes == frozenset()
    assert prompt.actor.plant_ids == frozenset()
    assert prompt.response_schema == "scenario_a_recommendation:v1"
    assert prompt.messages[0].content == FIXED_SMOKE_MESSAGE
    assert "ERP" not in FIXED_SMOKE_MESSAGE
    assert "mail" not in FIXED_SMOKE_MESSAGE.lower()
    assert "calendar" not in FIXED_SMOKE_MESSAGE.lower()
    assert "knowledge" not in FIXED_SMOKE_MESSAGE.lower()


@pytest.mark.parametrize(
    ("profile", "model", "adapter_type"),
    (
        ("openai", "gpt-5.6-luna", "OpenAIResponsesAdapter"),
        ("claude", "claude-sonnet-5", "ClaudeMessagesAdapter"),
        (
            "openrouter",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "OpenRouterChatCompletionsAdapter",
        ),
    ),
)
def test_smoke_adapter_composition_selects_only_the_configured_provider(
    profile: str,
    model: str,
    adapter_type: str,
) -> None:
    """The profile choice maps to one adapter without requesting a fallback or making a request."""
    from enterprise_agent.smoke import create_smoke_adapter

    adapter = create_smoke_adapter(
        ProviderConfiguration(profile=profile, model=model, api_key=f"smoke-{profile}-key")
    )

    assert type(adapter).__name__ == adapter_type


def test_llm_smoke_command_reports_only_safe_success_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A deliberate smoke call reports the selected profile and model without exposing output or key."""
    _clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROFILE", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("OPENAI_API_KEY", "smoke-openai-secret")
    observed_configurations: list[ProviderConfiguration] = []

    def fake_run_smoke(configuration: ProviderConfiguration) -> LLMGenerationResult:
        observed_configurations.append(configuration)
        return LLMGenerationResult.succeeded(
            provider="openai",
            model="gpt-5.6-luna",
            output={"outcome": "MANUAL_REVIEW", "reason": "Probe returned structured output."},
        )

    monkeypatch.setattr(cli, "run_smoke", fake_run_smoke)

    result = CliRunner().invoke(cli.app, ["llm-smoke"])

    assert result.exit_code == 0
    assert observed_configurations == [
        ProviderConfiguration(profile="openai", model="gpt-5.6-luna", api_key="smoke-openai-secret")
    ]
    assert "llm-smoke: succeeded (profile: openai, model: gpt-5.6-luna)" in result.stdout
    assert "no business data was sent" in result.stdout
    assert "smoke-openai-secret" not in result.output
    assert "Probe returned structured output" not in result.output


@pytest.mark.critical
def test_llm_smoke_command_fails_closed_without_business_data_on_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A provider failure is nonzero, reports only the normalized status, and never runs a fallback."""
    _clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROFILE", "openrouter")
    monkeypatch.setenv("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
    monkeypatch.setenv("OPENROUTER_API_KEY", "smoke-router-secret")
    calls: list[ProviderConfiguration] = []

    def fake_run_smoke(configuration: ProviderConfiguration) -> LLMGenerationResult:
        calls.append(configuration)
        return LLMGenerationResult.failed(
            provider="openrouter",
            model="nvidia/nemotron-3-ultra-550b-a55b:free",
            status=LLMGenerationStatus.PROVIDER_FAILURE,
        )

    monkeypatch.setattr(cli, "run_smoke", fake_run_smoke)

    result = CliRunner().invoke(cli.app, ["llm-smoke"])

    assert result.exit_code == 1
    assert calls == [
        ProviderConfiguration(
            profile="openrouter",
            model="nvidia/nemotron-3-ultra-550b-a55b:free",
            api_key="smoke-router-secret",
        )
    ]
    assert "provider_failure" in result.stderr
    assert "no business data was sent" in result.stderr
    assert "smoke-router-secret" not in result.output


@pytest.mark.critical
def test_llm_smoke_command_names_missing_profile_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A manual command must refuse missing configuration rather than start interactive setup."""
    _clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.app, ["llm-smoke"])

    assert result.exit_code == 1
    assert "Missing required configuration: LLM_PROFILE" in result.stderr
    assert "setup" not in result.output.lower()
