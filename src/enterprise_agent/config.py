"""Secret-safe runtime configuration."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from os import environ
from typing import Final

SUPPORTED_LLM_PROFILES = frozenset({"claude", "openai", "openrouter"})
PROFILE_ENVIRONMENT_PREFIXES: Final = {
    "claude": "ANTHROPIC",
    "openai": "OPENAI",
    "openrouter": "OPENROUTER",
}
_PROFILE_ALIASES: Final = {"anthropic": "claude"}


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class ProviderConfiguration:
    """Validated configuration for the selected LLM provider."""

    profile: str
    model: str
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class RuntimeConfiguration:
    """Validated runtime configuration required by application commands."""

    database_url: str = field(repr=False)
    provider: ProviderConfiguration

    def safe_summary(self) -> str:
        """Return operator-facing configuration status without credentials."""
        return "\n".join(
            (
                "configuration: valid",
                f"profile: {self.provider.profile}",
                f"model: {self.provider.model}",
                "database: configured",
            )
        )


def load_settings(environment: Mapping[str, str] | None = None) -> RuntimeConfiguration:
    """Validate configuration from an environment mapping without logging values."""
    source = environ if environment is None else environment
    provider = load_provider_settings(source)
    return RuntimeConfiguration(
        database_url=_required(source, "DATABASE_URL"),
        provider=provider,
    )


def load_provider_settings(environment: Mapping[str, str] | None = None) -> ProviderConfiguration:
    """Validate only the selected LLM profile, model, and key for setup and run bootstrap paths."""
    source = environ if environment is None else environment
    profile = normalize_llm_profile(_required(source, "LLM_PROFILE"))
    prefix = PROFILE_ENVIRONMENT_PREFIXES[profile]
    return ProviderConfiguration(
        profile=profile,
        model=_required(source, f"{prefix}_MODEL"),
        api_key=_required(source, f"{prefix}_API_KEY"),
    )


def normalize_llm_profile(value: str) -> str:
    """Return the canonical user-facing profile while accepting the prior Anthropic configuration alias."""
    profile = value.strip().lower()
    profile = _PROFILE_ALIASES.get(profile, profile)
    if profile not in SUPPORTED_LLM_PROFILES:
        choices = ", ".join(sorted(SUPPORTED_LLM_PROFILES))
        raise ConfigurationError(f"LLM_PROFILE must be one of: {choices}")
    return profile


def _required(environment: Mapping[str, str], name: str) -> str:
    """Return a non-empty setting while naming only the missing variable."""
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required configuration: {name}")
    return value
