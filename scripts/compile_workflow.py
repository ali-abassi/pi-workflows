#!/usr/bin/env python3
"""Compile a versioned deterministic workflow blueprint into a Pi harness bundle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from workflow_factory_common import FACTORY_VERSION, PI_COMPATIBILITY, assert_blueprint, canonical_digest, compiled_model_roles, compiled_peer_contract, compiled_stage_capabilities, read_json, safe_relative


TRANSITION_ENGINE = Path(__file__).resolve().parent / "evaluate_transition.py"
PEER_EXCHANGE_VALIDATOR = Path(__file__).resolve().parent / "validate_peer_exchange.py"
CERTIFICATION_EVALUATOR = Path(__file__).resolve().parent / "evaluate_certification.py"
SCHEMAS_ROOT = Path(__file__).resolve().parent.parent / "schemas"
PEER_REQUEST_SCHEMA = SCHEMAS_ROOT / "peer-request.schema.json"
PEER_RESPONSE_SCHEMA = SCHEMAS_ROOT / "peer-response.schema.json"
CERTIFICATION_CORPUS_SCHEMA = SCHEMAS_ROOT / "certification-corpus.schema.json"
CERTIFICATION_RUBRIC_SCHEMA = SCHEMAS_ROOT / "certification-rubric.schema.json"
CERTIFICATION_RESULT_SCHEMA = SCHEMAS_ROOT / "certification-result.schema.json"
CERTIFICATION_DECISION_SCHEMA = SCHEMAS_ROOT / "certification-decision.schema.json"
WORKFLOW_BLUEPRINT_SCHEMA = SCHEMAS_ROOT / "workflow-blueprint.schema.json"


def certification_contract_schema() -> dict[str, Any]:
    source = read_json(WORKFLOW_BLUEPRINT_SCHEMA)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://local.codex/certification-contract.schema.json",
        "title": "Deterministic workflow certification contract",
        **source["properties"]["certification_contract"],
    }


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content)
    os.replace(temporary, path)


def task_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["task_id", "objective", "objective_contract", "lifecycle", "workdir", "inputs", "constraints", "acceptance_criteria", "allowed_tools", "verification_commands", "verification_contracts", "context_policy"],
        "properties": {
            "task_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
            "objective": {"type": "string", "minLength": 1},
            "objective_contract": {
                "type": "object", "additionalProperties": False,
                "required": ["selector", "target", "decision", "non_goals"],
                "properties": {
                    "selector": {"type": "string", "minLength": 1}, "target": {"type": "string", "minLength": 1},
                    "decision": {"type": "string", "minLength": 1}, "non_goals": {"type": "array", "items": {"type": "string", "minLength": 1}}
                },
            },
            "lifecycle": {"type": "array", "minItems": 2, "uniqueItems": True, "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}},
            "workdir": {"type": "string", "minLength": 1},
            "inputs": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "acceptance_criteria": {"type": "array", "minItems": 1, "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "description"],
                "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}, "description": {"type": "string", "minLength": 1}},
            }},
            "allowed_tools": {"type": "array", "items": {"enum": ["read", "bash", "edit", "write", "grep", "find", "ls"]}},
            "verification_commands": {"type": "array", "minItems": 1, "items": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
            "verification_contracts": {"type": "array", "minItems": 1, "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "covers", "command", "timeout_seconds"],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
                    "covers": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}},
                    "command": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
            }},
            "verification_timeout_seconds": {"type": "integer", "minimum": 1},
            "max_repairs": {"type": "integer", "minimum": 0, "maximum": 3},
            "execution_commands": {"type": "array", "items": {"type": "array", "minItems": 1, "items": {"type": "string"}}},
            "allowed_write_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "immutable_paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "allow_bash": {"type": "boolean"}, "clean_allowed_write_paths": {"type": "boolean"},
            "required_steps": {"type": "array", "maxItems": 50, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
            "step_validation": {"type": "object"},
            "context_policy": {
                "type": "object", "additionalProperties": False, "required": ["strategy", "max_input_bytes"],
                "properties": {"strategy": {"enum": ["full", "deterministic_segments", "hierarchical_chunks"]}, "max_input_bytes": {"type": "integer", "minimum": 1}},
            },
        },
    }


def build_task(blueprint: dict[str, Any], root: Path) -> dict[str, Any]:
    template = blueprint["task_template"]
    criteria = blueprint["acceptance_criteria"]
    commands = [item["command"] for item in blueprint["verifiers"]]
    verification_contracts = [
        {"id": item["id"], "covers": item["covers"], "command": item["command"], "timeout_seconds": int(item.get("timeout_seconds", 60))}
        for item in blueprint["verifiers"]
    ]
    mutation = blueprint["effect"] == "mutation"
    stage_capabilities = compiled_stage_capabilities(blueprint)
    max_repairs = int(template.get("max_repairs", 1))
    task = {
        "task_id": template["task_id"],
        "objective": template["objective"],
        "objective_contract": blueprint["objective_contract"],
        "lifecycle": blueprint["lifecycle"],
        "workdir": str((root / "examples" / "workspace").resolve()),
        "inputs": template["inputs"],
        "constraints": template.get("constraints", []),
        "acceptance_criteria": criteria,
        "allowed_tools": list((stage_capabilities.get("execute") or {}).get("tools", ["read", "edit", "write"] if mutation else ["read", "grep", "find", "ls"])),
        "allowed_write_paths": template.get("allowed_write_paths", []),
        "immutable_paths": template.get("immutable_paths", []),
        "execution_commands": [],
        "allow_bash": "bash" in (stage_capabilities.get("execute") or {}).get("tools", []),
        "clean_allowed_write_paths": mutation,
        "context_policy": blueprint["context_policy"],
        "step_validation": {"enabled": False},
        "verification_commands": commands,
        "verification_contracts": verification_contracts,
        "verification_timeout_seconds": max([int(item.get("timeout_seconds", 60)) for item in blueprint["verifiers"]] or [60]),
        "max_repairs": max_repairs,
    }
    if template.get("required_steps"):
        task["required_steps"] = template["required_steps"]
    return task


def compile_blueprint(blueprint_path: Path, repo: Path, force: bool = False) -> Path:
    blueprint = assert_blueprint(read_json(blueprint_path))
    workflow_root = repo / ".codex" / "workflows" / blueprint["workflow"]
    root = workflow_root / "versions" / blueprint["version"]
    if root.exists() and any(root.iterdir()) and not force:
        raise SystemExit(f"refusing to overwrite immutable workflow version {root}; increment version or use --force")
    root.mkdir(parents=True, exist_ok=True)
    task = build_task(blueprint, root)
    control_flow = None
    if blueprint.get("control_flow"):
        control_flow = {"schema_version": "1.0", "workflow": blueprint["workflow"], "stages": blueprint["lifecycle"], **blueprint["control_flow"]}
    peer_contract = compiled_peer_contract(blueprint)
    certification_contract = blueprint.get("certification_contract")
    harness = {
        "workflow": blueprint["workflow"],
        "workflow_version": blueprint["version"],
        "factory_version": FACTORY_VERSION,
        "blueprint_digest": canonical_digest(blueprint),
        "effect": blueprint["effect"],
        "runtime": blueprint["runtime"],
        "models": compiled_model_roles(blueprint),
        "stage_capabilities": compiled_stage_capabilities(blueprint),
        "thinking": "low",
        "max_repairs": task["max_repairs"],
        "pi_timeout_seconds": 600,
        "pi_compatibility": PI_COMPATIBILITY,
        "control_flow": {
            "enabled": control_flow is not None,
            "graph_digest": canonical_digest(control_flow) if control_flow else None,
            "engine_digest": canonical_digest(TRANSITION_ENGINE.read_text()) if control_flow else None,
            "engine": "scripts/evaluate_transition.py" if control_flow else None,
        },
        "peer_collaboration": {
            "enabled": peer_contract is not None,
            "contract": "peer-collaboration.json" if peer_contract else None,
            "contract_digest": canonical_digest(peer_contract) if peer_contract else None,
            "validator": "scripts/validate_peer_exchange.py" if peer_contract else None,
            "validator_digest": canonical_digest(PEER_EXCHANGE_VALIDATOR.read_text()) if peer_contract else None,
            "request_schema": "schemas/peer-request.schema.json" if peer_contract else None,
            "request_schema_digest": canonical_digest(PEER_REQUEST_SCHEMA.read_text()) if peer_contract else None,
            "response_schema": "schemas/peer-response.schema.json" if peer_contract else None,
            "response_schema_digest": canonical_digest(PEER_RESPONSE_SCHEMA.read_text()) if peer_contract else None,
        },
        "certification_contract": {
            "enabled": certification_contract is not None,
            "path": "certification-contract.json" if certification_contract else None,
            "digest": canonical_digest(certification_contract) if certification_contract else None,
            "evaluator": "scripts/evaluate_certification.py" if certification_contract else None,
            "evaluator_digest": canonical_digest(CERTIFICATION_EVALUATOR.read_text()) if certification_contract else None,
            "contract_schema": "schemas/certification-contract.schema.json" if certification_contract else None,
            "contract_schema_digest": canonical_digest(certification_contract_schema()) if certification_contract else None,
            "decision_schema": "schemas/certification-decision.schema.json" if certification_contract else None,
            "decision_schema_digest": canonical_digest(CERTIFICATION_DECISION_SCHEMA.read_text()) if certification_contract else None,
            "corpus_schema": "schemas/certification-corpus.schema.json" if certification_contract else None,
            "corpus_schema_digest": canonical_digest(CERTIFICATION_CORPUS_SCHEMA.read_text()) if certification_contract else None,
            "rubric_schema": "schemas/certification-rubric.schema.json" if certification_contract else None,
            "rubric_schema_digest": canonical_digest(CERTIFICATION_RUBRIC_SCHEMA.read_text()) if certification_contract else None,
            "result_schema": "schemas/certification-result.schema.json" if certification_contract else None,
            "result_schema_digest": canonical_digest(CERTIFICATION_RESULT_SCHEMA.read_text()) if certification_contract else None,
        },
    }
    certification_profile = {
        "schema_version": "1.2",
        "required_static_gates": [
            "blueprint_contract", "bundle_structure", "objective_contract", "lifecycle", "model_pinning",
            "stage_capabilities", "verifier_coverage", "criterion_provenance", "path_policy", "context_policy", "operations_policy",
            "runtime_readiness", "runner_contract", "compiled_integrity",
            "control_flow_contract", "control_flow_compilation", "transition_engine_integrity",
            "peer_collaboration_contract", "peer_collaboration_compilation", "peer_exchange_validator_integrity",
            "certification_contract", "certification_artifacts", "certification_evaluator_integrity",
            "certification_contract_schema_integrity", "certification_decision_schema_integrity",
        ],
        "required_dynamic_gates": (
            ["certification_contract_evaluation"] if certification_contract else []
        ) + (
            ["approval_negative", "positive_end_to_end", "missing_input", "context_overflow", "seal_tamper"]
            if blueprint["effect"] == "mutation" and blueprint["runtime"]["kind"] == "generic_mutation"
            else ["specialized_fixture"]
        ),
        "domain_gates": [
            "selector_contamination", "hash_mismatch", "malformed_output_repair", "fabricated_evidence",
            "interruption_resume", "concurrent_isolation", "visible_aggregate",
        ],
    }
    operations = blueprint["operations"]
    operations_md = f"""# Operating {blueprint['workflow']} {blueprint['version']}

