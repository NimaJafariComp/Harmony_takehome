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
2. Update the matching row below with its state, concise evidence, and the acceptance criteria it advances.
3. Append a dated row to the activity log; do not delete prior entries.
4. Commit the completed task using `type(scope): summary [Task-ID]`.
5. Push the commit only after its validation passes. Never push credentials, `.env`, local plans, or assignment PDFs.

Recommended commit types are `feat`, `fix`, `test`, `docs`, `build`, `chore`, and `refactor`. If a task changes behavior and tests, keep the code and its focused tests in one commit.

## Current delivery dashboard

| Field | Value |
|---|---|
| Overall status | `not_started` |
| Current milestone | M1 - Foundation |
| Current task | M1.1 - Create the Python project skeleton |
| Required task progress | 0 / 58 complete |
| Acceptance criteria progress | 0 / 14 satisfied |
| Last validated commit | `2a4fec1` |
| Last push | `2a4fec1` to `origin/main` |
| Blocking issue | None |

## Required task register

### M1 - Foundation

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M1.1 Create Python project skeleton | `not_started` | AC-01 | - |
| M1.2 Add local runtime configuration | `not_started` | AC-01 | - |
| M1.3 Add PostgreSQL Compose service | `not_started` | AC-01 | - |
| M1.4 Add migration plumbing | `not_started` | AC-01 | - |
| M1.5 Add command and validation targets | `not_started` | AC-01 | - |
| M1.6 Establish test harness | `not_started` | AC-01 | - |

### M2 - Domain, persistence, seed data, and scoped providers

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M2.1 Define domain contracts | `not_started` | AC-02, AC-06 | - |
| M2.2 Define ports | `not_started` | AC-03, AC-12 | - |
| M2.3 Create minimal schema migration | `not_started` | AC-01, AC-02 | - |
| M2.4 Add integrity constraints and versions | `not_started` | AC-04, AC-06, AC-09, AC-10 | - |
| M2.5 Implement reset and seed | `not_started` | AC-02 | - |
| M2.6 Implement identity adapter | `not_started` | AC-03, AC-06, AC-07 | - |
| M2.7 Implement scoped providers | `not_started` | AC-03 | - |
| M2.8 Test seed and provider boundary | `not_started` | AC-02, AC-03 | - |

### M3 - Safe planning core with fake LLM

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M3.1 Implement attention lifecycle and dedupe | `not_started` | AC-04 | - |
| M3.2 Implement stockout detector | `not_started` | AC-04 | - |
| M3.3 Build authorized context bundle | `not_started` | AC-05 | - |
| M3.4 Implement candidate filtering | `not_started` | AC-05 | - |
| M3.5 Define planning schemas and fake LLM | `not_started` | AC-05 | - |
| M3.6 Implement gate and policy | `not_started` | AC-06 | - |
| M3.7 Implement immutable plan and approval records | `not_started` | AC-06 | - |
| M3.8 Test safe planning | `not_started` | AC-04, AC-05, AC-06 | - |

### M4 - Declared workflow, idempotent tools, and crash recovery

| Task | Status | Acceptance criteria | Evidence / commit |
|---|---|---|---|
| M4.1 Define tool catalog | `not_started` | AC-06, AC-09 | - |
| M4.2 Implement `po_reroute:v1` | `not_started` | AC-08 | - |
| M4.3 Persist workflow and step state | `not_started` | AC-08, AC-09 | - |
| M4.4 Implement workflow executor | `not_started` | AC-06, AC-08, AC-09 | - |
| M4.5 Implement external-style tool boundaries | `not_started` | AC-09, AC-11 | - |
| M4.6 Implement compensation | `not_started` | AC-09 | - |
| M4.7 Add crash injection | `not_started` | AC-09 | - |
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
| AC-01 Environment and validation | `not_started` | - |
| AC-02 Seed model and edge cases | `not_started` | - |
| AC-03 Provider authorization boundary | `not_started` | - |
| AC-04 Proactive detection and dedupe | `not_started` | - |
| AC-05 Authorized safe planning | `not_started` | - |
| AC-06 Gate, scope, policy, and approval | `not_started` | - |
| AC-07 Backup approval routing | `not_started` | - |
| AC-08 Fixed Scenario A workflow | `not_started` | - |
| AC-09 Idempotency, compensation, and recovery | `not_started` | - |
| AC-10 Durable scheduler and Tuesday loop | `not_started` | - |
| AC-11 Append-only audit reconstruction | `not_started` | - |
| AC-12 Scenario B free-form path | `not_started` | - |
| AC-13 Three-provider contract | `not_started` | - |
| AC-14 Demo, documentation, and transcript | `not_started` | - |

## Activity log

| Date (UTC) | Task | Change | Validation / evidence | Commit / push |
|---|---|---|---|---|
| 2026-08-25 | M0.1 | Created task register, acceptance criteria, and status protocol; removed local planning artifacts from Git tracking. | Staged diff passed whitespace validation; local plan/PDF files remain ignored and present. | `2a4fec1` pushed to `origin/main`. |
