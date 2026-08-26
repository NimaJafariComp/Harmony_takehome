# Requirement coverage review

This review maps the source take-home assignment to concrete repository evidence. It distinguishes the safe, reviewer-facing walkthrough from the fully executed deterministic proof run so that no fixture or live-model check is overstated.

## Review conclusion

All required assignment areas are covered by executable evidence and documentation. The main boundaries are intentional:

- `make demo` is the short, keyless reviewer walkthrough. It stages only reviewable local pending plans and labels the remaining stories as fixtures; it does not execute business effects.
- `make verify` is the documented one-command acceptance proof. It runs the complete deterministic suite, including the fully executed Scenario A timing/audit path, Scenario B, recovery, failure cases, migration, and the demo.
- A real LLM is used only through an explicit, synthetic, no-write adapter evaluation. It is deliberately not the correctness authority or an execution path. The deterministic gate and workflow controls remain in force regardless of model behavior.
- Scenario B is free-form relative to the fixed Scenario A declaration: the planner may select only typed registered quality actions and parameters, rather than enter `po_reroute:v1`. Its selected bounded tool plan is still persisted and executed by the common idempotency/recovery machinery; that is a durability control, not a scenario-specific fixed workflow.

## Required Scenario A and harness controls

| Assignment requirement | Evidence |
|---|---|
| Detect a projected stockout without a prompt and deduplicate repeated triggers | [tests/test_stockout_detector.py](tests/test_stockout_detector.py) proves projected availability and the seeded risk; [tests/test_attention_lifecycle.py](tests/test_attention_lifecycle.py) proves canonical durable deduplication. |
| Gather ERP, inbox, and calendar evidence through user-scoped providers | [tests/test_scoped_providers.py](tests/test_scoped_providers.py) and [tests/test_scenario_a_context.py](tests/test_scenario_a_context.py) prove provider-bound permissions and selection of the newest correlated shipment update. |
| Produce a recommendation, then enforce permission, policy, approval, and freshness gates before writes | [tests/test_scenario_a_gate.py](tests/test_scenario_a_gate.py) and [tests/test_plan_approvals.py](tests/test_plan_approvals.py) cover supplier eligibility, authority, immutable hashes, expiry, scope, and source-version denial. |
| Route unanswered end-of-day approval to a designated next-day-out-of-office backup | [tests/test_approval_routing.py](tests/test_approval_routing.py) and the complete Scenario A timing run prove the exact routing conditions and authorized backup decision. |
| Execute replacement PO, original PO update, production notice, and Tuesday task idempotently with recovery and compensation | [tests/test_workflow_executor.py](tests/test_workflow_executor.py) proves fixed order, durable starts, replay, compensation, and one replacement PO after crash; [tests/test_scenario_a_timing_audit.py](tests/test_scenario_a_timing_audit.py) executes the complete cross-boundary path. |
| Fire Tuesday follow-up, resolve receipt or reopen a distinct loop | [tests/test_arrival_check.py](tests/test_arrival_check.py) proves receipt resolution, missing/partial reopen behavior, source-version causality, and scheduler safety. |
| Reconstruct the result solely from append-only audit history | [tests/test_audit_explain.py](tests/test_audit_explain.py), the timing/audit integration test, and [TRANSCRIPT.md](TRANSCRIPT.md) prove ledger-only reconstruction. |

## Model, workflow, Scenario B, and provider requirements

| Assignment requirement | Evidence |
|---|---|
| Small synthetic ERP, mail, calendar, roles/scopes, tool catalog, clock, and deliberate noise | [MODEL.md](MODEL.md) documents the retained facts and intentional omissions; its seed tests cover the delayed PO, current/superseded mail, unrelated records, unapproved bait supplier, backup calendar fact, and quality capacity cases. |
| Scenario A uses an immutable six-step declared workflow, not model-selected steps | [DESIGN.md](DESIGN.md) documents `po_reroute:v1`; [tests/test_po_reroute_workflow.py](tests/test_po_reroute_workflow.py) and [tests/test_workflow_executor.py](tests/test_workflow_executor.py) reject added, skipped, and reordered steps. |
| Scenario B has a distinct quality actor, held-lot detection, alternate capacity decision, and purchasing escalation | [tests/test_scenario_b_execution.py](tests/test_scenario_b_execution.py), [tests/test_scenario_b_planning.py](tests/test_scenario_b_planning.py), and [MODEL.md](MODEL.md) prove the covered, insufficient, committed, approval-before-write, and recovery paths. |
| Use a real LLM API with safe provider boundaries | The OpenAI, Claude, and OpenRouter adapters have shared mocked contracts. The recorded explicit OpenAI evaluation in [TRANSCRIPT.md](TRANSCRIPT.md) passed 4/4 checks without a database or business write. The current OpenAI and Claude no-write smoke probes passed; OpenRouter's configured free model returned a normalized external `provider_failure`, with no fallback, raw error, credential, or business data exposure. |

## Required written deliverables

| Deliverable | Evidence |
|---|---|
| `README.md`: run instructions, extension guidance, and cuts | [README.md](README.md) documents `make demo`, `make verify`, the terminal, provider setup/evaluation, extension rules, and intentional limits. |
| `MODEL.md` | [MODEL.md](MODEL.md) explains retained sample concepts, changes, noise, scopes, clock, and omitted ERP breadth. |
| 2-3 page design document | [DESIGN.md](DESIGN.md) answers the required identity/authorization, durable memory, scaling, and deterministic-workflow questions. |
| Tests for gate, trigger dedupe, and workflow resumption at minimum | The focused test files above cover all three, and `make verify` passed all 579 tests at 85.70% coverage. |
| Recorded Scenario A run with approval, execution, and audit trail | [TRANSCRIPT.md](TRANSCRIPT.md) contains the reproducible commands and complete deterministic timing/audit trace, separately labeling the no-write live-model scorecard. |

## Optional additions delivered

| Addition | Evidence |
|---|---|
| Scenario C: supplier-risk bulletin | [MODEL.md](MODEL.md), [tests/test_scenario_c_execution.py](tests/test_scenario_c_execution.py), and [tests/test_scenario_c_recovery.py](tests/test_scenario_c_recovery.py). |
| Keyboard-first terminal HCI | [docs/terminal-interaction-contract.md](docs/terminal-interaction-contract.md) and [tests/test_terminal_usability.py](tests/test_terminal_usability.py). |
| Loopback-only local review UI | [README.md](README.md), [tests/test_web.py](tests/test_web.py), and the final container `/health` check. |

## Reviewer commands

```sh
make demo
make verify
enterprise-agent llm-evaluate --list
enterprise-agent audit explain RUN_ID
```

The first command is the short local walkthrough. The second is the full deterministic proof. The live-evaluation command remains opt-in and no-write; audit reconstruction remains ledger-only.
