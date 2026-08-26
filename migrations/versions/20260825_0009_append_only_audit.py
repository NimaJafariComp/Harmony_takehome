"""Make durable audit history database-enforced append-only.

Revision ID: 20260825_0009
Revises: 20260825_0008
Create Date: 2026-08-25
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260825_0009"
down_revision = "20260825_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Reject updates and deletes so every audit event remains historical evidence."""
    op.execute("""
        CREATE FUNCTION enterprise_agent_reject_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit events are append-only' USING ERRCODE = 'check_violation';
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION enterprise_agent_reject_audit_mutation();
    """)


def downgrade() -> None:
    """Remove append-only protection in reverse dependency order."""
    op.execute("DROP TRIGGER trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION enterprise_agent_reject_audit_mutation()")
