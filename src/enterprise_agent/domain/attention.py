"""Scenario A attention-trigger identity and lifecycle policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from enterprise_agent.domain.contracts import AttentionItem, AttentionStatus


class InvalidAttentionTransitionError(ValueError):
    """Raised when a caller attempts to move an attention item backward or after completion."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ScenarioAStockoutTrigger:
    """A versioned detector signal that identifies one Scenario A stockout risk."""

    detector: str
    part_id: str
    production_order_id: str
    inventory_version: int
    production_start_date: date
    detected_at: datetime
    source_versions: Mapping[str, int]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("detector", self.detector),
            ("part ID", self.part_id),
            ("production order ID", self.production_order_id),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.inventory_version < 1:
            raise ValueError("inventory version must be positive")
        if self.detected_at.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware")

        versions = {str(source): int(version) for source, version in self.source_versions.items()}
        if any(not source or version < 1 for source, version in versions.items()):
            raise ValueError("source versions must use non-empty names and positive values")
        object.__setattr__(self, "source_versions", MappingProxyType(versions))

    @property
    def dedupe_key(self) -> str:
        """Return a canonical key that changes with every material stockout-risk input."""
        canonical_signal = json.dumps(
            {
                "detector": self.detector,
                "inventory_version": self.inventory_version,
                "part_id": self.part_id,
                "production_order_id": self.production_order_id,
                "production_start_date": self.production_start_date.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        signal_hash = hashlib.sha256(canonical_signal.encode("utf-8")).hexdigest()
        return f"scenario_a:stockout:v1:{signal_hash}"


@dataclass(frozen=True, slots=True, kw_only=True)
class AttentionRegistration:
    """The durable result of creating or deduplicating one detector signal."""

    attention: AttentionItem
    created: bool


_ALLOWED_TRANSITIONS = {
    AttentionStatus.OPEN: frozenset(
        {AttentionStatus.PENDING_APPROVAL, AttentionStatus.RESOLVED, AttentionStatus.CANCELLED}
    ),
    AttentionStatus.PENDING_APPROVAL: frozenset(
        {AttentionStatus.IN_PROGRESS, AttentionStatus.RESOLVED, AttentionStatus.CANCELLED}
    ),
    AttentionStatus.IN_PROGRESS: frozenset({AttentionStatus.RESOLVED, AttentionStatus.CANCELLED}),
    AttentionStatus.RESOLVED: frozenset(),
    AttentionStatus.CANCELLED: frozenset(),
}


def require_attention_transition(current: AttentionStatus, target: AttentionStatus) -> None:
    """Reject lifecycle reversals and transitions out of terminal attention states."""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidAttentionTransitionError(
            f"attention transition from {current.value} to {target.value} is not allowed"
        )
