# Understand the synthetic company model

This reference describes the deterministic company data that powers the harness. It defines the minimal records needed to prove Scenario A and Scenario B, plus optional Scenario C. The data is synthetic, repeatable, and intentionally incomplete: it tests control-plane behavior rather than modelling a complete enterprise resource planning system.

Run `make demo` to reset and seed the model into the private local Compose database. The seed uses fixed identifiers and a fixed starting time, so test cases and audit records remain reproducible.

## Model boundaries

The model represents one plant, `PLANT-CHI`, with three parts, five suppliers, four users, three purchase orders, four production orders, three quality lots, three supplier messages, one calendar event, and three supplier-risk bulletins.

The schema is grouped by responsibility:

| Group | Records | Purpose |
|---|---|---|
| Identity and authorization | `users`, `user_scopes` | Stores role, approval limit, backup approver, plant-scoped capabilities, and visibility boundaries. |
| Material and planning | `parts`, `suppliers`, `inventory`, `purchase_orders`, `production_orders` | Stores the facts used to detect a stockout and select an eligible supplier. |
| Quality and production | `quality_lots`, `production_allocations` | Distinguishes held, released, and already committed lot capacity. |
| Communications and knowledge | `messages`, `calendar_events`, `supplier_risk_bulletins` | Supplies current shipment evidence, approver availability, and optional supplier-risk evidence. |
| Durable control plane | `attention_items`, `plans`, `approvals`, `workflow_instances`, `workflow_steps`, `tool_invocations`, `scheduled_tasks`, `audit_events`, `demo_clock` | Persists bounded planning, approval, declared execution, recovery, scheduling, and reconstruction state. |

Every seed mutation is local to the dedicated demo database. Reset rejects any other database name or host before connecting.

## Identity, roles, and scopes

The model uses four people to make scope boundaries visible:

| Person | Role | Relevant authority |
|---|---|---|
| Dana Buyer | Purchasing manager | Reads purchasing, mail, calendar, and supplier-risk evidence. May create, reroute, cancel, or hold a purchase order and notify production. Approval limit: $10,000. Avery is the backup approver. |
| Avery Backup | Purchasing director | Decides approvals up to $50,000 and reads purchasing context needed for approval routing. |
| Quinn Quality | Quality manager | Reads quality data, reallocates lots, notifies production, and escalates a purchasing shortage. Has no purchase-order, mail, calendar, or bulletin scope. |
| Priya Production | Production supervisor | Receives production notifications and reads production data. Has no purchasing or quality write scope. |

Providers enforce these scopes in their queries. The planner never receives records that the selected actor cannot read. In particular, Quinn cannot query Dana's purchase orders or mail, and Dana alone can read the supplier-risk bulletins.

## Scenario A: projected stockout

The seed starts at `2026-08-24T09:00:00Z`. Production order `4812` requires 100 units of `PART-X` on `2026-08-26`. On-hand inventory is 30 units with 20 units reserved as safety stock, leaving a projected shortage of 90 units before production starts.

`PO-4812-Y` is the delayed original order for 100 units from Supplier Y. It has received 40 units and expects the remainder on `2026-08-28`, after production starts. Its source version is 2. The newest matching supplier email reports the same late receipt date. An earlier email is explicitly superseded.

The supplier rows form a deliberate eligibility matrix:

| Supplier | Part and plant | Approval | Lead time | Unit price | Expected planner treatment |
|---|---|---:|---:|---:|---|
| Supplier Y (`SUP-Y`) | `PART-X`, Chicago | Yes | 4 days | $14 | Original delayed supplier, not a timely replacement. |
| Supplier Z (`SUP-Z`) | `PART-X`, Chicago | Yes | 1 day | $18 | The only eligible replacement that arrives by the production start. |
| Supplier Bait (`SUP-BAIT`) | `PART-X`, Chicago | No | 1 day | $4 | Cheaper and faster, but always excluded because it is not approved. |
| Supplier Slow (`SUP-SLOW`) | `PART-X`, Chicago | Yes | 8 days | $11 | Approved but excluded because it cannot meet the deadline. |
| Supplier W (`SUP-W`) | `PART-NOISE`, Chicago | Yes | 1 day | $5 | Wrong part for Scenario A and therefore unrelated evidence. |

The normal deterministic Scenario A planner creates a pending reroute approval only. It does not create a replacement purchase order. Execution requires an authorized decision, an unchanged plan hash, current source versions, and the fixed `po_reroute:v1` workflow.

