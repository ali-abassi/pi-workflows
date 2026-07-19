#!/usr/bin/env python3
"""Certify a compiled workflow bundle with static and optional live smoke gates."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow_factory_common import canonical_digest, compiled_model_roles, compiled_peer_contract, compiled_stage_capabilities, paths_overlap, read_json, validate_blueprint
from run_pi_harness import terminate_process_group, validate_spec
from evaluate_transition import graph_errors


SKILL_ROOT = Path(__file__).resolve().parent.parent
RUNNER = SKILL_ROOT / "scripts" / "run_pi_harness.py"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content)
    os.replace(temporary, path)


def gate(name: str, passed: bool, detail: str, evidence: Any = None) -> dict[str, Any]:
    return {"name": name, "status": "passed" if passed else "failed", "detail": detail, "evidence": evidence}


def safe_paths(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    for value in values:
        path = Path(value) if isinstance(value, str) else Path("..")
        if not isinstance(value, str) or not value or path.is_absolute() or value in {".", ".."} or ".." in path.parts:
            return False
    return True


PI_INVOCATION_SKIP_DIRS = {"runs", "certification", "__pycache__", ".git", "node_modules"}
PI_INVOCATION_PATTERN = re.compile(r"(pi_path\(\)|self\.pi\b|\bPI_BIN\b|shutil\.which\([\"']pi[\"']\))")
PI_SUBPROCESS_PATTERN = re.compile(r"subprocess\.(run|Popen)|child_process|\bspawn\(|\bexecFile\(")
PI_STOPREASON_GUARD_PATTERN = re.compile(
    r"validate_pi_event_stream|pi_jsonl_verdict"
    r"|stopReason[^\n]{0,80}(aborted|error)"
    r"|stop_reason[^\n]{0,80}(aborted|error)"
)


def pi_invocation_guard_findings(harness: Path, runtime: dict[str, Any]) -> list[dict[str, Any]]:
    """Structural guard against the exact bug pi_jsonl_verdict()/validate_pi_event_stream()
    fix in run_pi_harness.py: a Pi subprocess can exit 0 while its JSONL stream
    records a final stopReason of "aborted" (or "error", or a failed auto-retry).

    generic_mutation harnesses cannot regress this -- they call run_verifiers()/
    run_supervisor_commands() in the shared, tested run_pi_harness.py directly.
    A `specialized` runtime supplies its own entrypoint and is free to shell out
    to Pi with its own subprocess + JSONL-parsing code (as competitor-analysis
    and product-planning both do). pi_protocol_regression (below) only re-runs
    test_pi_protocol_verdict.py against the shared library -- it always passes
    even if a specialized harness's own reimplementation never checks stopReason
    at all, because that gate never looks at the harness under certification.

    This gate closes that gap mechanically: for specialized runtimes, scan every
    script for a direct Pi subprocess invocation (a reference to the pi binary
    alongside a subprocess/spawn call) and require it to also import the shared
    fail-closed helpers or contain its own explicit aborted/error stopReason
    check. Missing both is reported as a finding; certification treats any
    finding as a failed gate.
    """
    if runtime.get("kind") != "specialized":
        return []
    findings: list[dict[str, Any]] = []
    for path in sorted(harness.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".mjs", ".js", ".ts"}:
            continue
        if any(part in PI_INVOCATION_SKIP_DIRS for part in path.relative_to(harness).parts):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if PI_INVOCATION_PATTERN.search(text) and PI_SUBPROCESS_PATTERN.search(text) and not PI_STOPREASON_GUARD_PATTERN.search(text):
            findings.append({
                "file": str(path.relative_to(harness)),
                "reason": "direct Pi subprocess invocation with no import of validate_pi_event_stream/pi_jsonl_verdict "
                          "and no explicit aborted/error stopReason check -- a zero exit code could launder an aborted Pi turn",
            })
    return findings


def static_gates(harness: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    required = ["workflow.blueprint.json", "harness.json", "schemas/task.schema.json", "examples/task.json", "certification-profile.json", "workflow.md", "OPERATIONS.md"]
    missing = [item for item in required if not (harness / item).is_file()]
    results = [gate("bundle_structure", not missing, "all required compiled artifacts exist" if not missing else f"missing: {', '.join(missing)}", required)]
    if missing:
        return results, {}, {}
    blueprint = read_json(harness / "workflow.blueprint.json")
    config = read_json(harness / "harness.json")
    task = read_json(harness / "examples" / "task.json")
    blueprint_errors = validate_blueprint(blueprint)
    results.append(gate("blueprint_contract", not blueprint_errors, "blueprint contract valid" if not blueprint_errors else "; ".join(blueprint_errors), blueprint_errors))
    results.append(gate("objective_contract", task.get("objective_contract") == blueprint.get("objective_contract"), "compiled objective contract matches the blueprint"))
    results.append(gate("lifecycle", task.get("lifecycle") == blueprint.get("lifecycle"), "compiled lifecycle matches the blueprint", task.get("lifecycle")))
    expected_models = compiled_model_roles(blueprint)
    results.append(gate("model_pinning", config.get("models") == expected_models, "all Pi model roles match the explicit blueprint pins", config.get("models")))
    expected_capabilities = compiled_stage_capabilities(blueprint)
    capabilities_ok = config.get("stage_capabilities") == expected_capabilities and task.get("allowed_tools") == (expected_capabilities.get("execute") or {}).get("tools", task.get("allowed_tools"))
    results.append(gate("stage_capabilities", capabilities_ok, "compiled Pi tool profiles match the blueprint" if capabilities_ok else "compiled Pi tool profiles drifted from the blueprint", config.get("stage_capabilities")))
    criterion_ids = {item.get("id") for item in blueprint.get("acceptance_criteria", []) if isinstance(item, dict)}
    covered = {criterion for verifier in blueprint.get("verifiers", []) if isinstance(verifier, dict) for criterion in verifier.get("covers", [])}
    results.append(gate("verifier_coverage", criterion_ids == covered and bool(criterion_ids), f"{len(covered)}/{len(criterion_ids)} acceptance criteria covered", {"criteria": sorted(criterion_ids), "covered": sorted(covered)}))
    expected_contracts = [
        {"id": item.get("id"), "covers": item.get("covers"), "command": item.get("command"), "timeout_seconds": int(item.get("timeout_seconds", 60))}
        for item in blueprint.get("verifiers", []) if isinstance(item, dict)
    ]
    provenance_ok = task.get("acceptance_criteria") == blueprint.get("acceptance_criteria") and task.get("verification_contracts") == expected_contracts
    results.append(gate("criterion_provenance", provenance_ok, "criterion ids and verifier evidence mapping survive compilation" if provenance_ok else "compiled task lost or changed criterion/verifier provenance"))
    paths_ok = all(safe_paths(task.get(field, [])) for field in ("inputs", "immutable_paths", "allowed_write_paths"))
    immutable = set(task.get("immutable_paths", []))
    inputs = set(task.get("inputs", []))
    writes = set(task.get("allowed_write_paths", []))
    paths_ok = paths_ok and inputs.issubset(immutable) and not any(paths_overlap(write, locked) for write in writes for locked in immutable)
    results.append(gate("path_policy", paths_ok, "inputs are immutable, writes are disjoint, and all paths are relative" if paths_ok else "path policy is unsafe"))
    context = task.get("context_policy") or {}
    runtime = blueprint.get("runtime") or {}
    context_ok = context.get("strategy") == "full" if runtime.get("kind") == "generic_mutation" else context.get("strategy") in {"full", "deterministic_segments", "hierarchical_chunks"}
    results.append(gate("context_policy", context_ok, "context policy is executable by the selected runtime" if context_ok else "generic mutation runtime only supports full context"))
    operations = blueprint.get("operations") or {}
    operations_ok = bool(operations.get("owner") and operations.get("failure_response") and operations.get("retention_days") and isinstance(operations.get("redact_patterns"), list))
    results.append(gate("operations_policy", operations_ok, "owner, retention, redaction, concurrency, and failure response are declared" if operations_ok else "operations policy incomplete"))
    runtime_ok = runtime.get("kind") == "generic_mutation" or (runtime.get("kind") == "specialized" and (harness / str(runtime.get("entrypoint", ""))).is_file())
    results.append(gate("runtime_readiness", runtime_ok, "runtime entrypoint is available" if runtime_ok else "specialized runtime entrypoint is missing"))
    runner_error = None
    if runtime.get("kind") == "generic_mutation":
        try:
            validate_spec(task)
        except SystemExit as exc:
            runner_error = str(exc)
    runner_detail = "specialized runner contract is owned by its certification adapter" if runtime.get("kind") == "specialized" else "compiled task is accepted by the selected runner"
    results.append(gate("runner_contract", runner_error is None, runner_detail if runner_error is None else runner_error))
    pi_protocol_regression_path = SKILL_ROOT / "scripts" / "test_pi_protocol_verdict.py"
    pi_protocol_proc = subprocess.run(
        [sys.executable, str(pi_protocol_regression_path)],
        cwd=SKILL_ROOT / "scripts",
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    pi_protocol_ok = pi_protocol_proc.returncode == 0
    results.append(gate(
        "pi_protocol_regression",
        pi_protocol_ok,
        "verify/execute stages fail closed on an aborted/malformed/extension-error Pi JSONL stop reason even with exit code 0"
        if pi_protocol_ok else "run_verifiers()/run_supervisor_commands() accepted a zero exit code despite a non-'stop' Pi stop reason",
        {"exit_code": pi_protocol_proc.returncode, "stdout": pi_protocol_proc.stdout[-2000:], "stderr": pi_protocol_proc.stderr[-2000:]},
    ))
    guard_findings = pi_invocation_guard_findings(harness, runtime)
    results.append(gate(
        "specialized_pi_invocation_guard",
        not guard_findings,
        "no unguarded direct Pi subprocess invocation found in this harness's own scripts"
        if not guard_findings
        else "this harness invokes Pi directly without a fail-closed stopReason guard: "
        + "; ".join(f"{item['file']}" for item in guard_findings),
        guard_findings,
    ))
    guard_regression_path = SKILL_ROOT / "scripts" / "test_pi_invocation_guard.py"
    guard_regression_proc = subprocess.run(
        [sys.executable, str(guard_regression_path)],
        cwd=SKILL_ROOT / "scripts",
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    guard_regression_ok = guard_regression_proc.returncode == 0
    results.append(gate(
        "pi_invocation_guard_regression",
        guard_regression_ok,
        "specialized_pi_invocation_guard correctly flags unguarded direct Pi calls and leaves guarded/non-specialized code alone"
        if guard_regression_ok else "pi_invocation_guard_findings() regressed against its own fixtures",
        {"exit_code": guard_regression_proc.returncode, "stdout": guard_regression_proc.stdout[-2000:], "stderr": guard_regression_proc.stderr[-2000:]},
    ))
    declared_flow = blueprint.get("control_flow")
    compiled_flow_path = harness / "control-flow.json"
    compiled_engine_path = harness / "scripts" / "evaluate_transition.py"
    expected_flow = {"schema_version": "1.0", "workflow": blueprint.get("workflow"), "stages": blueprint.get("lifecycle"), **declared_flow} if isinstance(declared_flow, dict) else None
    flow_errors = graph_errors(expected_flow) if expected_flow else []
    flow_contract_ok = declared_flow is None or (runtime.get("kind") == "specialized" and not flow_errors)
    results.append(gate("control_flow_contract", flow_contract_ok, "conditional graph is valid and specialized" if declared_flow else "no conditional graph declared", flow_errors))
    compiled_flow = read_json(compiled_flow_path) if compiled_flow_path.is_file() else None
    flow_compilation_ok = (declared_flow is None and compiled_flow is None) or (expected_flow == compiled_flow and compiled_engine_path.is_file())
    results.append(gate("control_flow_compilation", flow_compilation_ok, "compiled conditional graph and evaluator are present" if declared_flow else "no conditional artifacts required"))
    flow_config = config.get("control_flow") or {}
    engine_integrity_ok = (
        declared_flow is None and flow_config.get("enabled") is False
    ) or (
        declared_flow is not None
        and flow_config.get("enabled") is True
        and flow_config.get("graph_digest") == canonical_digest(compiled_flow)
        and flow_config.get("engine_digest") == canonical_digest(compiled_engine_path.read_text())
    )
    results.append(gate("transition_engine_integrity", engine_integrity_ok, "conditional graph and evaluator match harness digests" if engine_integrity_ok else "conditional graph or evaluator digest mismatch"))
    declared_peer = compiled_peer_contract(blueprint)
    peer_contract_path = harness / "peer-collaboration.json"
    peer_validator_path = harness / "scripts" / "validate_peer_exchange.py"
    peer_request_schema = harness / "schemas" / "peer-request.schema.json"
    peer_response_schema = harness / "schemas" / "peer-response.schema.json"
    peer_contract_ok = declared_peer is None or runtime.get("kind") == "specialized"
    results.append(gate("peer_collaboration_contract", peer_contract_ok, "peer collaboration is bounded by a specialized controller" if declared_peer else "no peer collaboration declared"))
    compiled_peer = read_json(peer_contract_path) if peer_contract_path.is_file() else None
    peer_compilation_ok = (
        declared_peer is None
        and compiled_peer is None
        and not peer_validator_path.exists()
    ) or (
        declared_peer == compiled_peer
        and peer_validator_path.is_file()
        and peer_request_schema.is_file()
        and peer_response_schema.is_file()
    )
    results.append(gate("peer_collaboration_compilation", peer_compilation_ok, "peer contract, schemas, and exchange validator are present" if declared_peer else "no peer artifacts required"))
    peer_config = config.get("peer_collaboration") or {}
    peer_integrity_ok = (
        declared_peer is None and peer_config.get("enabled") is False
    ) or (
        declared_peer is not None
        and peer_config.get("enabled") is True
        and peer_config.get("contract") == "peer-collaboration.json"
        and peer_config.get("validator") == "scripts/validate_peer_exchange.py"
        and peer_config.get("request_schema") == "schemas/peer-request.schema.json"
        and peer_config.get("response_schema") == "schemas/peer-response.schema.json"
        and compiled_peer is not None
        and peer_validator_path.is_file()
        and peer_request_schema.is_file()
        and peer_response_schema.is_file()
        and peer_config.get("contract_digest") == canonical_digest(compiled_peer)
        and peer_config.get("validator_digest") == canonical_digest(peer_validator_path.read_text())
        and peer_config.get("request_schema_digest") == canonical_digest(peer_request_schema.read_text())
        and peer_config.get("response_schema_digest") == canonical_digest(peer_response_schema.read_text())
    )
    results.append(gate("peer_exchange_validator_integrity", peer_integrity_ok, "peer contract and validator match harness digests" if peer_integrity_ok else "peer contract or validator digest mismatch"))
    declared_certification = blueprint.get("certification_contract")
    certification_config = config.get("certification_contract") or {}
    certification_contract_path = harness / "certification-contract.json"
    certification_evaluator_path = harness / "scripts" / "evaluate_certification.py"
    certification_contract_schema_path = harness / "schemas" / "certification-contract.schema.json"
    certification_decision_schema_path = harness / "schemas" / "certification-decision.schema.json"
    certification_schema_paths = {
        "corpus": harness / "schemas" / "certification-corpus.schema.json",
        "rubric": harness / "schemas" / "certification-rubric.schema.json",
        "result": harness / "schemas" / "certification-result.schema.json",
    }
    compiled_certification = read_json(certification_contract_path) if certification_contract_path.is_file() else None
    certification_contract_ok = (
        declared_certification is None and certification_config.get("enabled") is False
    ) or (
        declared_certification is not None
        and certification_config.get("enabled") is True
        and declared_certification == compiled_certification
        and certification_config.get("digest") == canonical_digest(compiled_certification)
    )
    results.append(gate(
        "certification_contract",
        certification_contract_ok,
        "certification contract survived compilation unchanged" if declared_certification else "no certification contract declared",
    ))
    certification_artifacts_ok = (
        declared_certification is None
        and not certification_contract_path.exists()
        and not certification_evaluator_path.exists()
        and not any(path.exists() for path in (*certification_schema_paths.values(), certification_contract_schema_path, certification_decision_schema_path))
    ) or (
        declared_certification is not None
        and certification_contract_path.is_file()
        and certification_evaluator_path.is_file()
        and all(path.is_file() for path in certification_schema_paths.values())
        and certification_contract_schema_path.is_file()
        and certification_decision_schema_path.is_file()
        and (harness / "examples" / "workspace" / declared_certification["corpus"]["path"]).is_file()
        and all(
            (harness / "examples" / "workspace" / judge["rubric_path"]).is_file()
            for judge in declared_certification["judges"]
        )
    )
    results.append(gate(
        "certification_artifacts",
        certification_artifacts_ok,
        "contract, evaluator, schemas, corpus, and rubrics are present" if declared_certification else "no certification artifacts required",
    ))
    certification_evaluator_integrity_ok = (
        declared_certification is None
    ) or (
        certification_config.get("path") == "certification-contract.json"
        and certification_config.get("evaluator") == "scripts/evaluate_certification.py"
        and certification_config.get("evaluator_digest") == canonical_digest(certification_evaluator_path.read_text())
        and all(
            certification_config.get(f"{name}_schema") == f"schemas/certification-{name}.schema.json"
            and certification_config.get(f"{name}_schema_digest") == canonical_digest(path.read_text())
            for name, path in certification_schema_paths.items()
        )
    )
    results.append(gate(
        "certification_evaluator_integrity",
        certification_evaluator_integrity_ok,
        "certification evaluator and schemas match harness digests" if certification_evaluator_integrity_ok else "certification evaluator or schema digest mismatch",
    ))
    certification_contract_schema_ok = (
        declared_certification is None
    ) or (
        certification_config.get("contract_schema") == "schemas/certification-contract.schema.json"
        and certification_config.get("contract_schema_digest") == canonical_digest(read_json(certification_contract_schema_path))
    )
    results.append(gate(
        "certification_contract_schema_integrity",
        certification_contract_schema_ok,
        "certification contract schema matches harness digest" if certification_contract_schema_ok else "certification contract schema digest mismatch",
    ))
    certification_decision_schema_ok = (
        declared_certification is None
    ) or (
        certification_config.get("decision_schema") == "schemas/certification-decision.schema.json"
        and certification_config.get("decision_schema_digest") == canonical_digest(certification_decision_schema_path.read_text())
    )
    results.append(gate(
        "certification_decision_schema_integrity",
        certification_decision_schema_ok,
        "certification decision schema matches harness digest" if certification_decision_schema_ok else "certification decision schema digest mismatch",
    ))
    digest_ok = config.get("blueprint_digest") == canonical_digest(blueprint)
    results.append(gate("compiled_integrity", digest_ok, "compiled harness is bound to this exact blueprint" if digest_ok else "blueprint changed after compilation"))
    return results, blueprint, task


def snapshot_outputs(workdir: Path, paths: list[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for relative in paths:
        path = workdir / relative
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def run_command(argv: list[str], cwd: Path, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_group(proc)
        stdout, stderr = proc.communicate()
        stderr = f"{stderr or ''}\ncommand timed out after {timeout} seconds".lstrip()
        return subprocess.CompletedProcess(argv, 124, stdout, stderr)
    except BaseException:
        terminate_process_group(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def seal_status(run_dir: Path) -> str:
    seal_path = run_dir / "integrity" / "run-seal.json"
    if not seal_path.is_file():
        return "unsealed"
    seal = read_json(seal_path)
    for item in seal.get("artifacts", []):
        path = run_dir / item.get("path", "")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
            return "invalid"
    return "verified"


SPECIALIZED_ADAPTER_RELATIVE = Path("scripts") / "certify.py"


def specialized_dynamic_gates(harness: Path) -> list[dict[str, Any]]:
    """Run the harness-supplied certification adapter for a `specialized` runtime.

    Convention (matches every shipped specialized harness -- product-planning,
    competitor-analysis): the compiled bundle carries `scripts/certify.py`,
    invoked with no arguments and the harness root as cwd; it exits 0 on a
    passing fixture run and non-zero otherwise. Without this, every
    specialized runtime failed `specialized_fixture` unconditionally even
    when it shipped a working, passing adapter.
    """
    adapter = harness / SPECIALIZED_ADAPTER_RELATIVE
    if not adapter.is_file():
        return [gate("specialized_fixture", False, "specialized runtimes must supply their own executable certification adapter", None)]
    result = run_command(["python3", str(adapter)], harness, 1800)
    passed = result.returncode == 0
    evidence = {"exit_code": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}
    return [gate("specialized_fixture", passed, "harness-supplied certification adapter passed" if passed else "harness-supplied certification adapter failed", evidence)]


def dynamic_gates(harness: Path, blueprint: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    runtime_kind = (blueprint.get("runtime") or {}).get("kind")
    if blueprint.get("effect") != "mutation" or runtime_kind != "generic_mutation":
        if runtime_kind == "specialized":
            return specialized_dynamic_gates(harness)
        return [{"name": "specialized_fixture", "status": "failed", "detail": "specialized runtimes must supply their own executable certification adapter", "evidence": None}]
    spec_path = harness / "examples" / "task.json"
    workdir = Path(task["workdir"])
    output_paths = task.get("allowed_write_paths", [])
    before = snapshot_outputs(workdir, output_paths)
    prefix = f"factory-cert-{utc_stamp()}"
    gate_run_id = f"{prefix}-approval"
    base = ["python3", str(RUNNER), "--harness", str(harness), "--spec", str(spec_path)]
    approval = run_command(base + ["--run-id", gate_run_id], harness)
    after = snapshot_outputs(workdir, output_paths)
    gate_run = harness / "runs" / task["task_id"] / gate_run_id
    approval_ok = approval.returncode == 3 and before == after and (gate_run / "stages" / "plan.json").is_file()
    results.append(gate("approval_negative", approval_ok, "execution stopped at approval without changing outputs" if approval_ok else "approval gate did not fail closed", {"exit_code": approval.returncode, "stderr": approval.stderr[-2000:]}))
    if approval_ok:
        positive_id = f"{prefix}-positive"
        positive = run_command(base + ["--run-id", positive_id, "--approve-execution", "--approved-plan-artifact", str(gate_run / "stages" / "plan.json")], harness)
        positive_run = harness / "runs" / task["task_id"] / positive_id
        validation = read_json(positive_run / "validation" / "final_validation.json") if (positive_run / "validation" / "final_validation.json").is_file() else {}
        positive_ok = positive.returncode == 0 and validation.get("status") == "passed" and validation.get("mechanical_verification") == "passed"
        results.append(gate("positive_end_to_end", positive_ok, "approved fixture completed and passed mechanical verification" if positive_ok else "approved fixture failed", {"exit_code": positive.returncode, "validation": validation, "stderr": positive.stderr[-2000:]}))
        if positive_ok:
            with tempfile.TemporaryDirectory(prefix="workflow-seal-tamper-") as temporary:
                copy = Path(temporary) / "run"
                shutil.copytree(positive_run, copy)
                report = copy / "final_report.md"
                report.write_text(report.read_text() + "\npost-seal mutation\n")
                tamper_ok = seal_status(copy) == "invalid"
            results.append(gate("seal_tamper", tamper_ok, "post-seal mutation was detected" if tamper_ok else "post-seal mutation was not detected"))
        else:
            results.append(gate("seal_tamper", False, "positive run unavailable for tamper test"))
    else:
        results.extend([gate("positive_end_to_end", False, "approval prerequisite failed"), gate("seal_tamper", False, "positive run unavailable")])
    with tempfile.TemporaryDirectory(prefix="workflow-cert-spec-") as temporary:
        missing_spec = json.loads(json.dumps(task))
        missing_spec["task_id"] = f"{task['task_id']}-missing"
        missing_spec["inputs"] = ["factory-cert-missing-input.txt"]
        missing_spec["immutable_paths"] = ["factory-cert-missing-input.txt"]
        missing_path = Path(temporary) / "missing.json"
        missing_path.write_text(json.dumps(missing_spec))
        missing = run_command(["python3", str(RUNNER), "--harness", str(harness), "--spec", str(missing_path), "--run-id", f"{prefix}-missing"], harness, 120)
        results.append(gate("missing_input", missing.returncode != 0, "missing immutable input failed before execution" if missing.returncode != 0 else "missing input was accepted", {"exit_code": missing.returncode, "stderr": missing.stderr[-1000:]}))
        overflow_spec = json.loads(json.dumps(task))
        overflow_spec["task_id"] = f"{task['task_id']}-overflow"
        overflow_spec["context_policy"] = {"strategy": "full", "max_input_bytes": 1}
        overflow_path = Path(temporary) / "overflow.json"
        overflow_path.write_text(json.dumps(overflow_spec))
        overflow = run_command(["python3", str(RUNNER), "--harness", str(harness), "--spec", str(overflow_path), "--run-id", f"{prefix}-overflow"], harness, 120)
        overflow_run = harness / "runs" / overflow_spec["task_id"] / f"{prefix}-overflow"
        preflight_path = overflow_run / "validation" / "context-preflight.json"
        preflight = read_json(preflight_path) if preflight_path.is_file() else {}
        overflow_ok = overflow.returncode != 0 and preflight.get("passed") is False
        results.append(gate("context_overflow", overflow_ok, "oversized input failed during context preflight" if overflow_ok else "context overflow did not fail closed", {"exit_code": overflow.returncode, "preflight": preflight}))
    return results


def certification_contract_gate(
    harness: Path,
    results_path: Path,
    baseline_results_path: Path | None,
    operator_decision_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the compiled, digest-pinned contract evaluator against external evidence."""
    evaluator = harness / "scripts" / "evaluate_certification.py"
    with tempfile.TemporaryDirectory(prefix="workflow-contract-cert-") as temporary:
        output = Path(temporary) / "evaluation.json"
        argv = [
            "python3", str(evaluator),
            "--harness", str(harness),
            "--results", str(results_path),
            "--output", str(output),
        ]
        if baseline_results_path is not None:
            argv.extend(["--baseline-results", str(baseline_results_path)])
        if operator_decision_path is not None:
            argv.extend(["--operator-decision", str(operator_decision_path)])
        completed = run_command(argv, harness, 1800)
        evaluation = read_json(output) if output.is_file() else None
    status = evaluation.get("status") if evaluation else "invalid"
    return gate(
        "certification_contract_evaluation",
        completed.returncode == 0 and status == "certified",
        f"independent evidence status: {status}" if evaluation else f"contract evaluator failed: {completed.stderr[-2000:]}",
        {
            "exit_code": completed.returncode,
            "evaluation": evaluation,
            "stderr": completed.stderr[-2000:] if completed.stderr else "",
        },
    ), evaluation


