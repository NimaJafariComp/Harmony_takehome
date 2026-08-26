"""Narrow authorization lookup used by the optional local review read service."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from enterprise_agent.domain import AttentionId, UserId

_CAN_VIEW_ATTENTION = text("""
    SELECT EXISTS (
        SELECT 1
        FROM plans
        JOIN approvals ON approvals.plan_id = plans.id
        WHERE plans.attention_id = CAST(:attention_id AS UUID)
          AND (
              plans.actor_id = CAST(:actor_id AS UUID)
              OR plans.approver_id = CAST(:actor_id AS UUID)
              OR approvals.requester_id = CAST(:actor_id AS UUID)
              OR approvals.approver_id = CAST(:actor_id AS UUID)
          )
    )
""")


class PostgresLocalReviewAccessAdapter:
    """Answer one actor-scoped attention access question without returning plan or evidence data."""

    def __init__(self, database_url: str) -> None:
        """Connect the access projection to the same local durable control plane as its readers."""
        self._engine: Engine = create_engine(database_url)

    def can_view_attention(self, actor_id: UserId, attention_id: AttentionId) -> bool:
        """Authorize only participants of a plan bound to the requested attention item."""
        with self._engine.connect() as connection:
            return bool(
                connection.execute(
                    _CAN_VIEW_ATTENTION,
                    {"actor_id": str(actor_id), "attention_id": str(attention_id)},
                ).scalar_one()
            )
