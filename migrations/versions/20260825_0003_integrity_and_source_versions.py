"""Add integrity keys and versioned material-source records.

Revision ID: 20260825_0003
Revises: 20260825_0002
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260825_0003"
down_revision = "20260825_0002"
branch_labels = None
depends_on = None


UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)
SOURCE_VERSION = sa.Column(
    "source_version",
    sa.Integer(),
    nullable=False,
    server_default=sa.text("1"),
)


def upgrade() -> None:
    """Prevent duplicate work and version mutable material planning inputs."""
    op.create_unique_constraint(
        "uq_attention_items_dedupe_key",
        "attention_items",
        ["dedupe_key"],
    )
    op.create_unique_constraint(
        "uq_workflow_steps_workflow_instance_id_step_index",
        "workflow_steps",
        ["workflow_instance_id", "step_index"],
    )
    op.create_unique_constraint(
        "uq_workflow_steps_idempotency_key",
        "workflow_steps",
        ["idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_scheduled_tasks_idempotency_key",
        "scheduled_tasks",
        ["idempotency_key"],
    )

    for table_name in ("suppliers", "purchase_orders", "quality_lots"):
        op.add_column(table_name, SOURCE_VERSION.copy())
        op.create_check_constraint(
            f"ck_{table_name}_source_version_positive",
            table_name,
            "source_version > 0",
        )

    op.create_table(
        "inventory",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("part_id", UUID, sa.ForeignKey("parts.id"), nullable=False),
        sa.Column("plant_id", sa.String(length=100), nullable=False),
        sa.Column("available_quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("safety_stock_quantity", sa.Numeric(14, 3), nullable=False),
        SOURCE_VERSION.copy(),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint(
            "available_quantity >= 0",
            name="ck_inventory_available_quantity_non_negative",
        ),
        sa.CheckConstraint(
            "safety_stock_quantity >= 0",
            name="ck_inventory_safety_stock_quantity_non_negative",
        ),
        sa.CheckConstraint(
            "source_version > 0",
            name="ck_inventory_source_version_positive",
        ),
        sa.UniqueConstraint("part_id", "plant_id", name="uq_inventory_part_id_plant_id"),
    )

    op.create_table(
        "production_allocations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "quality_lot_id",
            UUID,
            sa.ForeignKey("quality_lots.id"),
            nullable=False,
        ),
        sa.Column(
            "production_order_id",
            UUID,
            sa.ForeignKey("production_orders.id"),
            nullable=False,
        ),
        sa.Column("allocated_quantity", sa.Numeric(14, 3), nullable=False),
        SOURCE_VERSION.copy(),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint(
            "allocated_quantity >= 0",
            name="ck_production_allocations_allocated_quantity_non_negative",
        ),
        sa.CheckConstraint(
            "source_version > 0",
            name="ck_production_allocations_source_version_positive",
        ),
        sa.UniqueConstraint(
            "quality_lot_id",
            "production_order_id",
            name="uq_production_allocations_quality_lot_id_production_order_id",
        ),
    )
    op.create_index(
        "ix_production_allocations_production_order_id",
        "production_allocations",
        ["production_order_id"],
    )


def downgrade() -> None:
    """Remove the integrity and material-source additions in reverse order."""
    op.drop_index(
        "ix_production_allocations_production_order_id",
        table_name="production_allocations",
    )
    op.drop_table("production_allocations")
    op.drop_table("inventory")

    for table_name in ("quality_lots", "purchase_orders", "suppliers"):
        op.drop_constraint(
            f"ck_{table_name}_source_version_positive",
            table_name,
            type_="check",
        )
        op.drop_column(table_name, "source_version")

    op.drop_constraint(
        "uq_scheduled_tasks_idempotency_key",
        "scheduled_tasks",
        type_="unique",
    )
    op.drop_constraint(
        "uq_workflow_steps_idempotency_key",
        "workflow_steps",
        type_="unique",
    )
    op.drop_constraint(
        "uq_workflow_steps_workflow_instance_id_step_index",
        "workflow_steps",
        type_="unique",
    )
    op.drop_constraint(
        "uq_attention_items_dedupe_key",
        "attention_items",
        type_="unique",
    )
