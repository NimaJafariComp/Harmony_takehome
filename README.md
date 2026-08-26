# Enterprise agent harness

A safe, durable enterprise-agent harness for the purchasing and quality scenarios in the take-home assignment. It is deliberately a control plane, not an autonomous buyer: a large language model (LLM) may propose a typed plan, but deterministic checks decide whether any local effect can occur.

The repository includes all required Scenario A and Scenario B behavior, optional Scenario C, a keyboard-first terminal shell, and a loopback-only local review user interface (UI).

## Review the control-plane evidence

| Area | Evidence in this harness |
|---|---|
| Scenario A: projected stockout | Detects a pre-production shortage, gathers current authorized evidence, rejects ineligible suppliers, and stages an immutable reroute plan for approval. |
| Scenario B: quality hold | Detects a held lot, accounts for free capacity and existing commitments, and either proposes an approved reallocation or escalates the uncovered quantity. |
| Scenario C: supplier-risk bulletin (optional) | Correlates an authorized, current bulletin to an open PO and future production demand, then stages a bounded hold-and-notify plan for review. |
| Human control | No plan writes before an authorized approver decides. Approval authority, scopes, plan hashes, policy, and source versions are revalidated. |
| Reliable effects | Workflows have declared steps, stable idempotency keys, crash recovery, compensation, durable scheduling, and an append-only audit ledger. |
| LLM boundaries | OpenAI, Claude, and OpenRouter adapters share schemas. Live calls are explicit, account-configured, and cannot execute a plan or fall back across providers. |
| Recorded walkthrough | [TRANSCRIPT.md](TRANSCRIPT.md) separates reproducible Scenario A/B/C control-plane proof from the opt-in, no-write OpenAI scorecard. |

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
docker compose up --wait db
docker compose --profile tools run --rm app enterprise-agent
```

The second command opens the keyboard-first Home screen. Choose **Guided company demo**, **Normal operator mode**, or **Configure an LLM profile**. Direct commands remain available for scripts and documentation.

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

The default interactive command is:

```sh
enterprise-agent
```

This command-line interface (CLI) is keyboard-only and renders bounded panels and tables. In a pipe, or with `--output json`, it never prompts and instead returns the safe command directory. All JSON output uses a versioned, sanitized envelope. Use `--no-color` for plain terminal output.

Useful direct commands:

```sh
enterprise-agent guide
enterprise-agent demo --list
enterprise-agent demo --case scenario-a-reroute-bait
enterprise-agent status
enterprise-agent audit explain RUN_ID
enterprise-agent llm-usage
enterprise-agent --install-completion
```

`demo` resets and seeds only the guarded local synthetic database after an explicit confirmation. `make demo` uses the intentional unattended form for continuous integration (CI) and repeatable reviewer runs. The guided stories clearly label whether they stage a pending plan or are a fixed acceptance-case walkthrough; neither route calls a live provider or executes business effects automatically.

See [the terminal interaction contract](docs/terminal-interaction-contract.md) for output, keyboard, JSON, cancellation, and error-handling guarantees.

## Scenario walkthroughs

| Scenario | Demonstration | Safety proof |
|---|---|---|
| A: stockout reroute | A late original supplier threatens production. Supplier Z is viable; a cheaper, faster supplier is visibly excluded because it is unapproved. | Candidate filtering, current-evidence selection, immutable approval, fixed `po_reroute:v1` workflow, and no replacement PO before approval. |
| A: recovery and follow-up | The story library covers a crash after replacement-PO creation, a newer on-schedule supplier update, hostile email content, and Tuesday receipt handling. | A replay produces exactly one replacement PO; new evidence yields no unnecessary reroute; email content cannot change system rules; a partial or missing receipt creates one freshness-bound follow-up. |
| B: quality hold | A held lot threatens production. A released lot can cover demand only when its uncommitted capacity is sufficient. | Quality scopes stay distinct from purchasing writes; insufficient or already committed inventory is never presented as complete cover; ambiguous lots go to review. |
| C: supplier risk (optional) | A current, scoped supplier-risk bulletin correlates to an open PO and future production demand. | Bulletin prose is evidence only. The sole automated shape is a typed, approved, freshness-checked local hold-and-notify workflow with compensation. |

The deterministic story catalogue is intentionally small and named. It includes the normal path plus unapproved and too-slow suppliers, stale/current email evidence, malicious text, authority and approver-availability cases, source mutation, restart recovery, Tuesday receipt outcomes, capacity/commitment limits, scope denial, released holds, and unresolved candidate ranking.

## Provider setup and live evaluation

Provider setup is optional. It is required only for a smoke probe or a live synthetic evaluation, not for the deterministic demo or test suite.

In an interactive terminal, run:

```sh
enterprise-agent llm-setup
```

The setup flow asks for one provider, accepts its application programming interface (API) key with hidden input, can perform an explicit low-cost metadata check, intersects the account's visible models with the small adapter-reviewed catalogue, and saves only the selected profile locally. It atomically writes or merges `.env` with owner-only permissions (`0600`); it never echoes a key and preserves other configured profiles.

The reviewed default models are:

| Profile | Environment names | Reviewed default |
|---|---|---|
| OpenAI | `OPENAI_API_KEY`, `OPENAI_MODEL` | `gpt-5.6-luna` |
| Claude | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | `claude-sonnet-5` |
| OpenRouter | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` |

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

## Optional local review UI

The UI is a FastAPI/Jinja review surface that publishes only to `127.0.0.1:8080`. Seed the synthetic database first, then start it through Compose:

```sh
make demo
docker compose --profile tools up --build ui
```

Open <http://127.0.0.1:8080>. The UI stays local-only and server-rendered. It has actor-scoped review pages, audit and recovery views, cross-site request forgery (CSRF) protected approval decisions through the same application service as the CLI, a guarded deterministic Demo tab, and a separately explicit live-LLM evaluation form. It never renders a credential, raw provider response, or direct database/tool control.

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
