# Enterprise Agent Harness - Delivery Status

## Purpose

This is the tracked execution record for the take-home. It monitors implementation progress and the evidence for each acceptance criterion. The detailed local plan is intentionally not tracked; this file contains the shared status, task identifiers, validation evidence, and commit history.

## Status rules

Use only these task states:

| State | Meaning |
|---|---|
| `not_started` | Work has not begun. |
| `in_progress` | Implementation is active; no acceptance claim is made. |
| `blocked` | Progress cannot continue without an external dependency or decision. |
| `complete` | Required implementation and task-level validation succeeded. |

After completing each task:

1. Run the task's focused validation and fix failures.
2. Update the matching row below with its state, named focused test or validation command, passing result, and the acceptance criteria it advances.
3. Append a dated row to the activity log; do not delete prior entries.
4. Commit the completed task using `type(scope): summary [Task-ID]`.
5. Push the commit only after its validation passes. Never push credentials, `.env`, local plans, or assignment PDFs.

Recommended commit types are `feat`, `fix`, `test`, `docs`, `build`, `chore`, and `refactor`. If a task changes behavior and tests, keep the code and its focused tests in one commit.

### Required task-evidence format

Every task register entry uses its `Evidence / commit` cell in this format when completed:

```text
Test: tests/path/test_name.py::test_name or `make target` - PASS
Evidence: one-sentence behavior proved
Commit: <short SHA> pushed to origin/main
```

For a documentation-only task, replace `Test:` with `Validation:` and name the checked command, link, or transcript section. A task without this evidence stays `in_progress`, even if its code appears complete.

## Current delivery dashboard

| Field | Value |
|---|---|
| Overall status | `in_progress` |
| Current milestone | M4 - Declared workflow, idempotent tools, and crash recovery |
| Current task | M4.8 - Test workflow invariants |
| Required task progress | 29 / 58 complete |
| Acceptance criteria progress | 4 / 14 satisfied; 8 in progress |
| Last validated commit | `98546f1` |
| Last completed-task push | `98546f1` to `origin/main` |
| Blocking issue | None |

## Required task register

### M1 - Foundation

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M1.1 Create Python project skeleton | `complete` | AC-01 | Test: `uv run pytest --cov=enterprise_agent tests/test_cli.py` - PASS (2 tests, 100% coverage). Validation: Ruff, mypy, `uv lock --check`, and `uv run enterprise-agent version` - PASS. Commit: `a0389dc` pushed to `origin/main`. |
| M1.2 Add local runtime configuration | `complete` | AC-01 | Test: `uv run pytest --cov=enterprise_agent tests/test_config.py tests/test_cli.py` - PASS (7 tests, 100% coverage). Validation: Ruff, mypy, lock check, and secret-safe installed `config-check` command - PASS. Commit: `0a9b80e` pushed to `origin/main`. |
| M1.3 Add PostgreSQL Compose service | `complete` | AC-01 | Test: `uv run pytest --cov=enterprise_agent` - PASS (9 tests, 100% coverage). Validation: Ruff, mypy, Compose config, and live `pg_isready` - PASS. Commit: `eeea5c7` pushed to `origin/main`. |
| M1.4 Add migration plumbing | `complete` | AC-01 | Test: `tests/test_migrations.py::test_baseline_migration_applies_to_a_clean_compose_database` - PASS.<br>Evidence: a freshly recreated private Compose database upgrades to revision `20260825_0001`; `make migrate` reruns safely at head.<br>Commit: `84e25e4` pushed to `origin/main`. |
| M1.5 Add command and validation targets | `complete` | AC-01 | Test: `tests/test_make_targets.py` and `make test-critical` - PASS.<br>Evidence: all required targets parse and the critical suite runs independently; `make verify` completes from this checkout without LLM credentials.<br>Commit: `15a355c` pushed to `origin/main`. |
| M1.6 Establish test harness | `complete` | AC-01 | Test: `tests/test_async_harness.py`, `tests/test_test_harness.py`, and `tests/test_migrations.py` - PASS.<br>Evidence: unit, contract, integration, critical, async, and isolated-PostgreSQL test layers execute; every disposable database is removed after use.<br>Commit: `3fe46b7` pushed to `origin/main`. |

