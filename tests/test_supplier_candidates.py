"""Contracts for deterministic Scenario A alternate-supplier eligibility filtering."""

from __future__ import annotations

import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from enterprise_agent.application.context import AuthorizedContextBundle
from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    Evidence,
    EvidenceId,
    PlantId,
    ScenarioAStockoutTrigger,
    Scope,
    UserId,
)

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
DANA = ActorContext(
    user_id=UserId("00000000-0000-0000-0000-000000000001"),
    role="purchasing_manager",
    scopes=frozenset({Scope("erp:read")}),
    plant_ids=frozenset({PlantId("PLANT-CHI")}),
    backup_approver_id=UserId("00000000-0000-0000-0000-000000000002"),
    approval_limits={"USD": Decimal("10000.00")},
)


def evidence(
    *,
    record_type: str,
    record_id: str,
    payload: dict[str, object],
    source_version: int = 1,
) -> Evidence:
    """Build one authorized ERP fact suitable for candidate-filtering contracts."""
    return Evidence(
        evidence_id=EvidenceId(f"erp:{record_type}:{record_id}"),
        source="erp",
        record_type=record_type,
        record_id=record_id,
        source_version=source_version,
        observed_at=NOW,
        payload=payload,
    )


def supplier(
    *,
    record_id: str,
    part_id: object = "part-x",
    plant_id: object = "PLANT-CHI",
    approved: object = True,
    lead_time_days: object = 1,
) -> Evidence:
    """Build a visible supplier record with independently adjustable eligibility facts."""
    return evidence(
        record_type="supplier",
        record_id=record_id,
        payload={
            "part_id": part_id,
            "plant_id": plant_id,
            "approved": approved,
            "lead_time_days": lead_time_days,
        },
    )


def scenario_context(*, suppliers: tuple[Evidence, ...]) -> AuthorizedContextBundle:
    """Build an already-authorized Scenario A context that candidate filtering may inspect."""
    trigger = ScenarioAStockoutTrigger(
        detector="stockout_detector:v1",
        part_id="part-x",
        production_order_id="production-4812",
        inventory_version=4,
        production_start_date=date(2026, 8, 27),
        detected_at=NOW,
        source_versions={
            "inventory:inventory-x": 4,
            "production_order:production-4812": 1,
        },
    )
    attention = AttentionItem(
        attention_id=AttentionId("attention-stockout"),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key=trigger.dedupe_key,
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions=trigger.source_versions,
    )
    return AuthorizedContextBundle(
        actor=DANA,
        attention=attention,
        trigger=trigger,
        inventory=evidence(
            record_type="inventory",
            record_id="inventory-x",
            source_version=4,
            payload={"part_id": "part-x"},
        ),
        production_order=evidence(
            record_type="production_order",
            record_id="production-4812",
            payload={"part_id": "part-x", "start_date": date(2026, 8, 27)},
        ),
        original_purchase_order=evidence(
            record_type="purchase_order",
            record_id="po-4812-y",
            source_version=2,
            payload={"part_id": "part-x", "supplier_id": "supplier-y"},
        ),
        suppliers=suppliers,
        shipment_update=evidence(
            record_type="message",
            record_id="shipment-current",
            payload={"shipment_status": "delayed"},
        ),
        calendar_events=(),
    )


def test_filter_allows_only_an_approved_fast_alternate_for_the_part_and_plant() -> None:
    """Only Supplier Z is eligible when the original, slow, and wrong-part vendors are visible."""
    from enterprise_agent.application.candidates import (
        SupplierCandidateFilter,
        SupplierExclusionReason,
    )

    result = SupplierCandidateFilter().filter(
        scenario_context(
            suppliers=(
                supplier(record_id="supplier-y", lead_time_days=4),
                supplier(record_id="supplier-slow", lead_time_days=8),
                supplier(record_id="supplier-wrong-part", part_id="part-noise"),
                supplier(record_id="supplier-z", lead_time_days=1),
            )
        )
    )

    assert [(candidate.supplier_id, candidate.arrival_date) for candidate in result.candidates] == [
        ("supplier-z", date(2026, 8, 25))
    ]
    assert result.allowed_supplier_ids == frozenset({"supplier-z"})
    assert {exclusion.supplier_id: exclusion.reasons for exclusion in result.exclusions} == {
        "supplier-slow": (SupplierExclusionReason.LEAD_TIME_TOO_LONG,),
        "supplier-wrong-part": (SupplierExclusionReason.WRONG_PART,),
        "supplier-y": (
            SupplierExclusionReason.ORIGINAL_SUPPLIER,
            SupplierExclusionReason.LEAD_TIME_TOO_LONG,
        ),
    }


