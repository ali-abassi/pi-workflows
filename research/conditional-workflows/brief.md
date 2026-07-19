# Conditional Workflow Research Brief

## Decision

Choose and implement the smallest first-class conditional-transition design
that lets the deterministic workflow factory route between stages safely,
visibly, and reproducibly while using Pi capabilities appropriately.

## Scope

- Current `deterministic-codex-workflows` repository and its existing generic
  mutation runner.
- Pi `0.80.5` through `0.80.6`, with current upstream documentation checked on
  2026-07-12.
- Deterministic condition evaluation, branch selection, defaults, ambiguity,
  terminal states, bounded loops, artifacts, replay, and operator visibility.
- A PR-review workflow as the primary worked example.

## Exclusions

- Building a complete GitHub PR-review integration or posting review comments.
- Automatic prompt/model/cost optimization inside the initial execution path.
- Treating Pi hooks as an OS sandbox.
- Arbitrary user-authored code expressions such as `eval`, JSONPath scripts, or
  shell conditions.
- Replacing the existing fixed generic mutation lifecycle in this iteration.

## Audience and deliverables

The audience is the skill author and future workflow builders. Deliver:

1. a claim-level evidence ledger;
2. a cited design report;
3. an executable deterministic transition evaluator;
4. blueprint/compiler/certification integration;
5. a PR-review example and adversarial tests;
6. skill documentation describing when to use the transition engine versus a
   specialized RPC/SDK host.

## Done criteria

- Conditions are data, not free-form model instructions or executable code.
- The evaluator supports a bounded operator allowlist and nested `all`/`any`/
  `not` groups.
- Exactly one transition wins by explicit priority; ties fail closed; exactly
  one default is required where conditional routes exist.
- The graph rejects missing nodes, unreachable nodes, invalid terminals,
  unbounded cycles, ambiguous priorities, and incompatible source paths before
  any model call.
- Every routing decision emits an artifact with inputs, evaluated predicates,
  chosen route, alternatives, and a deterministic digest.
- The compiler preserves and certifies the graph; a command can evaluate a
  stage artifact and return the next stage without invoking an LLM.
- Focused tests cover normal branching, defaults, nested predicates, missing
  fields, type mismatch, ambiguity, loops, tampering, and a PR-review example.
- The evidence auditor passes or unresolved corroboration gaps are named.

## Coverage matrix

| Subquestion | Required evidence | Preferred source | Freshness | Status |
|---|---|---|---|---|
| What Pi surface best hosts bounded versus long-lived branching? | JSON/RPC/SDK/extension semantics and failure behavior | Pi official docs/source | Current | verified |
| How should stage outputs become routing inputs? | Typed output/tool/schema mechanisms | Pi official docs/source | Current | verified |
| What condition language is safe and auditable? | Existing deterministic workflow/state-machine precedents | Standards and primary docs | Stable/current | verified |
| How should ambiguous routes, defaults, and loops fail? | State-machine and workflow orchestration semantics | Primary docs/specs | Stable/current | verified |
| What artifacts make routing replayable and visible? | Event/provenance/checkpoint guidance | Pi docs plus agent-eval primary sources | Current | verified |
| Where does this fit into the current repo? | Exact code/schema/compiler/runner seams | Local source and tests | Current tree | verified |
| What should remain specialized or deferred? | Capability limits and counterevidence | Pi docs, local audit, eval research | Current | verified |

## Competing hypotheses and counterclaims

- H1: Letting the model emit `next_stage` is sufficient. Counterclaim: this
  makes control flow depend on untrusted prose/JSON and permits undeclared
  routes.
- H2: A general expression language is more flexible. Counterclaim: arbitrary
  expressions expand the attack surface, complicate type checking, and make
  replay less portable.
- H3: Pi RPC should replace the current JSON subprocess runner. Counterclaim:
  JSON mode remains simpler and easier to isolate for bounded stages; RPC is
  justified only for long-lived steering/cancellation/UI requirements.
- H4: Cycles should be banned entirely. Counterclaim: bounded retry/review loops
  are useful if their iteration budget and terminal exhaustion route are
  explicit.
- H5: Adding a graph schema alone is enough. Counterclaim: without executable
  evaluation, certification, decision artifacts, and adversarial tests, the
  factory merely documents branches rather than controlling them.

## External-action and cost limits

Research is read-only on external sources. No deployment, GitHub write, global
Pi upgrade, paid API call, or external message is authorized. Local repository
edits and focused local tests are in scope.

## Stopping rule

Stop after all coverage rows are verified or explicitly provisional and two
focused follow-up rounds produce no new material capability, failure mode, or
contract change. Implementation stops at a reusable deterministic transition
engine integrated with compilation/certification; a full arbitrary-stage Pi
runtime is a separately versioned follow-up.
