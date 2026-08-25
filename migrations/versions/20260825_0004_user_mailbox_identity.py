"""Add the stable seeded mailbox identity used for mail authorization.

Revision ID: 20260825_0004
Revises: 20260825_0003
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260825_0004"
down_revision = "20260825_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a nullable mailbox key without invalidating already seeded local data."""
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=True))
    op.create_unique_constraint("uq_users_email", "users", ["email"])


def downgrade() -> None:
    """Remove the mailbox identity additions in reverse order."""
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.drop_column("users", "email")
