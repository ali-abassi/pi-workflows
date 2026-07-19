#!/usr/bin/env python3
"""Run a staged, artifact-backed task through Pi using Codex model roles."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow_factory_common import GENERIC_MUTATION_LIFECYCLE, assert_supported_pi_version, bounded_pi_json_flags, canonical_digest as factory_digest, paths_overlap, prepare_pi_runtime_dir


SKILL = Path(__file__).resolve().parents[1]
GUARD_EXTENSION = SKILL / "extensions" / "harness-guard.ts"
RESULT_EXTENSION = SKILL / "extensions" / "stage-result.ts"
TRACKER_RENDERER = SKILL / "scripts" / "render_harness_tracker.py"
REGISTRY_UPDATER = SKILL / "scripts" / "update_harness_registry.py"
FACTORY_COMMON = SKILL / "scripts" / "workflow_factory_common.py"
DEFAULT_PI = Path.home() / ".hermes" / "node" / "bin" / "pi"
CURRENT_RUN_DIR: Path | None = None
WORKSPACE_LOCK: Any = None
STREAM_RENDER_INTERVAL_SECONDS = 1.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_digest(value: Any) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def sealable_artifacts(run_dir: Path) -> list[dict[str, Any]]:
    included = []
    candidates = [path for path in run_dir.rglob("*") if path.is_file()]
    for path in sorted(candidates):
        relative_path = str(path.relative_to(run_dir))
        if relative_path == "integrity/run-seal.json":
            continue
        included.append({
            "path": relative_path,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        })
    return included


def write_run_seal(run_dir: Path) -> dict[str, Any]:
    included = sealable_artifacts(run_dir)
    seal = {
        "algorithm": "sha256",
        "created_at": utc_now(),
        "artifact_count": len(included),
        "artifacts": included,
        "digest": canonical_digest(included),
    }
    write_json(run_dir / "integrity" / "run-seal.json", seal)
    return seal


def verify_run_seal(run_dir: Path) -> bool:
    seal_path = run_dir / "integrity" / "run-seal.json"
    if not seal_path.is_file():
        return False
    seal = read_json(seal_path)
    artifacts = seal.get("artifacts")
    expected = sealable_artifacts(run_dir)
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or seal.get("artifact_count") != len(artifacts)
        or seal.get("digest") != canonical_digest(artifacts)
        or artifacts != expected
    ):
        return False
    return True


def acquire_workspace_lock(harness: Path, workdir: Path) -> Any:
    del harness  # Lock scope is machine-wide so separate harnesses cannot share a mutating workspace.
    lock_root = Path("/tmp") / f"deterministic-codex-workflows-{os.getuid()}" / "workspace-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{sha256_bytes(str(workdir).encode())}.lock"
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise SystemExit(f"workspace is already owned by another mutating harness run: {workdir}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps({"pid": os.getpid(), "workdir": str(workdir), "acquired_at": utc_now()}) + "\n")
    handle.flush()
    return handle


def process_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def reconcile_stale_runs(harness: Path) -> list[str]:
    reconciled: list[str] = []
    runs_root = harness / "runs"
    if not runs_root.is_dir():
        return reconciled
    for state_path in runs_root.glob("*/*/state.json"):
        state = read_json(state_path)
        if state.get("status") != "running":
            continue
        run_dir = state_path.parent
        manifest_path = run_dir / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        runner = manifest.get("runner") or {}
        same_host = runner.get("host") == socket.gethostname()
        if same_host and process_alive(runner.get("pid")):
            continue
        now = utc_now()
        write_json(run_dir / "failure.json", {
            "status": "abandoned",
            "error_type": "StaleRunReconciled",
            "error": "Run was left running without a live owning process.",
            "timestamp": now,
        })
        state.update({"status": "abandoned", "next_action": "Start a new run or resume from verified checkpoints.", "updated_at": now})
        write_json(state_path, state)
        append_event(run_dir, "stale_run_reconciled", previous_status="running")
        try:
            seal_and_render(run_dir)
        except Exception:
            write_run_seal(run_dir)
            pass
        reconciled.append(str(run_dir))
    return reconciled


def finalize_interrupted_run(run_dir: Path, reason: str = "Operator interrupted the run") -> None:
    now = utc_now()
    write_json(run_dir / "failure.json", {"status": "canceled", "error_type": "KeyboardInterrupt", "error": reason, "timestamp": now})
    append_event(run_dir, "canceled", reason=reason)
    state_path = run_dir / "state.json"
    if state_path.exists():
        state = read_json(state_path)
        state.update({"status": "canceled", "next_action": "Resume only from matching verified checkpoints.", "updated_at": now})
        write_json(state_path, state)
    stream_path = run_dir / "stream_state.json"
    if stream_path.exists():
        stream = read_json(stream_path)
        stream.update({"active": False, "updated_at": now})
        write_json(stream_path, stream)
    seal_and_render(run_dir)


def append_event(run_dir: Path, event_type: str, **data: Any) -> None:
    record = {"timestamp": utc_now(), "source": "runner", "type": event_type, **data}
    for path in (run_dir / "events" / "harness.jsonl", run_dir / "tracker.jsonl"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def render_run_tracker(run_dir: Path) -> None:
    subprocess.run([sys.executable, str(TRACKER_RENDERER), "--run", str(run_dir)], text=True, capture_output=True, check=True)


def render_tracker(run_dir: Path) -> None:
    render_run_tracker(run_dir)
    sync_registry(run_dir)


def seal_and_render(run_dir: Path) -> None:
    # The first seal lets the derived tracker display a verified state. The
    # second binds that exact rendered tracker into the authoritative inventory.
    write_run_seal(run_dir)
    render_tracker(run_dir)
    write_run_seal(run_dir)


def sync_registry(run_dir: Path) -> None:
    subprocess.run([sys.executable, str(REGISTRY_UPDATER), "--run", str(run_dir)], text=True, capture_output=True, check=True)


def stream_event_update(state: dict[str, Any], event: dict[str, Any]) -> bool:
    event_type = str(event.get("type", "unknown"))
    state["last_event"] = event_type
    state["updated_at"] = utc_now()
    immediate = event_type in {
        "tool_execution_start", "tool_execution_end", "turn_start", "turn_end",
        "message_start", "message_end", "auto_retry_start", "auto_retry_end", "extension_error",
    }
    if event_type == "turn_start":
        state["turns"] += 1
    elif event_type == "message_update":
        delta = event.get("assistantMessageEvent") or {}
        kind = delta.get("type")
        value = delta.get("delta")
        if isinstance(value, str):
            if kind == "text_delta":
                state["text_characters"] += len(value)
            elif kind == "thinking_delta":
                state["thinking_characters"] += len(value)
        state["stream_kind"] = kind
    elif event_type == "tool_execution_start":
        state["tool_calls_started"] += 1
        state["last_tool"] = event.get("toolName")
    elif event_type == "tool_execution_update":
        state["last_tool"] = event.get("toolName")
        partial = event.get("partialResult") or {}
        state["tool_progress_characters"] = len(json.dumps(partial, sort_keys=True))
    elif event_type == "tool_execution_end":
        state["last_tool"] = event.get("toolName")
        state["last_tool_error"] = event.get("isError") is True
    return immediate


def snapshot_inputs(spec: dict[str, Any], workdir: Path) -> dict[str, Any]:
    files = []
    for rel in spec["inputs"]:
        path = (workdir / rel).resolve()
        if not path.is_file():
            raise SystemExit(f"declared input is missing or not a file: {rel}")
        files.append({"path": rel, "sha256": sha256_file(path), "size": path.stat().st_size})
    return {"files": files, "digest": canonical_digest(files)}


def context_preflight(spec: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    policy = spec.get("context_policy", {"strategy": "full", "max_input_bytes": 1000000})
    total = sum(int(item.get("size", 0)) for item in baseline.get("files", []))
    largest = max((int(item.get("size", 0)) for item in baseline.get("files", [])), default=0)
    strategy = policy["strategy"]
    limit = int(policy["max_input_bytes"])
    if strategy != "full":
        return {
            "status": "specialized_runner_required",
            "strategy": strategy,
            "max_input_bytes": limit,
            "total_input_bytes": total,
            "largest_input_bytes": largest,
            "passed": False,
            "reason": "The generic runner cannot truthfully implement segmented or hierarchical context selection.",
        }
    passed = largest <= limit and total <= limit
    return {
        "status": "passed" if passed else "failed",
        "strategy": strategy,
        "max_input_bytes": limit,
        "total_input_bytes": total,
        "largest_input_bytes": largest,
        "passed": passed,
        "reason": None if passed else "Declared full-context inputs exceed the configured preflight limit.",
    }


def snapshot_paths(workdir: Path, rel_paths: list[str]) -> dict[str, Any]:
    entries = []
    for rel in rel_paths:
        root = (workdir / rel).resolve()
        if not root.exists():
            entries.append({"path": rel, "exists": False})
            continue
        if root.is_file():
            entries.append({
                "path": rel,
                "exists": True,
                "kind": "file",
                "sha256": sha256_file(root),
                "size": root.stat().st_size,
                "mtime_ns": root.stat().st_mtime_ns,
            })
            continue
        for child in sorted(path for path in root.rglob("*") if path.is_file()):
            entries.append({
                "path": str(child.relative_to(workdir)),
                "exists": True,
                "kind": "file",
                "sha256": sha256_file(child),
                "size": child.stat().st_size,
                "mtime_ns": child.stat().st_mtime_ns,
            })
    return {"paths": rel_paths, "entries": entries, "digest": canonical_digest(entries)}

def snapshot_undeclared_paths(workdir: Path, allowed_write_paths: list[str]) -> dict[str, Any]:
    entries = []
    for path in sorted(workdir.rglob("*")):
        rel = str(path.relative_to(workdir))
        if any(path_covered(rel, root) or path_covered(root, rel) for root in allowed_write_paths):
            continue
        if path.is_symlink():
            entries.append({"path": rel, "kind": "symlink", "target": os.readlink(path)})
        elif path.is_file():
            entries.append({"path": rel, "kind": "file", "sha256": sha256_file(path), "size": path.stat().st_size})
        elif path.is_dir():
            entries.append({"path": rel, "kind": "directory"})
    return {"entries": entries, "digest": canonical_digest(entries)}



def clean_allowed_write_paths(workdir: Path, rel_paths: list[str], immutable_paths: list[str]) -> list[str]:
    cleaned = []
    immutable = [(workdir / rel).resolve() for rel in immutable_paths]
    for rel in rel_paths:
        overlaps = [locked for locked in immutable_paths if paths_overlap(rel, locked)]
        if overlaps:
            raise SystemExit(f"refusing cleanup because allowed write path {rel!r} overlaps immutable path(s): {', '.join(overlaps)}")
        target = (workdir / rel).resolve()
        if not str(target).startswith(str(workdir) + os.sep) and target != workdir:
            raise SystemExit(f"refusing to clean path outside workdir: {rel}")
        if any(paths_overlap(str(target), str(item)) for item in immutable):
            raise SystemExit(f"refusing to clean immutable path: {rel}")
        if target.is_dir():
            shutil.rmtree(target)
            cleaned.append(rel)
        elif target.exists():
            target.unlink()
            cleaned.append(rel)
    return cleaned


def validate_resolved_path_policy(workdir: Path, spec: dict[str, Any]) -> None:
    resolved_workdir = workdir.resolve()
    resolved: dict[str, list[tuple[str, Path]]] = {}
    for field in ("inputs", "immutable_paths", "allowed_write_paths"):
        resolved[field] = []
        for relative in spec.get(field, []):
            path = (resolved_workdir / relative).resolve()
            try:
                path.relative_to(resolved_workdir)
            except ValueError as exc:
                raise SystemExit(f"{field} resolves outside the workdir through a symlink: {relative}") from exc
            resolved[field].append((relative, path))
    overlaps = [
        f"{write_name} -> {write_path} <-> {locked_name} -> {locked_path}"
        for write_name, write_path in resolved["allowed_write_paths"]
        for locked_name, locked_path in resolved["immutable_paths"]
        if paths_overlap(str(write_path), str(locked_path))
    ]
    if overlaps:
        raise SystemExit("resolved allowed-write and immutable paths overlap: " + ", ".join(overlaps))


def validate_spec(spec: Any) -> None:
    if not isinstance(spec, dict):
        raise SystemExit("task spec must be a JSON object")
    required = {
        "task_id": str,
        "objective": str,
        "objective_contract": dict,
        "lifecycle": list,
        "workdir": str,
        "inputs": list,
        "constraints": list,
        "acceptance_criteria": list,
        "allowed_tools": list,
        "verification_commands": list,
    }
    optional = {
        "verification_timeout_seconds", "max_repairs", "required_steps",
        "execution_commands", "allowed_write_paths", "immutable_paths", "allow_bash",
        "clean_allowed_write_paths", "step_validation", "context_policy", "verification_contracts"
    }
    missing = sorted(set(required) - set(spec))
    extra = sorted(set(spec) - set(required) - optional)
    if missing:
        raise SystemExit(f"task spec missing required keys: {', '.join(missing)}")
    if extra:
        raise SystemExit(f"task spec has unsupported keys: {', '.join(extra)}")
    for key, expected in required.items():
        if not isinstance(spec[key], expected):
            raise SystemExit(f"task spec {key} must be {expected.__name__}")
    if not spec["task_id"].strip() or not spec["objective"].strip():
        raise SystemExit("task_id and objective must be non-empty")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", spec["task_id"]):
        raise SystemExit("task_id must be a safe path segment using only letters, numbers, dot, underscore, or dash")
    objective_contract = spec["objective_contract"]
    if set(objective_contract) != {"selector", "target", "decision", "non_goals"}:
        raise SystemExit("objective_contract must contain exactly selector, target, decision, and non_goals")
    for key in ("selector", "target", "decision"):
        if not isinstance(objective_contract.get(key), str) or not objective_contract[key].strip():
            raise SystemExit(f"objective_contract.{key} must be non-empty")
    if not isinstance(objective_contract.get("non_goals"), list) or not all(isinstance(item, str) and item.strip() for item in objective_contract["non_goals"]):
        raise SystemExit("objective_contract.non_goals must contain non-empty strings")
    lifecycle = spec["lifecycle"]
    if len(lifecycle) < 2 or len(set(lifecycle)) != len(lifecycle) or not all(isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9-]*", item) for item in lifecycle):
        raise SystemExit("lifecycle must contain at least two unique safe stage names")
    if "report" not in lifecycle:
        raise SystemExit("lifecycle must include report")
    if lifecycle != GENERIC_MUTATION_LIFECYCLE:
        raise SystemExit("the generic mutation runner requires the exact lifecycle: " + " -> ".join(GENERIC_MUTATION_LIFECYCLE))
    criteria = spec["acceptance_criteria"]
    if not criteria or not all(
        isinstance(item, dict)
        and set(item) == {"id", "description"}
        and isinstance(item["id"], str)
        and re.fullmatch(r"[a-z][a-z0-9-]*", item["id"])
        and isinstance(item["description"], str)
        and item["description"].strip()
        for item in criteria
    ):
        raise SystemExit("acceptance_criteria must contain typed id and description objects")
    if len({item["id"] for item in criteria}) != len(criteria):
        raise SystemExit("acceptance criterion ids must be unique")
    allowed = {"read", "bash", "edit", "write", "grep", "find", "ls"}
    if not all(isinstance(x, str) and x in allowed for x in spec["allowed_tools"]):
        raise SystemExit("allowed_tools contains an unsupported Pi tool")
    if any(tool in spec["allowed_tools"] for tool in ("write", "edit")) and not spec.get("allowed_write_paths"):
        raise SystemExit("allowed_write_paths is required when write or edit is enabled")
    if "bash" in spec["allowed_tools"] and spec.get("allow_bash") is not True:
        raise SystemExit("allow_bash must be true when bash is enabled for the model")
    commands = spec["verification_commands"]
    if not commands or not all(isinstance(c, list) and c and all(isinstance(x, str) for x in c) for c in commands):
        raise SystemExit("verification_commands must be a non-empty array of argv arrays")
    contracts = spec.get("verification_contracts")
    criterion_ids = {item["id"] for item in criteria}
    if not isinstance(contracts, list) or not contracts:
        raise SystemExit("verification_contracts must preserve verifier ids, coverage, commands, and timeouts")
    covered: set[str] = set()
    verifier_ids: set[str] = set()
    for contract in contracts:
        if not isinstance(contract, dict) or set(contract) != {"id", "covers", "command", "timeout_seconds"}:
            raise SystemExit("each verification contract must contain exactly id, covers, command, and timeout_seconds")
        verifier_id = contract.get("id")
        covers = contract.get("covers")
        command = contract.get("command")
        timeout = contract.get("timeout_seconds")
        if not isinstance(verifier_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", verifier_id) or verifier_id in verifier_ids:
            raise SystemExit("verification contract ids must be unique safe names")
        if not isinstance(covers, list) or not covers or not set(covers).issubset(criterion_ids):
            raise SystemExit("verification contract covers must reference acceptance criterion ids")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise SystemExit("verification contract command must be a non-empty argv array")
        if not isinstance(timeout, int) or timeout < 1:
            raise SystemExit("verification contract timeout_seconds must be positive")
        verifier_ids.add(verifier_id)
        covered.update(covers)
    if covered != criterion_ids:
        raise SystemExit("every acceptance criterion must have runtime verifier coverage")
    if [contract["command"] for contract in contracts] != commands:
        raise SystemExit("verification_commands must exactly match verification_contracts commands")
    for key in ("execution_commands",):
        value = spec.get(key, [])
        if not isinstance(value, list) or not all(isinstance(c, list) and c and all(isinstance(x, str) for x in c) for c in value):
            raise SystemExit(f"{key} must be an array of argv arrays")
    for key in ("allowed_write_paths", "immutable_paths"):
        value = spec.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
            raise SystemExit(f"{key} must contain path strings")
    for key in ("inputs", "allowed_write_paths", "immutable_paths"):
        for value in spec.get(key, []):
            path = Path(value)
            if path.is_absolute() or value in {"", "."} or ".." in path.parts:
                raise SystemExit(f"{key} contains an unsafe path; paths must be non-rooted and cannot contain '..': {value}")
    overlaps = [
        f"{write_path} <-> {locked_path}"
        for write_path in spec.get("allowed_write_paths", [])
        for locked_path in spec.get("immutable_paths", spec["inputs"])
        if paths_overlap(write_path, locked_path)
    ]
    if overlaps:
        raise SystemExit("allowed_write_paths and immutable_paths must be disjoint in both directions: " + ", ".join(overlaps))
    immutable_paths = spec.get("immutable_paths", spec["inputs"])
    unprotected_inputs = [value for value in spec["inputs"] if not any(path_covered(value, root) for root in immutable_paths)]
    if unprotected_inputs:
        raise SystemExit(f"every declared input must be covered by immutable_paths: {', '.join(unprotected_inputs)}")
    if not isinstance(spec.get("allow_bash", False), bool):
        raise SystemExit("allow_bash must be boolean")
    if not isinstance(spec.get("clean_allowed_write_paths", False), bool):
        raise SystemExit("clean_allowed_write_paths must be boolean")
    max_repairs = spec.get("max_repairs", 1)
    if not isinstance(max_repairs, int) or not 0 <= max_repairs <= 3:
        raise SystemExit("max_repairs must be an integer from 0 through 3")
    required_steps = spec.get("required_steps", [])
    if not isinstance(required_steps, list) or len(required_steps) > 50:
        raise SystemExit("required_steps must be an array with at most 50 entries")
    if not all(isinstance(x, str) and x.strip() for x in required_steps):
        raise SystemExit("required_steps must contain non-empty strings")
    if len(set(required_steps)) != len(required_steps):
        raise SystemExit("required_steps must be unique")
    step_validation = spec.get("step_validation", {"enabled": False})
    if not isinstance(step_validation, dict):
        raise SystemExit("step_validation must be an object")
    step_keys = {"enabled", "model", "thinking", "mode", "min_score", "max_attempts_per_step", "timeout_seconds"}
    extra_step_keys = sorted(set(step_validation) - step_keys)
    if extra_step_keys:
        raise SystemExit(f"step_validation has unsupported keys: {', '.join(extra_step_keys)}")
    if not isinstance(step_validation.get("enabled", False), bool):
        raise SystemExit("step_validation.enabled must be boolean")
    if step_validation.get("enabled") and not required_steps:
        raise SystemExit("step_validation requires a non-empty required_steps contract")
    if not isinstance(step_validation.get("model", "openai-codex/gpt-5.6-luna"), str):
        raise SystemExit("step_validation.model must be a model string")
    if step_validation.get("thinking", "low") not in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        raise SystemExit("step_validation.thinking is unsupported")
    if step_validation.get("mode", "gate") not in {"gate", "advisory"}:
        raise SystemExit("step_validation.mode must be gate or advisory")
    score = step_validation.get("min_score", 8)
    if not isinstance(score, (int, float)) or not 0 <= score <= 10:
        raise SystemExit("step_validation.min_score must be from 0 through 10")
    max_step_attempts = step_validation.get("max_attempts_per_step", 2)
    if not isinstance(max_step_attempts, int) or not 1 <= max_step_attempts <= 3:
        raise SystemExit("step_validation.max_attempts_per_step must be from 1 through 3")
    if not isinstance(step_validation.get("timeout_seconds", 120), int) or step_validation.get("timeout_seconds", 120) < 1:
        raise SystemExit("step_validation.timeout_seconds must be a positive integer")
    context_policy = spec.get("context_policy", {"strategy": "full", "max_input_bytes": 1000000})
    if not isinstance(context_policy, dict) or set(context_policy) != {"strategy", "max_input_bytes"}:
        raise SystemExit("context_policy must contain exactly strategy and max_input_bytes")
    if context_policy.get("strategy") not in {"full", "deterministic_segments", "hierarchical_chunks"}:
        raise SystemExit("context_policy.strategy is unsupported")
    if not isinstance(context_policy.get("max_input_bytes"), int) or context_policy["max_input_bytes"] < 1:
        raise SystemExit("context_policy.max_input_bytes must be a positive integer")


def extract_json(raw: str) -> Any:
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "```" in text:
        for chunk in text.split("```"):
            candidate = chunk.removeprefix("json").strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("Pi response did not contain a JSON object")


def assistant_text_from_events(raw: str) -> str:
    assistant = None
    error = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant":
            message = event["message"]
            parts = (event["message"].get("content") or [])
            text = "".join(part.get("text", "") for part in parts if part.get("type") == "text")
            if text.strip():
                assistant = text
                error = None
            elif message.get("stopReason") == "error":
                error = message.get("errorMessage") or "assistant message ended with stopReason=error"
    if assistant is None:
        if error:
            raise RuntimeError(error)
        raise ValueError("Pi JSON stream did not contain a final assistant message")
    return assistant

def stage_result_from_events(raw: str, expected_stage: str) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") != "tool_execution_end" or event.get("toolName") != "submit_stage_result":
            continue
        if event.get("isError") is True:
            raise RuntimeError("submit_stage_result failed")
        details = (event.get("result") or {}).get("details")
        if not isinstance(details, dict) or details.get("submitted") is not True:
            raise ValueError("submit_stage_result did not return a submitted result")
        results.append(details)
    if len(results) != 1:
        raise ValueError(f"Pi stage must submit exactly one result; received {len(results)}")
    result = results[0]
    expected_result_stage = "repair" if expected_stage.startswith("repair-") else expected_stage
    if result.get("stage") != expected_result_stage:
        raise ValueError(
            f"Pi stage result mismatch: expected {expected_result_stage!r}, received {result.get('stage')!r}"
        )
    return {key: value for key, value in result.items() if key not in {"stage", "submitted"}}



def validate_pi_event_stream(
    raw: str,
    stderr: str,
    expected_model: str | None = None,
    expected_stage: str | None = None,
) -> dict[str, Any]:
    events = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Pi JSON protocol emitted malformed line {line_number}: {exc}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError(f"Pi JSON protocol emitted invalid event at line {line_number}")
        events.append(event)
    if not events:
        raise ValueError("Pi JSON protocol emitted no events")
    if any(event.get("type") == "extension_error" for event in events) or "Extension error (" in stderr:
        raise RuntimeError("Pi extension failed; stage evidence is not trustworthy")
    failed_retries = [event for event in events if event.get("type") == "auto_retry_end" and event.get("success") is False]
    settled_indices = [index for index, event in enumerate(events) if event.get("type") == "agent_settled"]
    assistants = [
        event.get("message") or {} for event in events
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant"
    ]
    if not assistants:
        raise ValueError("Pi JSON protocol emitted no final assistant message")
    final = assistants[-1]
    final_assistant_index = max(
        index for index, event in enumerate(events)
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant"
    )
    completion_index = final_assistant_index
    if expected_stage is not None:
        stage_result_from_events(raw, expected_stage)
        completion_index = max(
            completion_index,
            max(
                index for index, event in enumerate(events)
                if event.get("type") == "tool_execution_end"
                and event.get("toolName") == "submit_stage_result"
            ),
        )
    if not settled_indices or settled_indices[-1] < completion_index:
        raise RuntimeError("Pi stage never reached agent_settled after its terminal result")
    accepted_stop_reasons = {"stop", "toolUse"} if expected_stage is not None else {"stop"}
    if final.get("stopReason") not in accepted_stop_reasons or final.get("errorMessage") or failed_retries:
        raise RuntimeError(f"Pi stage ended unsuccessfully: {final.get('errorMessage') or final.get('stopReason') or 'retry failed'}")
    final_text = "".join(
        part.get("text", "") for part in (final.get("content") or [])
        if part.get("type") == "text"
    )
    if not final_text.strip() and expected_stage is None:
        raise ValueError("Pi JSON protocol emitted an empty final assistant message")
    if expected_model:
        expected_provider, expected_model_id = split_model(expected_model)
        if final.get("provider") != expected_provider or final.get("model") != expected_model_id:
            actual = f"{final.get('provider')}/{final.get('model')}"
            raise RuntimeError(f"Pi model pin drifted: expected {expected_model}, received {actual}")
    return {
        "events": len(events),
        "assistant_messages": len(assistants),
        "stop_reason": final.get("stopReason"),
        "failed_retries": len(failed_retries),
        "agent_settled": True,
        "provider": final.get("provider"),
        "model": final.get("model"),
        "structured_result": expected_stage is not None,
    }


def pi_jsonl_verdict(stdout: str) -> dict[str, Any] | None:
    """Inspect a subprocess's stdout for a genuine Pi JSON-protocol event stream and,
    if present, judge it the same way run_pi()/validate_pi_event_stream() judge a
    directly-run Pi stage.

    execution_commands and verification_contracts are arbitrary subprocess argv
    supplied by the task spec; some of them (LLM-judge style verifiers, or a
    verifier that itself shells out to `pi --mode json`) legitimately emit Pi's
    JSONL protocol on stdout. Pi's own CLI process can exit 0 even when the final
    assistant turn ended with stopReason "aborted" (or "error", or a failed
    auto-retry) rather than "stop" -- a clean process exit is not proof the
    underlying model turn actually completed. Without this check, run_verifiers()
    and run_supervisor_commands() previously judged those commands on exit code
    alone and could accept a zero exit code as "passed" even though the Pi JSONL
    they produced recorded an aborted/errored stop reason.

    Returns None when stdout does not unambiguously look like Pi protocol output
    (no confirmed assistant message_end event) -- callers must fall back to plain
    exit-code semantics in that case so ordinary verifier commands (pytest,
    domain assert scripts, shell tests, ...) are unaffected. Returns a verdict
    dict, with "passed" reflecting the same fail-closed rules as
    validate_pi_event_stream, when the stream is confirmed to be Pi protocol.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    events: list[dict[str, Any]] = []
    malformed_at: int | None = None
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed_at = line_number
            break
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            malformed_at = line_number
            break
        events.append(event)
    assistants = [
        event.get("message") or {} for event in events
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant"
    ]
    if not assistants:
        # Never confirmed this is Pi protocol output (either it's ordinary verifier
        # output, or it broke before ever proving itself Pi-shaped). Do not guess;
        # preserve legacy exit-code-only semantics for whatever this command is.
        return None
    extension_error = any(event.get("type") == "extension_error" for event in events)
    failed_retries = [event for event in events if event.get("type") == "auto_retry_end" and event.get("success") is False]
    settled_indices = [index for index, event in enumerate(events) if event.get("type") == "agent_settled"]
    final = assistants[-1]
    final_assistant_index = max(
        index for index, event in enumerate(events)
        if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant"
    )
    stop_reason = final.get("stopReason")
    reasons = []
    if malformed_at is not None:
        reasons.append(f"malformed Pi event at line {malformed_at}")
    if extension_error:
        reasons.append("Pi extension_error event present")
    if failed_retries:
        reasons.append(f"{len(failed_retries)} failed auto_retry")
    if not settled_indices or settled_indices[-1] < final_assistant_index:
        reasons.append("missing agent_settled after final assistant message")
    if stop_reason != "stop":
        reasons.append(f"final stopReason={stop_reason!r}")
    if final.get("errorMessage"):
        reasons.append(f"errorMessage={final['errorMessage']!r}")
    return {
        "is_pi_protocol": True,
        "passed": not reasons,
        "stop_reason": stop_reason,
        "events": len(events),
        "assistant_messages": len(assistants),
        "failed_retries": len(failed_retries),
        "extension_error": extension_error,
        "agent_settled": bool(settled_indices and settled_indices[-1] >= final_assistant_index),
        "malformed_at_line": malformed_at,
        "reasons": reasons,
    }


