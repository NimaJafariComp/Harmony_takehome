"""Secret-safe local LLM profile setup, environment loading, and no-generation verification."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import MappingProxyType
from typing import Final
from urllib.request import Request, urlopen

from enterprise_agent.config import PROFILE_ENVIRONMENT_PREFIXES, normalize_llm_profile

_ENV_ASSIGNMENT = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
_ANTHROPIC_VERSION: Final = "2023-06-01"
_VERIFICATION_ENDPOINTS: Final = {
    "openai": "https://api.openai.com/v1/models",
    "claude": "https://api.anthropic.com/v1/models?limit=1",
    "openrouter": "https://openrouter.ai/api/v1/auth/key",
}
_MODEL_DISCOVERY_ENDPOINTS: Final = {
    "openai": "https://api.openai.com/v1/models",
    "claude": "https://api.anthropic.com/v1/models?limit=1000",
    "openrouter": "https://openrouter.ai/api/v1/models",
}


@dataclass(frozen=True, slots=True)
class CuratedModel:
    """One model explicitly reviewed against this project's structured-output adapter contract."""

    model_id: str
    label: str
    recommended: bool = False


class ModelDiscoveryError(ValueError):
    """Raised when a provider model list cannot safely yield an adapter-reviewed suggestion."""


CURATED_MODEL_CATALOG: Mapping[str, tuple[CuratedModel, ...]] = MappingProxyType(
    {
        "openai": (
            CuratedModel(
                model_id="gpt-5.6-luna",
                label="GPT-5.6 Luna — cost-efficient",
                recommended=True,
            ),
            CuratedModel(
                model_id="gpt-5.6-terra",
                label="GPT-5.6 Terra — more capable",
            ),
        ),
        "claude": (
            CuratedModel(
                model_id="claude-sonnet-5",
                label="Claude Sonnet 5 — balanced",
                recommended=True,
            ),
        ),
        "openrouter": (
            CuratedModel(
                model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
                label="NVIDIA Nemotron 3 Ultra — free",
                recommended=True,
            ),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class LLMSetupSelection:
    """One unlogged provider/profile selection collected from an interactive local terminal."""

    profile: str
    api_key: str = field(repr=False)
    model: str


def curated_models_for(profile: str) -> tuple[CuratedModel, ...]:
    """Return only the small adapter-reviewed catalog for one supported profile."""
    normalized_profile = _profile_or_value_error(profile)
    return CURATED_MODEL_CATALOG[normalized_profile]


def discover_compatible_models(
    profile: str,
    api_key: str,
    *,
    timeout_seconds: float = 5.0,
) -> tuple[CuratedModel, ...]:
    """Return only account-visible curated models whose adapter contract is explicitly reviewed.

    The provider's key-scoped model list is used only in memory to prove account visibility.  The
    application never promotes arbitrary provider-list entries into recommendations.
    """
    normalized_profile = _profile_or_value_error(profile)
    credential = _safe_value(api_key, name="API key")
    if timeout_seconds <= 0:
        raise ValueError("model discovery timeout must be positive")

    request = Request(
        _MODEL_DISCOVERY_ENDPOINTS[normalized_profile],
        headers=_provider_headers(normalized_profile, credential),
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise ModelDiscoveryError("could not retrieve models for the selected provider") from error

    entries = _model_entries(payload)
    compatible = tuple(
        model
        for model in curated_models_for(normalized_profile)
        if _model_is_available_and_compatible(normalized_profile, model.model_id, entries)
    )
    if not compatible:
        raise ModelDiscoveryError("no adapter-reviewed models are available to this API key")
    return _with_available_recommendation(compatible)


def default_env_path() -> Path:
    """Return the only local profile file used by the CLI from its current working directory."""
    return Path.cwd() / ".env"


def load_local_environment(
    env_path: Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge literal `.env` assignments with explicit process settings without evaluating file content."""
    values = _read_env_file(env_path)
    values.update(os.environ if environment is None else environment)
    return values


def save_llm_profile(env_path: Path, selection: LLMSetupSelection) -> None:
    """Atomically merge one selected profile into local `.env` while preserving every other entry."""
    profile = _profile_or_value_error(selection.profile)
    api_key = _safe_value(selection.api_key, name="API key")
    model = _safe_value(selection.model, name="model")
    prefix = PROFILE_ENVIRONMENT_PREFIXES[profile]
    replacement_values = {
        "LLM_PROFILE": profile,
        f"{prefix}_API_KEY": api_key,
        f"{prefix}_MODEL": model,
    }
    existing_lines = _read_env_lines(env_path)
    replacement_lines = _merge_assignment_lines(existing_lines, replacement_values)
    _atomic_owner_only_write(env_path, "".join(replacement_lines))


def verify_credential(profile: str, api_key: str, *, timeout_seconds: float = 5.0) -> bool:
    """Make one optional metadata-only provider request and retain neither a response nor error detail."""
    normalized_profile = _profile_or_value_error(profile)
    credential = _safe_value(api_key, name="API key")
    if timeout_seconds <= 0:
        raise ValueError("verification timeout must be positive")

    headers = _provider_headers(normalized_profile, credential)
    request = Request(_VERIFICATION_ENDPOINTS[normalized_profile], headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds):
            return True
    except (OSError, TimeoutError, ValueError):
        return False


def _provider_headers(profile: str, credential: str) -> dict[str, str]:
    """Return selected-provider metadata headers without retaining the supplied credential elsewhere."""
    headers = {"Content-Type": "application/json"}
    if profile == "claude":
        headers.update({"Anthropic-Version": _ANTHROPIC_VERSION, "X-Api-Key": credential})
    else:
        headers["Authorization"] = f"Bearer {credential}"
    return headers


def _model_entries(payload: object) -> tuple[Mapping[str, object], ...]:
    """Extract the one trusted response shape while retaining no provider response outside this call."""
    if not isinstance(payload, Mapping):
        raise ModelDiscoveryError("could not retrieve models for the selected provider")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ModelDiscoveryError("could not retrieve models for the selected provider")
    entries: list[Mapping[str, object]] = []
    for value in data:
        if not isinstance(value, Mapping):
            raise ModelDiscoveryError("could not retrieve models for the selected provider")
        entries.append(value)
    return tuple(entries)


def _model_is_available_and_compatible(
    profile: str,
    model_id: str,
    entries: tuple[Mapping[str, object], ...],
) -> bool:
    """Require an exact provider-listed ID and Claude's declared structured-output capability."""
    for entry in entries:
        if entry.get("id") != model_id:
            continue
        if profile != "claude":
            return True
        capabilities = entry.get("capabilities")
        if not isinstance(capabilities, Mapping):
            return False
        structured_outputs = capabilities.get("structured_outputs")
        return (
            isinstance(structured_outputs, Mapping) and structured_outputs.get("supported") is True
        )
    return False


def _with_available_recommendation(
    models: tuple[CuratedModel, ...],
) -> tuple[CuratedModel, ...]:
    """Keep the curated default when visible, otherwise recommend the first safe visible option."""
    if any(model.recommended for model in models):
        return models
    return (replace(models[0], recommended=True), *models[1:])


def _read_env_file(env_path: Path) -> dict[str, str]:
    """Read a small dotenv-compatible subset as literal values and reject non-file profile paths."""
    values: dict[str, str] = {}
    for line in _read_env_lines(env_path):
        assignment = _assignment_from_line(line)
        if assignment is not None:
            name, value = assignment
            values[name] = value
    return values


def _read_env_lines(env_path: Path) -> list[str]:
    """Return local dotenv lines unchanged so unselected profiles and comments survive a later merge."""
    if not env_path.exists():
        return []
    if not env_path.is_file():
        raise ValueError(f"local environment path is not a file: {env_path.name}")
    return env_path.read_text(encoding="utf-8").splitlines(keepends=True)


def _assignment_from_line(line: str) -> tuple[str, str] | None:
    """Parse a literal assignment only; comments, blank lines, and shell syntax remain inert text."""
    candidate = line.strip()
    if not candidate or candidate.startswith("#"):
        return None
    match = _ENV_ASSIGNMENT.fullmatch(candidate)
    if match is None:
        return None
    value = match.group("value").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return (match.group("name"), value)


def _merge_assignment_lines(
    existing_lines: list[str],
    replacement_values: Mapping[str, str],
) -> list[str]:
    """Replace only selected settings once, preserve all other lines, and remove selected duplicates."""
    output: list[str] = []
    replaced: set[str] = set()
    for line in existing_lines:
        assignment = _assignment_from_line(line)
        if assignment is None or assignment[0] not in replacement_values:
            output.append(line)
            continue
        name = assignment[0]
        if name not in replaced:
            output.append(f"{name}={replacement_values[name]}\n")
            replaced.add(name)

    if output and not output[-1].endswith("\n"):
        output[-1] = f"{output[-1]}\n"
    for name, value in replacement_values.items():
        if name not in replaced:
            output.append(f"{name}={value}\n")
    return output


def _atomic_owner_only_write(env_path: Path, contents: str) -> None:
    """Write a same-directory replacement whose temporary and final paths are both owner-only."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=env_path.parent,
            prefix=f".{env_path.name}.",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            os.fchmod(temporary_file.fileno(), 0o600)
            temporary_file.write(contents)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, env_path)
        os.chmod(env_path, 0o600)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _profile_or_value_error(profile: str) -> str:
    """Normalize the configured profile while keeping setup-facing validation independent of CLI rendering."""
    try:
        return normalize_llm_profile(profile)
    except ValueError as error:
        raise ValueError("unsupported LLM profile") from error


def _safe_value(value: str, *, name: str) -> str:
    """Refuse empty or line-breaking terminal input before it can become a dotenv assignment."""
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    if "\n" in normalized or "\r" in normalized or "\x00" in normalized:
        raise ValueError(f"{name} must not contain line breaks or NUL")
    return normalized
