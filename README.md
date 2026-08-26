# Enterprise agent harness

A safe, durable enterprise-agent harness for the purchasing and quality scenarios in the take-home assignment. It is deliberately a control plane, not an autonomous buyer: a large language model (LLM) may propose a typed plan, but deterministic checks decide whether any local effect can occur.

The repository includes all required Scenario A and Scenario B behavior, optional Scenario C, a keyboard-first terminal shell, and a loopback-only local review user interface (UI).

Read [CONNECTOR_SCHEMAS.md](CONNECTOR_SCHEMAS.md) for the scoped ERP, quality, mail, calendar, knowledge, identity, and tool contracts with realistic synthetic seed examples.

## Review the control-plane evidence

| Area | Evidence in this harness |
|---|---|
| Scenario A: projected stockout | Detects a pre-production shortage, gathers current authorized evidence, rejects ineligible suppliers, and stages an immutable reroute plan for approval. |
| Scenario B: quality hold | Detects a held lot, accounts for free capacity and existing commitments, and either proposes an approved reallocation or escalates the uncovered quantity. |
| Scenario C: supplier-risk bulletin (optional) | Correlates an authorized, current bulletin to an open PO and future production demand, then stages a bounded hold-and-notify plan for review. |
| Human control | No plan writes before an authorized approver decides. Approval authority, scopes, plan hashes, policy, and source versions are revalidated. |
| Reliable effects | Workflows have declared steps, stable idempotency keys, crash recovery, compensation, durable scheduling, and an append-only audit ledger. |
| LLM boundaries | OpenAI, Claude, and OpenRouter adapters share schemas. Live calls are explicit, account-configured, and cannot execute a plan or fall back across providers. |
| Recorded walkthrough | [TRANSCRIPT.md](TRANSCRIPT.md) separates reproducible Scenario A/B/C control-plane proof from opt-in no-write OpenAI/Claude scorecards and guarded local proposal receipts. |

## Run the deterministic reviewer demo

Prerequisites: Docker with Compose. The deterministic demo needs no API key or local provider configuration.

```sh
make demo
```

This starts the private Compose PostgreSQL service, applies migrations, resets and seeds only the guarded synthetic database, then runs the unattended safety tour. It does not call an LLM, send email, or create a real business-system effect.

No provider profile or API key is required for this command. A failure from the separately opt-in `llm-smoke` or `llm-evaluate` commands is reported as a sanitized nonzero result and never changes, blocks, or becomes hidden inside the deterministic demo.

For the one-command, fully executed acceptance proof, run `make verify`. It runs the full deterministic test suite, including Scenario A through approval, effects, and Tuesday follow-up; Scenario B; crash/restart and other failure cases; then migration and this reviewer demo. It is an automated proof run rather than an interactive approval session.

To inspect the available stories before resetting the local demo data:

```sh
docker compose --profile tools run --rm app enterprise-agent demo --list
```

To use the terminal shell against the Compose database:

```sh
make tui
```

This is the recommended command for an interviewer or operator. It starts the private database, applies migrations, and opens the keyboard-first Home screen; profile setup, guided demos, guarded live demos, status, audit, smoke checks, and live evaluations are all available there.

### Start here: the recommended reviewer path

Use the terminal UI (TUI) first. It is the primary operator interface and keeps the available actions, their data boundaries, and required confirmations in one keyboard-first flow.

1. Run `make tui`
2. Choose **Guided company demo** to inspect the deterministic Scenario A, B, and C stories without an API key
3. Choose **Normal operator mode** to inspect status, audit evidence, LLM usage, or the fixed live-evaluation catalogue
4. Configure a provider only when you want to run a deliberate smoke probe, no-write evaluation, or guarded local live demo

Use the optional loopback UI only when a browser view helps a reviewer inspect pending approvals, audit history, recovery state, or the demo form. It is not the primary testing path.

## Local development