def split_model(model: str) -> tuple[str | None, str]:
    if "/" not in model:
        return None, model
    provider, model_id = model.split("/", 1)
    return provider, model_id


def validate_stage_output(stage: str, value: dict[str, Any]) -> None:
    kind = "repair" if stage.startswith("repair-") else stage
    contracts = {
        "intake": {"task_id": str, "objective": str, "inputs": list, "constraints": list, "acceptance_criteria": list, "ambiguities": list},
        "plan": {"summary": str, "steps": list, "files_expected_to_change": list, "risks": list, "verification_mapping": list},
        "execute": {"status": str, "summary": str, "files_changed": list, "commands_run": list, "residual_risk": list, "completed_steps": list},
        "repair": {"status": str, "diagnosis": str, "changes": list, "residual_risk": list, "completed_steps": list},
        "judge": {"accepted": bool, "score": (int, float), "criteria": list, "evidence": list, "residual_risk": list},
    }
    contract = contracts.get(kind)
    if not contract:
        return
    for key, expected in contract.items():
        if key not in value or not isinstance(value[key], expected):
            raise ValueError(f"Pi stage {stage} violates output contract: {key}")
    if kind == "judge" and not 0 <= value["score"] <= 10:
        raise ValueError("judge.score must be from 0 through 10")


