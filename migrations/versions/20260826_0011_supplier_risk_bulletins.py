"""Create versioned, scoped supplier-risk bulletins for optional Scenario C.

Revision ID: 20260826_0011
Revises: 20260825_0010
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260826_0011"
down_revision = "20260825_0010"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    """Persist supplier-scoped bulletins independently of the ERP records they may affect."""
    op.create_table(
        "supplier_risk_bulletins",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("bulletin_key", sa.String(length=160), nullable=False),
        sa.Column("supplier_id", UUID, sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("plant_id", sa.String(length=100), nullable=False),
        sa.Column("risk_level", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column(
            "superseded_by_id",
            UUID,
            sa.ForeignKey("supplier_risk_bulletins.id"),
            nullable=True,
        ),
        sa.Column("published_at", TIMESTAMPTZ, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'inactive')",
            name="ck_supplier_risk_bulletins_status",
        ),
        sa.CheckConstraint(
            "source_version > 0",
            name="ck_supplier_risk_bulletins_source_version_positive",
        ),
        sa.UniqueConstraint(
            "supplier_id",
            "bulletin_key",
            "source_version",
            name="uq_supplier_risk_bulletins_supplier_key_version",
        ),
    )
    op.create_index(
        "ix_supplier_risk_bulletins_supplier_id_status",
        "supplier_risk_bulletins",
        ["supplier_id", "status"],
    )
    op.create_index(
        "ix_supplier_risk_bulletins_plant_id_status",
        "supplier_risk_bulletins",
        ["plant_id", "status"],
    )


def downgrade() -> None:
    """Remove the optional Scenario C knowledge table and its lookup indexes."""
    op.drop_index(
        "ix_supplier_risk_bulletins_plant_id_status",
        table_name="supplier_risk_bulletins",
    )
    op.drop_index(
        "ix_supplier_risk_bulletins_supplier_id_status",
        table_name="supplier_risk_bulletins",
    )
    op.drop_table("supplier_risk_bulletins")
