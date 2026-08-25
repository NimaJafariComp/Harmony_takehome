"""Integration contract for the first durable domain schema migration."""

from __future__ import annotations

import json
import subprocess
from typing import Any, cast

import pytest

pytestmark = pytest.mark.integration


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and expose diagnostics on migration failure."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


def inspect_schema(database_url: str) -> dict[str, Any]:
    """Inspect the migrated schema from the private application network."""
    command = (
        "import json\n"
        "from os import environ\n"
        "from sqlalchemy import create_engine, inspect\n"
        "inspector = inspect(create_engine(environ['DATABASE_URL']))\n"
        "tables = sorted(inspector.get_table_names())\n"
        "schema = {\n"
        "    'tables': tables,\n"
        "    'columns': {table: sorted(column['name'] for column in inspector.get_columns(table)) for table in tables},\n"
        "    'foreign_keys': {table: sorted(f\"{foreign_key['constrained_columns'][0]}->{foreign_key['referred_table']}\" for foreign_key in inspector.get_foreign_keys(table)) for table in tables},\n"
        "    'indexes': {table: sorted(index['name'] for index in inspector.get_indexes(table)) for table in tables},\n"
        "    'unique_constraints': {table: sorted(constraint['name'] for constraint in inspector.get_unique_constraints(table) if constraint['name']) for table in tables},\n"
        "    'check_constraints': {table: sorted(constraint['name'] for constraint in inspector.get_check_constraints(table) if constraint['name']) for table in tables},\n"
        "}\n"
        "print(json.dumps(schema))\n"
    )
    result = compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={database_url}",
        "app",
        "python",
        "-c",
        command,
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def test_core_schema_migration_creates_domain_tables_relationships_and_indexes(
    disposable_database: str,
) -> None:
    """A new database supports the modeled entities and planned lookup paths."""
    compose(
        "--profile",
        "tools",
        "run",
        "--build",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "alembic",
        "upgrade",
        "head",
    )
    schema = inspect_schema(disposable_database)

    expected_tables = {
        "users",
        "user_scopes",
        "parts",
        "suppliers",
        "purchase_orders",
        "production_orders",
        "quality_lots",
        "messages",
        "calendar_events",
        "attention_items",
        "plans",
        "approvals",
        "workflow_instances",
        "workflow_steps",
        "scheduled_tasks",
        "audit_events",
    }
    assert expected_tables.issubset(schema["tables"])
    assert {"email", "role", "approval_limit_amount"}.issubset(schema["columns"]["users"])
    assert {"part_id", "supplier_id", "status"}.issubset(schema["columns"]["purchase_orders"])
    assert {"approver_id", "plan_hash", "policy_version", "source_versions"}.issubset(
        schema["columns"]["plans"]
    )
    assert {"payload", "lease_expires_at", "idempotency_key"}.issubset(
        schema["columns"]["scheduled_tasks"]
    )
    assert {"payload", "run_id", "event_type"}.issubset(schema["columns"]["audit_events"])
    assert {"part_id->parts", "supplier_id->suppliers"}.issubset(
        schema["foreign_keys"]["purchase_orders"]
    )
    assert "attention_id->attention_items" in schema["foreign_keys"]["plans"]
    assert "approver_id->users" in schema["foreign_keys"]["plans"]
    assert "plan_id->plans" in schema["foreign_keys"]["workflow_instances"]
    assert "workflow_instance_id->workflow_instances" in schema["foreign_keys"]["workflow_steps"]
    assert "ix_purchase_orders_part_id_status" in schema["indexes"]["purchase_orders"]
    assert "ix_messages_purchase_order_id_received_at" in schema["indexes"]["messages"]
    assert "ix_attention_items_status_created_at" in schema["indexes"]["attention_items"]
    assert (
        "ix_workflow_steps_workflow_instance_id_step_index" in schema["indexes"]["workflow_steps"]
    )
    assert "ix_scheduled_tasks_status_due_at" in schema["indexes"]["scheduled_tasks"]
    assert "ix_audit_events_run_id_occurred_at" in schema["indexes"]["audit_events"]


def test_integrity_migration_enforces_dedupe_idempotency_and_source_versions(
    disposable_database: str,
) -> None:
    """A clean database has the durability guarantees used by later control-plane work."""
    compose(
        "--profile",
        "tools",
        "run",
        "--build",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "alembic",
        "upgrade",
        "head",
    )
    schema = inspect_schema(disposable_database)

    assert {"inventory", "production_allocations"}.issubset(schema["tables"])
    for table in (
        "suppliers",
        "purchase_orders",
        "quality_lots",
        "inventory",
        "production_allocations",
    ):
        assert "source_version" in schema["columns"][table]

    assert "uq_attention_items_dedupe_key" in schema["unique_constraints"]["attention_items"]
    assert (
        "uq_workflow_steps_workflow_instance_id_step_index"
        in schema["unique_constraints"]["workflow_steps"]
    )
    assert "uq_workflow_steps_idempotency_key" in schema["unique_constraints"]["workflow_steps"]
    assert "uq_scheduled_tasks_idempotency_key" in schema["unique_constraints"]["scheduled_tasks"]
    assert "uq_inventory_part_id_plant_id" in schema["unique_constraints"]["inventory"]
    assert (
        "uq_production_allocations_quality_lot_id_production_order_id"
        in schema["unique_constraints"]["production_allocations"]
    )
    assert (
        "ck_purchase_orders_source_version_positive"
        in schema["check_constraints"]["purchase_orders"]
    )
    assert (
        "ck_production_allocations_source_version_positive"
        in schema["check_constraints"]["production_allocations"]
    )
