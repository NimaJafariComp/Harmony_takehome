"""Create the core durable domain schema.

Revision ID: 20260825_0002
Revises: 20260825_0001
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260825_0002"
down_revision = "20260825_0001"
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Create the normalized tables required by both required scenarios."""
    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=100), nullable=False),
        sa.Column("backup_approver_id", UUID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approval_limit_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("approval_limit_currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_users_backup_approver_id", "users", ["backup_approver_id"])

    op.create_table(
        "user_scopes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("scope", sa.String(length=150), nullable=False),
        sa.Column("plant_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_user_scopes_user_id", "user_scopes", ["user_id"])

    op.create_table(
        "parts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("part_number", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("plant_id", sa.String(length=100), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_parts_plant_id_part_number", "parts", ["plant_id", "part_number"])

    op.create_table(
        "suppliers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("supplier_code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("part_id", UUID, sa.ForeignKey("parts.id"), nullable=False),
        sa.Column("plant_id", sa.String(length=100), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_suppliers_part_id_plant_id", "suppliers", ["part_id", "plant_id"])

    op.create_table(
        "production_orders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("order_number", sa.String(length=100), nullable=False),
        sa.Column("part_id", UUID, sa.ForeignKey("parts.id"), nullable=False),
        sa.Column("plant_id", sa.String(length=100), nullable=False),
        sa.Column("required_quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index(
        "ix_production_orders_part_id_start_date", "production_orders", ["part_id", "start_date"]
    )

    op.create_table(
        "purchase_orders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("po_number", sa.String(length=100), nullable=False),
        sa.Column("part_id", UUID, sa.ForeignKey("parts.id"), nullable=False),
        sa.Column("supplier_id", UUID, sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("plant_id", sa.String(length=100), nullable=False),
        sa.Column("ordered_quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("received_quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("expected_receipt_date", sa.Date(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_purchase_orders_part_id_status", "purchase_orders", ["part_id", "status"])
    op.create_index("ix_purchase_orders_supplier_id", "purchase_orders", ["supplier_id"])

    op.create_table(
        "quality_lots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("lot_number", sa.String(length=100), nullable=False),
        sa.Column("part_id", UUID, sa.ForeignKey("parts.id"), nullable=False),
        sa.Column("plant_id", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column(
            "production_order_id",
            UUID,
            sa.ForeignKey("production_orders.id"),
            nullable=True,
        ),
        sa.Column("allocated_quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index(
        "ix_quality_lots_production_order_id_status",
        "quality_lots",
        ["production_order_id", "status"],
    )

    op.create_table(
        "messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("message_key", sa.String(length=200), nullable=False),
        sa.Column("purchase_order_id", UUID, sa.ForeignKey("purchase_orders.id"), nullable=True),
        sa.Column("supplier_id", UUID, sa.ForeignKey("suppliers.id"), nullable=True),
        sa.Column("sender", sa.String(length=320), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("received_at", TIMESTAMPTZ, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
    )
    op.create_index(
        "ix_messages_purchase_order_id_received_at",
        "messages",
        ["purchase_order_id", "received_at"],
    )

    op.create_table(
        "calendar_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_key", sa.String(length=200), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("starts_at", TIMESTAMPTZ, nullable=False),
        sa.Column("ends_at", TIMESTAMPTZ, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
    )
    op.create_index(
        "ix_calendar_events_user_id_starts_at", "calendar_events", ["user_id", "starts_at"]
    )

    op.create_table(
        "attention_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("scenario", sa.String(length=100), nullable=False),
        sa.Column("cause", sa.String(length=100), nullable=False),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("source_versions", JSONB, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("resolved_at", TIMESTAMPTZ, nullable=True),
    )
    op.create_index(
        "ix_attention_items_status_created_at",
        "attention_items",
        ["status", "created_at"],
    )

    op.create_table(
        "plans",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("attention_id", UUID, sa.ForeignKey("attention_items.id"), nullable=False),
        sa.Column("actor_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("intent", sa.String(length=100), nullable=False),
        sa.Column("workflow_name", sa.String(length=100), nullable=True),
        sa.Column("workflow_version", sa.Integer(), nullable=True),
        sa.Column("parameters", JSONB, nullable=False),
        sa.Column("source_versions", JSONB, nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("plan_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_plans_attention_id", "plans", ["attention_id"])
    op.create_index("ix_plans_actor_id", "plans", ["actor_id"])

    op.create_table(
        "approvals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("plan_id", UUID, sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("plan_hash", sa.String(length=128), nullable=False),
        sa.Column("requester_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approver_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("requested_at", TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
        sa.Column("decided_at", TIMESTAMPTZ, nullable=True),
    )
    op.create_index("ix_approvals_plan_id", "approvals", ["plan_id"])
    op.create_index("ix_approvals_approver_id_status", "approvals", ["approver_id", "status"])

    op.create_table(
        "workflow_instances",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("plan_id", UUID, sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("definition_name", sa.String(length=100), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("completed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_workflow_instances_plan_id", "workflow_instances", ["plan_id"])
    op.create_index("ix_workflow_instances_status", "workflow_instances", ["status"])

    op.create_table(
        "workflow_steps",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "workflow_instance_id",
            UUID,
            sa.ForeignKey("workflow_instances.id"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(length=100), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=True),
        sa.Column("input", JSONB, nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("completed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index(
        "ix_workflow_steps_workflow_instance_id_step_index",
        "workflow_steps",
        ["workflow_instance_id", "step_index"],
    )

    op.create_table(
        "scheduled_tasks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("attention_id", UUID, sa.ForeignKey("attention_items.id"), nullable=True),
        sa.Column(
            "workflow_instance_id",
            UUID,
            sa.ForeignKey("workflow_instances.id"),
            nullable=True,
        ),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("due_at", TIMESTAMPTZ, nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", TIMESTAMPTZ, nullable=True),
        sa.Column("completed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_scheduled_tasks_status_due_at", "scheduled_tasks", ["status", "due_at"])

    op.create_table(
        "audit_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("occurred_at", TIMESTAMPTZ, nullable=False),
        sa.Column("event_type", sa.String(length=150), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=False),
        sa.Column("actor_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "attention_id",
            UUID,
            sa.ForeignKey("attention_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "workflow_instance_id",
            UUID,
            sa.ForeignKey("workflow_instances.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("plan_id", UUID, sa.ForeignKey("plans.id", ondelete="SET NULL"), nullable=True),
        sa.Column("evidence_ids", JSONB, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=True),
        sa.Column("plan_hash", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=300), nullable=True),
        sa.Column("failure_category", sa.String(length=100), nullable=True),
    )
    op.create_index("ix_audit_events_run_id_occurred_at", "audit_events", ["run_id", "occurred_at"])
    op.create_index(
        "ix_audit_events_attention_id_occurred_at",
        "audit_events",
        ["attention_id", "occurred_at"],
    )


def downgrade() -> None:
    """Remove the schema in dependency-safe reverse order."""
    op.drop_table("audit_events")
    op.drop_table("scheduled_tasks")
    op.drop_table("workflow_steps")
    op.drop_table("workflow_instances")
    op.drop_table("approvals")
    op.drop_table("plans")
    op.drop_table("attention_items")
    op.drop_table("calendar_events")
    op.drop_table("messages")
    op.drop_table("quality_lots")
    op.drop_table("purchase_orders")
    op.drop_table("production_orders")
    op.drop_table("suppliers")
    op.drop_table("parts")
    op.drop_table("user_scopes")
    op.drop_table("users")
