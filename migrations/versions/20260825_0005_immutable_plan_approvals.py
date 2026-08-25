"""Bind immutable plan hashes to approver identity and durable approval decisions.

Revision ID: 20260825_0005
Revises: 20260825_0004
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260825_0005"
down_revision = "20260825_0004"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    """Make plans immutable and require every approval hash to match its referenced plan."""
    op.add_column("plans", sa.Column("approver_id", UUID, nullable=True))
    op.execute("UPDATE plans SET approver_id = actor_id WHERE approver_id IS NULL")
    op.alter_column("plans", "approver_id", nullable=False)
    op.create_foreign_key("fk_plans_approver_id_users", "plans", "users", ["approver_id"], ["id"])

    op.execute("""
        CREATE FUNCTION enterprise_agent_reject_plan_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'plans are immutable once created' USING ERRCODE = 'check_violation';
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_plans_immutable
        BEFORE UPDATE OR DELETE ON plans
        FOR EACH ROW EXECUTE FUNCTION enterprise_agent_reject_plan_mutation();
    """)
    op.execute("""
        CREATE FUNCTION enterprise_agent_enforce_approval_plan_hash()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_plan_hash text;
        BEGIN
            SELECT plan_hash INTO expected_plan_hash FROM plans WHERE id = NEW.plan_id;
            IF expected_plan_hash IS NULL OR NEW.plan_hash IS DISTINCT FROM expected_plan_hash THEN
                RAISE EXCEPTION 'approval plan hash must match its immutable plan' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_approvals_plan_hash
        BEFORE INSERT OR UPDATE ON approvals
        FOR EACH ROW EXECUTE FUNCTION enterprise_agent_enforce_approval_plan_hash();
    """)
    op.execute("""
        CREATE FUNCTION enterprise_agent_reject_approval_binding_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.plan_id IS DISTINCT FROM OLD.plan_id
               OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash
               OR NEW.requester_id IS DISTINCT FROM OLD.requester_id
               OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
                RAISE EXCEPTION 'approval binding fields are immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_approvals_binding_immutable
        BEFORE UPDATE ON approvals
        FOR EACH ROW EXECUTE FUNCTION enterprise_agent_reject_approval_binding_mutation();
    """)


def downgrade() -> None:
    """Remove plan/approval immutability enforcement in reverse dependency order."""
    op.execute("DROP TRIGGER trg_approvals_binding_immutable ON approvals")
    op.execute("DROP FUNCTION enterprise_agent_reject_approval_binding_mutation()")
    op.execute("DROP TRIGGER trg_approvals_plan_hash ON approvals")
    op.execute("DROP FUNCTION enterprise_agent_enforce_approval_plan_hash()")
    op.execute("DROP TRIGGER trg_plans_immutable ON plans")
    op.execute("DROP FUNCTION enterprise_agent_reject_plan_mutation()")
    op.drop_constraint("fk_plans_approver_id_users", "plans", type_="foreignkey")
    op.drop_column("plans", "approver_id")
