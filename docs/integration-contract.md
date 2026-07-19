# Integration contract

## Pi

Pi Workflows is a Pi package with one declared extension and one skill. The
extension registers `pi_workflows`; it never overrides a built-in tool. It uses
Pi's `exec` API, forwards cancellation, throws on non-zero CLI exit, and returns
bounded text plus structured command details.

## Agent X

Agent X exposes its own compact `workflows` tool because it has custom terminal
rendering and already owns a curated prompt surface. It invokes the same `piw`
CLI. Agent X must not contain a second runner or workflow schema.

## Codex

The installer links this repository into `~/.agents/skills/pi-workflows`.
Codex loads `SKILL.md` progressively and invokes `piw`; the workflow remains
portable because no Codex-only prompt or session state is embedded in YAML.

## Claude Code

The installer links this repository into `~/.claude/skills/pi-workflows`.
Claude Code follows supported skill-directory symlinks. Deterministic lifecycle
hooks remain optional; workflow control flow stays in the runner, not in a hook.

## Loops

Loops provides the localhost API, graph/configuration canvas, live run events,
and launchd-backed triggers. It resolves the installed Pi Workflows runner and
returns the exact event path for a started run. Pi Workflows may fall back to a
direct run when Loops is absent or resolves a different workflow path.

## Compatibility

- Reviewed Pi range: `0.80.5` through `0.80.10`.
- Tested Pi version: `0.80.10`.
- Python: 3.11 or newer, with PyYAML 6.x and ruamel.yaml 0.18.x.
- Workflow YAML is the cross-agent API. CLI JSON is the automation API.
