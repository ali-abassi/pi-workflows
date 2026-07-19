# Design Playbook

## Stage Machine Template

Use this baseline unless the domain demands another shape:

```text
intake
evidence
judgment_or_plan
dry_run
human_gate
execute
verify
report
```

For read-only work, omit `human_gate` and `execute`.

Do not reuse these stage names mechanically. The tracker must show the domain's
actual lifecycle. Start from an objective contract:

```json
{
  "selector": "failed decisions from scorer ID X",
  "target": "underlying agent task failures",
  "decision": "root cause and proposed fix per task",
  "non_goals": ["rejudge scorer quality", "mutate production"]
}
```

If the selector and target are the same phrase, challenge the design before
continuing.

## Fast Path Rule

A deterministic workflow should not be synonymous with a heavy workflow. Add a fast path when the common request is read-only triage or RCA:

```text
intake_handles
exact_lookup
bounded_evidence
answer_or_decision_point
```

Escalate to the full stage machine only after there is a concrete mutation candidate. This keeps the workflow deterministic while avoiding expensive setup, broad searches, and premature gate ceremony.

## Required Artifacts

```text
.codex/workflows/<name>/
  workflow.md
  manifest.template.json
  schemas/
    stage-output.schema.json
  hooks/
    guard.py
  examples/
    prompt.md
```

For live runs:

```text
.tmp-<name>/
  manifest.json
  intake.json
  evidence.json
  plan.json
  dry_run.json
  verification.json
  report.md
```

## Manifest Fields

Use at least:

```json
{
  "workflow": "name",
  "case_id": "stable-id",
  "stage": "intake",
  "allowed_to_mutate": false,
  "approval": {
    "human_gate": "not_approved",
    "approved_by": null,
    "approved_at": null,
    "approved_artifact": null
  },
  "artifacts": {},
  "history": []
}
```

## Schema Rules

- Use `additionalProperties: false` for downstream contracts.
- Put enums on status/classification fields.
- Include `evidence` arrays for claims.
- Include `residual_risk` in final outputs.
- Treat model-generated JSON as untrusted until parsed and validated.

## Runner Pattern

Use `codex exec` as a stage worker:

```bash
codex exec --json \
  --output-schema .codex/workflows/<name>/schemas/stage-output.schema.json \
  -o .tmp-<name>/stage-output.json \
  "Run the <stage> stage for <workflow>. Read .tmp-<name>/manifest.json and write only the schema output."
```

Prefer prompt-plus-stdin when a deterministic command already produced the evidence:

```bash
./scripts/collect-evidence.sh CASE-123 \
  | codex exec --json --output-schema schemas/judgment.schema.json \
      "Classify this evidence for the workflow."
```

## Gate Pattern

Before any mutation:

1. Produce an exact dry-run plan artifact.
2. Record the artifact path in the manifest.
3. Require approval.
4. Let hooks/scripts check the manifest before mutation commands run.

Do not let approval live only in chat text. Put it in the manifest.

## Validation Loop

Use a repair loop:

```text
produce -> validate -> classify failure -> repair -> validate again
```

Stop only when:

- deterministic checks pass,
- schema validation passes,
- mutation gates are closed or explicitly approved,
- final report links the artifacts and unresolved risks.

## Production Run Contract

Each run should persist:

```text
manifest.json                 objective, selector, digests, models, stages
state.json                    current terminal-safe state
events/*.jsonl                Pi and harness event streams
inputs/                       immutable exact source artifacts
context/                      deterministic bounded-context artifacts
stages/                       model outputs and attempts
validation/                   mechanical checks and recomputed metrics
integrity/run-seal.json       terminal artifact inventory and digest
tracker.html                  visible operator view (derived, not authoritative)
```

Checkpoint after every verified item and support safe resume. Resume requires
matching input and implementation digests. Interruptions become sealed
`canceled` runs rather than stale `running` rows.

## Pi Integration Choice

| Need | Pi surface |
|---|---|
| One bounded stage with streamed events | JSON mode |
| Long-lived cross-language controller | RPC mode |
| Typed Node host and custom session control | SDK |
| Tool gates, structured submission, UI widgets | Extension |

In JSON mode, extension UI is unavailable. Do not put a human approval dialog
inside a headless JSON worker; let the external harness own that gate.

## Context Escalation

```text
measure source
  -> full-input smoke test
  -> full source if it fits
  -> deterministic anchored windows if location is predictable
  -> hierarchical chunk extraction if evidence may occur anywhere
  -> partial or unjudgeable when coverage cannot support a conclusion
```

Always retain the complete source and mechanically verify quotes against it.

## Subagent Pattern

Use subagents for parallel read-heavy tasks:

- one agent gathers source docs,
- one inspects existing repo patterns,
- one audits likely risks,
- main agent merges summaries into the manifest.

Do not let subagents mutate shared files unless the workflow explicitly assigns disjoint files and has a merge gate.
