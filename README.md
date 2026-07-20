<p align="center">
  <img src="docs/assets/pi-workflows-hero.svg" alt="pi workflows — deterministic graphs, probabilistic work" width="100%">
</p>

<p align="center">
  <a href="https://github.com/ali-abassi/pi-workflows/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ali-abassi/pi-workflows/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/ali-abassi/pi-workflows/releases"><img alt="release" src="https://img.shields.io/github/v/release/ali-abassi/pi-workflows"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-11110f"></a>
</p>

# pi workflows

**Agents skip steps. pi workflows doesn't.**

A workflow harness your coding agent builds for itself. You describe the job to
Claude Code, Codex, or Pi; the agent writes a `steps.yaml` graph, picks the
model for each node, and runs it. From then on the graph owns the order, the
gates, the retries, and the budget — the model does the work but cannot quietly
skip a step, and every run leaves per-node evidence.

```bash
piw create review --action parallel-review     # agent scaffolds a valid graph
piw run review/steps.yaml --input-file task.md # code owns the order
piw detail review/steps.yaml RUN_ID --step verdict --io   # inspect one node
```

> Independent community project; not an official Pi project.

## Never used Pi? Start here

`piw` orchestrates. **Pi executes the model steps** — it is the client that
talks to model providers, so you authenticate once with Pi and every workflow
inherits it. You bring your own account; pi workflows never asks for a key.

```bash
# 1. Install Pi (the model runtime) and authenticate the provider you pay for
npm install -g @earendil-works/pi-coding-agent
pi                     # sign in when prompted, then /exit

# 2. Install pi workflows
git clone https://github.com/ali-abassi/pi-workflows.git
cd pi-workflows && ./install.sh

# 3. Confirm the whole chain is wired up
piw doctor
```

Requirements: macOS or Linux, Python 3.10+, Node.js 20+.

`pi --list-models` prints every model id your account can reach. A node pins one
of them as `provider/id`:

```yaml
model: openai-codex/gpt-5.6-luna    # whatever `pi --list-models` shows
```

If the provider serves a different model than the node pinned, the step
**fails** rather than returning another model's answer quietly.

`./install.sh` also links this repo as a skill for Claude Code and Codex, so
those agents can author, run, and inspect graphs without being told how.

## Why a graph instead of just asking the agent

An agent asked to "run these five steps on 1,000 records" will improvise: skip a
step it judges unnecessary, lose track of which items finished, and report
success it cannot substantiate. Here the graph is code. The agent's judgment is
confined to the *contents* of each node.

- **Node evidence** — input, output, model, attempts, gate, QA, tokens, cost,
  latency, for every node of every run.
- **Per-node QA** — an independent judge and a bounded improve/retest loop on
  any model step.
- **Model choice per node** — cheap model to draft, expensive one to judge;
  `piw eval` compares models over one corpus with judges held fixed.
- **Cost control** — ledgers, caching, run comparison, and fail-closed batch
  token/cost ceilings.
- **Scale** — one frozen graph over 1,000 isolated inputs with resumable
  receipts and ordered output.

> Deterministic orchestration does not mean identical LLM answers. pi workflows
> pins configuration, validates output, and preserves the evidence.

## Why not LangGraph, Temporal, or n8n?

They orchestrate **code**. This orchestrates **model calls**.

- **Temporal / Prefect / Dagster** give durable execution for code you write.
  They have no concept of a per-node model, a judge, or a token ledger.
- **LangGraph / AutoGen / CrewAI** are Python libraries you write agents in.
  Here the graph is a YAML file an agent can author and validate for free —
  and be mechanically prevented from deviating from.
- **n8n / Dify** are visual builders aimed at humans wiring SaaS nodes, not at
  a coding agent authoring, inspecting, and improving a graph from a terminal.

If your steps are deterministic code, use a real orchestrator. Use this when the
steps are model calls and you need per-node cost, QA, and proof of what ran.

## At scale: one graph, 1,000 items

```bash
piw batch steps.yaml --inputs corpus.jsonl --limit 5 --require-all   # canary
piw batch steps.yaml --inputs corpus.jsonl --parallel 16 \
  --require-all --stop-after-failures 3 \
  --max-tokens 2000000 --max-cost 25 --detach --json
```

Every item gets its own input, attempt directory, event stream, artifacts, gate
results, ledger, and receipt. The manifest pins SHA-256 digests of the workflow
and the corpus; `--resume` fails closed if either changed.

## Documentation

| Guide | What it covers |
|---|---|
| [`docs/workflow-format.md`](docs/workflow-format.md) | Every `steps.yaml` field |
| [`docs/node-system.md`](docs/node-system.md) | Node kinds, gates, routing, QA |
| [`docs/actions.md`](docs/actions.md) | Reusable action templates |
| [`examples/`](examples/) | Runnable workflows, simplest first |
| [`SKILL.md`](SKILL.md) | Instructions for an agent using the product |
| [`AGENTS.md`](AGENTS.md) | Instructions for working inside this repo |

`piw --help` lists every command; `piw schema --json` prints the full authoring
contract. The optional local Studio (`piw ui`) is a graph runner and flight
recorder over the same `steps.yaml`.

## Develop

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
npm ci --ignore-scripts
npm test && npm run check && npm run test:examples
```

pi workflows executes shell commands and model calls with your permissions.
Review third-party workflows before running them; see [`SECURITY.md`](SECURITY.md).

## License

[MIT](LICENSE) · Contributions welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md)
and [`CHANGELOG.md`](CHANGELOG.md).
