# Conditional Pi Workflow Research and Design Report

## Executive answer

The factory should support conditional LLM chains, but Pi or the model should
not own the route decision. The reliable design is:

```text
Pi stage produces typed facts
  → external deterministic router evaluates compiled JSON conditions
  → router writes a hash-chained decision receipt
  → specialized host launches the selected next Pi stage
  → mechanical verifier checks the branch outcome
```

The implementation now follows that architecture. Existing
`generic_mutation` stays honestly linear. Blueprint schema `1.1` adds
`control_flow` only for specialized runtimes; compilation emits a versioned
machine plus an independent evaluator.

## Decision-relevant findings

### 1. Conditions must be data, not code or model authority

JsonLogic demonstrates the useful minimum: JSON rules, a bounded operator at
each node, read-only input, and no `eval`, setters, loops, or side effects.
Amazon States Language likewise represents Choice rules as declarative JSON.
This supports a strict condition AST rather than JSONata, jq, shell, Python
expressions, or a model-emitted `next_stage`.
[JsonLogic](https://jsonlogic.com/),
[Amazon States Language Choice](https://states-language.net/spec.html#choice-state)

Custom operators are intentionally excluded. JsonLogic's own extension guide
shows that custom operations can introduce randomness/global side effects and
documents removal of arbitrary method calling after a prototype-pollution
problem. Flexibility here directly weakens replay and security.
[JsonLogic custom operations](https://jsonlogic.com/add_operation.html)

### 2. Defaults and ambiguity need explicit policy

ASL uses ordered first-match and raises `States.NoChoiceMatched` when neither a
rule nor `Default` applies. Open Workflow DSL also provides conditional cases
and a single default. The standards validate explicit fallback, but incidental
array order can silently shadow overlapping rules.
[ASL Choice](https://states-language.net/spec.html#choice-state),
[Open Workflow Switch](https://github.com/serverlessworkflow/specification/blob/v1.0.0/dsl-reference.md#switch)

The factory therefore evaluates every condition, selects the unique lowest
priority number, records all matches, and fails on an equal best-priority tie.
Each nonterminal stage requires exactly one default. Missing paths and invalid
types are errors, not false predicates that quietly trigger a default.

### 3. Cycles are useful only with budgets

ASL and Open Workflow both make retry attempts finite. The factory similarly
requires global `max_transitions`; every cyclic stage needs an explicit visit
limit; exhaustion routes to a declared terminal stage.
[ASL Retry](https://states-language.net/spec.html#retrying-after-error),
[Open Workflow Retry](https://github.com/serverlessworkflow/specification/blob/v1.0.0/dsl-reference.md#retry)

This semantic loop budget remains separate from Pi provider retries and harness
repair attempts. Stacking hidden retry layers would make cost, latency, and
completion claims misleading.

### 4. Replay requires its own authoritative history

Temporal's Event History is a complete ordered record used to replay workflow
state; external interaction results are recorded rather than repeated during
replay. Pi sessions are also append-only trees, but they serve model context and
can be compacted or branched. The harness therefore keeps routing truth outside
Pi sessions.
[Temporal workflows](https://docs.temporal.io/workflows),
[Pi session format](https://pi.dev/docs/latest/session-format)

Every decision receipt binds the exact control-flow digest, source-stage
artifact digest, evaluated predicates, matched routes, selected route, default
use, counters, and previous decision digest. Before routing again, the engine
revalidates the entire chain and every historical source artifact.

### 5. Pi provides execution primitives, not the deterministic DAG

JSON mode is appropriate for bounded isolated nodes. RPC adds long-lived
steering, follow-up, abort, queue, retry, compaction, and session control; a
prompt acknowledgement is only acceptance, and a robust host waits for
`agent_settled`. The SDK is the better TypeScript host when typed in-memory
settings, model registries, sessions, custom tools, and direct event APIs are
needed.
[Pi JSON](https://pi.dev/docs/latest/json),
[Pi RPC](https://pi.dev/docs/latest/rpc),
[Pi SDK](https://pi.dev/docs/latest/sdk)

Pi's TypeBox custom-tool plus `terminate:true` pattern is the strongest future
stage-output contract. It should become a generated `harness_submit` tool, but
the host must still require exactly one successful submission and mechanically
verify outcomes.
[Pi structured output example](https://github.com/badlogic/pi-mono/blob/2b3fda9921b5590f285165287bd442a25817f17b/packages/coding-agent/examples/extensions/structured-output.ts)

### 6. Two Pi fail-open assumptions required correction

Pinned Pi `0.80.6` source shows JSON print mode can exit normally while the
final assistant message reports `error` or `aborted`; exit code alone is not a
completion gate. The runner now validates every JSONL line, final stop reason,
failed terminal retries, and extension-error evidence.
[Pinned print mode](https://github.com/badlogic/pi-mono/blob/2b3fda9921b5590f285165287bd442a25817f17b/packages/coding-agent/src/modes/print-mode.ts#L121-L151)

Discovery-disable flags also leave global `settings.json` and `models.json` in
scope. Those can alter retry, compaction, provider base URL, or model records.
The runner now builds a sanitized agent directory with pinned retry/compaction
policy, no model overrides, and an auth reference.
[Pi settings](https://pi.dev/docs/latest/settings),
[Pi model overrides](https://github.com/badlogic/pi-mono/blob/2b3fda9921b5590f285165287bd442a25817f17b/packages/coding-agent/docs/models.md#overriding-built-in-providers)

`--offline` only disables startup network operations. Pi has no built-in
sandbox; unattended mutation still requires OS/container/VM isolation.
[Pi security](https://pi.dev/docs/latest/security)

## Implemented contract

- Blueprint schema `1.1` with specialized-only `control_flow`.
- Strict unknown-field rejection in handwritten validation.
- JSON Pointer condition paths with prototype-sensitive segments rejected.
- Operators: `exists`, `missing`, `type_is`, strict equality/inequality,
  numeric comparisons, `contains`, `in`, `all`, `any`, and `not`.
- Static reachability, terminal reachability, target/default/priority, condition
  depth/node, and cycle-budget validation.
- Compiled `control-flow.json` and copied, digest-bound evaluator.
- Idempotent `decision-id` and hash-chained individual plus JSONL receipts.
- Source, graph, ledger, and decision tamper detection.
- Path-aware tracker that marks unvisited branches skipped.
- Conditional PR-review example with docs, large-diff, code, and blocked routes.

## Counterevidence and limitations

- ASL and Open Workflow use ordered cases; priority plus tie failure is a
  stricter local policy, not a standards mandate.
- Static rejection of every unreachable node and terminal-dead path is a
  conservative certification inference, not a directly quoted requirement.
- The compiled evaluator selects stages but does not implement a domain PR
  client, GitHub posting, or arbitrary stage executor. Specialized entrypoints
  still own those actions and their certification adapter.
- Exactly-once local decision receipts do not imply exactly-once external side
  effects. Domain runners still need stable effect keys, receipts, and final
  external-state checks.
- Generated TypeBox `harness_submit`, RPC, and SDK runner profiles remain
  follow-up capabilities rather than being silently mixed into the Python
  generic runner.

## Method, scope, and stopping rule

Research covered the current local tree; Pi `0.80.5`/`0.80.6` docs and pinned
source; ASL; Open Workflow; JsonLogic; Temporal; and primary agent-eval
guidance. Claims were stored separately in `evidence.jsonl` and audited.

The search converged after two follow-ups: one comparison of ambiguity/default
policies and one source-level verification of Pi JSON/config behavior. Neither
introduced a new required control-flow primitive beyond the implemented
contract. The remaining gaps are separately versionable runner profiles and
domain adapters, not missing condition semantics.
