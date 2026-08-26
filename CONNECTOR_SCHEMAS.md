# Understand the connector schemas

This reference documents the data contracts used by the harness's enterprise resource planning (ERP), quality, mail, calendar, knowledge, identity, and tool connectors. The examples are current records from the deterministic seed in `src/enterprise_agent/seed.py`. They are realistic synthetic examples, not production company data or public HTTP API payloads.

The harness stores the synthetic records in PostgreSQL. Its scoped adapters convert permitted rows into a shared `Evidence` value before application services or a selected large language model (LLM) can use them. The adapter mappings live in `src/enterprise_agent/adapters/providers.py`; the database constraints live in `migrations/versions/`.

## Use the common evidence envelope

Every read connector returns the same envelope. This separates the caller's decision logic from connector-specific storage details and lets the application bind a plan to current evidence.

| Field | Meaning |
|---|---|
| `evidence_id` | Stable, source-prefixed identifier, such as `erp:purchase_order:<uuid>` |
| `source` | Connector owner: `erp`, `quality`, `mail`, `calendar`, or `knowledge` |
| `record_type` | Adapter-owned type vocabulary, such as `purchase_order` or `message` |
| `record_id` | Underlying synthetic record UUID |
| `source_version` | Positive revision used for freshness checks; mail and calendar currently use `1` |
| `observed_at` | Source timestamp chosen by the adapter |
| `payload` | Connector-specific, authorized fields only |

An example ERP inventory response is:

```json
{
  "evidence_id": "erp:inventory:00000000-0000-0000-0000-000000000501",
  "source": "erp",
  "record_type": "inventory",
  "record_id": "00000000-0000-0000-0000-000000000501",
  "source_version": 4,
  "observed_at": "2026-08-24T09:00:00+00:00",
  "payload": {
    "part_id": "00000000-0000-0000-0000-000000000101",
    "part_number": "PART-X",
    "plant_id": "PLANT-CHI",
    "available_quantity": 30.000,
    "safety_stock_quantity": 20.000
  }
}
```

The envelope is an internal Python contract defined by `Evidence` in `src/enterprise_agent/domain/contracts.py`. It is not an externally published REST response schema.

## Enforce authorization in each connector

Every provider receives an immutable `ActorContext` and a narrow `EvidenceQuery`. The query can request only known record types, optional record IDs, and an optional date range. Each adapter applies the actor's scope and visibility filters in its SQL query before it builds an `Evidence` value.

| Connector | Supported record types | Required scope | Additional provider filter |
|---|---|---|---|
| ERP | `inventory`, `purchase_order`, `production_order`, `supplier` | `erp:read` | Actor's permitted plant IDs |
| Quality | `quality_lot`, `production_allocation`, `production_impact` | `quality:lot:read` | Actor's permitted plant IDs |
| Mail | `message` | `mail:read` | The message recipient must equal the actor's email |
| Calendar | `calendar_event` | `calendar:read` | Event belongs to the actor and overlaps the requested date range |
| Knowledge | `supplier_risk_bulletin` | `knowledge:bulletin:read` | Actor's permitted plant IDs; bulletin must be active and unsuperseded |

The identity adapter creates the `ActorContext` from `users` and `user_scopes`. For example, the seed gives Dana purchasing, mail, calendar, and bulletin-read scopes at `PLANT-CHI`; it does not give Quinn access to purchasing, mail, calendar, or bulletin records.

```json
{
  "user_id": "00000000-0000-0000-0000-000000000001",
  "role": "purchasing_manager",
  "plant_ids": ["PLANT-CHI"],
  "scopes": ["erp:read", "mail:read", "calendar:read"],
  "backup_approver_id": "00000000-0000-0000-0000-000000000002",
  "approval_limits": {"USD": 10000.00}
}
```

The example shows only relevant scopes. The seed grants Dana additional declared write scopes; see `src/enterprise_agent/seed.py` for the complete deterministic set.

## Read ERP and quality records

The ERP adapter exposes the minimum material, supplier, purchase-order, and production-order facts needed for Scenario A. The quality adapter owns lot, allocation, and production-impact facts for Scenario B. The two adapters deliberately have distinct scope checks.

### Purchase order and supplier

The delayed Scenario A purchase order and its eligible alternate are represented as separate evidence records:

```json
{
  "purchase_order": {
    "po_number": "PO-4812-Y",
    "part_id": "00000000-0000-0000-0000-000000000101",
    "supplier_id": "00000000-0000-0000-0000-000000000201",
    "plant_id": "PLANT-CHI",
    "ordered_quantity": 100.000,
    "received_quantity": 40.000,
    "status": "delayed",
    "expected_receipt_date": "2026-08-28",
    "source_version": 2
  },
  "supplier": {
    "supplier_code": "SUP-Z",
    "name": "Supplier Z",
    "part_id": "00000000-0000-0000-0000-000000000101",
    "plant_id": "PLANT-CHI",
    "approved": true,
    "lead_time_days": 1,
    "unit_price": 18.00,
    "currency": "USD",
    "source_version": 1
  }
}
```

The provider can also return the deliberately tempting `SUP-BAIT` and `SUP-SLOW` records. Returning a record does not make it an eligible action: the deterministic gate recomputes approval, timing, scope, policy, and freshness before it stores a plan.

### Quality lot and allocation

Scenario B's available substitute lot and the held lot are separate records. Available capacity is derived from current quantity, status, and allocation data; no connector or model may assert capacity without those facts.