Owner: {operations['owner']}

- Effect: `{blueprint['effect']}`
- Runtime: `{blueprint['runtime']['kind']}`
- Concurrency: `{operations['concurrency']}`
- Retention: {operations['retention_days']} days
- Failure response: {operations['failure_response']}
- Redaction patterns: {', '.join(operations['redact_patterns']) or 'none declared'}
- Peer collaboration: {'enabled for ' + ', '.join(peer_contract['stages']) if peer_contract else 'disabled'}

This version is immutable after promotion. Run certification before promotion.
The factory creates a per-version ledger and tracker beneath this directory.
"""
    workflow_md = f"""# {blueprint['workflow']} {blueprint['version']}

{blueprint['description']}

## Objective contract

- Selector: {blueprint['objective_contract']['selector']}
- Target: {blueprint['objective_contract']['target']}
- Decision: {blueprint['objective_contract']['decision']}
- Non-goals: {', '.join(blueprint['objective_contract']['non_goals'])}

## Lifecycle

{' -> '.join(blueprint['lifecycle'])}

## Peer collaboration

{'Bounded peer collaboration is enabled for: ' + ', '.join(peer_contract['stages']) + '. The specialized controller remains authoritative for budgets, receipts, cancellation, and verification.' if peer_contract else 'Disabled.'}

