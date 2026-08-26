---
meta:
  title: "Operate the harness from a terminal"
  contentType: "Reference"
  navLabel: "Terminal Interaction Contract"
  category: "Reference"
  goal: "Specify safe, accessible, and scriptable terminal behavior for the enterprise agent harness"
  audience: "Operators and contributors who run harness commands"
  contentPlan:
    - "Define the command, presentation, and safety boundaries"
    - "Specify semantic states, output modes, prompts, errors, and identifiers"
    - "Set compatibility and test requirements for M9.2 through M9.6"
  openQuestions: []
---

# Operate the harness from a terminal

This reference defines the terminal contract for M9.2 through M9.6. It preserves Typer as the command grammar and confines Rich to presentation. Until those tasks land, existing commands retain their current output.

## Keep commands separate from presentation

Commands parse arguments, invoke application services, select an exit code, and return semantic data. They do not choose colors, tables, spinners, prompts, or business policy.

- **Typer**: command names, arguments, options, help, shell completion, and exit codes
- **Application services**: authorization, freshness, approval, workflow, audit, and tool policy
- **Presentation boundary**: human text, Rich rendering, JSON serialization, width handling, and color policy

Presentation code must not receive credentials, prompt bodies, raw model output, provider payloads, or database connections. It may receive only the sanitized result fields that a command already owns.

## Use stable semantic states

Every command result must map to one state before presentation. A state describes the operator outcome. An exit code describes whether the command invocation completed safely.

| State | Meaning | Default exit code |
|---|---|---:|
| `succeeded` | The requested read or approved action completed | 0 |
| `pending_approval` | A plan exists and requires a named human decision | 0 |
| `manual_review` | Evidence is ambiguous or policy forbids automation | 0 |
| `in_progress` | A durable workflow or scheduled task remains active | 0 |
| `recovery_required` | A durable workflow needs an operator recovery step | 1 |
| `refused` | Freshness, scope, safety, or configuration checks blocked the request | 1 |
| `failed` | A bounded operation could not finish safely | 1 |
| `cancelled` | The operator cancelled an interactive action | 130 |

Typer usage errors keep exit code 2. Commands must not use color alone to distinguish any state.

## Support four output surfaces

Each command must preserve the same semantic outcome across these surfaces:

| Surface | Contract |
|---|---|
| Interactive TTY | Human-readable status, copyable identifiers, keyboard-only prompts, and bounded progress for active local work |
| `--no-color` | The interactive text without ANSI color or style codes |
| `--output json` | One stable JSON object with no progress, decoration, or prompts |
| Non-TTY stdout | Plain human text with no ANSI codes, spinners, cursor control, or prompts |

`--no-color` overrides terminal capability detection. `--output json` overrides human presentation and writes only the result object to standard output. Diagnostics for a failed command write to standard error.

With no subcommand, the installed `enterprise-agent` executable is the interactive entry point. In a TTY it opens a keyboard-only Home surface that routes to Guided demo, Normal operator mode, or local LLM setup. JSON and non-TTY invocations never open that menu; they receive the same concise command directory as `enterprise-agent guide`. The Home surface delegates only to existing commands, so their confirmations, local-demo guard, provider opt-in, and exit-code contracts do not change.

The JSON envelope uses these top-level fields:

```json
{
  "schema_version": 1,
  "status": "pending_approval",
  "summary": "Scenario C plan awaits approval",
  "data": {},
  "next_actions": [],
  "error": null
}
```

`data` contains command-specific scalar values and structured public identifiers. `error` contains a sanitized code and message only for `refused` or `failed` results. Commands must add fields rather than rename or remove existing fields within schema version 1.

## Display identifiers and evidence clearly

An operator must be able to copy every durable identifier without parsing prose. Human output labels IDs on their own line or in a two-column table. JSON keeps the same IDs as strings.