### M2 - Domain, persistence, seed data, and scoped providers

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M2.1 Define domain contracts | `complete` | AC-02, AC-06 | Test: `tests/test_domain_contracts.py` - PASS (3 tests).<br>Evidence: typed IDs, validated money/date values, immutable actor/evidence/plan/approval/workflow/tool/task/audit contracts, and source-version bindings are available to later adapters and gates.<br>Commit: `fcafe13` pushed to `origin/main`. |
| M2.2 Define ports | `complete` | AC-03, AC-12 | Test: `tests/test_ports.py` - PASS.<br>Evidence: ERP, mail, calendar, identity, clock, audit, scheduler, and LLM protocols have explicit typed contracts and are independently fakeable without concrete dependencies.<br>Commit: `d490053` pushed to `origin/main`. |
| M2.3 Create minimal schema migration | `complete` | AC-01, AC-02 | Test: `tests/test_schema_migration.py` - PASS.<br>Evidence: a clean PostgreSQL database has all identity, ERP, communications, agent-state, workflow, scheduler, and audit tables with UUID keys, foreign keys, and planned query indexes.<br>Commit: `bcdf41d` pushed to `origin/main`. |
| M2.4 Add integrity constraints and versions | `complete` | AC-04, AC-06, AC-09, AC-10 | Test: `tests/test_schema_migration.py::test_integrity_migration_enforces_dedupe_idempotency_and_source_versions` - PASS.<br>Evidence: PostgreSQL enforces attention, workflow-step, and scheduler uniqueness; mutable supplier/PO/lot/inventory/allocation inputs have positive source versions for later freshness gates.<br>Commit: `3722987` pushed to `origin/main`. |
| M2.5 Implement reset and seed | `complete` | AC-02 | Test: `tests/test_seed.py::test_reset_and_seed_create_repeatable_scenario_and_edge_case_data` - PASS.<br>Evidence: a guarded local-only reset and deterministic seed create both scenarios, the delayed partial PO, current/superseded shipment updates, supplier eligibility traps, backup-routing availability, and quality coverage/no-coverage data; `make seed` succeeds without LLM credentials.<br>Commit: `9018d50` pushed to `origin/main`. |
| M2.6 Implement identity adapter | `complete` | AC-03, AC-06, AC-07 | Test: `tests/test_identity_adapter.py` - PASS (2).<br>Evidence: one explicit PostgreSQL join resolves immutable actor role, scopes, plant visibility, backup approver, and currency-normalized approval limit; unknown identities fail closed.<br>Commit: `c4a1067` pushed to `origin/main`. |
| M2.7 Implement scoped providers | `complete` | AC-03 | Test: `tests/test_scoped_providers.py` - PASS (3).<br>Evidence: fixed PostgreSQL provider queries enforce actor scope, plant, mailbox-recipient, calendar-owner, record-type, and optional-ID boundaries before evidence construction; unsupported types fail closed.<br>Commit: `c3837da` pushed to `origin/main`. |
| M2.8 Test seed and provider boundary | `complete` | AC-02, AC-03 | Test: `tests/test_seed.py::test_reset_and_seed_create_repeatable_scenario_and_edge_case_data` - PASS (1).<br>Evidence: the repeatable company fixture has exact supplier/PO/mail/quality edge cases, while real provider queries allow Dana's purchasing context and block Quinn's purchasing ERP/mail/calendar reads and Avery's access to Dana's mailbox/calendar.<br>Commit: `e1724c6` pushed to `origin/main`. |

### M3 - Safe planning core with fake LLM

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M3.1 Implement attention lifecycle and dedupe | `complete` | AC-04 | Test: `tests/test_attention_lifecycle.py` - PASS (6).<br>Evidence: a canonical key binds detector, part, production order, inventory version, and start date; PostgreSQL atomically creates or reuses one attention item, writes every duplicate attempt to audit, and permits only forward lifecycle transitions.<br>Commit: `eff7a1c` pushed to `origin/main`. |
| M3.2 Implement stockout detector | `complete` | AC-04 | Test: `tests/test_stockout_detector.py` - PASS (4).<br>Evidence: scoped inventory and current committed production demand are evaluated against safety stock; only positive shortfalls create durable, source-version-bound Scenario A attention registrations, including the seeded 90-unit risk.<br>Commit: `8252ed0` pushed to `origin/main`. |
| M3.3 Build authorized context bundle | `complete` | AC-05 | Test: `tests/test_scenario_a_context.py` - PASS (4).<br>Evidence: actor identity is re-resolved through its port; the bundle contains only typed authorized ERP, mail, and next-day calendar evidence with immutable source timestamps and versions, rejects mismatched attention snapshots, and selects only the newest valid PO shipment update.<br>Commit: `6fac4d7` pushed to `origin/main`. |
| M3.4 Implement candidate filtering | `complete` | AC-05 | Test: `tests/test_supplier_candidates.py` - PASS (4).<br>Evidence: deterministic policy admits only approved, visible plant/part-matching alternates that arrive by production start; it rejects the original supplier, too-slow, wrong-part/plant, unapproved, and malformed suppliers while retaining every exclusion reason.<br>Commit: `a60655c` pushed to `origin/main`. |
| M3.5 Define planning schemas and fake LLM | `complete` | AC-05 | Test: `tests/test_planning_schemas.py` - PASS (7).<br>Evidence: strict Pydantic schemas permit only explainable `NO_ACTION`, `MANUAL_REVIEW`, or positive-quantity `ENTER_WORKFLOW(po_reroute:v1)` outcomes; the scenario-keyed fake LLM is deterministic and returns safe manual review when unconfigured.<br>Commit: `68da3e3` pushed to `origin/main`. |
| M3.6 Implement gate and policy | `complete` | AC-06 | Test: `tests/test_scenario_a_gate.py` - PASS (17 tests; gate module 100% line and branch coverage).<br>Evidence: the non-executing gate rechecks exact source versions, required read/write scopes, PO IDs and remainder, supplier eligibility, price/currency, and approval limit; only a valid reroute becomes pending human approval.<br>Commit: `dd4d21e` pushed to `origin/main`. |
| M3.7 Implement immutable plan and approval records | `complete` | AC-06 | Test: `tests/test_plan_approvals.py` - PASS (15 tests; plan/approval modules 100% line and branch coverage).<br>Evidence: fresh gate-approved intent is atomically persisted with a deterministic hash binding original approver, workflow parameters, source versions, policy version, and expiry; stale, expired, mismatched, tampered, and raced plans cannot be approved.<br>Commit: `290669a` pushed to `origin/main`. |
| M3.8 Test safe planning | `complete` | AC-04, AC-05, AC-06 | Test: `tests/test_safe_planning.py` - PASS (critical seeded end-to-end flow).<br>Evidence: duplicate detection yields one attention; only Supplier Z survives deterministic filtering; the fake recommendation becomes exactly one immutable pending approval and neither it nor the approval decision modifies an ERP PO or starts a workflow. Existing focused contracts cover invalid/slow suppliers, missing scope, stale source evidence, and model-schema rejection.<br>Commit: `45d314d` pushed to `origin/main`. |

