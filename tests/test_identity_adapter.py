"""Unit contracts for the seeded PostgreSQL identity adapter."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from enterprise_agent.domain import UserId
from enterprise_agent.ports import IdentityPort

pytestmark = pytest.mark.unit


def configured_engine(rows: list[dict[str, object]]) -> MagicMock:
    """Return an engine double that exposes deterministic SQL result mappings."""
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.all.return_value = rows
    return engine


def test_identity_adapter_builds_dana_context_from_one_joined_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purchasing actor receives their role, read/write scopes, plant, backup, and limit."""
    from enterprise_agent.adapters import identity

    engine = configured_engine(
        [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "role": "purchasing_manager",
                "backup_approver_id": "00000000-0000-0000-0000-000000000002",
                "approval_limit_amount": Decimal("10000.00"),
                "approval_limit_currency": "usd",
                "scope": "erp:po:read",
                "plant_id": "PLANT-CHI",
            },
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "role": "purchasing_manager",
                "backup_approver_id": "00000000-0000-0000-0000-000000000002",
                "approval_limit_amount": Decimal("10000.00"),
                "approval_limit_currency": "usd",
                "scope": "erp:po:reroute",
                "plant_id": "PLANT-CHI",
            },
        ]
    )
    monkeypatch.setattr(identity, "create_engine", lambda _: engine)

    adapter = identity.PostgresIdentityAdapter("postgresql+psycopg://ignored")
    actor = adapter.actor_for(UserId("00000000-0000-0000-0000-000000000001"))

    assert isinstance(adapter, IdentityPort)
    assert actor.user_id == UserId("00000000-0000-0000-0000-000000000001")
    assert actor.role == "purchasing_manager"
    assert actor.scopes == frozenset({"erp:po:read", "erp:po:reroute"})
    assert actor.plant_ids == frozenset({"PLANT-CHI"})
    assert actor.backup_approver_id == UserId("00000000-0000-0000-0000-000000000002")
    assert actor.approval_limit_for("USD") == Decimal("10000.00")
    assert engine.connect.call_count == 1
    assert engine.connect.return_value.__enter__.return_value.execute.call_count == 1


def test_identity_adapter_keeps_quality_actor_separate_and_rejects_unknown_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quality actor gets only its own scope facts, while missing IDs fail closed."""
    from enterprise_agent.adapters import identity

    engine = configured_engine(
        [
            {
                "id": "00000000-0000-0000-0000-000000000003",
                "role": "quality_manager",
                "backup_approver_id": None,
                "approval_limit_amount": Decimal("5000.00"),
                "approval_limit_currency": "USD",
                "scope": "quality:lot:read",
                "plant_id": "PLANT-CHI",
            }
        ]
    )
    monkeypatch.setattr(identity, "create_engine", lambda _: engine)
    adapter = identity.PostgresIdentityAdapter("postgresql+psycopg://ignored")

    quality_actor = adapter.actor_for(UserId("00000000-0000-0000-0000-000000000003"))
    engine.connect.return_value.__enter__.return_value.execute.return_value.mappings.return_value.all.return_value = []

    assert quality_actor.scopes == frozenset({"quality:lot:read"})
    assert "erp:po:reroute" not in quality_actor.scopes
    with pytest.raises(identity.IdentityNotFoundError, match="unknown actor"):
        adapter.actor_for(UserId("00000000-0000-0000-0000-000000000099"))
