#!/usr/bin/env python3
"""Scaffold a repo-local Pi + Codex deterministic task harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def slug(value: str) -> str:
    return "-".join(filter(None, "".join(c.lower() if c.isalnum() else " " for c in value).split())) or "pi-task"


def write(path: Path, content: str, force: bool = False) -> None:
    if path.exists() and not force:
        raise SystemExit(f"refusing to overwrite {path}; use --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    name = slug(args.name)
    repo = Path(args.repo).expanduser().resolve()
    root = repo / ".codex" / "workflows" / name
    example_workdir = root / "examples" / "workspace"

    config = {
        "workflow": name,
        "models": {
            "intake": "openai-codex/gpt-5.6-luna",
            "plan": "openai-codex/gpt-5.6-sol",
            "execute": "openai-codex/gpt-5.6-terra",
            "repair": "openai-codex/gpt-5.6-terra",
            "judge": "openai-codex/gpt-5.6-sol",
        },
        "thinking": "low",
        "max_repairs": 1,
        "pi_timeout_seconds": 600,
    }
    task = {
        "task_id": "sample-uppercase-001",
        "objective": "Read input.txt and create result.txt containing the same text converted to uppercase, preserving the final newline.",
        "objective_contract": {
            "selector": "the declared input.txt file",
            "target": "result.txt in the task workspace",
            "decision": "whether result.txt exactly equals the uppercase input",
            "non_goals": ["modify input.txt", "change files outside result.txt"],
        },
        "lifecycle": ["intake", "plan", "approval", "execute", "verify", "judge", "report"],
        "workdir": str(example_workdir),
        "inputs": ["input.txt"],
        "constraints": ["Do not edit input.txt", "Only create or update result.txt"],
        "acceptance_criteria": ["result.txt exists", "result.txt exactly equals the uppercase form of input.txt"],
        "allowed_tools": ["read", "edit", "write"],
        "allowed_write_paths": ["result.txt"],
        "immutable_paths": ["input.txt"],
        "execution_commands": [],
        "allow_bash": False,
        "clean_allowed_write_paths": True,
        "context_policy": {"strategy": "full", "max_input_bytes": 1000000},
        "step_validation": {"enabled": False},
        "verification_commands": [
            [
                "python3",
                "-c",
                "from pathlib import Path; i=Path('input.txt').read_text(); r=Path('result.txt').read_text(); assert r == i.upper()",
            ]
        ],
        "max_repairs": 1,
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "task_id",
            "objective",
            "objective_contract",
            "lifecycle",
            "workdir",
            "inputs",
            "constraints",
            "acceptance_criteria",
            "allowed_tools",
            "verification_commands",
        ],
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "objective": {"type": "string", "minLength": 1},
            "objective_contract": {
                "type": "object",
                "additionalProperties": False,
                "required": ["selector", "target", "decision", "non_goals"],
                "properties": {
                    "selector": {"type": "string", "minLength": 1},
                    "target": {"type": "string", "minLength": 1},
                    "decision": {"type": "string", "minLength": 1},
                    "non_goals": {"type": "array", "items": {"type": "string", "minLength": 1}}
                }
            },
            "lifecycle": {"type": "array", "minItems": 2, "uniqueItems": True, "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}},
            "workdir": {"type": "string", "minLength": 1},
            "inputs": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "acceptance_criteria": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "allowed_tools": {"type": "array", "items": {"enum": ["read", "bash", "edit", "write", "grep", "find", "ls"]}},
            "verification_commands": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            },
            "verification_timeout_seconds": {"type": "integer", "minimum": 1},
            "max_repairs": {"type": "integer", "minimum": 0, "maximum": 3},
            "execution_commands": {
                "type": "array",
                "items": {"type": "array", "minItems": 1, "items": {"type": "string"}}
            },
            "allowed_write_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "immutable_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "allow_bash": {"type": "boolean"},
            "clean_allowed_write_paths": {"type": "boolean"},
            "step_validation": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "model": {"type": "string", "minLength": 1},
                    "thinking": {"enum": ["off", "minimal", "low", "medium", "high", "xhigh"]},
                    "mode": {"enum": ["gate", "advisory"]},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 10},
                    "max_attempts_per_step": {"type": "integer", "minimum": 1, "maximum": 3},
                    "timeout_seconds": {"type": "integer", "minimum": 1}
                }
            },
            "context_policy": {
                "type": "object",
                "additionalProperties": False,
                "required": ["strategy", "max_input_bytes"],
                "properties": {
                    "strategy": {"enum": ["full", "deterministic_segments", "hierarchical_chunks"]},
                    "max_input_bytes": {"type": "integer", "minimum": 1}
                }
            },
            "required_steps": {
                "type": "array",
                "maxItems": 50,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1}
            },
        },
    }
    workflow = f"""# {name}

