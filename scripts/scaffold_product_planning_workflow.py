#!/usr/bin/env python3
"""Compile and install the versioned product-planning specialized runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from compile_workflow import compile_blueprint
from certify_workflow import static_gates


SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL_ROOT / "templates" / "product-planning"
BLUEPRINT = SKILL_ROOT / "examples" / "product-planning-blueprint.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_runtime(harness: Path) -> None:
    copies = {
        TEMPLATE / "run.py": harness / "scripts" / "run.py",
        TEMPLATE / "planning_contract.py": harness / "scripts" / "planning_contract.py",
        TEMPLATE / "competitor_research.py": harness / "scripts" / "competitor_research.py",
        TEMPLATE / "render_brand_deck.mjs": harness / "scripts" / "render_brand_deck.mjs",
        TEMPLATE / "certify.py": harness / "scripts" / "certify.py",
        TEMPLATE / "judge-spec.json": harness / "judge-spec.json",
        TEMPLATE / "copy-judge-spec.json": harness / "copy-judge-spec.json",
        TEMPLATE / "prompt-spec.json": harness / "prompt-spec.json",
        TEMPLATE / "stack-policy.json": harness / "stack-policy.json",
        TEMPLATE / "idea.schema.json": harness / "schemas" / "idea.schema.json",
        harness / "examples" / "workspace" / "idea.json": harness / "examples" / "idea.json",
    }
    for source, destination in copies.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for path in (harness / "scripts" / "run.py", harness / "scripts" / "render_brand_deck.mjs", harness / "scripts" / "certify.py"):
        os.chmod(path, 0o755)
    resources = [
        "scripts/run.py", "scripts/planning_contract.py", "scripts/competitor_research.py", "scripts/render_brand_deck.mjs", "scripts/certify.py",
        "judge-spec.json", "copy-judge-spec.json", "prompt-spec.json", "stack-policy.json", "schemas/idea.schema.json",
        "workflow.blueprint.json", "examples/task.json", "workflow.md", "OPERATIONS.md",
    ]
    manifest = {"schema_version": "1.0", "files": [{"path": item, "sha256": sha256(harness / item)} for item in resources]}
    (harness / "resources.json").write_text(json.dumps(manifest, indent=2) + "\n")
    config_path = harness / "harness.json"
    config = json.loads(config_path.read_text())
    config["planning_runtime"] = {
        "resources": "resources.json", "resources_digest": hashlib.sha256((harness / "resources.json").read_bytes()).hexdigest(),
        "judge_spec": "judge-spec.json", "judge_status": "candidate", "default_target": 9.0,
        "copy_judge_spec": "copy-judge-spec.json", "copy_judge_status": "candidate",
        "prompt_spec": "prompt-spec.json", "prompt_version": "1.6.0",
        "judge_policy": "milestone",
        "judge_policies": ["final-only", "milestone", "all-high"],
        "model_routing": {
            "generation": "openai-codex/gpt-5.6-sol@medium",
            "high_volume_scoring": "openai-codex/gpt-5.6-terra@medium",
            "milestone_scoring": "openai-codex/gpt-5.6-terra@high",
            "coding_handoff": "openai-codex/gpt-5.6-sol@high",
            "fast_test": "openai-codex/gpt-5.6-luna@low",
        },
        "parallelism": {"default": 4, "maximum": 8}, "max_pages_default": 24,
        "max_model_calls_default": 80, "max_wall_seconds_default": 600,
        "competitor_research": {"provider": "Exa", "mode": "auto", "max_competitors": 6, "max_pages_per_competitor": 3, "max_characters_per_page": 8000, "freshness_hours": 24},
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repository that will receive .codex/workflows")
    parser.add_argument("--workflow", default="product-planning")
    parser.add_argument("--version", default="2.1.1")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    blueprint = json.loads(BLUEPRINT.read_text())
    blueprint["workflow"] = args.workflow
    blueprint["version"] = args.version
    with tempfile.TemporaryDirectory(prefix="product-planning-blueprint-") as temporary:
        path = Path(temporary) / "blueprint.json"
        path.write_text(json.dumps(blueprint, indent=2) + "\n")
        harness = compile_blueprint(path, Path(args.repo).expanduser().resolve(), args.force)
    install_runtime(harness)
    gates, _, _ = static_gates(harness)
    certification = {"schema_version": "1.0", "status": "passed" if all(item["status"] == "passed" for item in gates) else "failed", "gates": gates}
    (harness / "static-certification.json").write_text(json.dumps(certification, indent=2) + "\n")
    if certification["status"] != "passed":
        raise SystemExit("compiled planning harness failed static certification")
    print(harness)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
