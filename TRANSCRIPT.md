# Final reviewer transcript

This is the recorded, reproducible walkthrough for the enterprise-agent harness. It keeps three kinds of evidence distinct and never displays credentials, prompts, raw provider payloads, or volatile database identifiers.

| Evidence lane | Planner | Writes | What it proves |
|---|---|---|---|
| Deterministic control-plane | Fixed planner with synthetic Compose PostgreSQL data | Isolated disposable database only | Application correctness: authorization, policy, approval, recovery, scheduling, and audit behavior. |
| Live no-write evaluation | Explicit provider over fixed synthetic briefs | None; in-memory audit only | Bounded model/adaptor quality under a repeatable scorecard. |
| Guarded live local proposal | Explicit provider over reset-and-seeded local synthetic data | Local attention, plan, approval, workflow, schedule, and audit records only | A live provider output can be schema-validated and pass or fail the deterministic gate without executing a business tool. |

Live model behavior is never application-correctness evidence by itself. The deterministic gate, approval, freshness checks, idempotent executor, and audit controls govern every business effect.

## Commands used

Run the credential-free deterministic review:

```sh
make demo
uv run pytest -q tests/test_scenario_a_timing_audit.py \
  tests/test_scenario_b_execution.py \
  tests/test_scenario_c_execution.py \
  tests/test_scenario_c_recovery.py
uv run pytest -q tests/test_workflow_executor.py \
  -k crash_after_replacement_effect_restarts_with_the_same_started_key
```

Run the opt-in, fixed-synthetic provider scorecard:

```sh
enterprise-agent llm-evaluate --profile openai --all --execute
```

Run one guarded live local proposal. The command requires an interactive `live` confirmation and resets only the local synthetic target before every case:

```sh
docker compose --profile tools run --rm app \
  enterprise-agent live-demo --profile openai --case scenario-a-reroute
```

Replace the final case name with `scenario-b-quality-hold` or `scenario-c-supplier-risk` to inspect those bounded proposals.

## Deterministic control-plane evidence

The following recorded checks passed from the clean local walkthrough:

| Check | Observed result |
|---|---|
| `make demo` | Passed. It reset and seeded only the private Compose database, staged reviewable plans, did not call a provider, and did not execute a business workflow. |
| Scenario A/B/C PostgreSQL integration tests | Passed: four tests in 19.47 seconds. Each test created and removed an isolated disposable database. |
| Scenario A replacement-PO crash/restart | Passed: one focused test, with 32 deselected. |

### Scenario A — purchasing stockout, approval, recovery, and Tuesday follow-up

| Step | Deterministic outcome |
|---|---|
| Detect and deduplicate | Dana's scoped purchasing context finds the seeded `PART-X` shortage before production order `4812`; a repeated trigger deduplicates the attention item. |
| Gather and filter | The current delayed PO and newest correlated supplier update are used. Older mail, unrelated records, unapproved Supplier Bait, and too-slow Supplier Slow are excluded. |
| Gate and approval | The fixed planner proposes the registered `po_reroute:v1` intent. The gate binds source versions, policy, eligibility, amount, scope, and immutable plan hash before creating a pending approval. |
| Backup route | If Dana misses end of day and is unavailable next day, the unchanged approval routes to authorized backup Avery; Dana can no longer decide it. |
| Execute and recover | After Avery approves, the registered workflow creates the replacement PO, updates the original PO, notifies production, and schedules receipt verification. A crash after creation reuses the idempotency key and leaves exactly one replacement PO. |
| Tuesday | A full receipt resolves the attention; a partial or missing receipt creates one source-version-bound follow-up instead. |

### Scenario B — quality hold and real free capacity

| Step | Deterministic outcome |
|---|---|
| Scope | Quinn can read quality evidence but cannot perform purchasing writes. |
| Insufficient or committed capacity | The system flags only the uncovered remainder to purchasing; it never presents a held or committed lot as full cover. |
| Covered capacity | A released substitute lot is reallocated only after approval; pre-approval execution creates neither allocation nor notification. |
| Recovery | A simulated post-reallocation crash reuses its idempotency key, leaving one allocation and one notification. |