def deterministic_intake(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": spec["task_id"],
        "objective": spec["objective"],
        "inputs": spec["inputs"],
        "constraints": spec["constraints"],
        "acceptance_criteria": spec["acceptance_criteria"],
        "ambiguities": [],
    }


def deterministic_plan(spec: dict[str, Any]) -> dict[str, Any] | None:
    required_steps = spec.get("required_steps") or []
    execution_commands = spec.get("execution_commands") or []
    if not required_steps and not execution_commands:
        return None
    plan_steps = required_steps or [
        f"Run approved supervisor command {index}: {shlex.join(command)}"
        for index, command in enumerate(execution_commands, start=1)
    ]
    verifier_ids = [item["id"] for item in spec.get("verification_contracts", [])]
    return {
        "summary": f"Execute {len(plan_steps)} approved steps in order.",
        "steps": [
            {
                "id": f"step-{index}",
                "name": name,
                "action": name,
                "outputs": spec.get("allowed_write_paths", []),
                "verification": verifier_ids,
            }
            for index, name in enumerate(plan_steps, start=1)
        ],
        "execution_commands": execution_commands,
        "files_expected_to_change": spec.get("allowed_write_paths", []),
        "risks": ["Supervisor commands execute with controller privileges."] if execution_commands else [],
        "verification_mapping": [
            {"step": name, "verifier_ids": verifier_ids}
            for name in plan_steps
        ],
    }


