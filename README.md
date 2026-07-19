# Pi Workflows

An independently installable Pi product for creating, validating, running,
scheduling, inspecting, replaying, and optimizing deterministic agent graphs.
Agent X, Codex, Claude Code, and Loops all use the same workflow and CLI
contracts; none of those integrations owns a second runner.

The model is not treated as deterministic. The surrounding harness supplies
fixed objective contracts, stage transitions, approval gates, immutable input
snapshots, bounded repairs, mechanical verifiers, checkpoints, run ledgers,
trackers, tamper-evident seals, and compiled per-stage Pi tool profiles.
Specialized schema `1.2` workflows may also compile a bounded Pi peer-team
contract with pinned identities/models/tools, authenticated transport policy,
message/hop/time/byte budgets, typed exchanges, and digest-bound receipts.

## Install

Install the standalone runtime, `piw` CLI, and Codex/Claude Code skill links:

```bash
./install.sh
piw doctor
```

Pi can also load this checkout as a native package:

```bash
pi install /absolute/path/to/pi-workflows
```

Pi `0.80.5` through `0.80.10` is reviewed; `0.80.10` is the tested version.
Set `PI_BIN` when Pi is not on `PATH`. Model-backed workflows also require a
configured provider, but command-only workflows do not.

## Start

Read [`PRODUCT.md`](PRODUCT.md), then [`SKILL.md`](SKILL.md). The normal path is:

```bash
piw create research-brief --dir .codex/workflows/research-brief
piw validate .codex/workflows/research-brief/steps.yaml
piw run .codex/workflows/research-brief/steps.yaml --input-file request.md
piw detail .codex/workflows/research-brief/steps.yaml
piw schedule .codex/workflows/research-brief/steps.yaml --daily 09:00
```

Every inspection command accepts `--json`; failures return non-zero. Runs use
immutable per-run inputs, preserve artifacts and ledgers, and stream live into
Loops when its localhost adapter resolves the same workflow path.

A complete production workflow-factory example is in
[`examples/workflow-blueprint.json`](examples/workflow-blueprint.json).
Conditional specialized routing is demonstrated by
[`examples/pr-review-conditional-blueprint.json`](examples/pr-review-conditional-blueprint.json).
Bounded peer review is demonstrated by
[`examples/pr-review-peer-blueprint.json`](examples/pr-review-peer-blueprint.json).
The implementation-ready idea-to-plan harness is documented in
[`references/product-planning-harness.md`](references/product-planning-harness.md).

```bash
python3 scripts/compile_workflow.py \
  --blueprint examples/workflow-blueprint.json \
  --repo /path/to/project

python3 scripts/certify_workflow.py \
  --harness /path/to/project/.codex/workflows/uppercase-document/versions/1.0.0 \
  --run-smoke
```

For exhaustive pre-code product planning—including bounded Exa competitor discovery
and site capture, source-linked competitive synthesis, competing design theses,
latent pain and pricing hypotheses, brand, editable brand-deck, and asset systems, a web-framework ADR,
Cloudflare-first stack decisions, audience evidence, messaging and claim ledgers,
a canonical sitemap, every page/state, exact approved-for-build copy controls and
test candidates, architecture, and roadmap:

```bash
python3 scripts/scaffold_product_planning_workflow.py \
  --repo /path/to/product-repo \
  --workflow product-planning \
  --version 2.1.1
```

The specialized runtime runs independent parallel planning lanes, mechanically
validates every typed artifact, and applies strict keep-or-revert semantic
improvement to three integration milestones. The fast default targets `9.0/10`
with one revision; callers can opt into as many as three. A miss retains the
highest-scoring mechanically valid candidate and is visibly marked
`below_target_best_effort`. It writes plans only; application code and
deployment are out of scope.

The final readiness gate requires a route-complete sitemap, at least three
falsifiable latent pains, one selected pricing hypothesis with an honest
publication boundary, captured-source provenance for competitive claims, a
complete 12-14 slide brand-deck specification, canonical brand and asset decisions, exact
approved-for-build copy for every route/state/slot, and explicit resolution of
every critical or high cross-package inconsistency. Approval never claims copy
performance; that remains untested until real comprehension or experiment data
exists.

The default `milestone` policy uses Terra high only for product definition,
architecture, and the final plan.
Lanes, pages, and copy packs use authoritative mechanical gates without model
scoring. Sol handles planning generation; reserve Sol high for later coding and
execution review. Use `--judge-policy all-high` when exhaustive scoring matters
more than latency.

For a cheap model-route and controller smoke, use Luna low with `final-only`,
zero revisions, and an `artifacts-only` seed. The controller revalidates every
seeded artifact and reruns the final semantic verdict under Luna; it never
reuses a verdict produced by another model profile. This is test evidence, not
judge calibration or promotion evidence.

## Validate

```bash
./bin/piw doctor
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
```

## Repository policy

Do not commit generated workflow runs, credentials, Pi authentication state,
production traces, or customer data. Compiled harnesses and their run artifacts
belong in the consuming project's `.codex/workflows/` tree.