### Scenario C — optional supplier-risk hold

A current, authorized supplier-risk bulletin can produce only a typed `HOLD_AND_NOTIFY` plan. Superseded or unauthorized bulletin content produces no hold. Before approval, the PO remains open. After approved execution, the shared workflow holds it and sends exactly one notification; a post-hold crash resumes with the same idempotency key.

The primary executable evidence is [tests/test_scenario_a_timing_audit.py](tests/test_scenario_a_timing_audit.py), [tests/test_workflow_executor.py](tests/test_workflow_executor.py), [tests/test_scenario_b_execution.py](tests/test_scenario_b_execution.py), [tests/test_scenario_c_execution.py](tests/test_scenario_c_execution.py), and [tests/test_scenario_c_recovery.py](tests/test_scenario_c_recovery.py).

## Live no-write provider evaluation

The following observed OpenAI run used `gpt-5.6-terra` with the full fixed thirteen-case pack. It connected only to the selected provider and used the in-memory audit implementation; it did not access PostgreSQL, ERP, mail, scheduler, approval, workflow, or the durable audit ledger.

| Field | Observed value |
|---|---|
| Provider and model | OpenAI `gpt-5.6-terra` |
| Planner provenance | LIVE; fixed synthetic inputs; no business-system write |
| Cases and checks | 13/13 cases and 51/51 checks passed |
| Schema validation | Passed for every response |
| Metering | 9,051 input, 1,423 output, 10,474 total tokens; `$0.035178` estimated |

The pack passed the approved and unapproved supplier cases, too-slow supplier, newest-evidence handling, hostile email, covered and insufficient quality lots, ambiguity/manual-review cases, current/superseded/unauthorized supplier-risk bulletins, and hostile supplier-risk content. This is a useful live quality signal, not a claim of deterministic model behavior or production reliability.

Earlier observed provider comparisons remain useful context: Luna scored 11/12 checks over the three A/B/C comparison cases; Claude Sonnet 5 had normalized provider failures on two of those cases; the configured free Nemotron model and an additional free Nex candidate had provider availability or quality failures. No evaluation used fallback or retry.

## Guarded live local proposal evidence

These three observed Terra runs each reset and seeded the disposable local synthetic database, then made one bounded provider request. The command intentionally stops before any business tool effect. Every new run resets the same target, so only the final case's durable local records remain queryable afterward.

| Case and stable Run ID | Observed provider result | Schema and gate | Metering |
|---|---|---|---|
| Scenario A `live-demo:scenario-a-reroute` | `MANUAL_REVIEW`; no plan, approval, or workflow created | Schema passed; gate not invoked because manual review was proposed | 2,082 input, 213 output, 2,295 total; `$0.006720` estimated |
| Scenario B `live-demo:scenario-b-quality-hold` | `REALLOCATE_AND_NOTIFY`; pending approval staged | Schema passed; gate passed to pending approval | 1,460 input, 252 output, 1,712 total; `$0.005944` estimated |
| Scenario C `live-demo:scenario-c-supplier-risk` | `HOLD_AND_NOTIFY`; pending approval staged | Schema passed; gate passed to pending approval | 1,006 input, 202 output, 1,208 total; `$0.004436` estimated |

The Scenario A manual-review result is retained as observed rather than replaced with a preferred outcome. It demonstrates the fail-safe boundary: schema-valid model output that does not enter a registered workflow produces no plan and no gate invocation. Scenario B and C show that a live provider can produce a bounded recommendation which is still subject to the deterministic gate and a human approval.

No live approval, tool execution, crash recovery, or Tuesday follow-up was run in this lane. That omission is deliberate: `live-demo` is a proposal-staging command, and the deterministic lane above is the executable evidence for those downstream safeguards.

## Repeat this review

For a no-key reviewer demo, run `make demo` and inspect the bounded terminal panels. For the loopback-only UI, follow [README.md](README.md). To inspect a retained local live run, use:

```sh
enterprise-agent audit explain RUN_ID
```

`RUN_ID` is the stable text identifier such as `live-demo:scenario-c-supplier-risk`, not an approval or workflow UUID. The command reads the append-only audit ledger and does not reread mutable business records.