This harness completes an approved task end to end through Pi with Codex models.

The task spec declares its selector, target, decision, non-goals, context policy,
and exact lifecycle. Mutation stages are intake (Luna), plan (Sol), approval,
execute (Terra), mechanical verify, bounded repair (Terra), judge (Sol), report.

Run the included smoke task:

```bash
python3 ~/.agents/skills/pi-workflows/scripts/run_pi_harness.py \\
  --harness {root} \\
  --spec {root}/examples/task.json

python3 ~/.agents/skills/pi-workflows/scripts/run_pi_harness.py \\
  --harness {root} \\
  --spec {root}/examples/task.json \\
  --approve-execution \\
  --approved-plan-artifact {root}/runs/sample-uppercase-001/<gate-run-id>/stages/plan.json
```

Without `--approve-execution`, the harness writes intake, plan, and approval
artifacts, then stops before mutation.

Open the workflow-level tracker after one or more runs:

```text
{root}/runs/index.html
```
"""
    operations = f"""# Operating {name}

## Trust contract

- The model proposes and executes; scripts and declared verifiers decide pass/fail.
- Approval binds to the exact plan hash, spec hash, and input snapshot.
- Only `allowed_write_paths` may change; `immutable_paths` must retain their hashes.
- Step states are pending, claimed, verified, or failed. Only verified counts as complete.

## Configure before first real run

1. Give every task a stable `task_id` and decide whether reruns replace or append outputs.
2. Declare every input and immutable path used to make the decision.
3. Minimize model tools and write roots. Prefer supervisor commands over model-controlled bash.
4. Map every acceptance criterion to a deterministic verification command.
5. Declare `required_steps` only when order matters and evidence can be named.
6. Enable `step_validation` when each step benefits from a low-cost Luna review;
   keep it disabled when final mechanical verification is sufficient.
7. Use a separate worktree, container, or job workspace for concurrent mutating runs.
8. Classify failures as retryable, repairable, approval-blocked, or terminal.
9. Set retention and secret-redaction policy for prompts, tool events, and artifacts.

## Prove the harness

- Negative gate: omit approval and verify no output root changed.
- Positive path: approve the exact plan and require all checks to pass.
- Policy negative: attempt an immutable or out-of-allowlist write and require a blocked event.
- Verifier negative: inject a bad output and require final status failed.
- Stale-output negative: precreate outputs, enable cleanup, and require current-run model writes.
- Repeatability: run the same fixture at least three times and compare verifier results.
- Concurrency: run two isolated fixtures simultaneously and verify distinct run IDs and ledger rows.

## Observe and recover

- Tracker scope: this workflow has its own database and dashboard; runs from
  other workflow harnesses are rejected rather than merged.
- Aggregate UI: `{root}/runs/index.html`
- Machine export: `{root}/runs/index.json`
- Durable ledger: `{root}/runs/harness.sqlite3`
- Per-run audit: `runs/<task_id>/<run_id>/tracker.jsonl`
- Per-run evidence: `runs/<task_id>/<run_id>/validation/final_validation.json`

Backfill the aggregate ledger after importing or restoring run directories:

```bash
python3 ~/.agents/skills/pi-workflows/scripts/update_harness_registry.py --harness {root}
```

Do not treat Pi hooks as a security sandbox. Use container or OS isolation for untrusted code.
"""
    write(root / "harness.json", json.dumps(config, indent=2) + "\n", args.force)
    write(root / "schemas" / "task.schema.json", json.dumps(schema, indent=2) + "\n", args.force)
    write(root / "examples" / "task.json", json.dumps(task, indent=2) + "\n", args.force)
    write(example_workdir / "input.txt", "deterministic pi harness\n", args.force)
    write(root / "workflow.md", workflow, args.force)
    write(root / "OPERATIONS.md", operations, args.force)
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