- **Attention**: attention ID, scenario, cause, and current state
- **Plan and approval**: plan ID, plan hash prefix, approval ID, requester, approver, expiry, and decision state
- **Workflow**: workflow ID, status, current step, idempotency key prefix, and recovery state
- **Audit**: run ID, event count, timestamp range, and the read-only `audit explain` action

Commands may truncate an identifier only in decorative text. They must print its full value in a copyable field within the same result.

## Name planner provenance explicitly

Reviewer-facing demos and evaluations must show how a recommendation was produced, without exposing a credential, prompt body, raw provider response, or rationale. Text, local UI, and JSON use the same sanitized fields:

- **Planner**: `FAKE / DETERMINISTIC` for the fixed local planner, or `LIVE` for an explicitly opted-in provider call
- **Provider, profile, and model**: `none`/`deterministic-fake-v1` for the local planner; the selected configured provider profile and model for a live evaluation
- **Schema validation**: whether the returned structured recommendation was validated before its result was shown
- **Gate**: whether the application gate accepted a staged plan, was not invoked for a fixture walkthrough, or was not invoked because a live evaluation is intentionally no-write

The no-write evaluation wording is exact: `Gate: Not invoked (no-write evaluation)`. A live planner label proves only that the provider adapter was invoked for the fixed synthetic evaluation; it never implies a business-system write, approval, or production recommendation.

## Make keyboard-only decisions safe

Interactive flows must work without a mouse. A confirmation presents the exact effect, target ID, approver, and freshness consequence before it accepts a response.

- Default a destructive or approval decision to cancellation
- Accept an explicit cancellation key and return `cancelled` with exit code 130
- Hide API-key input and never echo the value, length, prefix, or provider response
- Do not prompt when standard input or output is not a TTY
- Name the required setting and the next safe command when noninteractive setup cannot continue

Approval, rejection, recovery, reset, and seed flows must explain whether they write data. A declined confirmation must not create an approval, workflow, tool invocation, or audit event.

## Return actionable errors

An error names one safe cause, the blocked operation, and one next action. It does not expose exception traces, credentials, raw provider output, or internal SQL details.

| Condition | Required guidance |
|---|---|
| Missing configuration | Name the setting and the command that configures or supplies it |
| Stale plan or evidence | State that execution stopped and direct the operator to refresh or recreate the plan |
| Missing scope | Name the blocked capability without disclosing another actor's data |
| Pending approval | Print the approval ID and the review action |
| Recovery state | Print the workflow ID and the recovery inspection action |
| Unsupported output option | Let Typer return its standard usage error |

Read-only commands must never prompt. A command that accepts sensitive input must describe how it protects that input before prompting.

## Preserve compatibility while the interface evolves

M9.2 introduces the shared Rich console and theme. M9.3 adds interactive safety flows. M9.4 adds the guided demo. M9.5 adds read-path discovery. M9.6 tests this contract without ANSI snapshots.

Existing command names and positional arguments remain valid. The no-subcommand TTY surface is additive to the installed command, while non-TTY and JSON no-subcommand invocations retain command discovery rather than prompting. New output options must be additive. A future command may opt out of progress only when it has no long-running local step.

## Verify each presentation change

Every M9 command change requires tests for these contracts:

- TTY and piped output produce the same status and identifiers
- `--no-color` contains no ANSI escape code
- `--output json` validates against the envelope and prints no extra standard-output text
- A narrow terminal preserves labels, full IDs, and next actions
- Interactive cancellation creates no durable write
- Hidden-key setup retains the existing credential protections
- Demo output identifies synthetic data, state transitions, and the next safe action
- Guided demo and live-evaluation output identify planner mode, provider/profile/model, schema-validation status, and the applicable gate boundary in text, UI, and JSON
- The Home and nested menus expose short labelled choices; wide terminals use bounded tables and ordinary terminals use bounded labelled cards

The test suite asserts semantic content, exit codes, and durable side effects. It does not use visual snapshots of tables, colors, or spacing.
