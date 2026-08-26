"""Persist idempotent external-style tool effects independently from workflow transitions.

Revision ID: 20260825_0007
Revises: 20260825_0006
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260825_0007"
down_revision = "20260825_0006"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the independent idempotency journal owned by the simulated external tool system."""
    op.create_table(
        "tool_invocations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "workflow_instance_id",
            UUID,
            sa.ForeignKey("workflow_instances.id"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("parameters", JSONB, nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", TIMESTAMPTZ, nullable=False),
        sa.Column("completed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_tool_invocations_idempotency_key"),
        sa.CheckConstraint(
            "attempt_count > 0",
            name="ck_tool_invocations_attempt_count_positive",
        ),
    )
    op.create_index(
        "ix_tool_invocations_workflow_instance_id_status",
        "tool_invocations",
        ["workflow_instance_id", "status"],
    )


def downgrade() -> None:
    """Remove the external-style invocation journal after its consumers are removed."""
    op.drop_index(
        "ix_tool_invocations_workflow_instance_id_status",
        table_name="tool_invocations",
    )
    op.drop_table("tool_invocations")
