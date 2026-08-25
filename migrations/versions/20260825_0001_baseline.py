"""Create the empty baseline for the enterprise-agent schema.

Revision ID: 20260825_0001
Revises:
Create Date: 2026-08-25
"""

# revision identifiers, used by Alembic.
revision = "20260825_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record the initial migration state; M2 adds the first tables."""


def downgrade() -> None:
    """Reverse the empty baseline migration."""
