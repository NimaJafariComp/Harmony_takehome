"""Sanitized planner provenance shared by reviewer-facing CLI, JSON, and UI surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PlannerMode(StrEnum):
    """Name whether a recommendation came from a deterministic fake or a selected live provider."""

    FAKE_DETERMINISTIC = "fake_deterministic"
    LIVE = "live"

    @property
    def label(self) -> str:
        """Return the explicit human label that must not depend on terminal or UI color."""
        if self is PlannerMode.FAKE_DETERMINISTIC:
            return "FAKE / DETERMINISTIC"
        return "LIVE"


class SchemaValidation(StrEnum):
    """Describe whether the selected recommendation was validated against an owned schema."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"

    @property
    def label(self) -> str:
        """Return concise reviewer text for the safe schema-validation fact."""
        return {
            SchemaValidation.PASSED: "Passed",
            SchemaValidation.FAILED: "Failed",
            SchemaValidation.NOT_RUN: "Not run",
        }[self]


class GateStatus(StrEnum):
    """Describe the deterministic control-plane gate status without exposing any underlying payload."""

    PENDING_APPROVAL = "pending_approval"
    NOT_INVOKED_FIXTURE = "not_invoked_fixture"
    NOT_INVOKED_NO_WRITE_EVALUATION = "not_invoked_no_write_evaluation"

    @property
    def label(self) -> str:
        """Return a specific, human-readable gate boundary."""
        return {
            GateStatus.PENDING_APPROVAL: "Passed to pending approval",
            GateStatus.NOT_INVOKED_FIXTURE: "Not invoked (fixture walkthrough)",
            GateStatus.NOT_INVOKED_NO_WRITE_EVALUATION: "Not invoked (no-write evaluation)",
        }[self]


@dataclass(frozen=True, slots=True)
class PlannerProvenance:
    """Only the selected planner identity and deterministic-boundary facts safe for reviewer display."""

    mode: PlannerMode
    provider: str | None
    profile: str | None
    model: str
    schema_validation: SchemaValidation
    gate_status: GateStatus

    def __post_init__(self) -> None:
        """Keep identity labels scalar, nonempty, and impossible to confuse with a secret-bearing config."""
        model = self.model.strip()
        if not model:
            raise ValueError("planner provenance requires a model label")
        object.__setattr__(self, "model", model)
        for field_name in ("provider", "profile"):
            value = getattr(self, field_name)
            if value is not None:
                normalized = value.strip()
                if not normalized:
                    raise ValueError(f"planner provenance {field_name} cannot be blank")
                object.__setattr__(self, field_name, normalized)
        if self.mode is PlannerMode.LIVE and (self.provider is None or self.profile is None):
            raise ValueError("live planner provenance requires provider and profile")
        if self.mode is PlannerMode.FAKE_DETERMINISTIC and (
            self.provider is not None or self.profile is not None
        ):
            raise ValueError("fake planner provenance cannot name a live provider or profile")

    @property
    def mode_label(self) -> str:
        """Return the visible planner mode label."""
        return self.mode.label

    @property
    def provider_label(self) -> str:
        """Return a clear non-live placeholder rather than an empty presentation cell."""
        return self.provider or "none"

    @property
    def profile_label(self) -> str:
        """Return a clear non-live placeholder rather than an empty presentation cell."""
        return self.profile or "none"

    @property
    def schema_validation_label(self) -> str:
        """Return the human-safe schema-validation label."""
        return self.schema_validation.label

    @property
    def gate_label(self) -> str:
        """Return the human-safe deterministic-gate label."""
        return self.gate_status.label

    def to_data(self) -> dict[str, str | None]:
        """Return a stable JSON-safe projection with no credential, prompt, payload, or output field."""
        return {
            "mode": self.mode.value,
            "label": self.mode_label,
            "provider": self.provider,
            "profile": self.profile,
            "model": self.model,
            "schema_validation": self.schema_validation.value,
            "schema_validation_label": self.schema_validation_label,
            "gate_status": self.gate_status.value,
            "gate_label": self.gate_label,
        }