def certification_evidence_paths(
    harness: Path,
    blueprint: dict[str, Any],
    results_path: Path | None,
    baseline_results_path: Path | None,
    operator_decision_path: Path | None,
) -> list[tuple[str, Path]]:
    """Return every certification input that must be preserved with the decision."""
    paths = [
        ("compiled/workflow.blueprint.json", harness / "workflow.blueprint.json"),
        ("compiled/harness.json", harness / "harness.json"),
        ("compiled/certification-contract.json", harness / "certification-contract.json"),
        ("compiled/evaluate_certification.py", harness / "scripts" / "evaluate_certification.py"),
        ("compiled/certification-contract.schema.json", harness / "schemas" / "certification-contract.schema.json"),
        ("compiled/certification-corpus.schema.json", harness / "schemas" / "certification-corpus.schema.json"),
        ("compiled/certification-rubric.schema.json", harness / "schemas" / "certification-rubric.schema.json"),
        ("compiled/certification-result.schema.json", harness / "schemas" / "certification-result.schema.json"),
        ("compiled/certification-decision.schema.json", harness / "schemas" / "certification-decision.schema.json"),
    ]
    contract = blueprint.get("certification_contract")
    if isinstance(contract, dict):
        corpus = contract.get("corpus", {}).get("path")
        if isinstance(corpus, str):
            paths.append((f"fixtures/{Path(corpus).name}", harness / "examples" / "workspace" / corpus))
        for judge in contract.get("judges", []):
            if isinstance(judge, dict) and isinstance(judge.get("rubric_path"), str):
                rubric = judge["rubric_path"]
                paths.append((f"rubrics/{Path(rubric).name}", harness / "examples" / "workspace" / rubric))
    for name, path in (
        ("submitted/candidate-results.json", results_path),
        ("submitted/baseline-results.json", baseline_results_path),
        ("submitted/operator-decision.json", operator_decision_path),
    ):
        if path is not None:
            paths.append((name, path))
    return paths


