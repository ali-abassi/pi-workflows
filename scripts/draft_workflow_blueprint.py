#!/usr/bin/env python3
"""Draft a validated workflow blueprint from a plain-language brief using Pi + Sol."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from workflow_factory_common import PI_COMPATIBILITY, assert_blueprint, assert_supported_pi_version, bounded_pi_json_flags, canonical_digest, prepare_pi_runtime_dir


DEFAULT_PI = Path.home() / ".hermes" / "node" / "bin" / "pi"
SKILL_ROOT = Path(__file__).resolve().parent.parent


def pi_path() -> str:
    configured = os.environ.get("PI_BIN")
    if configured:
        return configured
    found = shutil.which("omp") or shutil.which("pi")
    if found:
        return found
    if DEFAULT_PI.is_file():
        return str(DEFAULT_PI)
    raise SystemExit("Pi/OMP not found; set PI_BIN")


def assistant_text(raw: str) -> str:
    final: str | None = None
    error: str | None = None
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Pi JSON protocol emitted malformed line {line_number}: {exc}") from exc
        message = event.get("message") or {}
        if event.get("type") == "message_end" and message.get("role") == "assistant":
            if message.get("stopReason") in {"error", "aborted"}:
                error = message.get("errorMessage") or message.get("stopReason")
            final = "".join(item.get("text", "") for item in message.get("content", []) if item.get("type") == "text")
    if error:
        raise ValueError(f"Pi blueprint draft failed: {error}")
    if not final:
        raise ValueError("Pi stream contained no final assistant message")
    return final


def extract_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("assistant output contained no JSON object")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", required=True, help="Plain text or Markdown workflow brief")
    parser.add_argument("--output", required=True, help="Destination workflow blueprint JSON")
    parser.add_argument("--workflow", required=True, help="Safe kebab-case workflow name")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--effect", choices=["mutation", "read_only"], required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    pi_binary = pi_path()
    version = subprocess.run([pi_binary, "--version"], text=True, capture_output=True, timeout=10, check=False)
    try:
        assert_supported_pi_version((version.stdout or version.stderr).strip(), PI_COMPATIBILITY, pi_binary)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    brief = Path(args.brief).expanduser().resolve().read_text()
    schema = (SKILL_ROOT / "schemas" / "workflow-blueprint.schema.json").read_text()
    example = (SKILL_ROOT / "examples" / "workflow-blueprint.json").read_text()
    peer_example = (SKILL_ROOT / "examples" / "pr-review-peer-blueprint.json").read_text()
    prompt = f"""You are the design compiler for a deterministic Pi workflow factory.
Return exactly one JSON object and no Markdown. It must conform to the supplied schema.

Workflow: {args.workflow}
Version: {args.version}
Effect: {args.effect}

Rules:
- Separate selector, target, decision, and non-goals.
- Every acceptance criterion must have a stable ID and complete mechanical verifier coverage.
- Verifier commands must be argv arrays and must decide facts mechanically; never use an LLM as the only verifier.
- Mutation workflows use runtime kind generic_mutation and lifecycle intake, plan, approval, execute, verify, judge, report unless the brief requires a checked-in specialized runtime.
- If the requested effect is a deterministic local transformation, declare argv-only execution_commands and let the controller execute them; do not spend a model call on the mutation.
- required_steps contains only task actions performed by the selected executor. Approval, input snapshots, verifier execution, telemetry, cost accounting, and report emission are controller-owned and must not be presented as model-completed steps.
- Never claim zero model calls for a model-owned stage. Put deterministic work under execution_commands when a zero-model execution path is required.
- Read-only workflows use runtime kind specialized and must name scripts/run.py as the entrypoint.
- Use schema_version 1.1 and control_flow only when a specialized runtime needs conditional routing; use 1.2 when that workflow also declares peer_collaboration.
- Use schema_version 1.2 and peer_collaboration only when separate peer context, evidence access, model diversity, or an independent specialist materially improves a named stage. Peer workflows require a specialized runtime.
- Pin every peer's provider/model, thinking, tools, and purpose. Keep peer messages untrusted, use bounded message/hop/time/byte limits, require metadata-only logs, and never treat peer agreement as mechanical proof.
- Local peer transport uses OS-user isolation. Network peer transport uses per-agent credentials or mTLS through an endpoint environment variable; never use one shared bearer as peer identity.
- Conditions are a typed JSON AST over stage artifacts. Never emit next_stage from a model or use strings-as-code, JSONata, jq, shell, regex, or custom operators.
- Every nonterminal stage has exactly one default transition. Conditional priorities are unique. Cycles declare max visits and route exhaustion to a terminal stage.
- Inputs must be immutable. Write paths must be minimal and disjoint.
- Include a small but representative fixture. Do not include secrets.
- Declare an owner, isolated concurrency for mutation, retention, redaction patterns, and failure response.
- Do not add unsupported properties.

Brief:
{brief}

Schema:
{schema}

Example shape:
{example}

Peer collaboration example:
{peer_example}
"""
    command = [
        pi_binary, *bounded_pi_json_flags("You compile a workflow brief into exactly one schema-valid JSON blueprint.", pi_binary), "--no-tools",
        "--provider", "openai-codex", "--model", "gpt-5.6-sol", "--thinking", "low", prompt,
    ]
    pi_agent_dir, _ = prepare_pi_runtime_dir(f"blueprint:{args.workflow}:{args.version}:{args.output}")
    env = {**os.environ, "PI_OFFLINE": "1", "PI_CODING_AGENT_DIR": str(pi_agent_dir)}
    proc = subprocess.run(command, text=True, capture_output=True, timeout=args.timeout, check=False, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"Pi blueprint draft failed with exit {proc.returncode}: {proc.stderr[-2000:]}")
    blueprint = extract_object(assistant_text(proc.stdout))
    blueprint["workflow"] = args.workflow
    blueprint["version"] = args.version
    blueprint["effect"] = args.effect
    assert_blueprint(blueprint)
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(blueprint, indent=2) + "\n")
    print(json.dumps({
        "status": "awaiting_approval",
        "blueprint": str(output),
        "workflow": blueprint["workflow"],
        "version": blueprint["version"],
        "stages": blueprint["lifecycle"],
        "plan_sha256": canonical_digest(blueprint),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