## Scenario B: quality-hold capacity

Scenario B uses `PART-QUALITY` and two production orders starting on `2026-08-27`.

| Production order | Requirement | Current material state | Expected result |
|---|---:|---|---|
| `Q-7001` | 80 units | `LOT-QUALITY-HELD` is held and allocated for all 80 units. `LOT-QUALITY-GOOD` is released with 120 unallocated units. | A full substitute can be proposed after approval. |
| `Q-7002` | 200 units | `LOT-QUALITY-NO-COVER` is held and already allocated for all 200 units. | The model must not present it as available; the system escalates the shortage or requests review. |

The quality model keeps `quantity`, `allocated_quantity`, lot status, and allocation rows separate. Availability is therefore a rule over current facts, not an arbitrary recommendation assertion. A test-only mutation can partially commit or release a lot to prove that the gate rechecks freshness and never turns partial cover into a full reallocation.

## Scenario C: supplier-risk bulletin

Scenario C is an optional extension that reuses the same control plane. Supplier W has an open purchase order, `PO-C-9001-W`, for 75 units of `PART-NOISE`; production order `C-9001` needs the same part on `2026-08-28`.

The supplier-risk table contains three deliberately different facts:

| Bulletin | Supplier | State | Source version | Use |
|---|---|---|---:|---|
| `supplier-w-disruption` | Supplier W | Active, high risk | 2 | Current authorized input for the optional scenario. |
| `supplier-w-disruption` | Supplier W | Superseded, medium risk | 1 | Must be ignored in favor of version 2. |
| `supplier-y-weather` | Supplier Y | Inactive, low risk | 1 | Unrelated inactive noise. |

Bulletin body text is opaque evidence. It cannot change policy, tool selection, or workflow membership. The only write-capable recommendation shape is a typed, approval-gated purchase-order hold plus production notification. It checks the bulletin, purchase order, part, plant, and source versions again before execution.

## Freshness, durability, and audit data

Mutable planning evidence has a positive `source_version`. The model versions suppliers, purchase orders, inventory, quality lots, production allocations, and supplier-risk bulletins. Plans bind the relevant source-version map, a policy version, a hash of the approved intent, and an expiry. A changed original purchase order, released held lot, or superseding bulletin invalidates an older plan before it can write.

The durable state has database-enforced uniqueness for attention deduplication, workflow step position, workflow-step idempotency, scheduled-task idempotency, and external-style tool idempotency. Workflow instances and scheduled tasks store explicit status, attempts, leases, and timestamps. The append-only audit table rejects updates and deletes at the database level, so `audit explain` reconstructs a run from historical events rather than live mutable tables.

`demo_clock` is a one-row clock fixed initially at `2026-08-24T09:00:00Z`. It replaces wall time in deterministic tests and drives end-of-day approval routing plus receipt follow-up. Advancing the clock is allowed only through the guarded local demo path.

## Deliberate noise and test mutations

The base seed contains irrelevant facts so the system must filter rather than rely on a perfectly clean context:

- `PO-NOISE-77` and its Supplier W email are unrelated to Scenario A
- Supplier W serves a different part from `PART-X`
- The cheaper supplier is unapproved, while another approved supplier is too slow
- The old Supplier Y email is superseded by a newer update
- An inactive Supplier Y bulletin and a superseded Supplier W bulletin must not influence Scenario C
- Dana's out-of-office calendar event supports the end-of-day backup-approver rule

Named deterministic test stories apply small, fixed mutations for conditions that cannot coexist in one base state. They cover an on-schedule newer email, hostile email text, a changed original purchase order, authority limits, answered and unavailable approvers, a replacement-PO crash and restart, full or missing Tuesday receipts, partial or committed quality capacity, a released held lot, and multiple unranked alternate lots.

## Intentional omissions

This is not a general-purpose enterprise resource planning (ERP) schema. It does not model vendors, contracts, invoices, receiving locations, reservations, bills of material, routing, calendars across plants, inventory transactions, quality disposition history, identity provisioning, or production scheduling breadth.

The model also omits arbitrary email ingestion and unbounded knowledge search. Messages and bulletins have structured correlation fields so the application can select current, actor-authorized evidence without treating prose as instructions. Production connectors, mail delivery, calendar synchronization, and ERP effects are seeded external-style adapters, which makes failure, retry, compensation, and audit behavior deterministic and testable.