```json
{
  "quality_lot": {
    "lot_number": "LOT-QUALITY-GOOD",
    "part_number": "PART-QUALITY",
    "plant_id": "PLANT-CHI",
    "quantity": 120.000,
    "status": "released",
    "production_order_id": null,
    "allocated_quantity": 0.000,
    "source_version": 1
  },
  "production_impact": {
    "order_number": "Q-7001",
    "part_id": "00000000-0000-0000-0000-000000000102",
    "required_quantity": 80.000,
    "start_date": "2026-08-27",
    "supervisor_email": "priya.production@example.com"
  }
}
```

## Read mail, calendar, and knowledge records

Mail and calendar adapters expose only an actor's own mailbox or calendar. The knowledge adapter returns current, authorized supplier-risk bulletins and retains bulletin prose as evidence rather than executable policy.

### Shipment update email

The current delayed-shipment message for Dana's mailbox is:

```json
{
  "message_key": "shipment-update-po-4812-y-v2",
  "purchase_order_id": "00000000-0000-0000-0000-000000000401",
  "supplier_id": "00000000-0000-0000-0000-000000000201",
  "sender": "operations@supplier-y.example",
  "recipient": "dana.buyer@example.com",
  "subject": "PO-4812-Y shipment update",
  "received_at": "2026-08-24T09:00:00+00:00",
  "payload": {
    "current": true,
    "expected_receipt_date": "2026-08-28",
    "shipment_status": "delayed"
  }
}
```

The seed also includes an older message with `payload.superseded_by` and an unrelated message for `PO-NOISE-77`. Scenario A's context assembly selects the current correlated evidence rather than relying on all mailbox text.

### Out-of-office calendar event

The calendar provider exposes Dana's next-day availability fact used by approval escalation:

```json
{
  "event_key": "dana-out-of-office-2026-08-25",
  "user_id": "00000000-0000-0000-0000-000000000001",
  "event_type": "out_of_office",
  "starts_at": "2026-08-25T09:00:00+00:00",
  "ends_at": "2026-08-25T17:00:00+00:00",
  "payload": {"reason": "business travel"}
}
```

### Supplier-risk bulletin

The optional Scenario C connector exposes this current bulletin to an authorized purchasing actor:

```json
{
  "bulletin_key": "supplier-w-disruption",
  "supplier_id": "00000000-0000-0000-0000-000000000203",
  "plant_id": "PLANT-CHI",
  "risk_level": "high",
  "status": "active",
  "body": "Port closure may delay Supplier W shipments through the coming week.",
  "source_version": 2,
  "superseded_by_id": null,
  "published_at": "2026-08-24T08:00:00+00:00"
}
```

An adapter excludes the superseded version of this bulletin and the inactive Supplier Y bulletin before planning. The body remains opaque evidence, so text cannot select a tool, bypass approval, or alter workflow membership.

## Use the closed write-tool catalog

The harness does not expose arbitrary connector writes. `src/enterprise_agent/application/tools.py` defines every allowed write with a Pydantic input schema, required scope, compensation action, and retry-stable idempotency key.

| Tool | Required scope | Bounded input | Compensation |
|---|---|---|---|
| `create_replacement_po` | `erp:po:create` | Original PO, approved supplier, production order, quantity | Cancel the created replacement PO |
| `reduce_or_cancel_po` | `erp:po:cancel` | Original PO and quantity | Restore the original PO state |
| `place_purchase_order_hold` | `erp:po:hold` | PO, production order, expected PO version | Restore the held PO state |
| `notify_production` | `production:notify` | Production order and message | Send a correction notification |
| `schedule_arrival_check` | `scheduler:write` | PO and timezone-aware due time | Cancel the scheduled check |
| `reallocate_lot` | `erp:lot:write` | Quality lot, source/destination order, quantity | Restore the prior allocation |
| `flag_shortage_to_purchasing` | `production:notify` | Production order, part, shortage quantity | Send a correction notification |

For example, a replacement-PO request must have exactly these fields; the schema rejects extras and non-positive quantities:

```json
{
  "original_purchase_order_id": "00000000-0000-0000-0000-000000000401",
  "supplier_id": "00000000-0000-0000-0000-000000000202",
  "production_order_id": "00000000-0000-0000-0000-000000000301",
  "quantity": 60.000
}
```

The tool catalog is not an authorization bypass. The application gate verifies policy and freshness before approval; the executor verifies the approved plan, actor scopes, declared tool contract, and source versions immediately before each effect.

## Inspect and reset the synthetic records

Run `make demo` to reset and seed only the guarded Compose database, then use `make tui` to inspect the resulting demo and audit records. The seed is deterministic, so the identifiers and examples in this document remain stable unless `src/enterprise_agent/seed.py` changes.

For the broader entity model, deliberate noise, and omissions, read [MODEL.md](MODEL.md). For authorization, workflow, production integration, and scaling decisions, read [DESIGN.md](DESIGN.md).

## Know the production boundary

This document describes a local harness contract, not a production integration specification. There are no live ERP, mail, calendar, identity, or knowledge-system endpoints; no OAuth token exchange; and no external webhook or polling connector. A production adapter would map its upstream API into the same scoped evidence and closed tool contracts, add connector-specific authentication and retries, and preserve the existing authorization, freshness, idempotency, approval, and audit boundaries.