def mechanical_judge(criteria_validation: dict[str, Any]) -> dict[str, Any]:
    accepted = criteria_validation.get("status") == "passed"
    criteria = criteria_validation.get("criteria") or []
    return {
        "accepted": accepted,
        "score": 10 if accepted else 0,
        "criteria": criteria,
        "evidence": [
            evidence
            for criterion in criteria
            for evidence in criterion.get("evidence", [])
        ],
        "residual_risk": [
            criterion["id"]
            for criterion in criteria
            if criterion.get("status") != "passed"
        ],
    }


def write_deterministic_stage(run_dir: Path, stage: str, value: dict[str, Any]) -> dict[str, Any]:
    validate_stage_output(stage, value)
    value["_harness"] = {"mode": "deterministic", "elapsed_seconds": 0.0}
    write_json(run_dir / "stages" / f"{stage}.json", value)
    append_event(run_dir, "deterministic_stage_end", stage=stage)
    return value


def stage_completed(value: dict[str, Any]) -> bool:
    status = str(value.get("status", "")).lower()
    return status in {"completed", "complete", "passed", "done", "success"} or status.startswith("repaired")


def hook_policy_summary(run_dir: Path) -> dict[str, Any]:
    tracker = run_dir / "tracker.jsonl"
    blocked = []
    write_paths = []
    tool_calls = 0
    if tracker.exists():
        for line in tracker.read_text().splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "tool_call":
                tool_calls += 1
            if event.get("blocked") is True:
                blocked.append({
                    "stage": event.get("stage"),
                    "tool": event.get("toolName"),
                    "reason": event.get("reason"),
                    "timestamp": event.get("timestamp"),
                })
            if event.get("blocked") is False and event.get("toolName") in {"write", "edit"} and event.get("path"):
                write_paths.append(str(event["path"]))
    return {
        "status": "passed" if not blocked else "failed",
        "tool_calls": tool_calls,
        "blocked_count": len(blocked),
        "blocked": blocked,
        "model_write_paths": sorted(set(write_paths)),
    }

def runtime_metrics(run_dir: Path, started_at: float, supervisor_owned: bool) -> dict[str, Any]:
    calls = input_tokens = output_tokens = 0
    api_equivalent_cost = 0.0
    for path in sorted((run_dir / "events").glob("pi-*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message") or {}
            if event.get("type") != "message_end" or message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            if not usage:
                continue
            calls += 1
            input_tokens += int(usage.get("input", 0) or 0)
            output_tokens += int(usage.get("output", 0) or 0)
            api_equivalent_cost += float((usage.get("cost") or {}).get("total", 0) or 0)
    return {
        "execution_owner": "deterministic_supervisor" if supervisor_owned else "model",
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "model_calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "api_equivalent_cost_usd": round(api_equivalent_cost, 6),
    }



def path_covered(path: str, root: str) -> bool:
    clean_path = path.strip("/")
    clean_root = root.strip("/")
    return clean_path == clean_root or clean_path.startswith(clean_root + "/")


def model_write_coverage(allowed_write_paths: list[str], hook_policy: dict[str, Any]) -> dict[str, Any]:
    roots = allowed_write_paths or []
    write_paths = hook_policy.get("model_write_paths", [])
    missing = [root for root in roots if not any(path_covered(path, root) for path in write_paths)]
    return {
        "status": "passed" if not missing else "failed",
        "required_roots": roots,
        "model_write_paths": write_paths,
        "missing_roots": missing,
    }


def step_validation_summary(run_dir: Path, spec: dict[str, Any]) -> dict[str, Any]:
    config = spec.get("step_validation") or {"enabled": False}
    required = spec.get("required_steps", [])
    if not config.get("enabled"):
        return {"status": "disabled", "label": "Luna review", "enabled": False, "mode": config.get("mode", "gate"), "required": len(required), "accepted": 0, "attempts": 0, "average_score": None, "steps": []}
    steps = []
    attempts_total = 0
    for index, name in enumerate(required, start=1):
        directory = run_dir / "step-validation" / f"{index:02d}"
        attempts = sorted(directory.glob("attempt-*.json")) if directory.is_dir() else []
        attempts_total += len(attempts)
        accepted = read_json(directory / "accepted.json") if (directory / "accepted.json").exists() else None
        steps.append({
            "index": index,
            "name": name,
            "accepted": bool(accepted and accepted.get("validator_accepted") is True),
            "accepted_for_progress": bool(accepted and accepted.get("accepted_for_progress") is True),
            "score": (accepted or {}).get("review", {}).get("score"),
            "review": (accepted or {}).get("review", {}).get("review"),
            "guidance": (accepted or {}).get("review", {}).get("guidance", []),
            "attempts": len(attempts),
            "artifact": str(directory / "accepted.json") if accepted else None,
        })
    mode = config.get("mode", "gate")
    passed = all(step["accepted"] if mode == "gate" else step["accepted_for_progress"] for step in steps)
    scores = [float(step["score"]) for step in steps if isinstance(step.get("score"), (int, float))]
    return {
        "status": "passed" if passed else "failed",
        "label": "Luna review",
        "enabled": True,
        "mode": mode,
        "validator_model": config.get("model", "openai-codex/gpt-5.6-luna"),
        "min_score": config.get("min_score", 8),
        "required": len(required),
        "accepted": sum(1 for step in steps if step["accepted"]),
        "progress_accepted": sum(1 for step in steps if step["accepted_for_progress"]),
        "attempts": attempts_total,
        "average_score": round(sum(scores) / len(scores), 2) if scores else None,
        "steps": steps,
    }


def set_state(run_dir: Path, manifest: dict[str, Any], stage: str, status: str, next_action: str | None) -> None:
    write_json(
        run_dir / "state.json",
        {
            "workflow": manifest["workflow"],
            "task_id": manifest["task_id"],
            "run_id": manifest["run_id"],
            "stage": stage,
            "status": status,
            "updated_at": utc_now(),
            "next_action": next_action,
            "models": manifest["models"],
        },
    )
    append_event(run_dir, "state", stage=stage, status=status, next_action=next_action)
    sync_registry(run_dir)


def pi_path() -> str:
    configured = os.environ.get("PI_BIN")
    if configured:
        return configured
    found = shutil.which("omp") or shutil.which("pi")
    if found:
        return found
    if DEFAULT_PI.exists():
        return str(DEFAULT_PI)
    raise SystemExit("Pi/OMP not found; set PI_BIN")


def command_version(command: str) -> str:
    proc = subprocess.run([command, "--version"], text=True, capture_output=True, timeout=10, check=False)
    return (proc.stdout or proc.stderr).strip() or f"exit-{proc.returncode}"


def build_pi_stage_command(model: str, thinking: str, prompt: str, tools: list[str] | None) -> list[str]:
    provider, model_id = split_model(model)
    command = [
        pi_path(),
        *bounded_pi_json_flags(
            "You are a bounded workflow stage worker. Follow the supplied task contract, use only enabled tools, and return exactly the requested structured result."
        ),
        "--extension",
        str(GUARD_EXTENSION),
        "--extension",
        str(RESULT_EXTENSION),
        "--skill",
        str(SKILL),
    ]
    if provider:
        command += ["--provider", provider]
    command += ["--model", model_id, "--thinking", thinking]
    command += ["--tools", ",".join([*(tools or []), "submit_stage_result"]), prompt]
    return command




def terminate_process_group(proc: subprocess.Popen[Any], grace_seconds: float = 2) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=grace_seconds)

