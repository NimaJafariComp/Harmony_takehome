"""Secret-safe local LLM profile setup and local environment loading contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from urllib.request import Request

import pytest

from enterprise_agent.config import ConfigurationError, load_settings

pytestmark = pytest.mark.unit


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
