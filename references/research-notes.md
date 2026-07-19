# Research Notes

This file captures current patterns for deterministic Codex workflows.

## Pi 0.80.6 Documentation Refresh (2026-07-12)

- Pi intentionally keeps workflow policy outside the core; extensions, skills,
  prompt templates, packages, RPC, or the SDK provide orchestration. Source:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/usage.md
- Extensions can register tools and commands, intercept lifecycle/tool events,
  persist session entries, render custom UI, and alter active tools. They run
  with full system permissions. `tool_call` handler errors block fail-safe;
  general extension errors are logged and the agent continues. Source:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md
- `ctx.hasUI` is false in JSON and print modes. RPC supports an extension UI
  sub-protocol; headless JSON workers must not depend on dialogs. Source:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md
- JSON mode streams JSONL events. RPC provides a long-lived JSON protocol over
  stdin/stdout. For Node hosts, the SDK is preferred and exposes typed session,
  settings, resource-loader, model-registry, and event APIs. Sources:
  https://pi.dev/docs/latest/rpc and
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/sdk.md
- Pi model records expose `contextWindow` and `maxTokens`; configured limits are
  planning inputs, not proof that a provider will accept a wrapper-heavy payload.
  Source:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md
- Sessions are JSONL trees and can be resumed or forked, but workflow semantic
  checkpoints still belong to the external harness. Source:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md
- Project settings override global settings, and package/extension/skill paths
  can be project-local. Pin resources and record resolved versions. Sources:
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/settings.md and
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/packages.md

Production lesson: model context, source retrieval, and retry behavior must be
validated with real maximum-size artifacts. A nominal context window, successful
fixture, or streamed completion claim is not a deterministic acceptance test.

## OpenAI Docs Patterns

- Codex workflow docs emphasize explicit context and a clear definition of done. Each workflow example includes surface selection, steps, context notes, and verification. Source: https://developers.openai.com/codex/workflows
- Skills are the reusable workflow format. A skill can include instructions, references, scripts, and UI metadata. Source: https://developers.openai.com/codex/skills
- `AGENTS.md` is loaded before work starts and is appropriate for repo-level conventions and commands. Source: https://developers.openai.com/codex/guides/agents-md
- `codex exec` is the automation surface. `--json` emits JSONL events, `--output-schema` constrains the final response, and `-o` writes the final message to a file. Source: https://developers.openai.com/codex/noninteractive
- Hooks run deterministic scripts during Codex lifecycle events. Use `PreToolUse` and `PermissionRequest` for blocking, `PostToolUse`/`Stop` for review feedback, and remember that untrusted hooks must be reviewed. Source: https://developers.openai.com/codex/hooks
- The Agents SDK guide frames Codex CLI as an MCP server that can be orchestrated into deterministic, reviewable multi-agent workflows with guardrails and traces. Source: https://developers.openai.com/codex/guides/agents-sdk
- Auto-review is not a deterministic security guarantee; deterministic workflows still need sandboxing, explicit policy, monitoring, and gates. Source: https://developers.openai.com/codex/concepts/sandboxing/auto-review

## OpenAI Cookbook Patterns

- Iterative repair loops follow a closed loop: produce output, validate it, feed validation failures into the next repair pass, and repeat until validation passes. Source: https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex
- Agent improvement loops start with traces, add human/model feedback, turn feedback into evals, then use evidence to propose harness changes. Source: https://developers.openai.com/cookbook/examples/agents_sdk/agent_improvement_loop
- CI autofix workflows isolate Codex in a read-limited job, save generated diffs as patch artifacts, and open PRs in a separate write-capable job. Source: https://developers.openai.com/cookbook/examples/codex/autofix-github-actions

## GitHub Patterns Observed

- `shinpr/awesome-codex-workflows` curates workflow repos only when they contain reusable artifacts such as commands, agents, skills, templates, scripts, runtimes, or concrete workflow assets. Pattern: durable artifacts are the signal, not prompt snippets. Source: https://github.com/shinpr/awesome-codex-workflows
- `openai/codex-plugin-cc` uses hook lifecycle scripts, including a stop-time review gate. Pattern: gates can be packaged as plugin hooks around session lifecycle. Source: https://github.com/openai/codex-plugin-cc/blob/main/plugins/codex/hooks/hooks.json
- `falcosecurity/prempti` maps external policy verdicts into Codex hook outputs and fails closed by default. Pattern: centralize policy logic outside the hook shim, use `PreToolUse` for earliest denial, and treat empty stdout as allow. Source: https://github.com/falcosecurity/prempti/blob/main/hooks/codex/README.md
- `OthmanAdi/planning-with-files` persists `task_plan.md`, `findings.md`, and `progress.md` on disk so work survives context loss. Pattern: persistent files are more reliable than relying on conversation state. Source: https://github.com/OthmanAdi/planning-with-files
- `Q00/ouroboros` wraps Codex CLI with a specification-first harness: seed files, acceptance criteria, evaluation principles, exit conditions, role profiles, and runtime/session handles. Pattern: make Codex a worker inside a deterministic orchestrator when the workflow needs stronger control. Source: https://github.com/Q00/ouroboros/blob/main/docs/runtime-guides/codex.md
- Open Codex issues around project-local hooks and `--output-schema` show why workflow bundles need smoke tests for hook loading and schema behavior in the target environment. Pattern: verify the harness on the machine before trusting it. Sources: https://github.com/openai/codex/issues/27133, https://github.com/openai/codex/issues/19816, https://github.com/openai/codex/issues/22998

## Design Conclusion

The best deterministic Codex workflow is a layered harness:

```text
AGENTS.md / skill instructions
  -> stage manifest and artifacts
  -> schemas and codex exec runner
  -> hooks/rules for gates
  -> validation loop
  -> final report and durable evidence
```

Use prompts for judgment, scripts for mechanics, schemas for contracts, hooks for enforcement, and artifacts for memory.
