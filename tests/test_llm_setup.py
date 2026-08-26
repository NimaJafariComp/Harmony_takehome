"""Secret-safe local LLM profile setup and local environment loading contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from urllib.request import Request

import pytest
import typer
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.config import ConfigurationError, load_settings
from enterprise_agent.llm_setup import curated_models_for

pytestmark = pytest.mark.unit

_LLM_ENVIRONMENT_NAMES = (
    "LLM_PROFILE",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
)


def clear_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep process-level user configuration out of isolated first-run setup contracts."""
    for name in _LLM_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_curated_model_catalog_has_only_supported_profiles_and_recommended_defaults() -> None:
    """The interactive menu exposes a small reviewed catalog before a user explicitly enters custom ID."""
    from enterprise_agent.llm_setup import CURATED_MODEL_CATALOG, curated_models_for

    assert set(CURATED_MODEL_CATALOG) == {"claude", "openai", "openrouter"}
    assert [model.model_id for model in curated_models_for("openai")] == [
        "gpt-5.6-luna",
        "gpt-5.6-terra",
    ]
    assert curated_models_for("openai")[0].recommended is True
    assert curated_models_for("claude")[0].model_id == "claude-sonnet-5"
    assert curated_models_for("openrouter")[0].model_id == "nvidia/nemotron-3-ultra-550b-a55b:free"


@pytest.mark.parametrize(
    ("profile", "response_data", "expected_models", "expected_url"),
    (
        (
            "openai",
            {"data": [{"id": "gpt-5.6-luna"}, {"id": "gpt-5.6-terra"}, {"id": "other"}]},
            ("gpt-5.6-luna", "gpt-5.6-terra"),
            "https://api.openai.com/v1/models",
        ),
        (
            "claude",
            {
                "data": [
                    {
                        "id": "claude-sonnet-5",
                        "capabilities": {"structured_outputs": {"supported": True}},
                    },
                    {"id": "other"},
                ]
            },
            ("claude-sonnet-5",),
            "https://api.anthropic.com/v1/models?limit=1000",
        ),
        (
            "openrouter",
            {
                "data": [
                    {"id": "nvidia/nemotron-3-ultra-550b-a55b:free"},
                    {"id": "other"},
                ]
            },
            ("nvidia/nemotron-3-ultra-550b-a55b:free",),
            "https://openrouter.ai/api/v1/models",
        ),
    ),
)
def test_live_model_discovery_intersects_account_visible_models_with_adapter_reviewed_catalog(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    response_data: dict[str, object],
    expected_models: tuple[str, ...],
    expected_url: str,
) -> None:
    """The setup menu can suggest only models both listed for the key and covered by an adapter contract."""
    import json

    from enterprise_agent import llm_setup

    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(response_data).encode("utf-8")
    captured: dict[str, Request] = {}

    def fake_urlopen(request: Request, *, timeout: float) -> MagicMock:
        captured["request"] = request
        assert timeout == 3.5
        return response

    monkeypatch.setattr(llm_setup, "urlopen", fake_urlopen)

    models = llm_setup.discover_compatible_models(profile, "discovery-key", timeout_seconds=3.5)

    assert [model.model_id for model in models] == list(expected_models)
    assert models[0].recommended is True
    assert captured["request"].full_url == expected_url
    assert captured["request"].get_method() == "GET"
    if profile == "claude":
        assert captured["request"].get_header("X-api-key") == "discovery-key"
        assert captured["request"].get_header("Anthropic-version") == "2023-06-01"
    else:
        assert captured["request"].get_header("Authorization") == "Bearer discovery-key"


