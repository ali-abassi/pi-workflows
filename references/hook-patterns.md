# Hook Patterns

## When To Use Hooks

Use hooks when written instructions are not enough:

- block dangerous commands,
- enforce stage order,
- require a manifest before mutation,
- validate command arguments,
- add stop-time verification feedback,
- log tool usage for audit.

Do not use hooks for soft preferences. Put those in `AGENTS.md` or a skill.

## Minimal PreToolUse Deny

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Blocked by workflow gate."
  }
}
```

## Hook Script Rules

- Read JSON from stdin.
- For Bash and `apply_patch`, inspect `tool_input.command`.
- Return valid camelCase Codex hook JSON on stdout.
- Fail closed for policy engines unless the workflow explicitly accepts fail-open behavior.
- Keep hook shims small; put complex policy in a separate script/module.
- Test both negative and positive paths.

## Common Pitfalls

- Empty stdout with exit 0 usually allows the call.
- `PermissionRequest` only runs when Codex is about to request approval, so do not rely on it as the only safety gate.
- Hook trust is required for non-managed hooks; the user may need `/hooks` in the CLI.
- Project-local hooks depend on trusted project config and the active repo root.
- Avoid broad command regexes that block read-only inspection.

## Suggested Tests

```bash
# No manifest: should deny
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"dangerous command"}}' \
  | .codex/hooks/guard.py

# Read-only command: should allow
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"sed -n 1,80p file"}}' \
  | .codex/hooks/guard.py

# Approved manifest: should allow
WORKFLOW_MANIFEST=.tmp-workflow/manifest.json \
  printf '%s' '{"tool_name":"Bash","tool_input":{"command":"dangerous command"}}' \
  | .codex/hooks/guard.py
```