## Certification

```bash
python3 ~/.agents/skills/pi-workflows/scripts/certify_workflow.py --harness {root}
```

{'Contract evaluation (independent result evidence plus explicit operator decision):' if certification_contract else ''}
{'```bash' if certification_contract else ''}
{f'python3 scripts/evaluate_certification.py --harness . --results /path/to/results.json --operator-decision /path/to/operator-decision.json' if certification_contract else ''}
{'```' if certification_contract else ''}
"""
    atomic_write(root / "workflow.blueprint.json", json.dumps(blueprint, indent=2) + "\n")
    atomic_write(root / "harness.json", json.dumps(harness, indent=2) + "\n")
    atomic_write(root / "schemas" / "task.schema.json", json.dumps(task_schema(), indent=2) + "\n")
    atomic_write(root / "examples" / "task.json", json.dumps(task, indent=2) + "\n")
    atomic_write(root / "certification-profile.json", json.dumps(certification_profile, indent=2) + "\n")
    atomic_write(root / "workflow.md", workflow_md)
    atomic_write(root / "OPERATIONS.md", operations_md)
    if control_flow:
        atomic_write(root / "control-flow.json", json.dumps(control_flow, indent=2) + "\n")
        atomic_write(root / "scripts" / "evaluate_transition.py", TRANSITION_ENGINE.read_text())
        os.chmod(root / "scripts" / "evaluate_transition.py", 0o755)
    if peer_contract:
        atomic_write(root / "peer-collaboration.json", json.dumps(peer_contract, indent=2) + "\n")
        atomic_write(root / "scripts" / "validate_peer_exchange.py", PEER_EXCHANGE_VALIDATOR.read_text())
        os.chmod(root / "scripts" / "validate_peer_exchange.py", 0o755)
        atomic_write(root / "schemas" / "peer-request.schema.json", PEER_REQUEST_SCHEMA.read_text())
        atomic_write(root / "schemas" / "peer-response.schema.json", PEER_RESPONSE_SCHEMA.read_text())
    if certification_contract:
        atomic_write(root / "certification-contract.json", json.dumps(certification_contract, indent=2) + "\n")
        atomic_write(root / "schemas" / "certification-contract.schema.json", json.dumps(certification_contract_schema(), indent=2) + "\n")
        atomic_write(root / "schemas" / "certification-decision.schema.json", CERTIFICATION_DECISION_SCHEMA.read_text())
        atomic_write(root / "scripts" / "evaluate_certification.py", CERTIFICATION_EVALUATOR.read_text())
        os.chmod(root / "scripts" / "evaluate_certification.py", 0o755)
        atomic_write(root / "schemas" / "certification-corpus.schema.json", CERTIFICATION_CORPUS_SCHEMA.read_text())
        atomic_write(root / "schemas" / "certification-rubric.schema.json", CERTIFICATION_RUBRIC_SCHEMA.read_text())
        atomic_write(root / "schemas" / "certification-result.schema.json", CERTIFICATION_RESULT_SCHEMA.read_text())
    for relative, content in blueprint["task_template"]["fixture_files"].items():
        safe_relative(relative, "fixture_files")
        atomic_write(root / "examples" / "workspace" / relative, content)
    atomic_write(workflow_root / "factory-index.json", json.dumps({
        "workflow": blueprint["workflow"],
        "versions_root": str((workflow_root / "versions").resolve()),
        "latest_compiled": blueprint["version"],
        "latest_compiled_path": str(root.resolve()),
    }, indent=2) + "\n")
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blueprint", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args()
    blueprint_path = Path(args.blueprint).expanduser().resolve()
    blueprint = assert_blueprint(read_json(blueprint_path))
    plan_sha256 = canonical_digest(blueprint)
    if args.approved_plan_sha256 != plan_sha256:
        print(json.dumps({
            "status": "awaiting_approval",
            "workflow": blueprint["workflow"],
            "version": blueprint["version"],
            "stages": blueprint["lifecycle"],
            "plan_sha256": plan_sha256,
        }, indent=2))
        return 3
    output = compile_blueprint(blueprint_path, Path(args.repo).expanduser().resolve(), args.force)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
