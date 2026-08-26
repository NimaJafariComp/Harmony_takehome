# Final reviewer transcript

This is the recorded, reproducible walkthrough for the enterprise-agent harness. It keeps three kinds of evidence distinct and never displays credentials, prompts, raw provider payloads, or volatile database identifiers.

| Evidence lane | Planner | Writes | What it proves |
|---|---|---|---|
| Deterministic control-plane | Fixed planner with synthetic Compose PostgreSQL data | Isolated disposable database only | Application correctness: authorization, policy, approval, recovery, scheduling, and audit behavior. |
| Live no-write evaluation | Explicit provider over fixed synthetic briefs | None; in-memory audit only | Bounded model/adaptor quality under a repeatable scorecard. |
| Guarded live local proposal | Explicit provider over reset-and-seeded local synthetic data | Local attention, plan, approval, workflow, schedule, and audit records only | A live provider output can be schema-validated and pass or fail the deterministic gate without executing a business tool. |

Live model behavior is never application-correctness evidence by itself. The deterministic gate, approval, freshness checks, idempotent executor, and audit controls govern every business effect.

## How to replay this evidence

For an interactive reviewer, use `make tui`. It is the preferred route and reaches the same guarded application commands used to produce the receipts below:

| Evidence lane | TUI route |
|---|---|
| Deterministic control-plane | Home → **Guided company demo** |
| Live no-write evaluation | Home → **Normal operator mode** → **Live-evaluation catalogue** |
| Guarded live local proposal | Home → **Guarded live local demo** |

The command forms below are retained as precise, scriptable evidence of the original recorded runs—not because the TUI cannot produce the same results. Database-backed commands run through Compose because the synthetic database is intentionally private.

## Commands used to record the receipt

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

Run the opt-in, fixed-synthetic provider scorecard directly (or use the TUI route above):

```sh
enterprise-agent llm-evaluate --profile openai --all --execute
```

Run one guarded live local proposal by script. It requires an interactive `live` confirmation and resets only the local synthetic target before every case:

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

The following observed runs used the full fixed thirteen-case pack after the grounded-evaluation and provider-adapter remediation. Each used only an in-memory audit implementation; neither accessed PostgreSQL, ERP, mail, scheduler, approval, workflow, or the durable audit ledger.

| Provider and model | Planner provenance | Cases and checks | Schema validation | Metering |
|---|---|---|---|---|
| OpenAI `gpt-5.6-terra` | LIVE; fixed synthetic inputs; no business-system write | 13/13 cases and 51/51 checks passed | Passed for every response | 9,874 input, 1,169 output, 11,043 total; `$0.033776` estimated |
| Claude `claude-sonnet-5` | LIVE; fixed synthetic inputs; no business-system write | 13/13 cases and 51/51 checks passed | Passed for every response | 27,336 input, 2,536 output, 29,872 total; `$0.080032` estimated |

Both runs passed approved and unapproved supplier cases, too-slow supplier, newest-evidence handling, hostile email, covered and insufficient quality lots, ambiguity/manual-review cases, current/superseded/unauthorized supplier-risk bulletins, and hostile supplier-risk content. These are useful live quality signals, not a claim of deterministic model behavior or production reliability.

The earlier limited M13.5 comparison is historical pre-remediation evidence, not the current provider assessment. The configured free OpenRouter account was rate-limited during the current recheck, so its no-write and smoke requests are truthfully recorded as unavailable rather than treated as model-quality failures. No evaluation used fallback or retry.

## Guarded live local proposal evidence

After the UUID-serialization and Claude-identity fixes, the following observed local runs reset and seeded the disposable synthetic database, then made one bounded provider request. Every response passed schema validation and the deterministic gate to a pending approval. The command intentionally stops before any business tool effect. Every new run resets the same target, so only the final case's durable local records remain queryable afterward.

| Provider | Case and stable Run ID | Observed provider result | Metering |
|---|---|---|---|
| OpenAI `gpt-5.6-terra` | Scenario A `live-demo:scenario-a-reroute` | `ENTER_WORKFLOW`; pending approval staged | 2,082 input, 462 output, 2,544 total; `$0.009708` estimated |
| OpenAI `gpt-5.6-terra` | Scenario B `live-demo:scenario-b-quality-hold` | `REALLOCATE_AND_NOTIFY`; pending approval staged | 1,460 input, 236 output, 1,696 total; `$0.0031294` estimated |
| OpenAI `gpt-5.6-terra` | Scenario C `live-demo:scenario-c-supplier-risk` | `HOLD_AND_NOTIFY`; pending approval staged | 1,006 input, 187 output, 1,193 total; `$0.004256` estimated |
| Claude `claude-sonnet-5` | Scenario A `live-demo:scenario-a-reroute` | `ENTER_WORKFLOW`; pending approval staged | 3,570 input, 1,893 output, 5,463 total; `$0.026070` estimated |
| Claude `claude-sonnet-5` | Scenario B `live-demo:scenario-b-quality-hold` | `REALLOCATE_AND_NOTIFY`; pending approval staged | 3,259 input, 677 output, 3,936 total; `$0.013288` estimated |
| Claude `claude-sonnet-5` | Scenario C `live-demo:scenario-c-supplier-risk` | `HOLD_AND_NOTIFY`; pending approval staged | 2,216 input, 1,410 output, 3,626 total; `$0.018532` estimated |

The current live receipts demonstrate provider proposals entering the same deterministic approval boundary. They do not authorize a provider to select tool steps or bypass scope, source freshness, approval, or workflow controls.

No live approval, tool execution, crash recovery, or Tuesday follow-up was run in this lane. That omission is deliberate: `live-demo` is a proposal-staging command, and the deterministic lane above is the executable evidence for those downstream safeguards.

## Repeat this review

For a no-key reviewer demo, run `make tui` and choose **Guided company demo** (or use unattended `make demo`). For the loopback-only UI, follow [README.md](README.md). To inspect a retained local live run by command, use:

```sh
docker compose --profile tools run --rm app enterprise-agent audit explain RUN_ID
```

`RUN_ID` is the stable text identifier such as `live-demo:scenario-c-supplier-risk`, not an approval or workflow UUID. The command reads the append-only audit ledger and does not reread mutable business records.
