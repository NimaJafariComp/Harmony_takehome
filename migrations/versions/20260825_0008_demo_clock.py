"""Persist the deterministic mutable clock used by the local demo.

Revision ID: 20260825_0008
Revises: 20260825_0007
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260825_0008"
down_revision = "20260825_0007"
branch_labels = None
depends_on = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the one-row mutable clock owned by the local deterministic demo."""
    op.create_table(
        "demo_clock",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("current_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint("id = 1", name="ck_demo_clock_singleton"),
    )


def downgrade() -> None:
    """Remove the local demo-clock state."""
    op.drop_table("demo_clock")
