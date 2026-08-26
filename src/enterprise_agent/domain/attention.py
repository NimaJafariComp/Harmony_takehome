"""Scenario A attention-trigger identity and lifecycle policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Protocol

from enterprise_agent.domain.contracts import AttentionItem, AttentionStatus


class InvalidAttentionTransitionError(ValueError):
    """Raised when a caller attempts to move an attention item backward or after completion."""


class AttentionTrigger(Protocol):
    """Scenario-neutral immutable signal accepted by the shared attention control plane."""

    @property
    def detector(self) -> str:
        """Return the versioned detector identity."""
        ...

    @property
    def detected_at(self) -> datetime:
        """Return the timezone-aware business instant of the detector observation."""
        ...

    @property
    def source_versions(self) -> Mapping[str, int]:
        """Return every material version bound to the detector observation."""
        ...

    @property
    def scenario(self) -> str:
        """Return the scenario namespace persisted with the durable attention item."""
        ...

    @property
    def cause(self) -> str:
        """Return the stable detector cause persisted with the durable attention item."""
        ...

    @property
    def dedupe_key(self) -> str:
        """Return the canonical key used for idempotent attention registration."""
        ...

    @property
    def audit_payload(self) -> Mapping[str, object]:
        """Return safe scenario-specific fields for the generic detection audit event."""
        ...


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

    @property
    def scenario(self) -> str:
        """Identify the bounded Scenario A attention namespace."""
        return "scenario_a"

    @property
    def cause(self) -> str:
        """Identify the durable business cause created by this trigger."""
        return "projected_stockout"

    @property
    def audit_payload(self) -> Mapping[str, object]:
        """Expose safe stockout facts without requiring adapter-level scenario branches."""
        return {
            "detector": self.detector,
            "part_id": self.part_id,
            "production_order_id": self.production_order_id,
            "inventory_version": self.inventory_version,
            "production_start_date": self.production_start_date.isoformat(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ScenarioBQualityHoldTrigger:
    """A versioned detector signal for a held lot allocated to imminent production demand."""

    detector: str
    part_id: str
    quality_lot_id: str
    quality_lot_version: int
    production_allocation_id: str
    production_allocation_version: int
    production_order_id: str
    production_order_version: int
    production_start_date: date
    detected_at: datetime
    source_versions: Mapping[str, int]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("detector", self.detector),
            ("part ID", self.part_id),
            ("quality-lot ID", self.quality_lot_id),
            ("production-allocation ID", self.production_allocation_id),
            ("production-order ID", self.production_order_id),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if (
            min(
                self.quality_lot_version,
                self.production_allocation_version,
                self.production_order_version,
            )
            < 1
        ):
            raise ValueError("quality-hold source versions must be positive")
        if self.detected_at.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware")

        versions = {str(source): int(version) for source, version in self.source_versions.items()}
        if any(not source or version < 1 for source, version in versions.items()):
            raise ValueError("source versions must use non-empty names and positive values")
        object.__setattr__(self, "source_versions", MappingProxyType(versions))

    @property
    def scenario(self) -> str:
        """Identify the bounded Scenario B attention namespace."""
        return "scenario_b"

    @property
    def cause(self) -> str:
        """Identify the durable business cause created by this trigger."""
        return "quality_hold"

    @property
    def dedupe_key(self) -> str:
        """Return a key that changes whenever a material held-allocation fact changes."""
        canonical_signal = json.dumps(
            {
                "detector": self.detector,
                "part_id": self.part_id,
                "quality_lot_id": self.quality_lot_id,
                "quality_lot_version": self.quality_lot_version,
                "production_allocation_id": self.production_allocation_id,
                "production_allocation_version": self.production_allocation_version,
                "production_order_id": self.production_order_id,
                "production_order_version": self.production_order_version,
                "production_start_date": self.production_start_date.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        signal_hash = hashlib.sha256(canonical_signal.encode("utf-8")).hexdigest()
        return f"scenario_b:quality_hold:v1:{signal_hash}"

    @property
    def audit_payload(self) -> Mapping[str, object]:
        """Expose safe held-lot facts without requiring adapter-level scenario branches."""
        return {
            "detector": self.detector,
            "part_id": self.part_id,
            "quality_lot_id": self.quality_lot_id,
            "quality_lot_version": self.quality_lot_version,
            "production_allocation_id": self.production_allocation_id,
            "production_allocation_version": self.production_allocation_version,
            "production_order_id": self.production_order_id,
            "production_order_version": self.production_order_version,
            "production_start_date": self.production_start_date.isoformat(),
        }


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