def test_live_model_discovery_refuses_to_suggest_unreviewed_or_unsupported_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No provider-list entry becomes a suggestion merely because a key can see it."""
    import json

    from enterprise_agent import llm_setup

    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(
        {
            "data": [
                {
                    "id": "claude-sonnet-5",
                    "capabilities": {"structured_outputs": {"supported": False}},
                },
                {"id": "unreviewed-model"},
            ]
        }
    ).encode("utf-8")
    monkeypatch.setattr(llm_setup, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(llm_setup.ModelDiscoveryError, match="no adapter-reviewed models"):
        llm_setup.discover_compatible_models("claude", "discovery-key")


def test_save_llm_profile_merges_existing_provider_profiles_atomically_and_owner_only(
    tmp_path: Path,
) -> None:
    """Saving Claude updates only the selected profile and preserves existing OpenAI values and comments."""
    from enterprise_agent.llm_setup import LLMSetupSelection, save_llm_profile

    env_path = tmp_path / ".env"
    env_path.write_text(
        "# local configuration\n"
        "DATABASE_URL=postgresql+psycopg://agent:agent@db:5432/agent\n"
        "OPENAI_API_KEY=existing-openai-key\n"
        "OPENAI_MODEL=gpt-5.6-luna\n",
        encoding="utf-8",
    )
    selection = LLMSetupSelection(
        profile="claude",
        api_key="new-anthropic-key",
        model="claude-sonnet-5",
    )

    save_llm_profile(env_path, selection)

    contents = env_path.read_text(encoding="utf-8")
    assert "# local configuration" in contents
    assert "DATABASE_URL=postgresql+psycopg://agent:agent@db:5432/agent" in contents
    assert "OPENAI_API_KEY=existing-openai-key" in contents
    assert "OPENAI_MODEL=gpt-5.6-luna" in contents
    assert "LLM_PROFILE=claude" in contents
    assert "ANTHROPIC_API_KEY=new-anthropic-key" in contents
    assert "ANTHROPIC_MODEL=claude-sonnet-5" in contents
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert "new-anthropic-key" not in repr(selection)


def test_local_environment_uses_process_values_over_env_file_without_evaluating_shell_syntax(
    tmp_path: Path,
) -> None:
    """The local parser reads literal assignments and gives an explicit environment higher precedence."""
    from enterprise_agent.llm_setup import load_local_environment

    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENAI_API_KEY=from-file\nOPENAI_MODEL=gpt-5.6-luna\nCOMMAND=$(not-executed)\n",
        encoding="utf-8",
    )

    environment = load_local_environment(env_path, {"OPENAI_API_KEY": "from-process"})

    assert environment["OPENAI_API_KEY"] == "from-process"
    assert environment["OPENAI_MODEL"] == "gpt-5.6-luna"
    assert environment["COMMAND"] == "$(not-executed)"


@pytest.mark.parametrize(
    ("profile", "expected_url", "expected_headers"),
    (
        (
            "openai",
            "https://api.openai.com/v1/models",
            {"Authorization": "Bearer verification-key"},
        ),
        (
            "claude",
            "https://api.anthropic.com/v1/models?limit=1",
            {"X-api-key": "verification-key", "Anthropic-version": "2023-06-01"},
        ),
        (
            "openrouter",
            "https://openrouter.ai/api/v1/auth/key",
            {"Authorization": "Bearer verification-key"},
        ),
    ),
)
def test_credential_verification_uses_only_selected_provider_no_generation_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    expected_url: str,
    expected_headers: dict[str, str],
) -> None:
    """Explicit verification checks the selected key through a no-generation endpoint without printing it."""
    from enterprise_agent import llm_setup

    response = MagicMock()
    response.__enter__.return_value = response
    captured: dict[str, Any] = {}

    def fake_urlopen(request: object, *, timeout: float) -> MagicMock:
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(llm_setup, "urlopen", fake_urlopen)

    assert llm_setup.verify_credential(profile, "verification-key", timeout_seconds=3.5) is True

    request = cast(Request, captured["request"])
    assert request.full_url == expected_url
    assert request.get_method() == "GET"
    assert {name: request.get_header(name) for name in expected_headers} == expected_headers
    assert captured["timeout"] == 3.5


def test_credential_verification_fails_closed_without_secret_or_provider_error_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected no-generation verification becomes false rather than leaking an exception payload."""
    from enterprise_agent import llm_setup

    monkeypatch.setattr(
        llm_setup, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("bad"))
    )

    assert llm_setup.verify_credential("openrouter", "verification-key") is False


@pytest.mark.parametrize(
    "selection",
    (
        {"profile": "unknown", "api_key": "valid-key", "model": "valid-model"},
        {"profile": "openai", "api_key": "", "model": "valid-model"},
        {"profile": "openai", "api_key": "key\npoison", "model": "valid-model"},
        {"profile": "openai", "api_key": "valid-key", "model": "model\npoison"},
    ),
)
def test_llm_profile_save_rejects_invalid_or_injectable_selection(
    tmp_path: Path,
    selection: dict[str, str],
) -> None:
    """Untrusted terminal input cannot select an unknown profile or inject another environment setting."""
    from enterprise_agent.llm_setup import LLMSetupSelection, save_llm_profile

    with pytest.raises(ValueError):
        save_llm_profile(tmp_path / ".env", LLMSetupSelection(**selection))


def test_load_settings_uses_claude_profile_with_anthropic_environment_names() -> None:
    """The user-facing Claude selection remains distinct from Anthropic's credential variable names."""
    configuration = load_settings(
        {
            "DATABASE_URL": "postgresql+psycopg://agent:agent@localhost:5432/agent",
            "LLM_PROFILE": "claude",
            "ANTHROPIC_API_KEY": "test-claude-key",
            "ANTHROPIC_MODEL": "claude-sonnet-5",
        }
    )

    assert configuration.provider.profile == "claude"
    assert configuration.provider.model == "claude-sonnet-5"
    assert "test-claude-key" not in repr(configuration)


def test_load_settings_names_the_missing_claude_key_without_exposing_any_value() -> None:
    """A malformed Claude profile fails with the exact setting name only."""
    environment = {
        "DATABASE_URL": "postgresql+psycopg://agent:agent@localhost:5432/agent",
        "LLM_PROFILE": "claude",
        "ANTHROPIC_MODEL": "claude-sonnet-5",
    }

    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        load_settings(environment)