def test_filter_retains_all_reasons_for_an_ineligible_supplier_without_failing_open() -> None:
    """Malformed or disallowed supplier facts cannot turn into a candidate by omission."""
    from enterprise_agent.application.candidates import (
        SupplierCandidateFilter,
        SupplierExclusionReason,
    )

    result = SupplierCandidateFilter().filter(
        scenario_context(
            suppliers=(
                supplier(
                    record_id="supplier-disallowed",
                    part_id="part-noise",
                    plant_id="PLANT-NYC",
                    approved=False,
                    lead_time_days=8,
                ),
                supplier(record_id="supplier-invalid-lead", lead_time_days="tomorrow"),
            )
        )
    )

    assert result.candidates == ()
    assert {exclusion.supplier_id: exclusion.reasons for exclusion in result.exclusions} == {
        "supplier-disallowed": (
            SupplierExclusionReason.NOT_APPROVED,
            SupplierExclusionReason.WRONG_PART,
            SupplierExclusionReason.WRONG_PLANT,
            SupplierExclusionReason.LEAD_TIME_TOO_LONG,
        ),
        "supplier-invalid-lead": (SupplierExclusionReason.INVALID_LEAD_TIME,),
    }


def test_filter_excludes_the_original_supplier_even_when_it_could_arrive_in_time() -> None:
    """A reroute candidate must be an alternate supplier, not a no-op rewrite of the original PO."""
    from enterprise_agent.application.candidates import (
        SupplierCandidateFilter,
        SupplierExclusionReason,
    )

    result = SupplierCandidateFilter().filter(
        scenario_context(suppliers=(supplier(record_id="supplier-y", lead_time_days=1),))
    )

    assert result.candidates == ()
    assert result.exclusions[0].reasons == (SupplierExclusionReason.ORIGINAL_SUPPLIER,)


def compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a Compose command and expose diagnostics if it fails."""
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.critical
@pytest.mark.integration
@pytest.mark.scenario
def test_seeded_filter_exposes_only_supplier_z_as_the_allowed_alternate(
    disposable_database: str,
) -> None:
    """The seeded company makes the approved, fast alternate explicit before any LLM call."""
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
    command = (
        "from datetime import UTC, datetime\n"
        "from os import environ\n"
        "from enterprise_agent.adapters import (\n"
        "    PostgresAttentionAdapter,\n"
        "    PostgresCalendarAdapter,\n"
        "    PostgresDemoClock,\n"
        "    PostgresErpAdapter,\n"
        "    PostgresIdentityAdapter,\n"
        "    PostgresMailAdapter,\n"
        ")\n"
        "from enterprise_agent.application.candidates import SupplierCandidateFilter\n"
        "from enterprise_agent.application.context import ScenarioAContextAssembler\n"
        "from enterprise_agent.application.stockout import StockoutDetector\n"
        "from enterprise_agent.domain import RunId, UserId\n"
        "from enterprise_agent.seed import reset_database, seed_database\n"
        "database_url = environ['DATABASE_URL']\n"
        "reset_database(database_url, allow_test_database=True)\n"
        "seed_database(database_url, allow_test_database=True)\n"
        "identity = PostgresIdentityAdapter(database_url)\n"
        "actor = identity.actor_for(UserId('00000000-0000-0000-0000-000000000001'))\n"
        "detection = StockoutDetector(PostgresErpAdapter(database_url), PostgresAttentionAdapter(database_url), PostgresDemoClock(database_url)).detect(actor, RunId('run-seeded-candidates'))[0]\n"
        "context = ScenarioAContextAssembler(identity, PostgresErpAdapter(database_url), PostgresMailAdapter(database_url), PostgresCalendarAdapter(database_url)).assemble(user_id=actor.user_id, attention=detection.registration.attention, trigger=detection.risk.trigger)\n"
        "result = SupplierCandidateFilter().filter(context)\n"
        "assert [candidate.evidence.payload['supplier_code'] for candidate in result.candidates] == ['SUP-Z']\n"
        "assert {entry.evidence.payload['supplier_code']: {reason.value for reason in entry.reasons} for entry in result.exclusions} == {\n"
        "    'SUP-BAIT': {'not_approved'},\n"
        "    'SUP-SLOW': {'lead_time_too_long'},\n"
        "    'SUP-W': {'wrong_part'},\n"
        "    'SUP-Y': {'lead_time_too_long', 'original_supplier'},\n"
        "}\n"
    )
    compose(
        "--profile",
        "tools",
        "run",
        "--rm",
        "-e",
        f"DATABASE_URL={disposable_database}",
        "app",
        "python",
        "-c",
        command,
    )
