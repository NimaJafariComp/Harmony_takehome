"""Add complete durable workflow-state and lease invariants.

Revision ID: 20260825_0006
Revises: 20260825_0005
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260825_0006"
down_revision = "20260825_0005"
branch_labels = None
depends_on = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Add lease ownership plus one-plan/valid-counter workflow-state guarantees."""
    for table_name in ("workflow_instances", "workflow_steps"):
        op.add_column(table_name, sa.Column("lease_owner", sa.String(length=100), nullable=True))
        op.add_column(table_name, sa.Column("lease_expires_at", TIMESTAMPTZ, nullable=True))
        op.create_check_constraint(
            f"ck_{table_name}_lease_pair",
            table_name,
            "(lease_owner IS NULL) = (lease_expires_at IS NULL)",
        )

    op.create_unique_constraint("uq_workflow_instances_plan_id", "workflow_instances", ["plan_id"])
    op.create_check_constraint(
        "ck_workflow_instances_current_step_non_negative",
        "workflow_instances",
        "current_step >= 0",
    )
    op.create_check_constraint(
        "ck_workflow_instances_definition_version_positive",
        "workflow_instances",
        "definition_version > 0",
    )
    op.create_check_constraint(
        "ck_workflow_steps_step_index_positive",
        "workflow_steps",
        "step_index > 0",
    )
    op.create_check_constraint(
        "ck_workflow_steps_attempt_count_non_negative",
        "workflow_steps",
        "attempt_count >= 0",
    )
    op.create_index(
        "ix_workflow_instances_status_lease_expires_at",
        "workflow_instances",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    """Remove durable workflow-state additions in reverse dependency order."""
    op.drop_index("ix_workflow_instances_status_lease_expires_at", table_name="workflow_instances")
    op.drop_constraint(
        "ck_workflow_steps_attempt_count_non_negative", "workflow_steps", type_="check"
    )
    op.drop_constraint("ck_workflow_steps_step_index_positive", "workflow_steps", type_="check")
    op.drop_constraint(
        "ck_workflow_instances_definition_version_positive", "workflow_instances", type_="check"
    )
    op.drop_constraint(
        "ck_workflow_instances_current_step_non_negative", "workflow_instances", type_="check"
    )
    op.drop_constraint("uq_workflow_instances_plan_id", "workflow_instances", type_="unique")

    for table_name in ("workflow_steps", "workflow_instances"):
        op.drop_constraint(f"ck_{table_name}_lease_pair", table_name, type_="check")
        op.drop_column(table_name, "lease_expires_at")
        op.drop_column(table_name, "lease_owner")
