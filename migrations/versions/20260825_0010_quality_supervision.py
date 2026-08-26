"""Bind production orders to their responsible supervisor.

Revision ID: 20260825_0010
Revises: 20260825_0009
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260825_0010"
down_revision = "20260825_0009"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    """Add an optional owner so quality actions target the affected production supervisor."""
    op.add_column(
        "production_orders",
        sa.Column(
            "supervisor_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_production_orders_supervisor_id",
        "production_orders",
        ["supervisor_id"],
    )


def downgrade() -> None:
    """Remove the optional supervision binding in reverse dependency order."""
    op.drop_index("ix_production_orders_supervisor_id", table_name="production_orders")
    op.drop_column("production_orders", "supervisor_id")
