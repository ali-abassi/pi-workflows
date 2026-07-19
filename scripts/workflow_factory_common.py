#!/usr/bin/env python3
"""Shared contracts for the deterministic workflow factory."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from evaluate_transition import graph_errors


BLUEPRINT_VERSIONS = {"1.0", "1.1", "1.2", "1.3"}
FACTORY_VERSION = "1.3.0"
GENERIC_MUTATION_LIFECYCLE = ["intake", "plan", "approval", "execute", "verify", "judge", "report"]
PI_COMPATIBILITY = {"minimum_version": "0.80.5", "maximum_version": "0.80.10", "tested_version": "0.80.10", "transport": "json"}
OMP_COMPATIBILITY = {"minimum_version": "17.0.0", "maximum_version": "17.0.0", "tested_version": "17.0.0", "transport": "json"}
SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
SAFE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?$")
MODEL_ROLES = {
    "intake": "openai-codex/gpt-5.6-luna",
    "plan": "openai-codex/gpt-5.6-sol",
    "execute": "openai-codex/gpt-5.6-terra",
    "repair": "openai-codex/gpt-5.6-terra",
    "judge": "openai-codex/gpt-5.6-sol",
}
BUILTIN_TOOLS = {"read", "bash", "edit", "write", "grep", "find", "ls"}
DEFAULT_STAGE_CAPABILITIES = {
    "intake": {"tools": []},
    "plan": {"tools": []},
    "execute": {"tools": ["read", "edit", "write"]},
    "repair": {"tools": ["read", "edit", "write"]},
    "judge": {"tools": []},
}


def compiled_peer_contract(blueprint: dict[str, Any]) -> dict[str, Any] | None:
    declared = blueprint.get("peer_collaboration")
    if not isinstance(declared, dict):
        return None
    return {
        "schema_version": "1.0",
        "workflow": blueprint["workflow"],
        "workflow_version": blueprint["version"],
        **declared,
    }


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise SystemExit(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def parse_semver(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"\s*(?:[A-Za-z][A-Za-z0-9_-]*/)?v?(\d+)\.(\d+)\.(\d+)\s*", value)
    if not match:
        raise ValueError(f"unable to parse semantic version: {value!r}")
    return tuple(int(part) for part in match.groups())


def assert_supported_pi_version(version: str, compatibility: dict[str, Any], executable: str | None = None) -> None:
    selected = OMP_COMPATIBILITY if executable and Path(executable).name == "omp" else compatibility
    minimum = selected.get("minimum_version")
    maximum = selected.get("maximum_version")
    if not isinstance(minimum, str) or not isinstance(maximum, str):
        raise ValueError("runtime compatibility must declare minimum_version and maximum_version")
    parsed = parse_semver(version)
    if parsed < parse_semver(minimum):
        raise ValueError(f"runtime {version} is below the harness minimum {minimum}")
    if parsed > parse_semver(maximum):
        raise ValueError(f"runtime {version} is above the harness maximum {maximum}; review and certify the new protocol before use")


def bounded_pi_json_flags(system_prompt: str, executable: str | None = None) -> list[str]:
    """Return the smallest supported isolation flags for Pi or OMP JSON calls."""
    flags = ["--mode", "json", "--no-session", "--no-extensions", "--no-skills"]
    if not executable or Path(executable).name != "omp":
        flags += ["--no-approve", "--offline", "--no-context-files", "--no-prompt-templates", "--no-themes"]
    return [*flags, "--system-prompt", system_prompt]


def prepare_pi_runtime_dir(scope_id: str) -> tuple[Path, dict[str, Any]]:
    """Create a sanitized Pi agent directory while bridging auth without copying it."""
    settings = {
        "defaultProjectTrust": "never",
        "compaction": {"enabled": False},
        "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
    }
    root = Path("/tmp") / f"deterministic-codex-workflows-{os.getuid()}" / "pi-agent" / canonical_digest(scope_id)[:24]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    (root / "settings.json").write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    configured_root = Path(
        os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))
    ).expanduser().resolve()
    candidates = [(configured_root / "auth.json", "configured")]
    if os.environ.get("PI_WORKFLOW_AUTH_FALLBACK", "").strip().lower() in {"1", "true", "yes"}:
        candidates.append((Path.home() / ".pi" / "agent" / "auth.json", "home_fallback"))
    source_auth: Path | None = None
    source_kind = "none"
    for candidate, candidate_kind in candidates:
        if not candidate.is_file():
            continue
        try:
            auth = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Pi auth file: {candidate}") from exc
        if not isinstance(auth, dict):
            raise ValueError(f"invalid Pi auth file: {candidate}")
        if auth:
            source_auth = candidate
            source_kind = candidate_kind
            break
    target_auth = root / "auth.json"
    if source_auth is None:
        if target_auth.exists() or target_auth.is_symlink():
            target_auth.unlink()
    else:
        if target_auth.is_symlink() and target_auth.resolve() != source_auth:
            target_auth.unlink()
        elif target_auth.exists() and not target_auth.is_symlink():
            raise ValueError(f"refusing unexpected auth file in sanitized Pi runtime: {target_auth}")
        if not target_auth.exists():
            target_auth.symlink_to(source_auth)
    models = root / "models.json"
    if models.exists():
        models.unlink()
    policy = {
        **settings,
        "auth": {
            "source": source_kind,
            "fallback_used": source_kind == "home_fallback",
            "bridged_by": "symlink" if source_auth else "none",
        },
    }
    return root, policy


def safe_relative(value: str, field: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or value in {".", ".."} or ".." in path.parts:
        raise ValueError(f"{field} must be a safe relative path: {value!r}")
    return value


def paths_overlap(left: str, right: str) -> bool:
    """Return whether either safe relative path contains the other."""
    left_parts = Path(left).parts
    right_parts = Path(right).parts
    shortest = min(len(left_parts), len(right_parts))
    return left_parts[:shortest] == right_parts[:shortest]


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def validate_blueprint(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["blueprint must be an object"]
    base_fields = {"schema_version", "workflow", "version", "description", "effect", "runtime", "objective_contract", "lifecycle", "acceptance_criteria", "verifiers", "task_template", "context_policy", "operations"}
    allowed_fields = base_fields | {"control_flow", "models", "stage_capabilities", "peer_collaboration", "certification_contract"}
    unknown = sorted(set(value) - allowed_fields)
    missing = sorted(base_fields - set(value))
    if unknown:
        errors.append("unsupported blueprint fields: " + ", ".join(unknown))
    if missing:
        errors.append("missing blueprint fields: " + ", ".join(missing))
    schema_version = value.get("schema_version")
    if schema_version not in BLUEPRINT_VERSIONS:
        errors.append("schema_version must be 1.0, 1.1, 1.2, or 1.3")
    workflow = value.get("workflow")
    if not isinstance(workflow, str) or not SAFE_NAME.fullmatch(workflow):
        errors.append("workflow must match ^[a-z][a-z0-9-]*$")
    version = value.get("version")
    if not isinstance(version, str) or not SAFE_VERSION.fullmatch(version):
        errors.append("version must be semantic version text such as 1.0.0")
    effect = value.get("effect")
    if effect not in {"mutation", "read_only"}:
        errors.append("effect must be mutation or read_only")
    if not isinstance(value.get("description"), str) or not value["description"].strip():
        errors.append("description must be non-empty")
    runtime = value.get("runtime")
    if isinstance(runtime, dict):
        unknown_runtime = sorted(set(runtime) - {"kind", "entrypoint"})
        if unknown_runtime:
            errors.append("unsupported runtime fields: " + ", ".join(unknown_runtime))
    if not isinstance(runtime, dict) or runtime.get("kind") not in {"generic_mutation", "specialized"}:
        errors.append("runtime.kind must be generic_mutation or specialized")
    elif effect == "read_only" and runtime.get("kind") == "generic_mutation":
        errors.append("read_only workflows require a specialized runtime entrypoint")
    elif runtime.get("kind") == "specialized" and not runtime.get("entrypoint"):
        errors.append("specialized runtime requires entrypoint")
    elif runtime.get("kind") == "specialized":
        try:
            safe_relative(runtime["entrypoint"], "runtime.entrypoint")
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
    max_repairs = value.get("max_repairs")
    if max_repairs is not None and (not isinstance(max_repairs, int) or isinstance(max_repairs, bool) or not 0 <= max_repairs <= 3):
        errors.append("max_repairs must be an integer from 0 through 3")
    execution_commands = value.get("execution_commands")
    if execution_commands is not None:
        if (
            not isinstance(execution_commands, list)
            or not all(
                isinstance(command, list)
                and command
                and all(isinstance(argument, str) and argument for argument in command)
                for command in execution_commands
            )
        ):
            errors.append("execution_commands must be an array of non-empty argv arrays")
        elif not isinstance(runtime, dict) or runtime.get("kind") != "generic_mutation":
            errors.append("execution_commands are supported only by generic_mutation runtimes")
    contract = value.get("objective_contract")
    if not isinstance(contract, dict) or set(contract) != {"selector", "target", "decision", "non_goals"}:
        errors.append("objective_contract must contain selector, target, decision, and non_goals")
    else:
        for key in ("selector", "target", "decision"):
            if not isinstance(contract.get(key), str) or not contract[key].strip():
                errors.append(f"objective_contract.{key} must be non-empty")
        if not isinstance(contract.get("non_goals"), list) or not all(isinstance(item, str) and item.strip() for item in contract["non_goals"]):
            errors.append("objective_contract.non_goals must be a string array")
    lifecycle = value.get("lifecycle")
    if not isinstance(lifecycle, list) or len(lifecycle) < 2 or len(set(lifecycle or [])) != len(lifecycle or []):
        errors.append("lifecycle must contain at least two unique stages")
    elif not all(isinstance(item, str) and SAFE_NAME.fullmatch(item) for item in lifecycle):
        errors.append("lifecycle stages must use safe kebab-case names")
    elif isinstance(runtime, dict) and runtime.get("kind") == "generic_mutation" and lifecycle != GENERIC_MUTATION_LIFECYCLE:
        errors.append("generic_mutation lifecycle must exactly match the executable runner sequence: " + " -> ".join(GENERIC_MUTATION_LIFECYCLE))
    elif effect == "read_only" and not {"verify", "report"}.issubset(lifecycle):
        errors.append("read_only lifecycle must include verify and report")
    models = value.get("models")
    if models is not None:
        if not isinstance(models, dict) or set(models) != set(MODEL_ROLES):
            errors.append("models must contain exactly intake, plan, execute, repair, and judge")
        else:
            for role, model in models.items():
                if not isinstance(model, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", model):
                    errors.append(f"models.{role} must be a pinned provider/model identifier")
    stage_capabilities = value.get("stage_capabilities")
    if stage_capabilities is not None:
        if not isinstance(stage_capabilities, dict):
            errors.append("stage_capabilities must be an object")
        else:
            allowed_stages = set(lifecycle or []) | {"repair"}
            for stage, profile in stage_capabilities.items():
                if not isinstance(stage, str) or not SAFE_NAME.fullmatch(stage) or stage not in allowed_stages:
                    errors.append(f"stage_capabilities has undeclared stage: {stage!r}")
                    continue
                if not isinstance(profile, dict) or set(profile) != {"tools"}:
                    errors.append(f"stage_capabilities.{stage} must contain exactly tools")
                    continue
                tools = profile["tools"]
                if not isinstance(tools, list) or len(tools) != len(set(tools or [])) or not all(isinstance(tool, str) and tool in BUILTIN_TOOLS for tool in tools):
                    errors.append(f"stage_capabilities.{stage}.tools must contain unique supported Pi tool names")
    control_flow = value.get("control_flow")
    if control_flow is not None:
        if schema_version not in {"1.1", "1.2", "1.3"}:
            errors.append("control_flow requires blueprint schema_version 1.1, 1.2, or 1.3")
        if not isinstance(runtime, dict) or runtime.get("kind") != "specialized":
            errors.append("control_flow is supported only by specialized runtimes")
        if isinstance(control_flow, dict) and isinstance(lifecycle, list):
            graph = {"schema_version": "1.0", "workflow": workflow, "stages": lifecycle, **control_flow}
            errors.extend(f"control_flow: {error}" for error in graph_errors(graph))
        else:
            errors.append("control_flow must be an object")
    peer_collaboration = value.get("peer_collaboration")
    if peer_collaboration is not None:
        if schema_version not in {"1.2", "1.3"}:
            errors.append("peer_collaboration requires blueprint schema_version 1.2 or 1.3")
        if not isinstance(runtime, dict) or runtime.get("kind") != "specialized":
            errors.append("peer_collaboration is supported only by specialized runtimes")
        required_peer_fields = {"stages", "peers", "transport", "limits", "required_responses", "on_unavailable", "logging"}
        if not isinstance(peer_collaboration, dict) or set(peer_collaboration) != required_peer_fields:
            errors.append("peer_collaboration must contain exactly: " + ", ".join(sorted(required_peer_fields)))
        else:
            stages = peer_collaboration["stages"]
            if (
                not isinstance(stages, list)
                or not stages
                or len(stages) != len(set(stages or []))
                or not all(isinstance(stage, str) and stage in set(lifecycle or []) for stage in stages)
            ):
                errors.append("peer_collaboration.stages must contain unique declared lifecycle stages")

            peers = peer_collaboration["peers"]
            peer_ids: list[str] = []
            if not isinstance(peers, list) or not 1 <= len(peers) <= 8:
                errors.append("peer_collaboration.peers must contain between 1 and 8 peers")
                peers = []
            for index, peer in enumerate(peers, start=1):
                field = f"peer_collaboration.peers[{index}]"
                if not isinstance(peer, dict) or set(peer) != {"id", "purpose", "model", "thinking", "tools"}:
                    errors.append(f"{field} must contain exactly id, purpose, model, thinking, and tools")
                    continue
                if not isinstance(peer["id"], str) or not SAFE_NAME.fullmatch(peer["id"]):
                    errors.append(f"{field}.id must use safe kebab-case")
                else:
                    peer_ids.append(peer["id"])
                if not isinstance(peer["purpose"], str) or not peer["purpose"].strip():
                    errors.append(f"{field}.purpose must be non-empty")
                if not isinstance(peer["model"], str) or "/" not in peer["model"]:
                    errors.append(f"{field}.model must pin provider/model")
                if peer["thinking"] not in {"off", "minimal", "low", "medium", "high"}:
                    errors.append(f"{field}.thinking must be off, minimal, low, medium, or high")
                tools = peer["tools"]
                if not isinstance(tools, list) or len(tools) != len(set(tools or [])) or not all(isinstance(tool, str) and tool in BUILTIN_TOOLS for tool in tools):
                    errors.append(f"{field}.tools must contain unique supported Pi tool names")
            if len(peer_ids) != len(set(peer_ids)):
                errors.append("peer_collaboration peer ids must be unique")

            transport = peer_collaboration["transport"]
            if not isinstance(transport, dict) or not {"kind", "authentication"}.issubset(transport) or set(transport) - {"kind", "authentication", "endpoint_env"}:
                errors.append("peer_collaboration.transport must contain kind, authentication, and optional endpoint_env")
            else:
                kind = transport["kind"]
                authentication = transport["authentication"]
                if kind not in {"local_socket", "http_sse"}:
                    errors.append("peer_collaboration.transport.kind must be local_socket or http_sse")
                if kind == "local_socket" and (authentication != "os_user" or "endpoint_env" in transport):
                    errors.append("local_socket transport requires os_user authentication and no endpoint_env")
                if kind == "http_sse":
                    if authentication not in {"per_agent_token", "mtls"}:
                        errors.append("http_sse transport requires per_agent_token or mtls authentication")
                    endpoint_env = transport.get("endpoint_env")
                    if not isinstance(endpoint_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", endpoint_env):
                        errors.append("http_sse transport requires an uppercase endpoint_env")

            limits = peer_collaboration["limits"]
            expected_limits = {"max_messages", "max_hops", "timeout_seconds", "max_response_bytes"}
            if not isinstance(limits, dict) or set(limits) != expected_limits:
                errors.append("peer_collaboration.limits must contain exactly: " + ", ".join(sorted(expected_limits)))
            else:
                for field, minimum, maximum in (
                    ("max_messages", 1, 100),
                    ("max_hops", 1, 20),
                    ("timeout_seconds", 1, 3600),
                    ("max_response_bytes", 1024, 1048576),
                ):
                    number = limits[field]
                    if isinstance(number, bool) or not isinstance(number, int) or not minimum <= number <= maximum:
                        errors.append(f"peer_collaboration.limits.{field} must be an integer between {minimum} and {maximum}")
            required_responses = peer_collaboration["required_responses"]
            if isinstance(required_responses, bool) or not isinstance(required_responses, int) or not 1 <= required_responses <= len(peers):
                errors.append("peer_collaboration.required_responses must be between 1 and the declared peer count")
            if peer_collaboration["on_unavailable"] not in {"fail_stage", "continue_with_degraded_evidence"}:
                errors.append("peer_collaboration.on_unavailable must be fail_stage or continue_with_degraded_evidence")
            if peer_collaboration["logging"] != "metadata_only":
                errors.append("peer_collaboration.logging must be metadata_only")
    criteria = value.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        errors.append("acceptance_criteria must be a non-empty array")
        criteria = []
    criterion_ids: list[str] = []
    for index, item in enumerate(criteria, start=1):
        if isinstance(item, dict) and set(item) != {"id", "description"}:
            errors.append(f"acceptance_criteria[{index}] must contain exactly id and description")
        if not isinstance(item, dict) or not SAFE_NAME.fullmatch(str(item.get("id", ""))) or not isinstance(item.get("description"), str) or not item["description"].strip():
            errors.append(f"acceptance_criteria[{index}] contract invalid")
        else:
            criterion_ids.append(item["id"])
    if len(set(criterion_ids)) != len(criterion_ids):
        errors.append("acceptance criterion ids must be unique")
    verifiers = value.get("verifiers")
    covered: list[str] = []
    verifier_ids: list[str] = []
    if not isinstance(verifiers, list) or not verifiers:
        errors.append("verifiers must be a non-empty array")
        verifiers = []
    for index, item in enumerate(verifiers, start=1):
        if isinstance(item, dict):
            unknown_verifier = sorted(set(item) - {"id", "covers", "command", "timeout_seconds"})
            if unknown_verifier:
                errors.append(f"unsupported verifiers[{index}] fields: " + ", ".join(unknown_verifier))
        if not isinstance(item, dict) or not SAFE_NAME.fullmatch(str(item.get("id", ""))):
            errors.append(f"verifiers[{index}] id invalid")
            continue
        verifier_ids.append(item["id"])
        covers = item.get("covers")
        command = item.get("command")
        if not isinstance(covers, list) or not covers or not all(entry in criterion_ids for entry in covers):
            errors.append(f"verifiers[{index}].covers must reference acceptance criterion ids")
        else:
            covered.extend(covers)
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            errors.append(f"verifiers[{index}].command must be a non-empty argv array")
        timeout = item.get("timeout_seconds", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            errors.append(f"verifiers[{index}].timeout_seconds must be a positive integer")
    missing_coverage = sorted(set(criterion_ids) - set(covered))
    if missing_coverage:
        errors.append(f"acceptance criteria lack verifier coverage: {', '.join(missing_coverage)}")
    if len(set(verifier_ids)) != len(verifier_ids):
        errors.append("verifier ids must be unique")
    task = value.get("task_template")
    if not isinstance(task, dict):
        errors.append("task_template must be an object")
    else:
        task_fields = {"task_id", "objective", "inputs", "immutable_paths", "allowed_write_paths", "constraints", "required_steps", "fixture_files", "max_repairs"}
        required_task_fields = {"task_id", "objective", "inputs", "immutable_paths", "allowed_write_paths", "fixture_files"}
        unknown_task = sorted(set(task) - task_fields)
        if unknown_task:
            errors.append("unsupported task_template fields: " + ", ".join(unknown_task))
        missing_task = sorted(required_task_fields - set(task))
        if missing_task:
            errors.append("missing task_template fields: " + ", ".join(missing_task))
        for field in ("task_id", "objective"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                errors.append(f"task_template.{field} must be non-empty")
        if not isinstance(task.get("fixture_files"), dict) or not task["fixture_files"]:
            errors.append("task_template.fixture_files must contain at least one fixture")
        else:
            for path, content in task["fixture_files"].items():
                try:
                    safe_relative(path, "task_template.fixture_files")
                except (TypeError, ValueError) as exc:
                    errors.append(str(exc))
                if not isinstance(content, str):
                    errors.append(f"fixture content for {path!r} must be text")
        for field in ("inputs", "immutable_paths", "allowed_write_paths"):
            paths = task.get(field, [])
            if not isinstance(paths, list):
                errors.append(f"task_template.{field} must be an array")
                continue
            for path in paths:
                try:
                    safe_relative(path, f"task_template.{field}")
                except (TypeError, ValueError) as exc:
                    errors.append(str(exc))
        if "constraints" in task and (not isinstance(task["constraints"], list) or not all(isinstance(item, str) for item in task["constraints"])):
            errors.append("task_template.constraints must be a string array")
        if "required_steps" in task and (not isinstance(task["required_steps"], list) or len(task["required_steps"]) > 50 or len(task["required_steps"]) != len(set(task["required_steps"] or [])) or not all(isinstance(item, str) and item for item in task["required_steps"])):
            errors.append("task_template.required_steps must be an array of up to 50 unique non-empty strings")
        if "max_repairs" in task and (isinstance(task["max_repairs"], bool) or not isinstance(task["max_repairs"], int) or not 0 <= task["max_repairs"] <= 3):
            errors.append("task_template.max_repairs must be an integer from 0 through 3")
        if effect == "mutation" and not task.get("allowed_write_paths"):
            errors.append("mutation workflow requires allowed_write_paths")
        immutable_paths = task.get("immutable_paths", []) if isinstance(task.get("immutable_paths", []), list) else []
        write_paths = task.get("allowed_write_paths", []) if isinstance(task.get("allowed_write_paths", []), list) else []
        overlaps = sorted(
            f"{write_path} <-> {immutable_path}"
            for write_path in write_paths
            for immutable_path in immutable_paths
            if isinstance(write_path, str) and isinstance(immutable_path, str) and paths_overlap(write_path, immutable_path)
        )
        if overlaps:
            errors.append("allowed_write_paths and immutable_paths overlap: " + ", ".join(overlaps))
    certification = value.get("certification_contract")
    if schema_version == "1.3" and certification is None:
        errors.append("schema_version 1.3 requires certification_contract")
    if certification is not None:
        if schema_version != "1.3":
            errors.append("certification_contract requires blueprint schema_version 1.3")
        required_certification = {"corpus", "deterministic_gates", "judges", "dimensions", "replay", "promotion"}
        if not isinstance(certification, dict) or set(certification) != required_certification:
            errors.append("certification_contract must contain exactly: " + ", ".join(sorted(required_certification)))
        else:
            corpus_path: str | None = None
            corpus = certification["corpus"]
            required_corpus = {"path", "minimum_cases", "required_classes"}
            if not isinstance(corpus, dict) or set(corpus) != required_corpus:
                errors.append("certification_contract.corpus must contain exactly: " + ", ".join(sorted(required_corpus)))
            else:
                try:
                    corpus_path = safe_relative(corpus["path"], "certification_contract.corpus.path")
                except (TypeError, ValueError) as exc:
                    errors.append(str(exc))
                    corpus_path = None
                if corpus_path and not corpus_path.endswith(".json"):
                    errors.append("certification_contract.corpus.path must name a JSON file")
                minimum_cases = corpus["minimum_cases"]
                if isinstance(minimum_cases, bool) or not isinstance(minimum_cases, int) or minimum_cases < 3:
                    errors.append("certification_contract.corpus.minimum_cases must be at least 3")
                required_classes = corpus["required_classes"]
                if (
                    not isinstance(required_classes, list)
                    or not all(isinstance(item, str) for item in required_classes)
                    or len(required_classes) != len(set(required_classes))
                    or not {"positive", "adversarial", "regression"}.issubset(set(required_classes))
                    or not all(item in {"positive", "adversarial", "regression", "integration", "safety"} for item in required_classes)
                ):
                    errors.append("certification_contract.corpus.required_classes must uniquely include positive, adversarial, and regression")

            deterministic_gates = certification["deterministic_gates"]
            if (
                not isinstance(deterministic_gates, list)
                or not deterministic_gates
                or not all(isinstance(gate_id, str) for gate_id in deterministic_gates)
                or len(deterministic_gates) != len(set(deterministic_gates))
                or not all(gate_id in verifier_ids for gate_id in deterministic_gates)
            ):
                errors.append("certification_contract.deterministic_gates must contain unique verifier ids")

            judges = certification["judges"]
            judge_ids: list[str] = []
            rubric_paths: list[str] = []
            if not isinstance(judges, list) or not judges:
                errors.append("certification_contract.judges must be a non-empty array")
                judges = []
            required_judge = {"id", "rubric_path", "model", "threshold", "evidence_fields"}
            for index, judge in enumerate(judges, start=1):
                field = f"certification_contract.judges[{index}]"
                if not isinstance(judge, dict) or set(judge) != required_judge:
                    errors.append(f"{field} must contain exactly: " + ", ".join(sorted(required_judge)))
                    continue
                if not isinstance(judge["id"], str) or not SAFE_NAME.fullmatch(judge["id"]):
                    errors.append(f"{field}.id must use safe kebab-case")
                else:
                    judge_ids.append(judge["id"])
                try:
                    rubric_path = safe_relative(judge["rubric_path"], f"{field}.rubric_path")
                    rubric_paths.append(rubric_path)
                    if not rubric_path.endswith(".json"):
                        errors.append(f"{field}.rubric_path must name a JSON file")
                except (TypeError, ValueError) as exc:
                    errors.append(str(exc))
                if not isinstance(judge["model"], str) or "/" not in judge["model"]:
                    errors.append(f"{field}.model must pin provider/model")
                threshold = judge["threshold"]
                if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 10:
                    errors.append(f"{field}.threshold must be a number from 0 through 10")
                evidence_fields = judge["evidence_fields"]
                if not isinstance(evidence_fields, list) or not evidence_fields or not all(isinstance(item, str) and item for item in evidence_fields) or len(evidence_fields) != len(set(evidence_fields)):
                    errors.append(f"{field}.evidence_fields must contain unique non-empty strings")
            if len(judge_ids) != len(set(judge_ids)):
                errors.append("certification_contract judge ids must be unique")

            dimensions = certification["dimensions"]
            dimension_names = {"truth", "eval_quality", "cost_efficiency", "integration", "safety"}
            if not isinstance(dimensions, dict) or set(dimensions) != dimension_names:
                errors.append("certification_contract.dimensions must contain exactly: " + ", ".join(sorted(dimension_names)))
            else:
                dimension_references = [reference for references in dimensions.values() for reference in references] if all(isinstance(references, list) for references in dimensions.values()) else []
                for dimension, references in dimensions.items():
                    if not isinstance(references, list) or not references or not all(isinstance(reference, str) for reference in references) or len(references) != len(set(references)) or not all(reference in criterion_ids for reference in references):
                        errors.append(f"certification_contract.dimensions.{dimension} must contain unique acceptance criterion ids")
                if set(dimension_references) != set(criterion_ids) or len(dimension_references) != len(set(dimension_references)):
                    errors.append("certification_contract.dimensions must map every acceptance criterion exactly once")

            replay = certification["replay"]
            required_replay = {"same_corpus", "max_cost_regression_percent", "max_latency_regression_percent"}
            if not isinstance(replay, dict) or not required_replay.issubset(replay) or set(replay) - (required_replay | {"baseline_version"}):
                errors.append("certification_contract.replay must contain same_corpus and cost/latency regression limits, plus optional baseline_version")
            else:
                if replay["same_corpus"] is not True:
                    errors.append("certification_contract.replay.same_corpus must be true")
                baseline_version = replay.get("baseline_version")
                if baseline_version is not None and (not isinstance(baseline_version, str) or not SAFE_VERSION.fullmatch(baseline_version)):
                    errors.append("certification_contract.replay.baseline_version must be null or semantic version text")
                for metric in ("max_cost_regression_percent", "max_latency_regression_percent"):
                    limit = replay[metric]
                    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit < 0:
                        errors.append(f"certification_contract.replay.{metric} must be a non-negative number")

            promotion = certification["promotion"]
            required_promotion = {"minimum_pass_rate", "minimum_dimension_score", "require_independent_validation", "require_operator_decision", "block_on_regression"}
            if not isinstance(promotion, dict) or set(promotion) != required_promotion:
                errors.append("certification_contract.promotion must contain exactly: " + ", ".join(sorted(required_promotion)))
            else:
                pass_rate = promotion["minimum_pass_rate"]
                dimension_score = promotion["minimum_dimension_score"]
                if isinstance(pass_rate, bool) or not isinstance(pass_rate, (int, float)) or not 0 < pass_rate <= 1:
                    errors.append("certification_contract.promotion.minimum_pass_rate must be greater than 0 and at most 1")
                if isinstance(dimension_score, bool) or not isinstance(dimension_score, (int, float)) or not 0 <= dimension_score <= 10:
                    errors.append("certification_contract.promotion.minimum_dimension_score must be from 0 through 10")
                for flag in ("require_independent_validation", "require_operator_decision", "block_on_regression"):
                    if promotion[flag] is not True:
                        errors.append(f"certification_contract.promotion.{flag} must be true")

            fixture_paths = set(task.get("fixture_files", {})) if isinstance(task, dict) and isinstance(task.get("fixture_files"), dict) else set()
            for certification_path in ([corpus_path] if corpus_path else []) + rubric_paths:
                if certification_path not in fixture_paths:
                    errors.append(f"certification artifact must be declared in task_template.fixture_files: {certification_path}")
    context = value.get("context_policy")
    if isinstance(context, dict) and set(context) != {"strategy", "max_input_bytes"}:
        errors.append("context_policy must contain exactly strategy and max_input_bytes")
    if not isinstance(context, dict) or context.get("strategy") not in {"full", "deterministic_segments", "hierarchical_chunks"} or isinstance(context.get("max_input_bytes"), bool) or not isinstance(context.get("max_input_bytes"), int) or context.get("max_input_bytes", 0) < 1:
        errors.append("context_policy requires a supported strategy and positive max_input_bytes")
    operations = value.get("operations")
    required_ops = {"owner", "concurrency", "retention_days", "redact_patterns", "failure_response"}
    if isinstance(operations, dict) and set(operations) != required_ops:
        errors.append("operations must contain exactly: " + ", ".join(sorted(required_ops)))
    if not isinstance(operations, dict) or not required_ops.issubset(operations):
        errors.append(f"operations must define: {', '.join(sorted(required_ops))}")
    else:
        if not isinstance(operations.get("owner"), str) or not operations["owner"].strip():
            errors.append("operations.owner must be non-empty")
        if operations.get("concurrency") not in {"isolated_workspace", "read_only_shared"}:
            errors.append("operations.concurrency must be isolated_workspace or read_only_shared")
        if isinstance(operations.get("retention_days"), bool) or not isinstance(operations.get("retention_days"), int) or operations["retention_days"] < 1:
            errors.append("operations.retention_days must be positive")
        if not isinstance(operations.get("redact_patterns"), list) or not all(isinstance(item, str) for item in operations.get("redact_patterns", [])):
            errors.append("operations.redact_patterns must be a string array")
        if not isinstance(operations.get("failure_response"), str) or not operations["failure_response"].strip():
            errors.append("operations.failure_response must be non-empty")
    return errors


def assert_blueprint(value: Any) -> dict[str, Any]:
    errors = validate_blueprint(value)
    if errors:
        raise SystemExit("invalid workflow blueprint:\n- " + "\n- ".join(errors))
    return value


def compiled_model_roles(blueprint: dict[str, Any]) -> dict[str, str]:
    declared = blueprint.get("models")
    if isinstance(declared, dict):
        return declared
    return MODEL_ROLES


def compiled_stage_capabilities(blueprint: dict[str, Any]) -> dict[str, Any]:
    declared = blueprint.get("stage_capabilities")
    if isinstance(declared, dict):
        return declared
    if (blueprint.get("runtime") or {}).get("kind") == "generic_mutation":
        return DEFAULT_STAGE_CAPABILITIES
    return {}