def test_run_interactively_creates_a_hidden_key_profile_with_the_recommended_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A first `run` requests a hidden key, saves the selected profile, and never prints the key."""
    clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    prompts = iter(("openai", "interactive-openai-key", "1"))
    prompt_calls: list[tuple[str, bool]] = []

    def fake_prompt(message: str, *args: Any, **kwargs: Any) -> str:
        prompt_calls.append((message, bool(kwargs.get("hide_input", False))))
        return next(prompts)

    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(typer, "prompt", fake_prompt)
    monkeypatch.setattr(typer, "confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        cli, "discover_compatible_models", lambda profile, _key: curated_models_for(profile)
    )

    result = CliRunner().invoke(cli.app, ["run"])

    env_path = tmp_path / ".env"
    assert result.exit_code == 0
    assert prompt_calls[1] == ("Openai API key", True)
    assert "interactive-openai-key" not in result.output
    assert "LLM_PROFILE=openai" in env_path.read_text(encoding="utf-8")
    assert "OPENAI_MODEL=gpt-5.6-luna" in env_path.read_text(encoding="utf-8")
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_interactive_setup_displays_only_key_accessible_adapter_reviewed_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The menu displays a recommendation only after the selected key's metadata list confirms access."""
    from enterprise_agent.llm_setup import CuratedModel

    clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    prompts = iter(("openai", "interactive-openai-key", "1"))
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(typer, "prompt", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(typer, "confirm", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        cli,
        "discover_compatible_models",
        lambda profile, api_key: (
            CuratedModel(
                model_id="gpt-5.6-terra",
                label="GPT-5.6 Terra — more capable",
                recommended=True,
            ),
        ),
    )

    result = CliRunner().invoke(cli.app, ["llm-setup"])

    assert result.exit_code == 0
    assert "Available adapter-compatible models for this key:" in result.stdout
    assert "gpt-5.6-terra (recommended)" in result.stdout
    assert "gpt-5.6-luna" not in result.stdout
    assert "interactive-openai-key" not in result.output
    assert "OPENAI_MODEL=gpt-5.6-terra" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_explicit_setup_verifies_only_the_selected_provider_and_allows_a_custom_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A user-confirmed verification is injected, successful, and persists the expressly entered model ID."""
    clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    prompts = iter(("openrouter", "interactive-router-key", "2", "vendor/custom-structured-model"))
    verification_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(typer, "prompt", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(typer, "confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        cli, "discover_compatible_models", lambda profile, _key: curated_models_for(profile)
    )

    def record_verification(profile: str, api_key: str) -> bool:
        verification_calls.append((profile, api_key))
        return True

    monkeypatch.setattr(
        cli,
        "verify_credential",
        record_verification,
    )

    result = CliRunner().invoke(cli.app, ["llm-setup"])

    contents = (tmp_path / ".env").read_text(encoding="utf-8")
    assert result.exit_code == 0
    assert verification_calls == [("openrouter", "interactive-router-key")]
    assert "interactive-router-key" not in result.output
    assert "OPENROUTER_MODEL=vendor/custom-structured-model" in contents
    assert "key saved without live verification" not in result.output


def test_noninteractive_run_names_the_missing_setting_and_setup_command_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scripts and CI fail closed rather than waiting for a secret prompt or reading an ambient profile."""
    clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        typer,
        "prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    result = CliRunner().invoke(cli.app, ["run"])

    assert result.exit_code == 1
    assert "LLM_PROFILE" in result.stderr
    assert "enterprise-agent llm-setup" in result.stderr


def test_failed_explicit_verification_does_not_write_or_echo_the_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed verification refuses persistence and keeps the hidden key out of all terminal output."""
    clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    prompts = iter(("claude", "rejected-claude-key"))
    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(typer, "prompt", lambda *_args, **_kwargs: next(prompts))
    monkeypatch.setattr(typer, "confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        cli, "discover_compatible_models", lambda profile, _key: curated_models_for(profile)
    )
    monkeypatch.setattr(cli, "verify_credential", lambda *_args, **_kwargs: False)

    result = CliRunner().invoke(cli.app, ["llm-setup"])

    assert result.exit_code == 1
    assert "credential verification failed" in result.stderr
    assert "rejected-claude-key" not in result.output
    assert not (tmp_path / ".env").exists()


def test_config_check_loads_an_isolated_local_env_file_without_displaying_its_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CLI startup reads the local profile file for noninteractive configuration commands safely."""
    clear_llm_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://agent:agent@localhost:5432/agent\n"
        "LLM_PROFILE=openrouter\n"
        "OPENROUTER_API_KEY=local-router-key\n"
        "OPENROUTER_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli.app, ["config-check"])

    assert result.exit_code == 0
    assert "profile: openrouter" in result.stdout
    assert "local-router-key" not in result.stdout