### M4 - Declared workflow, idempotent tools, and crash recovery

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M4.1 Define tool catalog | `complete` | AC-06, AC-09 | Test: `tests/test_tool_catalog.py` - PASS (11; `tools.py` 100% coverage).<br>Validation: `make test-critical` - PASS (11 selected); `make verify` - PASS (101 tests, 96.30% coverage, clean migration).<br>Evidence: the closed catalog declares exactly six reviewed tools with strict Pydantic inputs, per-tool scopes, compensation metadata, opaque workflow-step idempotency keys, and no effect implementation.<br>Commit: `55ca577` pushed to `origin/main`. |
| M4.2 Implement `po_reroute:v1` | `complete` | AC-08 | Test: `tests/test_po_reroute_workflow.py` - PASS (4; `workflows.py` 100% coverage).<br>Validation: `make test-critical` - PASS (12 selected); `make verify` - PASS (105 tests, 96.39% coverage, clean migration).<br>Evidence: the immutable `po_reroute:v1` declaration fixes six reviewed steps in order—two read-only guards followed by replacement PO, original PO, notification, and arrival-check tools—and only exact registered name/version resolution is permitted.<br>Commit: `3f01408` pushed to `origin/main`. |
| M4.3 Persist workflow and step state | `complete` | AC-08, AC-09 | Test: `tests/test_workflow_state.py` and `tests/test_schema_migration.py` - PASS (7; new stager/adapter modules 100% coverage).<br>Validation: `make test-critical` - PASS (14 selected); `make verify` - PASS (110 tests, 96.64% coverage, clean migration).<br>Evidence: plan-bound `po_reroute:v1` snapshots persist all six pending steps, plan/source/hash input snapshot, status, counters, result/error slots, timestamps, idempotency slots, and lease fields atomically; PostgreSQL permits only one workflow instance per plan.<br>Commit: `e498840` pushed to `origin/main`. |
| M4.4 Implement workflow executor | `complete` | AC-06, AC-08, AC-09 | Test: `tests/test_workflow_executor.py` - PASS (16; executor 100% direct branch coverage), plus workflow-state and approval-adapter contracts - PASS.<br>Validation: `make test-critical` - PASS (19 selected); `make verify` - PASS (129 tests, 96.78% coverage, clean migration).<br>Evidence: before a durable claim, the executor revalidates the exact approved immutable plan/hash, expiry, current actor write scopes, source versions, persisted declaration, and step snapshot. PostgreSQL atomically leases one pending/expired workflow (or renews its owner lease) and completes only the next declared read-only guard; no ERP write occurs in M4.4.<br>Commit: `ae7e132` pushed to `origin/main`. |
| M4.5 Implement external-style tool boundaries | `complete` | AC-09, AC-11 | Test: focused executor/state/tool-adapter contracts - PASS (37), including durable started-before-effect ordering, all four declared effects, replay, stale/lost-lease rejection, and an approved-but-too-slow supplier; seeded PostgreSQL flow - PASS.<br>Validation: `make verify` - PASS (144 tests, 94.29% coverage, clean migration).<br>Evidence: a workflow step commits `running` plus its opaque stable key before the concrete tool boundary; the tool independently enforces its scope, commits its ERP/mail/scheduler effect plus an idempotency-journal result, and a later transaction records result/cursor progression. The final transition marks the workflow succeeded; replay returns the original external result without duplicating the replacement PO.<br>Commit: `774636d` pushed to `origin/main`. |
| M4.6 Implement compensation | `complete` | AC-09 | Test: focused executor, workflow-state, and tool-adapter compensation contracts - PASS (43), plus seeded PostgreSQL compensation flow - PASS.<br>Validation: `make verify` - PASS (150 tests, 92.06% coverage, clean migration).<br>Evidence: a terminal tool failure persists the failed step and retains its lease; only completed effects are reversed in LIFO order using new stable compensation keys. PostgreSQL cancels the replacement PO and Tuesday task, restores the original PO only when its version/state still match, sends a correction notification, and marks both original provider journals and workflow steps compensated without re-running an effect.<br>Commit: `6a0ae64` pushed to `origin/main`. |
| M4.7 Add crash injection | `complete` | AC-09 | Test: `tests/test_workflow_executor.py::test_crash_after_replacement_effect_restarts_with_the_same_started_key` and seeded PostgreSQL crash/restart flow - PASS.<br>Validation: `make verify` - PASS (151 tests, 91.98% coverage, clean migration).<br>Evidence: an explicit injector raises only after replacement-PO provider success and before local cursor completion. After the lease expires, a new worker reclaims the exact `running` step, reconstructs its original stable key, replays the provider journal, and advances without a second local start or replacement PO.<br>Commit: `98546f1` pushed to `origin/main`. |
| M4.8 Test workflow invariants | `not_started` | AC-08, AC-09 | - |