def run_pi(
    *,
    run_dir: Path,
    stage: str,
    model: str,
    thinking: str,
    prompt: str,
    workdir: Path,
    tools: list[str] | None,
    timeout_seconds: int,
    allowed_write_paths: list[str] | None = None,
    immutable_paths: list[str] | None = None,
    allow_bash: bool = False,
    required_steps: list[str] | None = None,
    step_validation: dict[str, Any] | None = None,
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_path = run_dir / "drafts" / f"{stage}.raw.txt"
    stderr_path = run_dir / "drafts" / f"{stage}.stderr.txt"
    command = build_pi_stage_command(model, thinking, prompt, tools)
    env = os.environ.copy()
    env.update({
        "HARNESS_AUDIT_FILE": str(run_dir / "events" / f"pi-{stage}-hooks.jsonl"),
        "HARNESS_TRACKER_FILE": str(run_dir / "tracker.jsonl"),
        "HARNESS_STAGE": stage,
        "HARNESS_WORKDIR": str(workdir),
        "HARNESS_ALLOWED_WRITES": json.dumps(allowed_write_paths or []),
        "HARNESS_IMMUTABLE_PATHS": json.dumps(immutable_paths or []),
        "HARNESS_ALLOW_BASH": "1" if allow_bash else "0",
        "HARNESS_RUN_DIR": str(run_dir),
        "HARNESS_PI_BIN": runtime_binary,
        "HARNESS_REQUIRED_STEPS": json.dumps(required_steps or []),
        "HARNESS_STEP_VALIDATION": json.dumps(step_validation or {"enabled": False}),
        "HARNESS_TASK_CONTEXT": json.dumps(task_context or {}),
        "PI_OFFLINE": "1",
        "PI_CODING_AGENT_DIR": os.environ["HARNESS_PI_AGENT_DIR"],
    })
    append_event(run_dir, "pi_stage_start", stage=stage, model=model, thinking=thinking)
    started = time.monotonic()
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events" / f"pi-{stage}.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    stream_state = {
        "active": True,
        "stage": stage,
        "model": model,
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "last_event": "starting",
        "stream_kind": None,
        "text_characters": 0,
        "thinking_characters": 0,
        "tool_calls_started": 0,
        "tool_progress_characters": 0,
        "last_tool": None,
        "last_tool_error": False,
        "turns": 0,
    }
    write_json(run_dir / "stream_state.json", stream_state)
    render_run_tracker(run_dir)
    timed_out = threading.Event()
    lines: list[str] = []
    with stderr_path.open("w") as stderr_handle:
        proc = subprocess.Popen(
            command,
            cwd=workdir,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            env=env,
            bufsize=1,
            start_new_session=True,
        )
        def terminate_for_timeout() -> None:
            timed_out.set()
            terminate_process_group(proc)
        timer = threading.Timer(timeout_seconds, terminate_for_timeout)
        timer.daemon = True
        timer.start()
        last_render = time.monotonic()
        try:
            with events_path.open("w") as events_handle:
                assert proc.stdout is not None
                for line in proc.stdout:
                    lines.append(line)
                    events_handle.write(line)
                    events_handle.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    immediate = stream_event_update(stream_state, event)
                    now = time.monotonic()
                    if immediate or now - last_render >= STREAM_RENDER_INTERVAL_SECONDS:
                        write_json(run_dir / "stream_state.json", stream_state)
                        append_event(
                            run_dir,
                            "pi_stream_progress",
                            stage=stage,
                            last_event=stream_state["last_event"],
                            last_tool=stream_state["last_tool"],
                            text_characters=stream_state["text_characters"],
                            thinking_characters=stream_state["thinking_characters"],
                            tool_calls_started=stream_state["tool_calls_started"],
                            turns=stream_state["turns"],
                        )
                        render_run_tracker(run_dir)
                        last_render = now
            returncode = proc.wait()
        except BaseException:
            terminate_process_group(proc)
            raise
        finally:
            timer.cancel()
    stream_state.update({"active": False, "updated_at": utc_now(), "last_event": "timed_out" if timed_out.is_set() else "complete"})
    write_json(run_dir / "stream_state.json", stream_state)
    append_event(run_dir, "pi_stream_end", stage=stage, timed_out=timed_out.is_set(), **{key: stream_state[key] for key in ("text_characters", "thinking_characters", "tool_calls_started", "turns")})
    render_tracker(run_dir)
    if timed_out.is_set():
        raise TimeoutError(f"Pi stage {stage} exceeded {timeout_seconds} seconds")
    if returncode != 0:
        raise RuntimeError(f"Pi stage {stage} failed with exit {proc.returncode}; see {stderr_path}")
    raw_output = "".join(lines)
    protocol = validate_pi_event_stream(
        raw_output,
        stderr_path.read_text(errors="replace"),
        expected_model=model,
        expected_stage=stage,
    )
    parsed = stage_result_from_events(raw_output, stage)
    raw_path.write_text(json.dumps(parsed, indent=2) + "\n")
    if not isinstance(parsed, dict):
        raise ValueError(f"Pi stage {stage} did not return a JSON object")
    validate_stage_output(stage, parsed)
    parsed["_harness"] = {
        "model": model,
        "thinking": thinking,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "protocol": protocol,
    }
    write_json(run_dir / "stages" / f"{stage}.json", parsed)
    append_event(run_dir, "pi_stage_end", stage=stage, model=model, elapsed_seconds=parsed["_harness"]["elapsed_seconds"])
    return parsed


def resolve_stage_tools(config: dict[str, Any], stage: str, requested: list[str] | None = None) -> list[str] | None:
    profile = (config.get("stage_capabilities") or {}).get(stage)
    if profile is None:
        return requested
    allowed = list(profile.get("tools") or [])
    if requested is None:
        return allowed
    # Step validation registers this pinned extension tool for execution. It is
    # not a Pi built-in capability and therefore is absent from blueprint tool
    # profiles, but it must survive the same least-privilege resolution.
    internal_tools = {"harness_step"} if stage == "execute" else set()
    denied = sorted(set(requested) - set(allowed) - internal_tools)
    if denied:
        raise SystemExit(f"task requested tools outside the compiled {stage} capability profile: {', '.join(denied)}")
    return requested


def run_pi_checkpointed(*, implementation_digest: str, resume: bool, **kwargs: Any) -> dict[str, Any]:
    run_dir = kwargs["run_dir"]
    stage = kwargs["stage"]
    stage_path = run_dir / "stages" / f"{stage}.json"
    checkpoint_path = run_dir / "checkpoints" / f"{stage}.json"
    contract = {
        "stage": stage,
        "model": kwargs["model"],
        "thinking": kwargs["thinking"],
        "prompt_sha256": sha256_bytes(kwargs["prompt"].encode()),
        "tools": kwargs.get("tools"),
        "implementation_digest": implementation_digest,
    }
    digest = canonical_digest(contract)
    if resume and stage_path.is_file() and checkpoint_path.is_file():
        checkpoint = read_json(checkpoint_path)
        if checkpoint.get("status") == "verified" and checkpoint.get("digest") == digest and checkpoint.get("artifact_sha256") == sha256_file(stage_path):
            value = read_json(stage_path)
            validate_stage_output(stage, value)
            append_event(run_dir, "checkpoint_reused", stage=stage, digest=digest)
            return value
    value = run_pi(**kwargs)
    write_json(checkpoint_path, {
        "status": "verified",
        "stage": stage,
        "digest": digest,
        "contract": contract,
        "artifact": str(stage_path.relative_to(run_dir)),
        "artifact_sha256": sha256_file(stage_path),
        "verified_at": utc_now(),
    })
    append_event(run_dir, "checkpoint_written", stage=stage, digest=digest)
    return value


def completed_step_contract(spec: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    required = spec.get("required_steps", [])
    completed = execution.get("completed_steps", []) if required else []
    actual_names = [item.get("name") for item in completed if isinstance(item, dict)]
    statuses_ok = all(item.get("status") == "done" for item in completed if isinstance(item, dict))
    evidence_ok = all(isinstance(item.get("evidence"), str) and item["evidence"].strip() for item in completed if isinstance(item, dict))
    passed = not required or (
        len(completed) == len(required)
        and actual_names == required
        and statuses_ok
        and evidence_ok
    )
    return {
        "kind": "required_step_contract",
        "required_count": len(required),
        "completed_count": len(completed),
        "required_names": required,
        "completed_names": actual_names,
        "statuses_ok": statuses_ok,
        "evidence_ok": evidence_ok,
        "passed": passed,
    }


def check_integrity(workdir: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for item in baseline["files"]:
        path = (workdir / item["path"]).resolve()
        actual = sha256_file(path) if path.is_file() else None
        checks.append({"path": item["path"], "expected_sha256": item["sha256"], "actual_sha256": actual, "passed": actual == item["sha256"]})
    return {"kind": "input_integrity", "checks": checks, "passed": all(item["passed"] for item in checks)}


def run_supervisor_commands(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    workdir = Path(spec["workdir"]).expanduser().resolve()
    results = []
    for index, command in enumerate(spec.get("execution_commands", []), start=1):
        started = time.monotonic()
        proc = subprocess.run(command, cwd=workdir, text=True, capture_output=True, timeout=spec.get("verification_timeout_seconds", 120), check=False)
        pi_protocol = pi_jsonl_verdict(proc.stdout)
        item = {
            "index": index, "command": command, "display_command": shlex.join(command),
            "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "pi_protocol": pi_protocol,
            "passed": proc.returncode == 0 and (pi_protocol is None or pi_protocol["passed"]),
        }
        results.append(item)
        write_json(run_dir / "execution" / f"command-{index}.json", item)
        append_event(
            run_dir, "supervisor_command",
            **{k: item[k] for k in ("index", "display_command", "exit_code", "elapsed_seconds", "passed")},
            pi_protocol_passed=(pi_protocol["passed"] if pi_protocol else None),
        )
        if not item["passed"]:
            break
    result = {"status": "passed" if all(item["passed"] for item in results) else "failed", "commands": results}
    write_json(run_dir / "execution" / "summary.json", result)
    return result


def run_verifiers(spec: dict[str, Any], run_dir: Path, attempt: int, execution: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    workdir = Path(spec["workdir"]).expanduser().resolve()
    results = []
    integrity = check_integrity(workdir, baseline)
    results.append(integrity)
    write_json(run_dir / "validation" / f"attempt-{attempt}-integrity.json", integrity)
    step_contract = completed_step_contract(spec, execution)
    if spec.get("required_steps"):
        results.append(step_contract)
        write_json(run_dir / "validation" / f"attempt-{attempt}-step-contract.json", step_contract)
    verifier_results = []
    verifier_env = os.environ.copy()
    verifier_env["WORKFLOW_BASELINE_SHA256_JSON"] = json.dumps({
        item["path"]: item["sha256"] for item in baseline.get("files", [])
    }, sort_keys=True)
    for index, contract in enumerate(spec["verification_contracts"], start=1):
        command = contract["command"]
        started = time.monotonic()
        proc = subprocess.run(
            command,
            cwd=workdir,
            text=True,
            capture_output=True,
            timeout=contract["timeout_seconds"],
            check=False,
            env=verifier_env,
        )
        pi_protocol = pi_jsonl_verdict(proc.stdout)
        item = {
            "index": index,
            "id": contract["id"],
            "covers": contract["covers"],
            "command": command,
            "display_command": shlex.join(command),
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "pi_protocol": pi_protocol,
            "passed": proc.returncode == 0 and (pi_protocol is None or pi_protocol["passed"]),
        }
        results.append(item)
        verifier_results.append(item)
        write_json(run_dir / "validation" / f"attempt-{attempt}-command-{index}.json", item)
    result = {
        "attempt": attempt,
        "status": "passed" if all(item["passed"] for item in results) else "failed",
        "step_contract": step_contract,
        "commands": results,
        "verifiers": verifier_results,
    }
    write_json(run_dir / "validation" / f"attempt-{attempt}.json", result)
    append_event(run_dir, "verification", attempt=attempt, status=result["status"], checks=len(results))
    return result


def criterion_results(spec: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    verifiers = verification.get("verifiers") or []
    items = []
    for criterion in spec["acceptance_criteria"]:
        evidence = [
            {
                "verifier_id": verifier.get("id"),
                "passed": verifier.get("passed") is True,
                "exit_code": verifier.get("exit_code"),
                "pi_protocol_passed": (verifier.get("pi_protocol") or {}).get("passed") if verifier.get("pi_protocol") else None,
                "artifact": f"validation/attempt-{verification.get('attempt')}-command-{verifier.get('index')}.json",
            }
            for verifier in verifiers
            if criterion["id"] in (verifier.get("covers") or [])
        ]
        passed = bool(evidence) and all(item["passed"] for item in evidence)
        items.append({**criterion, "status": "passed" if passed else "failed", "evidence": evidence})
    return {
        "status": "passed" if items and all(item["status"] == "passed" for item in items) else "failed",
        "passed": sum(item["status"] == "passed" for item in items),
        "total": len(items),
        "criteria": items,
    }


def load_approved_plan(
    *,
    approved_plan_path: Path,
    harness: Path,
    workflow: str,
    task_id: str,
    spec_digest: str,
    input_digest: str,
    implementation_digest: str,
) -> dict[str, Any]:
    approved_plan_path = approved_plan_path.expanduser().resolve()
    source_run = approved_plan_path.parent.parent
    expected_source_parent = (harness / "runs" / task_id).resolve()
    if approved_plan_path != source_run / "stages" / "plan.json" or source_run.parent != expected_source_parent:
        raise SystemExit("approved plan must be the canonical plan artifact from this harness and task")
    required = [source_run / "manifest.json", source_run / "state.json", source_run / "stages" / "approval.json", approved_plan_path]
    if not all(path.is_file() for path in required) or not verify_run_seal(source_run):
        raise SystemExit("approved plan source run has missing artifacts or an invalid evidence seal")
    source_manifest = read_json(source_run / "manifest.json")
    source_state = read_json(source_run / "state.json")
    source_approval = read_json(source_run / "stages" / "approval.json")
    if (
        source_manifest.get("spec_sha256") != spec_digest
        or source_manifest.get("input_snapshot_digest") != input_digest
        or source_manifest.get("implementation_digest") != implementation_digest
        or source_manifest.get("workflow") != workflow
        or source_manifest.get("task_id") != task_id
        or source_state.get("stage") != "approval"
        or source_state.get("status") != "blocked"
        or source_approval.get("status") != "not_approved"
        or source_approval.get("spec_sha256") != spec_digest
        or source_approval.get("input_snapshot_digest") != input_digest
        or source_approval.get("implementation_digest") != implementation_digest
        or source_approval.get("plan_sha256") != sha256_file(approved_plan_path)
    ):
        raise SystemExit("approved plan or source approval record does not match the current sealed task contract")
    plan = read_json(approved_plan_path)
    validate_stage_output("plan", plan)
    plan["_approval_source"] = str(approved_plan_path)
    return plan


def main() -> int:
    global CURRENT_RUN_DIR, WORKSPACE_LOCK
    controller_started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, help="Path to the scaffolded harness bundle")
    parser.add_argument("--spec", required=True, help="Task specification JSON")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--resume-run", help="Existing run directory to resume from verified matching checkpoints")
    parser.add_argument("--approve-execution", action="store_true")
    parser.add_argument("--approved-plan-artifact", help="Exact plan.json produced by a prior gated run")
    args = parser.parse_args()

    if args.resume_run and args.run_id:
        raise SystemExit("--resume-run and --run-id are mutually exclusive")

    if args.approve_execution and not args.approved_plan_artifact:
        raise SystemExit("--approve-execution requires --approved-plan-artifact so approval binds to an exact plan")

    harness = Path(args.harness).expanduser().resolve()
    reconciled_runs = reconcile_stale_runs(harness)
    spec_path = Path(args.spec).expanduser().resolve()
    config = read_json(harness / "harness.json")
    spec = read_json(spec_path)
    validate_spec(spec)
    supervisor_only = bool(spec.get("execution_commands"))
    compatibility = config.get("pi_compatibility") or {}
    if supervisor_only:
        pi_binary = "not_used"
        pi_version = "not_used"
    else:
        pi_binary = pi_path()
        pi_version = command_version(pi_binary)
        try:
            assert_supported_pi_version(pi_version, compatibility, pi_binary)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    spec["step_validation"] = {
        "enabled": False,
        "model": "openai-codex/gpt-5.6-luna",
        "thinking": "low",
        "mode": "gate",
        "min_score": 8,
        "max_attempts_per_step": 2,
        "timeout_seconds": 120,
        **(spec.get("step_validation") or {}),
    }
    resume = bool(args.resume_run)
    resume_dir = Path(args.resume_run).expanduser().resolve() if args.resume_run else None
    run_id = resume_dir.name if resume_dir else (args.run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{secrets.token_hex(3)}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", run_id):
        raise SystemExit("run_id must be a safe path segment using only letters, numbers, dot, underscore, or dash")
    run_dir = resume_dir if resume_dir else harness / "runs" / spec["task_id"] / run_id
    if resume:
        expected_parent = (harness / "runs" / spec["task_id"]).resolve()
        if run_dir.parent != expected_parent or not (run_dir / "manifest.json").is_file():
            raise SystemExit("--resume-run must name an existing run for this harness and task_id")
    else:
        run_dir.mkdir(parents=True, exist_ok=False)
    CURRENT_RUN_DIR = run_dir
    workdir = Path(spec["workdir"]).expanduser().resolve()
    if not workdir.is_dir():
        raise SystemExit(f"task workdir does not exist: {workdir}")
    validate_resolved_path_policy(workdir, spec)
    if spec.get("allowed_write_paths"):
        WORKSPACE_LOCK = acquire_workspace_lock(harness, workdir)
    if supervisor_only:
        pi_runtime_policy = {"mode": "not_used", "reason": "deterministic_supervisor_execution"}
    else:
        pi_agent_dir, pi_runtime_policy = prepare_pi_runtime_dir(str(run_dir))
        os.environ["HARNESS_PI_AGENT_DIR"] = str(pi_agent_dir)
    baseline = snapshot_inputs(spec, workdir)
    preflight = context_preflight(spec, baseline)
    cleaned_write_paths: list[str] = []
    write_baseline = snapshot_paths(workdir, spec.get("allowed_write_paths", []))
    undeclared_baseline = snapshot_undeclared_paths(workdir, spec.get("allowed_write_paths", []))
    spec_digest = sha256_file(spec_path)
    if resume:
        undeclared_snapshot = run_dir / "integrity" / "undeclared-paths.before.json"
        if not undeclared_snapshot.is_file():
            raise SystemExit("resume run lacks the undeclared-path baseline required by the current runner")
        write_baseline = read_json(run_dir / "integrity" / "allowed-writes.before.json")
        undeclared_baseline = read_json(undeclared_snapshot)
    else:
        write_json(run_dir / "integrity" / "inputs.before.json", baseline)
        write_json(run_dir / "integrity" / "allowed-writes.before.json", write_baseline)
        write_json(run_dir / "integrity" / "undeclared-paths.before.json", undeclared_baseline)
    write_json(run_dir / "validation" / "context-preflight.json", preflight)

    models = config["models"]
    thinking = config.get("thinking", "low")
    implementation = {
        "skill_sha256": sha256_file(SKILL / "SKILL.md"),
        "harness_config_sha256": sha256_file(harness / "harness.json"),
        "runner_sha256": sha256_file(Path(__file__).resolve()),
        "guard_extension_sha256": sha256_file(GUARD_EXTENSION),
        "run_tracker_renderer_sha256": sha256_file(TRACKER_RENDERER),
        "registry_updater_sha256": sha256_file(REGISTRY_UPDATER),
        "factory_common_sha256": sha256_file(FACTORY_COMMON),
        "pi_binary": pi_binary,
        "pi_version": pi_version,
        "pi_compatibility": compatibility,
        "pi_runtime_policy": pi_runtime_policy,
        "pi_runtime_policy_digest": factory_digest(pi_runtime_policy),
        "python_version": sys.version.split()[0],
    }
    implementation_digest = canonical_digest(implementation)
    new_manifest = {
        "workflow": config["workflow"],
        "task_id": spec["task_id"],
        "run_id": run_id,
        "started_at": utc_now(),
        "spec": str(spec_path),
        "workdir": str(workdir),
        "stages": spec["lifecycle"],
        "objective": spec["objective"],
        "objective_contract": spec["objective_contract"],
        "context_policy": spec.get("context_policy", {"strategy": "full", "max_input_bytes": 1000000}),
        "models": models,
        "thinking": thinking,
        "allowed_to_mutate": args.approve_execution,
        "approval": {
            "status": "approved" if args.approve_execution else "not_approved",
            "approved_at": utc_now() if args.approve_execution else None,
            "approved_spec": str(spec_path) if args.approve_execution else None,
            "approved_plan_artifact": str(Path(args.approved_plan_artifact).resolve()) if args.approved_plan_artifact else None,
        },
        "max_repairs": spec.get("max_repairs", config.get("max_repairs", 1)),
        "acceptance_criteria": spec["acceptance_criteria"],
        "verification_commands": spec["verification_commands"],
        "required_steps": spec.get("required_steps", []),
        "execution_commands": spec.get("execution_commands", []),
        "allowed_write_paths": spec.get("allowed_write_paths", []),
        "immutable_paths": spec.get("immutable_paths", spec["inputs"]),
        "clean_allowed_write_paths": spec.get("clean_allowed_write_paths", False),
        "step_validation": spec["step_validation"],
        "cleaned_write_paths": cleaned_write_paths,
        "spec_sha256": spec_digest,
        "input_snapshot_digest": baseline["digest"],
        "implementation": implementation,
        "implementation_digest": implementation_digest,
        "runner": {"pid": os.getpid(), "host": socket.gethostname(), "started_at": utc_now()},
        "stale_runs_reconciled_at_start": reconciled_runs,
    }
    if resume:
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("spec_sha256") != spec_digest or manifest.get("input_snapshot_digest") != baseline["digest"] or manifest.get("implementation_digest") != implementation_digest:
            raise SystemExit("resume refused: spec, immutable inputs, or harness implementation changed")
        manifest["runner"] = {"pid": os.getpid(), "host": socket.gethostname(), "resumed_at": utc_now()}
        manifest["resume_count"] = int(manifest.get("resume_count", 0)) + 1
        manifest["stale_runs_reconciled_at_resume"] = reconciled_runs
        append_event(run_dir, "run_resumed", resume_count=manifest["resume_count"])
    else:
        manifest = new_manifest
    write_json(run_dir / "manifest.json", manifest)
    if not preflight["passed"]:
        write_json(run_dir / "failure.json", {"status": "failed", "error_type": "ContextPreflightFailed", "error": preflight["reason"], "timestamp": utc_now()})
        set_state(run_dir, manifest, spec["lifecycle"][0], "failed", "Use a specialized runner or reduce declared full-context inputs")
        seal_and_render(run_dir)
        print(run_dir)
        return 1
    set_state(run_dir, manifest, "intake", "running", "Normalize task input")

    intake = run_pi_checkpointed(
        implementation_digest=implementation_digest,
        resume=resume,
        run_dir=run_dir,
        stage="intake",
        model=models["intake"],
        thinking=thinking,
        workdir=workdir,
        tools=resolve_stage_tools(config, "intake"),
        timeout_seconds=config["pi_timeout_seconds"],
        prompt=(
            "Return only JSON. Normalize this task without changing its meaning. "
            "Required keys: task_id (string), objective (string), inputs (array of strings — "
            "copy spec.inputs verbatim, do not restructure it into an object), "
            "constraints (array of strings), acceptance_criteria (array), ambiguities (array of strings; "
            "use an empty array if there are none).\n"
            + json.dumps(spec, indent=2)
        ),
    )

    if args.approved_plan_artifact:
        approved_plan_path = Path(args.approved_plan_artifact).expanduser().resolve()
        plan = load_approved_plan(
            approved_plan_path=approved_plan_path,
            harness=harness,
            workflow=config["workflow"],
            task_id=spec["task_id"],
            spec_digest=spec_digest,
            input_digest=baseline["digest"],
            implementation_digest=implementation_digest,
        )
        write_json(run_dir / "stages" / "plan.json", plan)
        append_event(run_dir, "plan_reused", source=str(approved_plan_path), sha256=sha256_file(approved_plan_path))
    else:
        set_state(run_dir, manifest, "plan", "running", "Create an executable plan")
        plan = deterministic_plan(spec)
        if plan is not None:
            plan = write_deterministic_stage(run_dir, "plan", plan)
        else:
            plan = run_pi_checkpointed(
                implementation_digest=implementation_digest,
                resume=resume,
                run_dir=run_dir,
                stage="plan",
                model=models["plan"],
                thinking=thinking,
                workdir=workdir,
                tools=resolve_stage_tools(config, "plan"),
                timeout_seconds=config["pi_timeout_seconds"],
                prompt=(
                    "Return only JSON. Design the smallest safe plan that completes the task. "
                    "Required keys: summary, steps (array), files_expected_to_change (array), risks (array), "
                    "verification_mapping (array). Each step must be an object with id, name, action, outputs, "
                    "and verification. Do not execute.\nTASK:\n"
                    + json.dumps(spec, indent=2)
                    + "\nVALIDATED INTAKE:\n"
                    + json.dumps(intake, indent=2)
                ),
            )
    if spec.get("required_steps"):
        planned = plan.get("steps", [])
        planned_names = [item.get("name") for item in planned if isinstance(item, dict)]
        plan_contract = {
            "required_count": len(spec["required_steps"]),
            "planned_count": len(planned),
            "required_names": spec["required_steps"],
            "planned_names": planned_names,
            "passed": planned_names == spec["required_steps"],
        }
        write_json(run_dir / "validation" / "plan-step-contract.json", plan_contract)
        if not plan_contract["passed"]:
            set_state(run_dir, manifest, "plan", "failed", "Plan did not satisfy required_steps contract")
            seal_and_render(run_dir)
            print(run_dir)
            return 1
        for index, name in enumerate(spec["required_steps"], start=1):
            write_json(
                run_dir / "stages" / "task-steps" / f"{index:02d}.json",
                {"index": index, "name": name, "status": "pending", "evidence": None, "mechanically_verified": False},
            )
        sync_registry(run_dir)

    set_state(run_dir, manifest, "approval", "waiting", "Approve the plan artifact before mutation")
    write_json(
        run_dir / "stages" / "approval.json",
        {
            "status": manifest["approval"]["status"],
            "approved_spec": manifest["approval"]["approved_spec"],
            "plan_artifact": str(run_dir / "stages" / "plan.json"),
            "plan_sha256": sha256_file(run_dir / "stages" / "plan.json"),
            "spec_sha256": spec_digest,
            "input_snapshot_digest": baseline["digest"],
            "implementation_digest": implementation_digest,
        },
    )
    if not args.approve_execution:
        set_state(run_dir, manifest, "approval", "blocked", "Approve this exact plan artifact in a new run")
        seal_and_render(run_dir)
        print(run_dir)
        return 3

    if spec.get("clean_allowed_write_paths", False) and not resume:
        cleaned_write_paths = clean_allowed_write_paths(workdir, spec.get("allowed_write_paths", []), spec.get("immutable_paths", spec["inputs"]))
        write_baseline = snapshot_paths(workdir, spec.get("allowed_write_paths", []))
        manifest["cleaned_write_paths"] = cleaned_write_paths
        write_json(run_dir / "manifest.json", manifest)
        write_json(run_dir / "integrity" / "allowed-writes.before.json", write_baseline)
        write_json(run_dir / "integrity" / "allowed-writes.cleaned.json", {"paths": cleaned_write_paths, "cleaned_at": utc_now()})
        append_event(run_dir, "allowed_writes_cleaned", paths=cleaned_write_paths)

    set_state(run_dir, manifest, "execute", "running", "Execute the approved plan")
    execution_tools = list(spec.get("allowed_tools", ["read", "bash", "edit", "write"]))
    if spec["step_validation"]["enabled"]:
        execution_tools.append("harness_step")
    task_context = {
        "task_id": spec["task_id"],
        "objective": spec["objective"],
        "constraints": spec["constraints"],
        "acceptance_criteria": spec["acceptance_criteria"],
        "required_steps": spec.get("required_steps", []),
    }
    execution = run_pi_checkpointed(
        implementation_digest=implementation_digest,
        resume=resume,
        run_dir=run_dir,
        stage="execute",
        model=models["execute"],
        thinking=thinking,
        workdir=workdir,
        tools=resolve_stage_tools(config, "execute", execution_tools),
        timeout_seconds=config["pi_timeout_seconds"],
        allowed_write_paths=spec.get("allowed_write_paths", []),
        immutable_paths=spec.get("immutable_paths", spec["inputs"]),
        allow_bash=spec.get("allow_bash", False),
        required_steps=spec.get("required_steps", []),
        step_validation=spec["step_validation"],
        task_context=task_context,
        prompt=(
            "Execute the approved task completely in the current working directory. Follow the plan, "
            "respect constraints, and do not stop at recommendations. Create or overwrite the declared "
            "allowed_write_paths during this run; do not rely on pre-existing outputs. Then return only JSON with keys: "
            "status, summary, files_changed (array), commands_run (array), residual_risk (array), and "
            "completed_steps (array). completed_steps must contain every required_steps entry exactly "
            "once and in order, as {id, name, status:'done', evidence}.\n"
            "VERIFICATION OWNERSHIP: the harness runs the declared verifier commands mechanically and "
            "independently immediately after this stage, using its own subprocess invocation outside your "
            "control. You are not being asked to establish pass/fail; you are being asked to make the edits. "
            "If your tool set for this stage does not include bash/command execution, that is deliberate: do "
            "not treat the absence of a command-execution tool as a blocker. Report status 'completed' once "
            "you have made every required edit and are confident via direct inspection (reading files back) "
            "that they satisfy the acceptance criteria; leave commands_run empty in that case and note in "
            "residual_risk that command-based self-verification was unavailable. Reserve status 'blocked' only "
            "for cases where you genuinely cannot proceed: missing information, contradictory constraints, or "
            "inability to read/write the required files.\nTASK:\n"
            + (
                "STEP VALIDATION: After completing each required step, call harness_step with its exact index and name, summary, direct evidence, and artifact paths. "
                "Do not continue to the next step until harness_step accepts it. If rejected, repair the step using the returned guidance and call harness_step again.\n"
                if spec["step_validation"]["enabled"] else ""
            )
            + json.dumps(spec, indent=2)
            + "\nAPPROVED PLAN:\n"
            + json.dumps(plan, indent=2)
        ),
    )

    effective_execution = execution
    if supervisor_execution["status"] != "passed":
        append_event(run_dir, "execution_failed", source="supervisor_commands")
    verification = run_verifiers(spec, run_dir, 0, effective_execution, baseline)
    repairs = []
    attempt = 0
    while not supervisor_owned and verification["status"] != "passed" and attempt < manifest["max_repairs"]:
        attempt += 1
        set_state(run_dir, manifest, "repair", "running", f"Repair verifier failure attempt {attempt}")
        repair = run_pi_checkpointed(
            implementation_digest=implementation_digest,
            resume=resume,
            run_dir=run_dir,
            stage=f"repair-{attempt}",
            model=models["repair"],
            thinking=thinking,
            workdir=workdir,
            tools=resolve_stage_tools(config, "repair", spec.get("allowed_tools", ["read", "bash", "edit", "write"])),
            timeout_seconds=config["pi_timeout_seconds"],
            allowed_write_paths=spec.get("allowed_write_paths", []),
            immutable_paths=spec.get("immutable_paths", spec["inputs"]),
            allow_bash=spec.get("allow_bash", False),
            prompt=(
                "Repair the task implementation using the verifier evidence. Do not weaken or edit the "
                "verification commands. Return only JSON with keys: status, diagnosis, changes (array), "
                "residual_risk (array), and completed_steps (array). completed_steps must contain every "
                "required_steps entry exactly once and in order, as {id, name, status:'done', evidence}.\n"
                "VERIFICATION OWNERSHIP: the harness re-runs the declared verifier commands mechanically and "
                "independently right after this stage. If your tool set has no bash/command execution, that is "
                "deliberate; do not treat it as a blocker. Report status 'completed' (or a status starting with "
                "'repaired') once you have made every edit needed to address the verifier failure and are "
                "confident via direct inspection; reserve 'blocked' for genuine inability to proceed.\nTASK:\n"
                + json.dumps(spec, indent=2)
                + "\nPREVIOUS EXECUTION:\n"
                + json.dumps(execution, indent=2)
                + "\nVERIFIER FAILURE:\n"
                + json.dumps(verification, indent=2)
            ),
        )
        repairs.append(repair)
        effective_execution = repair
        supervisor_execution = run_supervisor_commands(spec, run_dir)
        verification = run_verifiers(spec, run_dir, attempt, effective_execution, baseline)

    task_step_artifacts = []
    step_contract = completed_step_contract(spec, effective_execution)
    if spec.get("required_steps") and step_contract["passed"]:
        for index, item in enumerate(effective_execution["completed_steps"], start=1):
            rel = f"stages/task-steps/{index:02d}.json"
            live_step = read_json(run_dir / rel) if (run_dir / rel).exists() else {}
            write_json(
                run_dir / rel,
                {
                    "index": index,
                    "name": item["name"],
                    "status": "verified" if verification["status"] == "passed" else "claimed",
                    "reported_status": item["status"],
                    "evidence": item["evidence"],
                    "mechanically_verified": verification["status"] == "passed",
                    "model_validation": live_step.get("model_validation"),
                    "execution_artifact": "stages/execute.json" if not repairs else f"stages/repair-{attempt}.json",
                },
            )
            task_step_artifacts.append(rel)
    elif spec.get("required_steps"):
        for index, name in enumerate(spec["required_steps"], start=1):
            write_json(
                run_dir / "stages" / "task-steps" / f"{index:02d}.json",
                {"index": index, "name": name, "status": "failed", "evidence": "Execution did not satisfy the exact step contract.", "mechanically_verified": False},
            )
    sync_registry(run_dir)

    criteria_validation = criterion_results(spec, verification)
    write_json(run_dir / "validation" / "criteria.json", criteria_validation)
    set_state(run_dir, manifest, "judge", "running", "Apply mechanical acceptance criteria")
    judge = write_deterministic_stage(run_dir, "judge", mechanical_judge(criteria_validation))

    required = [
        "manifest.json",
        "state.json",
        "stages/intake.json",
        "stages/plan.json",
        "stages/approval.json",
        "stages/execute.json",
        "stages/judge.json",
    ] + task_step_artifacts
    core_artifacts_ok = all((run_dir / rel).is_file() and (run_dir / rel).stat().st_size > 0 for rel in required)
    hook_policy = hook_policy_summary(run_dir)
    step_review = step_validation_summary(run_dir, spec)
    write_coverage = (
        {
            "status": "passed",
            "owner": "deterministic_supervisor",
            "required_roots": spec.get("allowed_write_paths", []),
            "model_write_paths": [],
            "missing_roots": [],
        }
        if supervisor_owned
        else model_write_coverage(spec.get("allowed_write_paths", []), hook_policy)
    )
    write_after = snapshot_paths(workdir, spec.get("allowed_write_paths", []))
    write_activity = {
        "status": "passed" if not spec.get("allowed_write_paths") or write_after["digest"] != write_baseline["digest"] else "failed",
        "before_digest": write_baseline["digest"],
        "after_digest": write_after["digest"],
        "required": bool(spec.get("allowed_write_paths")),
    }
    undeclared_after = snapshot_undeclared_paths(workdir, spec.get("allowed_write_paths", []))
    undeclared_integrity = {
        "status": "passed" if undeclared_after["digest"] == undeclared_baseline["digest"] else "failed",
        "before_digest": undeclared_baseline["digest"],
        "after_digest": undeclared_after["digest"],
    }
    write_json(run_dir / "integrity" / "undeclared-paths.after.json", undeclared_after)
    write_json(run_dir / "validation" / "undeclared-path-integrity.json", undeclared_integrity)
    write_json(run_dir / "integrity" / "allowed-writes.after.json", write_after)
    write_json(run_dir / "validation" / "write-activity.json", write_activity)
    write_json(run_dir / "validation" / "hook-policy.json", hook_policy)
    write_json(run_dir / "validation" / "step-validation.json", step_review)
    execution_status_ok = stage_completed(effective_execution)
    provisional_pass = (
        execution_status_ok
        and hook_policy["status"] == "passed"
        and write_coverage["status"] == "passed"
        and write_activity["status"] == "passed"
        and undeclared_integrity["status"] == "passed"
        and step_review["status"] in {"passed", "disabled"}
        and supervisor_execution["status"] == "passed"
        and verification["status"] == "passed"
        and criteria_validation["status"] == "passed"
        and judge.get("accepted") is True
        and core_artifacts_ok
    )
    metrics = runtime_metrics(run_dir, controller_started, supervisor_owned)
    report = f"""# Pi Deterministic Task Run

Status: {'passed' if provisional_pass else 'failed'}

Task: {spec['task_id']}

Objective: {spec['objective']}

Models: intake={models['intake']}, plan={models['plan']}, execute={models['execute']}, repair={models['repair']}, judge={models['judge']}
Runtime: owner={metrics['execution_owner']}, elapsed_seconds={metrics['elapsed_seconds']}, model_calls={metrics['model_calls']}, input_tokens={metrics['input_tokens']}, output_tokens={metrics['output_tokens']}, api_equivalent_cost_usd={metrics['api_equivalent_cost_usd']}


Undeclared path integrity: {undeclared_integrity['status']}

Mechanical verification: {verification['status']}

Acceptance criteria: {criteria_validation['passed']}/{criteria_validation['total']} passed

Step validation: {step_review['status']} ({step_review.get('accepted', 0)}/{step_review.get('required', 0)}, average score {step_review.get('average_score')})

Judge accepted: {judge.get('accepted')}

Judge score: {judge.get('score')}

Repair attempts: {attempt}

Artifact root: {run_dir}
"""
    (run_dir / "final_report.md").write_text(report)
    render_tracker(run_dir)
    required_artifacts = [
        *required,
        "final_report.md",
        "events/harness.jsonl",
        "tracker.jsonl",
        "tracker.html",
        "validation/criteria.json",
        "validation/undeclared-path-integrity.json",
        "integrity/undeclared-paths.before.json",
        "integrity/undeclared-paths.after.json",
    ]
    artifacts_ok = all((run_dir / rel).is_file() and (run_dir / rel).stat().st_size > 0 for rel in required_artifacts)
    passed = (
        execution_status_ok
        and hook_policy["status"] == "passed"
        and write_coverage["status"] == "passed"
        and write_activity["status"] == "passed"
        and undeclared_integrity["status"] == "passed"
        and step_review["status"] in {"passed", "disabled"}
        and supervisor_execution["status"] == "passed"
        and verification["status"] == "passed"
        and criteria_validation["status"] == "passed"
        and judge.get("accepted") is True
        and artifacts_ok
    )
    final_validation = {
        "status": "passed" if passed else "failed",
        "task_id": spec["task_id"],
        "run_id": run_id,
        "mechanical_verification": verification["status"],
        "criterion_verification": criteria_validation,
        "supervisor_execution": supervisor_execution["status"],
        "input_integrity": check_integrity(workdir, baseline)["passed"],
        "execution_status_ok": execution_status_ok,
        "hook_policy": hook_policy,
        "model_write_coverage": write_coverage,
        "write_activity": write_activity,
        "undeclared_path_integrity": undeclared_integrity,
        "step_validation": step_review,
        "judge_accepted": judge.get("accepted") is True,
        "judge_score": judge.get("score"),
        "runtime_metrics": metrics,
        "required_artifacts_present": artifacts_ok,
        "required_artifacts": required_artifacts,
        "tracker_events": str(run_dir / "tracker.jsonl"),
        "tracker_ui": str(run_dir / "tracker.html"),
        "workflow_tracker_ui": str(harness / "runs" / "index.html"),
        "workflow_ledger": str(harness / "runs" / "harness.sqlite3"),
        "run_seal": str(run_dir / "integrity" / "run-seal.json"),
        "repair_attempts": attempt,
        "required_step_count": len(spec.get("required_steps", [])),
        "task_step_artifact_count": len(task_step_artifacts),
        "models": models,
        "checked_at": utc_now(),
    }
    write_json(run_dir / "validation" / "final_validation.json", final_validation)
    write_json(run_dir / "integrity" / "inputs.after.json", snapshot_inputs(spec, workdir))
    set_state(run_dir, manifest, "report", "done" if passed else "failed", None)
    seal_and_render(run_dir)
    print(run_dir)
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        if CURRENT_RUN_DIR is not None:
            finalize_interrupted_run(CURRENT_RUN_DIR)
        raise SystemExit(130)
    except SystemExit:
        raise
    except Exception as exc:
        if CURRENT_RUN_DIR is not None:
            write_json(CURRENT_RUN_DIR / "failure.json", {"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "timestamp": utc_now()})
            append_event(CURRENT_RUN_DIR, "failure", error_type=type(exc).__name__, error=str(exc))
            state_path = CURRENT_RUN_DIR / "state.json"
            if state_path.exists():
                state = read_json(state_path)
                state.update({"status": "failed", "next_action": "Inspect failure.json", "updated_at": utc_now()})
                write_json(state_path, state)
            seal_and_render(CURRENT_RUN_DIR)
        raise
