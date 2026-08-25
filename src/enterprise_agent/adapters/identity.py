"""Read-only PostgreSQL identity adapter for the seeded company model."""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from enterprise_agent.domain import ActorContext, PlantId, Scope, UserId


class IdentityNotFoundError(LookupError):
    """Raised when no seeded identity is available for the requested actor ID."""


ACTOR_CONTEXT_QUERY = text("""
SELECT
    users.id,
    users.role,
    users.backup_approver_id,
    users.approval_limit_amount,
    users.approval_limit_currency,
    user_scopes.scope,
    user_scopes.plant_id
FROM users
LEFT JOIN user_scopes ON user_scopes.user_id = users.id
WHERE users.id = :user_id
ORDER BY user_scopes.scope
""")


class PostgresIdentityAdapter:
    """Resolve one immutable actor context with a single scoped identity query."""

    def __init__(self, database_url: str) -> None:
        """Connect the adapter to its configured PostgreSQL database."""
        self._engine: Engine = create_engine(database_url)

    def actor_for(self, user_id: UserId) -> ActorContext:
        """Load the actor's role, scopes, plants, backup approver, and approval limit."""
        with self._engine.connect() as connection:
            rows = (
                connection.execute(ACTOR_CONTEXT_QUERY, {"user_id": str(user_id)}).mappings().all()
            )

        if not rows:
            raise IdentityNotFoundError(f"unknown actor: {user_id}")

        first = rows[0]
        backup_approver_id = first["backup_approver_id"]
        scopes = frozenset(Scope(str(row["scope"])) for row in rows if row["scope"] is not None)
        plant_ids = frozenset(
            PlantId(str(row["plant_id"])) for row in rows if row["plant_id"] is not None
        )
        return ActorContext(
            user_id=UserId(str(first["id"])),
            role=cast(str, first["role"]),
            scopes=scopes,
            plant_ids=plant_ids,
            backup_approver_id=(
                None if backup_approver_id is None else UserId(str(backup_approver_id))
            ),
            approval_limits={
                cast(str, first["approval_limit_currency"]): cast(
                    Decimal,
                    first["approval_limit_amount"],
                )
            },
        )