### M5 - Approval routing, durable scheduling, and audit explanation

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M5.1 Implement mutable demo clock | `not_started` | AC-07, AC-10 | - |
| M5.2 Implement durable task storage and claiming | `not_started` | AC-10 | - |
| M5.3 Implement end-of-day approval routing | `not_started` | AC-07 | - |
| M5.4 Implement Tuesday arrival task | `not_started` | AC-10 | - |
| M5.5 Implement append-only audit writer | `not_started` | AC-11 | - |
| M5.6 Implement `audit explain` | `not_started` | AC-11 | - |
| M5.7 Test timing and audit behavior | `not_started` | AC-07, AC-10, AC-11 | - |

### M6 - Scenario B and bounded free-form execution

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M6.1 Add quality-lot data and quality actor | `not_started` | AC-02, AC-12 | - |
| M6.2 Implement quality detector and context | `not_started` | AC-12 | - |
| M6.3 Define Scenario B proposal schemas | `not_started` | AC-12 | - |
| M6.4 Implement Scenario B tools | `not_started` | AC-06, AC-09, AC-12 | - |
| M6.5 Reuse core control plane | `not_started` | AC-12 | - |
| M6.6 Test Scenario B | `not_started` | AC-12 | - |

### M7 - OpenAI, Claude, and OpenRouter profiles

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M7.1 Finalize LLM adapter contract | `not_started` | AC-13 | - |
| M7.2 Implement OpenAI adapter | `not_started` | AC-13 | - |
| M7.3 Implement Claude adapter | `not_started` | AC-13 | - |
| M7.4 Implement OpenRouter adapter | `not_started` | AC-13 | - |
| M7.5 Add profile configuration and selection | `not_started` | AC-13 | - |
| M7.6 Add mocked adapter contract tests | `not_started` | AC-13 | - |
| M7.7 Add manual smoke commands | `not_started` | AC-13 | - |
| M7.8 Verify live provider paths | `not_started` | AC-13 | - |

### M8 - Documentation, transcript, and final verification

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M8.1 Write README | `not_started` | AC-14 | - |
| M8.2 Write `MODEL.md` | `not_started` | AC-02, AC-14 | - |
| M8.3 Write `DESIGN.md` | `not_started` | AC-14 | - |
| M8.4 Create recorded Scenario A transcript | `not_started` | AC-11, AC-14 | - |
| M8.5 Finalize deterministic demo command | `not_started` | AC-14 | - |
| M8.6 Run final validation suite | `not_started` | AC-01, AC-13, AC-14 | - |
| M8.7 Review requirements coverage | `not_started` | AC-01 through AC-14 | - |

## Acceptance-criteria status