def preserve_certification_evidence(
    output_dir: Path,
    paths: list[tuple[str, Path]],
) -> list[dict[str, str]]:
    """Copy immutable inputs into the run directory and return their digests."""
    manifest: list[dict[str, str]] = []
    for relative, source in paths:
        if not source.is_file():
            continue
        destination = output_dir / "evidence" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        manifest.append({
            "path": str(destination.relative_to(output_dir)),
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        })
    return sorted(manifest, key=lambda item: item["path"])


def seal_certification_run(output_dir: Path, artifacts: list[dict[str, str]]) -> None:
    seal = {
        "schema_version": "1.0",
        "sealed_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
    }
    atomic_write(output_dir / "integrity" / "run-seal.json", json.dumps(seal, indent=2) + "\n")


def render_html(report: dict[str, Any]) -> str:
    rows = []
    for item in report["gates"]:
        color = "#1f9d55" if item["status"] == "passed" else "#d64545" if item["status"] == "failed" else "#d68b00"
        rows.append(f"<tr><td><span style='color:{color}'>●</span></td><td><b>{html.escape(item['name'])}</b></td><td>{html.escape(item['status'])}</td><td>{html.escape(item['detail'])}</td></tr>")
    return f"""<!doctype html><meta charset='utf-8'><title>Workflow certification</title><style>body{{font:14px system-ui;background:#f5f6f8;color:#17191c;margin:0}}main{{max-width:980px;margin:auto;padding:32px}}section{{background:white;border:1px solid #ddd;border-radius:12px;padding:20px}}h1{{margin-top:0}}table{{width:100%;border-collapse:collapse}}td,th{{padding:10px;border-top:1px solid #eee;text-align:left}}th{{color:#667085;font-size:11px;text-transform:uppercase}}</style><main><section><h1>{html.escape(report['workflow'])} {html.escape(report['version'])}</h1><p>Status: <b>{html.escape(report['status'])}</b> · {report['passed_gates']}/{report['total_gates']} gates passed</p><table><thead><tr><th></th><th>Gate</th><th>Status</th><th>Evidence</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section></main>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--run-smoke", action="store_true", help="Run Pi approval and positive fixture tests; may use model tokens")
    parser.add_argument("--results", help="Independent certification results JSON")
    parser.add_argument("--baseline-results", help="Optional baseline results JSON for regression comparison")
    parser.add_argument("--operator-decision", help="Explicit operator promotion decision JSON")
    args = parser.parse_args()
    harness = Path(args.harness).expanduser().resolve()
    results_path = Path(args.results).expanduser().resolve() if args.results else None
    baseline_results_path = Path(args.baseline_results).expanduser().resolve() if args.baseline_results else None
    operator_decision_path = Path(args.operator_decision).expanduser().resolve() if args.operator_decision else None
    static, blueprint, task = static_gates(harness)
    static_ok = bool(blueprint and task and all(item["status"] == "passed" for item in static))
    dynamic = dynamic_gates(harness, blueprint, task) if args.run_smoke and static_ok else []
    contract_evaluation = None
    contract_gates: list[dict[str, Any]] = []
    if results_path is not None and static_ok:
        contract_gate, contract_evaluation = certification_contract_gate(
            harness,
            results_path,
            baseline_results_path,
            operator_decision_path,
        )
        contract_gates.append(contract_gate)
    gates = static + dynamic + contract_gates
    failed = [item for item in gates if item["status"] == "failed"]
    contract_certified = bool(contract_evaluation and contract_evaluation.get("status") == "certified")
    if failed:
        status = "failed"
    elif contract_certified:
        status = "certified"
    elif args.run_smoke:
        status = "fixture_certified"
    else:
        status = "static_ready"
    if status == "certified":
        residual_requirements: list[str] = []
    elif status == "fixture_certified":
        residual_requirements = ["submit independent corpus results", "record explicit operator promotion decision"]
    else:
        residual_requirements = ["run executable smoke certification", "submit independent certification evidence"]
    stamp = utc_stamp()
    output_dir = harness / "certification" / stamp
    evidence_manifest = preserve_certification_evidence(
        output_dir,
        certification_evidence_paths(
            harness,
            blueprint,
            results_path,
            baseline_results_path,
            operator_decision_path,
        ),
    )
    report = {
        "schema_version": "1.1",
        "workflow": blueprint.get("workflow", harness.name),
        "version": blueprint.get("version", "unknown"),
        "harness": str(harness),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "contract" if results_path is not None else "smoke" if args.run_smoke else "static",
        "status": status,
        "promotion_eligible": status == "certified",
        "promotable": status == "certified",
        "passed_gates": sum(item["status"] == "passed" for item in gates),
        "total_gates": len(gates),
        "gates": gates,
        "contract_evaluation": contract_evaluation,
        "evidence_manifest": evidence_manifest,
        "residual_requirements": residual_requirements,
    }
    report_path = output_dir / "certification.json"
    index_path = output_dir / "index.html"
    atomic_write(report_path, json.dumps(report, indent=2) + "\n")
    atomic_write(index_path, render_html(report))
    sealed_artifacts = evidence_manifest + [
        {"path": "certification.json", "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest()},
        {"path": "index.html", "sha256": hashlib.sha256(index_path.read_bytes()).hexdigest()},
    ]
    seal_certification_run(output_dir, sorted(sealed_artifacts, key=lambda item: item["path"]))
    atomic_write(harness / "certification" / "latest.json", json.dumps(report, indent=2) + "\n")
    atomic_write(harness / "certification" / "index.html", render_html(report))
    print(output_dir)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
