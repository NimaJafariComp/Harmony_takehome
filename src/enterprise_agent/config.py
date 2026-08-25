"""Secret-safe runtime configuration."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from os import environ

SUPPORTED_LLM_PROFILES = frozenset({"anthropic", "openai", "openrouter"})


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
    profile = _required(source, "LLM_PROFILE").lower()

    if profile not in SUPPORTED_LLM_PROFILES:
        choices = ", ".join(sorted(SUPPORTED_LLM_PROFILES))
        raise ConfigurationError(f"LLM_PROFILE must be one of: {choices}")

    prefix = profile.upper()
    return RuntimeConfiguration(
        database_url=_required(source, "DATABASE_URL"),
        provider=ProviderConfiguration(
            profile=profile,
            model=_required(source, f"{prefix}_MODEL"),
            api_key=_required(source, f"{prefix}_API_KEY"),
        ),
    )


def _required(environment: Mapping[str, str], name: str) -> str:
    """Return a non-empty setting while naming only the missing variable."""
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required configuration: {name}")
    return value
