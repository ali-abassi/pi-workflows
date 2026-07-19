# Pi Workflows

Pi Workflows is the canonical source for the deterministic workflow product.
Agent X, Codex, Claude Code, and Loops are integrations; none of them owns the
runner, workflow schema, CLI contract, or agent skill.

## Product invariants

- Code owns control flow. Models may generate or judge artifacts, but cannot
  decide whether a gate passed or silently skip declared nodes.
- `steps.yaml` is source truth. `steps.layout.json` is presentation state owned
  by the graph canvas.
- Validate before paid execution. A failed run exits non-zero and preserves its
  artifacts, event log, stderr, and ledger.
- Every model, tool, shell, file, schedule, and external-effect boundary must be
  explicit and inspectable.
- Workflow inputs are immutable per run. Concurrent runs must never share an
  input staging file.
- The product works without Agent X or Loops. Optional integrations may add a
  TUI tool, a visual graph, and durable scheduling without changing semantics.
- Keep prompts compact. Use deterministic checks for schemas, routes, gates,
  budgets, permissions, and completion.

## Canonical commands

```text
./bin/piw doctor
./bin/piw ls
./bin/piw graph <workflow>
./bin/piw validate <workflow>
./bin/piw run <workflow> --input-file <path>
./bin/piw detail <workflow>
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
```

Read `PRODUCT.md`, then `SKILL.md`, before changing product behavior. Preserve
unrelated working-tree changes. Do not commit generated runs, cache entries,
credentials, authentication state, or customer data.