| Criterion | Status | Evidence |
|---|---|---|
| AC-01 Environment and validation | `in_progress` | Compose health, clean migration, command-target, test-harness, and local reset/seed evidence are passing; final delivery verification remains M8. |
| AC-02 Seed model and edge cases | `complete` | The deterministic local-only seed is exhaustively asserted to contain both scenarios, roles/scopes, authorized mailboxes, a partial changed delayed PO, superseded/current shipment updates, eligible/too-slow/wrong-part suppliers, backup availability, and quality coverage/no-coverage cases. |
| AC-03 Provider authorization boundary | `complete` | Real PostgreSQL provider assertions prove Dana's authorized purchasing context is visible while Quinn cannot read purchasing ERP/mail/calendar data and Avery cannot read Dana's mailbox or calendar; providers additionally enforce actor context, scopes, plant visibility, record types, and record IDs. |
| AC-04 Proactive detection and dedupe | `complete` | Unit and seeded PostgreSQL contracts prove the scoped detector emits only positive, source-version-bound stockout risks; canonical keys atomically deduplicate repeats and retain audit attempts. The M3.8 end-to-end test proves the duplicate Scenario A trigger reaches the same one pending approval. |
| AC-05 Authorized safe planning | `complete` | Typed context re-resolves identity and preserves only authorized ERP, mail, and calendar evidence; deterministic filtering, strict schemas, and the fake LLM leave only safe bounded outcomes. The seeded M3.8 run proves the entire authorized path admits only Supplier Z and produces one gate-approved immutable pending plan. |
| AC-06 Gate, scope, policy, and approval | `in_progress` | The Scenario A gate fails closed on stale evidence, scope, PO parameter/remainder, supplier, pricing, currency-authority, and approval-limit violations. A fresh gate-approved plan is now immutable in PostgreSQL and bound to its policy/source/expiry hash plus pending approval; stale, expired, altered, mismatched, or raced records are unapprovable. M4.5 now rechecks these conditions before the first external effect and makes every concrete tool independently enforce its own scope and current ERP safety invariants; routing remains M5. |
| AC-07 Backup approval routing | `in_progress` | Seeded Dana identity exposes a backup approver and next-day out-of-office evidence; end-of-day routing behavior remains M5. |
| AC-08 Fixed Scenario A workflow | `in_progress` | `po_reroute:v1` resolves only to its immutable six-step declaration and a plan-bound durable snapshot persists that exact ordered state. M4.5 executes every declared effect only in sequence: replacement PO, source-PO cancellation, production notice, then next-Tuesday arrival task; crash recovery and invariant testing remain M4. |
| AC-09 Idempotency, compensation, and recovery | `in_progress` | The M4.1 catalog derives opaque, stable keys from workflow instance, step, declared tool, and canonical typed input, and assigns compensation metadata to every reviewed effect. M4.5 adds the independently committed invocation journal and started/result workflow transactions; replays return the original effect result with no duplicate replacement PO. Compensation and injected-crash recovery remain M4. |
| AC-10 Durable scheduler and Tuesday loop | `in_progress` | M4.5 creates the idempotent durable `arrival_check` record at the next Tuesday 09:00 with the replacement PO only; task claiming and Tuesday receipt handling remain M5. |
| AC-11 Append-only audit reconstruction | `in_progress` | M4.5 persists an external tool invocation journal and workflow started/result records. The formal append-only audit writer and human-readable reconstruction remain M5. |
| AC-12 Scenario B free-form path | `in_progress` | The reusable scoped-provider, planner, audit, and scheduler boundaries are established; Scenario B behavior remains M6. |
| AC-13 Three-provider contract | `not_started` | - |
| AC-14 Demo, documentation, and transcript | `not_started` | - |

## Activity log

