"""Runtime configuration contracts."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from enterprise_agent import cli
from enterprise_agent.config import ConfigurationError, load_settings


def configured_openai_environment() -> dict[str, str]:
    """Return a complete non-secret environment for the OpenAI profile."""
    return {
        "DATABASE_URL": "postgresql+psycopg://agent:agent@localhost:5432/agent",
        "LLM_PROFILE": "openai",
        "OPENAI_API_KEY": "test-openai-secret",
        "OPENAI_MODEL": "test-model",
    }


def test_missing_selected_provider_key_is_identified_without_exposing_a_value() -> None:
    """The loader identifies a missing selected-provider key safely."""
    environment = configured_openai_environment()
    del environment["OPENAI_API_KEY"]

    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY") as error:
        load_settings(environment)

    assert "test-openai-secret" not in str(error.value)


def test_config_check_reports_safe_selected_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI validates configuration without exposing credentials."""
    for name, value in configured_openai_environment().items():
        monkeypatch.setenv(name, value)

    result = CliRunner().invoke(cli.app, ["config-check"])

    assert result.exit_code == 0
    assert "profile: openai" in result.stdout
    assert "test-openai-secret" not in result.stdout


def test_env_example_documents_all_runtime_variables() -> None:
    """Every supported provider is documented without real credentials."""
    contents = Path(".env.example").read_text(encoding="utf-8")

    required_names = {
        "DATABASE_URL",
        "LLM_PROFILE",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
    }

    assert all(f"{name}=" in contents for name in required_names)