Install Python 3.12 and [uv](https://docs.astral.sh/uv/), then create the development environment:

```sh
uv sync --all-groups
source .venv/bin/activate
enterprise-agent --help
```

`enterprise-agent` is the operator command. `uv run` remains useful for contributors running checks, but is not required for normal terminal interaction once the environment is activated.

The local-demo mutation guard accepts only the private Compose hostname `db` and database `enterprise_agent`. Run reset, seed, guided-demo, and Scenario C mutations through the Compose commands in this README. Those commands reject every other database target.

## Terminal use

Use the Docker-backed terminal UI for normal interactive use:

```sh
make tui
```

It is keyboard-only and renders bounded panels and tables. In a pipe, or with `--output json`, it never prompts and instead returns the safe command directory. All JSON output uses a versioned, sanitized envelope. Use `--no-color` for plain terminal output.

Do not use host `enterprise-agent` for database-backed operator actions: the guarded synthetic database is deliberately private to Compose at hostname `db`. Direct host commands are useful only for development/read-only commands after activating the `uv` environment. For a scripted database-backed command, run it through Compose instead:

```sh
docker compose --profile tools run --rm app enterprise-agent status
docker compose --profile tools run --rm app enterprise-agent live-demo --list
```

### Home menu

Run `make tui`, then choose one of these modes:

| Key | Mode | What it does | Safety boundary |
|---:|---|---|---|
| 1 | Guided company demo | Runs the deterministic Scenario A, B, and C walkthroughs or the short safety tour. | Resets and seeds only the local synthetic database. It never calls an LLM or executes a business-system effect. |
| 2 | Guarded live local demo | Sends one fixed synthetic Scenario A, B, or C context to one selected provider and stages its proposal for review. | Requires a configured profile, a fixed case, and typing `live` to confirm. It may create only local attention, plan, approval, workflow, schedule, and audit records; it cannot execute a business effect. |
| 3 | Normal operator mode | Opens read paths, provider checks, and the no-write live-evaluation pack. | Each action states whether it is read-only or makes an explicit provider request. |
| 4 | Configure an LLM profile | Selects one provider and reviewed model for this machine. | API-key input is hidden; saving remains explicitly confirmed. |

The guarded live local demo is intentionally separate from the deterministic guided demo. The former makes one real provider request; the latter is the repeatable acceptance proof and requires no provider configuration.

### Which LLM path should I use?

| Path | TUI route | Data and writes | What a result means |
|---|---|---|---|
| Guided company demo | Home → 1 | Resets/seeds fixed local data; deterministic fake planner; no provider call and no business effect. | Proves the application control plane deterministically. |
| Guarded live local demo | Home → 2 | Resets/seeds the fuller local Scenario A, B, or C data; one real provider proposal; may stage only local review records. | Shows whether a provider response can survive schema validation and the deterministic gate before human approval. |
| Live-evaluation pack | Home → 3 → 6 | Compact fixed synthetic briefs; one real provider request per selected case; no database, workflow, audit, or business-system write. | Scores a model against known safe outcomes and grounding checks. It is a model-quality probe, not an execution demo. |

A passing live-evaluation case does **not** guarantee that the guarded live demo will pass. The guarded demo has a richer, scenario-specific context and must produce the exact schema shape needed to stage a local plan. Conversely, a guarded-demo schema rejection does not mean the deterministic application controls failed: it means the provider response was safely rejected before the gate. Use the live-evaluation pack to compare provider behavior; use the guarded live demo to demonstrate the full approval-gated control path.

### Guided company demo cases

Choose Home option 1, then select a case:

| Key | Case | What it proves |
|---:|---|---|
| 1 | Safety tour | Runs the short cross-scenario deterministic proof set in its fixed order. |
| 2 | Scenario A — viable reroute rejects the tempting supplier | An unapproved supplier is excluded even when it is cheaper and faster; the viable reroute stages a pending plan. |
| 3 | Scenario A — recovery after replacement-PO crash | A restart resumes the original effect with its idempotency key and does not create a duplicate replacement purchase order. |
| 4 | Scenario A — newest evidence and hostile email handling | Newer on-schedule supplier evidence produces no action; hostile email content remains evidence only. |
| 5 | Scenario A — Tuesday arrival follow-up | A full receipt resolves attention; a partial or missing receipt produces one guarded follow-up. |
| 6 | Scenario B — quality-lot capacity respects commitments | Released quality inventory is usable only when its free capacity covers demand. |
| 7 | Scenario C — supplier-risk bulletin awaits review | A current authorized bulletin creates a local plan awaiting approval. |

### Guarded live local demo cases

Choose Home option 2, select a provider and one fixed case, then type `live` to confirm:

| Case ID | Scenario | Provider proposal is limited to |
|---|---|---|
| `scenario-a-reroute` | A | A typed reroute recommendation that must pass schema and policy checks before it can stage approval. |
| `scenario-b-quality-hold` | B | A typed quality-lot reallocation and notification recommendation that remains approval-gated. |
| `scenario-c-supplier-risk` | C | A typed hold-and-notify recommendation that remains approval-gated. |

### Normal operator menu

Choose Home option 3 to reach these actions:

| Key | Action | What it does | Safety boundary |
|---:|---|---|---|
| 1 | Control-plane status | Shows pending approvals, workflow state, recovery state, and copyable IDs. | Read-only local database query. |
| 2 | Audit explanation | Reconstructs one durable run from the append-only ledger. Enter the run ID shown in status under **Audit actions**. | Read-only local query. |
| 3 | LLM usage | Shows recorded token and cost totals. | Read-only local ledger query; no provider request. |
| 4 | LLM profile setup | Opens the same hidden-key provider setup flow as Home option 4. | Saving a profile is explicitly confirmed. |
| 5 | Provider smoke check | Sends one fixed, no-business-data probe to the active configured provider. | Explicit provider request; no business-system write. |
| 6 | Live-evaluation catalogue | Lists the 13 fixed synthetic LLM safety cases, then lets you deliberately run one case or all cases. | Provider requests require confirmation. Evaluation has no workflow, ERP, mail, audit, or business-system write. |

After an action finishes, its result stays on screen until you press Enter. Enter `b` from a nested menu to return Home and `q` from Home to quit.

Useful host development/read-only commands after `uv sync --all-groups` and activating `.venv`:

```sh
enterprise-agent guide
enterprise-agent demo --list
enterprise-agent llm-evaluate --list
enterprise-agent --install-completion
```

For interactive demos and all database-backed commands, return to `make tui`. `make demo` uses the intentional unattended form for continuous integration (CI) and repeatable reviewer runs. The guided stories clearly label whether they stage a pending plan or are a fixed acceptance-case walkthrough; neither route calls a live provider or executes business effects automatically.

See [the terminal interaction contract](docs/terminal-interaction-contract.md) for output, keyboard, JSON, cancellation, and error-handling guarantees.

## Scenario walkthroughs

| Scenario | Demonstration | Safety proof |
|---|---|---|
| A: stockout reroute | A late original supplier threatens production. Supplier Z is viable; a cheaper, faster supplier is visibly excluded because it is unapproved. | Candidate filtering, current-evidence selection, immutable approval, fixed `po_reroute:v1` workflow, and no replacement PO before approval. |
| A: recovery and follow-up | The story library covers a crash after replacement-PO creation, a newer on-schedule supplier update, hostile email content, and Tuesday receipt handling. | A replay produces exactly one replacement PO; new evidence yields no unnecessary reroute; email content cannot change system rules; a partial or missing receipt creates one freshness-bound follow-up. |
| B: quality hold | A held lot threatens production. A released lot can cover demand only when its uncommitted capacity is sufficient. | Quality scopes stay distinct from purchasing writes; insufficient or already committed inventory is never presented as complete cover; ambiguous lots go to review. |
| C: supplier risk (optional) | A current, scoped supplier-risk bulletin correlates to an open PO and future production demand. | Bulletin prose is evidence only. The sole automated shape is a typed, approved, freshness-checked local hold-and-notify workflow with compensation. |

The deterministic story catalogue is intentionally small and named. It includes the normal path plus unapproved and too-slow suppliers, stale/current email evidence, malicious text, authority and approver-availability cases, source mutation, restart recovery, Tuesday receipt outcomes, capacity/commitment limits, scope denial, released holds, and unresolved candidate ranking.

### Why Scenario C is included

Scenario C is an optional addition, not a substitute for the required Scenario A and B work. It demonstrates that the same control-plane design can safely handle a different business trigger: a current, authorized supplier-risk bulletin rather than a projected stockout or a quality hold.

It adds three meaningful checks to the demo. The system must correlate the bulletin to the correct open purchase order and production demand, reject superseded or unauthorized bulletins, and treat instruction-like bulletin text as untrusted evidence. When the evidence is valid, the only permitted result is a bounded `HOLD_AND_NOTIFY` plan that still requires approval and freshness revalidation. This shows that a model cannot turn a new kind of text input into arbitrary tool actions.

## Provider setup and live evaluation

Provider setup is optional. It is required only for a smoke probe or a live synthetic evaluation, not for the deterministic demo or test suite.

In an interactive terminal, run:

```sh
enterprise-agent llm-setup
```

The setup flow asks for one provider, accepts its application programming interface (API) key with hidden input, can perform an explicit low-cost metadata check, intersects the account's visible models with the small adapter-reviewed catalogue, and saves only the selected profile locally. The chosen profile takes effect immediately in the current TUI session. The host CLI atomically writes or merges `.env` with owner-only permissions (`0600`). The Docker TUI instead persists its selection in the ignored, owner-only `.enterprise-agent/profile.env`, mounted as its only writable host configuration path; Compose loads it after `.env`. It never echoes a key or includes one in an image, log, audit record, or Git commit.

The reviewed default models are:

| Profile | Environment names | Reviewed default |
|---|---|---|
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL` | `gpt-5.6-terra` |
| Claude | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | `claude-sonnet-5` |
| OpenRouter | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` |

### Provider recommendations and observed results

Start live testing with the reviewed OpenAI or Claude profiles. In the latest explicit, no-write full-pack checks, OpenAI `gpt-5.6-terra` and Claude `claude-sonnet-5` each passed all 13 fixed synthetic cases and all 51 score checks. They also produced schema-valid, deterministic-gate-approved proposals for the guarded local A, B, and C demos. These are observed results for fixed synthetic inputs, not a claim that either model is deterministic or production-ready.

OpenRouter remains supported through the same closed schema and fail-closed adapter boundary. Its listed model is a free-tier option, but the configured account was rate-limited during the latest recheck. Treat free-tier availability and allowance limits as external constraints. If an OpenRouter request is unavailable or invalid, the app records a safe failure; it does not retry through another provider or weaken validation. See [the recorded provider evidence](TRANSCRIPT.md#live-no-write-provider-evaluation) for the exact observed runs and metering.

Set `LLM_PROFILE` to `openai`, `claude`, or `openrouter`. The legacy `anthropic` value is accepted as an alias for `claude`. Copy `.env.example` only as a local starting point; `.env` is ignored and must never be committed.

Run a harmless connection probe explicitly:

```sh
enterprise-agent llm-smoke
```

Inspect the thirteen fixed synthetic evaluation stories, then opt in to one named request:

```sh
enterprise-agent llm-evaluate --list
enterprise-agent llm-evaluate --profile openai --case a-unapproved-bait --execute
```

Live evaluation sends fixed synthetic facts only. It runs with an in-memory audit adapter and cannot read or write the database, enterprise resource planning (ERP) system, mail, workflow, or business audit ledger. It returns only scalar score checks plus normalized token/cost totals; credentials, prompts, provider payloads, and model output are never retained or displayed. A nonzero exit means that a selected scorecard was not fully satisfied, not that the deterministic application controls failed.

Every provider request has a 5,000-output-token ceiling and a 60-second transport timeout. These are cost and latency bounds, not execution permissions: schema validation, deterministic policy, approval, and freshness checks remain mandatory.

### Direct CLI commands and prompts

The TUI is recommended for manual exploration, but direct commands remain available for development and scripted review. Activate the local environment first, then run these commands in an interactive terminal:

```sh
enterprise-agent run
enterprise-agent llm-setup
enterprise-agent llm-smoke
enterprise-agent llm-evaluate --list
enterprise-agent llm-evaluate --profile openai --case a-unapproved-bait --execute
```

When no usable provider profile exists, `enterprise-agent run` starts the hidden-key setup prompt. `enterprise-agent llm-setup` also prompts you to choose OpenAI, Claude, or OpenRouter; enter an API key without echoing it; optionally verify the key; then select a reviewed or custom model before an explicit save. In non-interactive terminals, setup refuses instead of prompting and names the missing setting.

For an interactive, database-backed guarded live proposal, use Compose and follow the `live` confirmation prompt:

```sh
docker compose --profile tools run --rm app \
  enterprise-agent live-demo --profile openai --case scenario-a-reroute
```

This command resets and seeds only the local synthetic database, sends one provider request, and stages at most one local review record. It cannot execute a business-system effect. Use `enterprise-agent live-demo --list` first to inspect the fixed case IDs without calling a provider.

## Optional local review UI

The optional UI is a FastAPI/Jinja review surface that publishes only to `127.0.0.1:8080`. It exists for browser-based inspection and reviewer convenience; use the TUI first for normal testing and guided demonstrations. Seed the synthetic database first, then start it through Compose:

```sh
make demo
docker compose --profile tools up --build ui
```

Open <http://127.0.0.1:8080>. The UI stays local-only and server-rendered. It has actor-scoped review pages, audit and recovery views, cross-site request forgery (CSRF) protected approval decisions through the same application service as the CLI, a guarded deterministic Demo tab, and separately explicit forms for no-write evaluation and a local A/B/C live proposal. It never renders a credential, raw provider response, or direct database/tool control.

## Tests and validation

| Command | Purpose |
|---|---|
| `make format-check` | Check Ruff formatting. |
| `make lint` | Run Ruff static checks. |
| `make typecheck` | Run strict MyPy. |
| `make test` | Run the full pytest suite with branch coverage. |
| `make test-critical` | Run the focused safety regression marker. |
| `make migrate` | Start the private database and migrate it to the current revision. |
| `make demo` | Run the unattended deterministic safety tour. |
| `make verify` | Run format, lint, type checking, tests, migration, and demo in order. |

The suite separates deterministic unit, PostgreSQL integration, mocked provider-contract, terminal/UI contract, and named business-story tests. Live-provider evaluation is manual and outside continuous integration (CI), because model behavior and account availability are not deterministic.

## How to extend safely

1. Add a typed domain contract and a migration before writing an adapter.
2. Expose data through a scoped port; authorization belongs in the provider query, not after context assembly.
3. Add a typed recommendation outcome and register every allowed tool. Do not let a model select arbitrary actions or workflow steps.
4. Bind planned source versions, policy version, and plan hash to human approval; revalidate immediately before execution.
5. Give each effect a stable idempotency key, durable workflow state, compensation, and append-only audit events.
6. Add deterministic unit, integration, and named scenario coverage before adding any optional live-model evaluation case.

## Intentional cuts

- This is a synthetic local harness, not a production ERP, mail, calendar, identity, or knowledge-system connector.
- The database service has no host-published port. The optional UI receives only its loopback-published port and connects to the database through the private Compose network.
- The UI is loopback-only and intentionally has no client-side state store, external deployment path, API-key setup, or direct tool invocation.
- LLMs recommend within a closed schema; they never authorize, execute, or dynamically compose workflows. Live provider checks complement deterministic tests and do not run in CI.
- Scenario C, the local UI, and the terminal app shell are completed optional enhancements; the required Scenario A/B control plane remains the primary deliverable.