| Date (UTC) | Task | Change | Validation / evidence | Commit / push |
|---|---|---|---|---|
| 2026-08-25 | M0.1 | Created task register, acceptance criteria, universal test policy, and status protocol; removed local planning artifacts from Git tracking. | Validation: plan/status task count and Markdown diff checks - PASS. | `2a4fec1` pushed to `origin/main`; test-policy status update committed and pushed with this ledger entry. |
| 2026-08-25 | M1.1 | Started the Python package and CLI contract. | Test: `tests/test_cli.py::test_version_command_reports_package_version` - RED due solely to missing `enterprise_agent.cli`, as intended. | `3858187` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M1.1 | Completed the Python package skeleton and installed CLI version command. | Test: `uv run pytest --cov=enterprise_agent tests/test_cli.py` - PASS (2 tests, 100% coverage); Ruff, mypy, lock, and installed-command checks - PASS. | `a0389dc` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M1.2 | Started local runtime configuration and secret-safe validation. | Test: `tests/test_config.py` - RED due solely to missing `enterprise_agent.config`, as intended. | `9618670` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M1.2 | Completed secret-safe runtime configuration and CLI validation. | Test: `uv run pytest --cov=enterprise_agent tests/test_config.py tests/test_cli.py` - PASS (7 tests, 100% coverage); Ruff, mypy, lock, and installed `config-check` command - PASS. | `0a9b80e` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M1.3 | Started the PostgreSQL Compose contract. | Test: `tests/test_compose.py` - RED because `docker-compose.yml` is absent and the example URL does not target the Compose service, as intended. | `7e5a631` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M1.3 | Completed private durable PostgreSQL Compose service. | Test: `uv run pytest --cov=enterprise_agent` - PASS (9 tests, 100% coverage); Ruff, mypy, Compose config, and live `pg_isready` - PASS. | `eeea5c7` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M1.4 | Started the database migration contract. | Test: `tests/test_migrations.py::test_baseline_migration_applies_to_a_clean_compose_database` - RED because the Compose migration runner does not exist yet, as intended. | `6468886` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M1.4 | Completed Alembic migration plumbing and the empty baseline. | Test: `tests/test_migrations.py::test_baseline_migration_applies_to_a_clean_compose_database` - PASS; full suite (10 tests, 100% coverage), Ruff, mypy, lock check, Compose config, and repeatable `make migrate` - PASS. | `84e25e4` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M1.5 | Started the developer-command contract. | Test: `tests/test_make_targets.py` - RED because the validation and critical-test Make targets do not exist yet, as intended. | `5186cbe` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M1.5 | Completed standard developer commands and the focused critical-test target. | Test: `tests/test_make_targets.py` - PASS (2 tests); `make test-critical` - PASS (1 selected); `make verify` - PASS (12 tests, 100% coverage). | `15a355c` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M1.6 | Started the reusable test-harness contract. | Test: `tests/test_test_harness.py` and `tests/test_async_harness.py` - RED because the disposable-database fixture and async marker/plugin support are absent, as intended. | `57cb9d8` and `1f2033c` RED checkpoints, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M1.6 | Completed the reusable test harness with async support and isolated database fixtures. | Test: focused harness tests - PASS (4); unit layer - PASS (9); integration layer - PASS (2); contract layer - PASS (4); `make test-critical` - PASS (1 selected); `make verify` - PASS (15 tests, 100% coverage). | `3fe46b7` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M2.1 | Started the stable domain-contract boundary. | Test: `tests/test_domain_contracts.py` - RED because the `enterprise_agent.domain` contract package does not exist yet, as intended. | `e14abb4` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M2.1 | Completed immutable core domain contracts. | Test: `tests/test_domain_contracts.py` - PASS (3 tests); `make test-critical` - PASS (1 selected); `make verify` - PASS (18 tests, 99.62% coverage). | `fcafe13` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M2.2 | Started the provider and application-port contract. | Test: `tests/test_ports.py` - RED because the `enterprise_agent.ports` module does not exist yet, as intended. | `331d044` and `21f99e6` RED checkpoints, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M2.2 | Completed provider and control-plane protocol contracts. | Test: `tests/test_ports.py` - PASS; `make test-critical` - PASS (1 selected); `make verify` - PASS (19 tests, 99.69% coverage). | `d490053` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M2.3 | Started the core PostgreSQL schema contract. | Test: `tests/test_schema_migration.py` - RED because the baseline database lacks the required domain tables, foreign keys, and indexes. | `e98fa7c` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M2.3 | Completed the first durable domain schema migration. | Test: `tests/test_schema_migration.py` - PASS; `make test-critical` - PASS (1 selected); `make verify` - PASS (20 tests, 99.69% coverage). | `bcdf41d` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M2.4 | Started integrity and source-version migration contract. | Test: `tests/test_schema_migration.py::test_integrity_migration_enforces_dedupe_idempotency_and_source_versions` - RED because the current schema has no inventory/allocation tables, source-version columns, or unique integrity constraints. | `c44cb4a` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M2.4 | Completed integrity constraints and material-source versioning. | Test: `tests/test_schema_migration.py` - PASS (2); `make test-critical` - PASS (1 selected); `make verify` - PASS (21 tests, 99.69% coverage). | `3722987` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M2.5 | Started deterministic reset and seed contract. | Test: `tests/test_seed.py` - RED because no reset/seed implementation or seeded scenario records exist. | `40edbf4`, `d1a9630`, and `a79410c` RED checkpoints, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M2.5 | Completed guarded reset and deterministic scenario seed. | Test: `tests/test_seed.py` - PASS (4); `make seed` - PASS; `make test-critical` - PASS (1 selected); `make verify` - PASS (27 tests, 98.18% coverage). | `9018d50` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M2.6 | Started seeded identity-adapter contract. | Test: `tests/test_identity_adapter.py` - RED because no PostgreSQL-backed identity adapter exists. | `ad87745` RED checkpoint, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M2.6 | Completed seeded PostgreSQL identity adapter. | Test: `tests/test_identity_adapter.py` - PASS (2); seeded Compose adapter assertion - PASS; `make test-critical` - PASS (1 selected); `make verify` - PASS (29 tests, 98.28% coverage). | `c4a1067` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M2.7 | Started scoped ERP, mail, and calendar provider contracts. | Test: `tests/test_scoped_providers.py` and updated schema contract - RED because no provider adapters or user-mailbox column exist. | `74c1633` and `3a98971` RED checkpoints, pushed to `origin/main` with the GREEN task completion. |
| 2026-08-25 | M2.7 | Completed scoped PostgreSQL ERP, mail, and calendar providers. | Test: `tests/test_scoped_providers.py` - PASS (3), including a seeded PostgreSQL empty-filter regression; seeded Compose provider assertion - PASS; `make test-critical` - PASS (1 selected); `make verify` - PASS (32 tests, 97.79% coverage). | `c3837da` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M2.8 | Started end-to-end seed and provider-boundary regression coverage. | Test: `tests/test_seed.py::test_reset_and_seed_create_repeatable_scenario_and_edge_case_data` - RED because the quality actor can currently read a purchasing PO through the ERP provider. | `42ddab4` pushed to `origin/main`. |
| 2026-08-25 | M2.8 | Completed scenario fixture and provider-boundary regression coverage. | Test: `tests/test_seed.py::test_reset_and_seed_create_repeatable_scenario_and_edge_case_data` - PASS (1); `make test-critical`, `uv lock --check`, and Compose config - PASS; full 32-test suite, format, lint, and type checks - PASS (97.79% coverage); `make migrate` and `make demo` - PASS. | `e1724c6` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M3.1 | Started durable attention-item lifecycle and deduplication coverage. | Test: `tests/test_attention_lifecycle.py` - RED because the Scenario A trigger, attention adapter, lifecycle policy, and atomic audit persistence do not exist. | `8d570cd` pushed to `origin/main`. |
| 2026-08-25 | M3.1 | Completed durable attention lifecycle and deduplication. | Test: `tests/test_attention_lifecycle.py` - PASS (6); `make test-critical` - PASS (2); formatting, lint, type checks, lock, and Compose config - PASS; all 38 tests passed in non-integration/integration batches at 96.97% coverage; `make migrate` and `make demo` - PASS. | `eff7a1c` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M3.2 | Started proactive stockout-detector coverage. | Test: `tests/test_stockout_detector.py` - RED because the application-level detector and its evidence-to-attention calculation do not exist. | `4ccbbe1` pushed to `origin/main`. |
| 2026-08-25 | M3.2 | Completed deterministic proactive stockout detection. | Test: `tests/test_stockout_detector.py` - PASS (4), including the seeded PostgreSQL 90-unit shortfall; non-integration suite - PASS (35, 95.97% coverage); `make test-critical` - PASS (3); format, lint, mypy, `make migrate`, and `make demo` - PASS. | `8252ed0` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M3.3 | Started typed authorized Scenario A context coverage. | Test: `tests/test_scenario_a_context.py` - RED because the context assembler and its source-bound bundle do not exist. | `30dac04` pushed to `origin/main`. |
| 2026-08-25 | M3.3 | Completed typed authorized Scenario A context assembly. | Test: `tests/test_scenario_a_context.py` - PASS (4), including the seeded PostgreSQL path and newest-valid-shipment selection; non-integration suite - PASS (38, 94.21% coverage); `make test-critical` - PASS (4); format, lint, mypy, `make migrate`, and `make demo` - PASS. | `6fac4d7` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M3.4 | Started deterministic supplier-candidate filtering coverage. | Test: `tests/test_supplier_candidates.py` - RED because the candidate filter and auditable exclusion contracts do not exist. | `0cef445` pushed to `origin/main`. |
| 2026-08-25 | M3.4 | Completed deterministic supplier-candidate filtering. | Test: `tests/test_supplier_candidates.py` - PASS (4), including seeded Supplier Z-only eligibility; non-integration suite - PASS (41, 94.46% coverage); `make test-critical` - PASS (5); format, lint, mypy, `make migrate`, and `make demo` - PASS. | `a60655c` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M3.5 | Started bounded Scenario A planning-schema and fake-LLM coverage. | Test: `tests/test_planning_schemas.py` - RED because the recommendation schemas, parser, and deterministic fake adapter do not exist. | `2d36767` pushed to `origin/main`. |
| 2026-08-25 | M3.5 | Completed bounded Scenario A recommendation schemas and deterministic fake LLM. | Test: `tests/test_planning_schemas.py` - PASS (7), including seeded authorized-context and Supplier Z recommendation flow; non-integration suite - PASS (47, 94.79% coverage); `make test-critical` - PASS (6); format, lint, mypy, lock, `make migrate`, and `make demo` - PASS. | `68da3e3` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M3.6 | Started Scenario A gate and policy enforcement. | Test: `tests/test_scenario_a_gate.py` contract is being added before implementation. | Pending RED checkpoint. |
| 2026-08-25 | M3.6 | Completed non-executing Scenario A policy and approval gate. | Test: `tests/test_scenario_a_gate.py` - PASS (17, direct gate 100%); non-integration suite - PASS (64, 95.58% coverage); `make test-critical` - PASS (7); format, lint, mypy, clean migration, and current demo command - PASS. | `dd4d21e` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M3.7 | Started immutable Scenario A plan and approval persistence. | Test: `tests/test_plan_approvals.py` contract is being added before implementation. | Pending RED checkpoint. |
| 2026-08-25 | M3.7 | Completed immutable, hash-bound Scenario A plan and approval persistence. | Test: `tests/test_plan_approvals.py` - PASS (15, direct plan/approval modules 100%); PostgreSQL immutability/CAS contract and schema migration tests - PASS; non-integration suite - PASS (78, 96.01% coverage); `make test-critical` - PASS (8); format, lint, mypy, clean migration, and current demo command - PASS. | `290669a` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M3.8 | Started cross-boundary Scenario A safe-planning verification. | Test: `tests/test_safe_planning.py` critical seeded flow is being added; this verification-only task requires no production-code RED cycle. | Pending test validation. |
| 2026-08-25 | M3.8 | Completed cross-boundary Scenario A safe-planning verification. | Test: `tests/test_safe_planning.py` - PASS; critical suite - PASS (9); non-integration suite - PASS (78, 96.01% coverage); format, lint, mypy, clean migration, and current demo command - PASS. | `45d314d` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M4.1 | Started the declared tool-catalog contract. | Test: `tests/test_tool_catalog.py` is being added before implementation. | Pending RED checkpoint. |
| 2026-08-25 | M4.1 | Completed the declared, non-executing tool catalog. | Test: `tests/test_tool_catalog.py` - PASS (11; `tools.py` 100% direct coverage); `make test-critical` - PASS (11); `make verify` - PASS (101, 96.30% coverage, migration). | `55ca577` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M4.2 | Started the fixed `po_reroute:v1` workflow-definition contract. | Test: `tests/test_po_reroute_workflow.py` is being added before implementation. | Pending RED checkpoint. |
| 2026-08-25 | M4.2 | Completed the immutable `po_reroute:v1` declaration. | Test: `tests/test_po_reroute_workflow.py` - PASS (4; `workflows.py` 100% direct coverage); `make test-critical` - PASS (12); `make verify` - PASS (105, 96.39% coverage, migration). | `3f01408` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M4.3 | Started durable workflow-instance and step-state persistence. | Test: `tests/test_workflow_state.py` and workflow-schema assertions are being added before implementation. | Pending RED checkpoint. |
| 2026-08-25 | M4.3 | Completed durable workflow-instance and step-state persistence. | Test: workflow-state/schema contracts - PASS (7; new stager/adapter modules 100% coverage); `make test-critical` - PASS (14); `make verify` - PASS (110, 96.64% coverage, migration). | `e498840` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M4.4 | Started the workflow-executor safety contract. | Test: `tests/test_workflow_executor.py` - RED because no executor exists to revalidate approval, source freshness, write scopes, or safely claim and advance declared guards. | Pending RED checkpoint. |
| 2026-08-25 | M4.4 | Completed approved declared-workflow claiming and guard execution. | Test: executor, workflow-state, and plan/approval persistence contracts - PASS (39 focused); critical suite - PASS (19); `make verify` - PASS (129 tests, 96.78% coverage, clean migration). | `ae7e132` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M4.5 | Started external-style tool-boundary coverage. | Test: `tests/test_workflow_executor.py` contract is being added before implementation. | Pending RED checkpoint. |
| 2026-08-25 | M4.5 | Completed external-style ERP, production-message, and scheduler tool boundaries. | Test: direct executor/state/tool contracts plus seeded PostgreSQL flow - PASS; approved-but-too-slow supplier, stable-key replay, and one replacement-PO behavior covered. `make verify` - PASS (144 tests, 94.29% coverage, clean migration). | `774636d` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M4.6 | Started reverse-compensation coverage. | Test: `tests/test_workflow_executor.py` contract is being added before implementation. | Pending RED checkpoint. |
| 2026-08-25 | M4.6 | Completed terminal-failure compensation. | Test: focused executor/workflow-state/tool-adapter compensation contracts - PASS (43); seeded PostgreSQL compensation flow - PASS; `make verify` - PASS (150 tests, 92.06% coverage, clean migration). | `6a0ae64` pushed to `origin/main`; status completion record pending this commit. |
| 2026-08-25 | M4.7 | Started deterministic crash/restart coverage. | Test: `tests/test_workflow_executor.py` contract is being added before implementation. | Pending RED checkpoint. |
| 2026-08-25 | M4.7 | Completed deterministic replacement-PO crash/restart recovery. | Test: focused crash/restart unit plus seeded PostgreSQL journal replay - PASS; `make verify` - PASS (151 tests, 91.98% coverage, clean migration). | `98546f1` pushed to `origin/main`; status completion record pending this commit. |
