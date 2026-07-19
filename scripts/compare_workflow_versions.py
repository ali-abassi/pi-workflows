#!/usr/bin/env python3
"""Compare certified workflow versions on an identical replay corpus and optionally promote."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow_factory_common import canonical_digest, read_json


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content)
    os.replace(temporary, path)


def metric(side: dict[str, Any], key: str) -> float | None:
    value = side.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def summarize(cases: list[dict[str, Any]], side: str) -> dict[str, Any]:
    records = [item.get(side, {}) for item in cases]
    passed = [record for record in records if record.get("passed") is True]
    judge = [metric(record, "judge_score") for record in records]
    judge = [value for value in judge if value is not None]
    latency = [metric(record, "duration_seconds") for record in records]
    latency = [value for value in latency if value is not None]
    cost = [metric(record, "cost_usd") for record in records]
    cost = [value for value in cost if value is not None]
    repairs = [metric(record, "repair_attempts") for record in records]
    repairs = [value for value in repairs if value is not None]
    return {
        "cases": len(records), "passed": len(passed), "pass_rate": len(passed) / len(records) if records else 0,
        "average_judge_score": sum(judge) / len(judge) if judge else None,
        "average_duration_seconds": sum(latency) / len(latency) if latency else None,
        "total_cost_usd": sum(cost) if len(cost) == len(records) and records else None,
        "average_repairs": sum(repairs) / len(repairs) if repairs else None,
    }


def relative_increase(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None:
        return None
    if baseline == 0:
        return 0 if candidate == 0 else float("inf")
    return (candidate - baseline) / baseline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Baseline compiled harness version directory")
    parser.add_argument("--candidate", required=True, help="Candidate compiled harness version directory")
    parser.add_argument("--replay-results", required=True, help="JSON cases evaluated against both versions")
    parser.add_argument("--max-cost-increase", type=float, default=0.10)
    parser.add_argument("--max-latency-increase", type=float, default=0.20)
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()
    baseline = Path(args.baseline).expanduser().resolve()
    candidate = Path(args.candidate).expanduser().resolve()
    baseline_blueprint = read_json(baseline / "workflow.blueprint.json")
    candidate_blueprint = read_json(candidate / "workflow.blueprint.json")
    baseline_cert = read_json(baseline / "certification" / "latest.json")
    candidate_cert = read_json(candidate / "certification" / "latest.json")
    replay = read_json(Path(args.replay_results).expanduser().resolve())
    cases = replay.get("cases", []) if isinstance(replay, dict) else []
    gates: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        gates.append({"name": name, "status": "passed" if passed else "failed", "detail": detail})

    same_workflow = baseline_blueprint.get("workflow") == candidate_blueprint.get("workflow")
    add("same_workflow", same_workflow, "versions belong to the same workflow" if same_workflow else "workflow identities differ")
    add("baseline_certified", baseline_cert.get("status") == "fixture_certified", f"baseline status: {baseline_cert.get('status')}")
    add("candidate_certified", candidate_cert.get("status") == "fixture_certified", f"candidate status: {candidate_cert.get('status')}")
    valid_cases = bool(cases) and all(isinstance(item, dict) and item.get("case_id") and isinstance(item.get("baseline"), dict) and isinstance(item.get("candidate"), dict) for item in cases)
    add("paired_replay", valid_cases, f"{len(cases)} paired replay cases" if valid_cases else "replay corpus missing or malformed")
    baseline_summary = summarize(cases, "baseline") if valid_cases else summarize([], "baseline")
    candidate_summary = summarize(cases, "candidate") if valid_cases else summarize([], "candidate")
    add("no_correctness_regression", candidate_summary["pass_rate"] >= baseline_summary["pass_rate"] and candidate_summary["passed"] >= baseline_summary["passed"], f"pass rate {baseline_summary['pass_rate']:.3f} -> {candidate_summary['pass_rate']:.3f}")
    regressed_cases = [item["case_id"] for item in cases if item["baseline"].get("passed") is True and item["candidate"].get("passed") is not True] if valid_cases else []
    add("no_case_regression", not regressed_cases, "no previously passing case regressed" if not regressed_cases else f"regressed: {', '.join(regressed_cases)}")
    baseline_judge = baseline_summary["average_judge_score"]
    candidate_judge = candidate_summary["average_judge_score"]
    judge_ok = baseline_judge is None or (candidate_judge is not None and candidate_judge >= baseline_judge)
    add("judge_non_regression", judge_ok, f"judge average {baseline_judge} -> {candidate_judge}")
    cost_delta = relative_increase(baseline_summary["total_cost_usd"], candidate_summary["total_cost_usd"])
    cost_ok = cost_delta is not None and cost_delta <= args.max_cost_increase
    add("cost_budget", cost_ok, "complete cost data required" if cost_delta is None else f"cost change {cost_delta:.1%}; limit {args.max_cost_increase:.1%}")
    latency_delta = relative_increase(baseline_summary["average_duration_seconds"], candidate_summary["average_duration_seconds"])
    latency_ok = latency_delta is not None and latency_delta <= args.max_latency_increase
    add("latency_budget", latency_ok, "complete latency data required" if latency_delta is None else f"latency change {latency_delta:.1%}; limit {args.max_latency_increase:.1%}")
    required = {item.get("id") for item in baseline_blueprint.get("acceptance_criteria", [])}
    candidate_criteria = {item.get("id") for item in candidate_blueprint.get("acceptance_criteria", [])}
    add("criteria_preserved", required.issubset(candidate_criteria), "candidate preserves baseline acceptance criteria" if required.issubset(candidate_criteria) else "candidate removed acceptance criteria")
    eligible = all(item["status"] == "passed" for item in gates)
    report = {
        "schema_version": "1.0", "workflow": candidate_blueprint.get("workflow"),
        "baseline_version": baseline_blueprint.get("version"), "candidate_version": candidate_blueprint.get("version"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "recommendation": "promote" if eligible else "reject",
        "decision": "eligible_not_promoted" if eligible else "rejected",
        "eligible_for_promotion": eligible,
        "promotion_executed": False,
        "gates": gates,
        "baseline": baseline_summary, "candidate": candidate_summary, "replay_digest": canonical_digest(replay),
    }
    output = candidate / "optimization" / f"compare-{baseline_blueprint.get('version')}-to-{candidate_blueprint.get('version')}.json"
    atomic_write(output, json.dumps(report, indent=2) + "\n")
    atomic_write(candidate / "optimization" / "latest.json", json.dumps(report, indent=2) + "\n")
    if args.promote:
        if not eligible:
            print(output)
            raise SystemExit("promotion refused: optimization gates failed")
        workflow_root = candidate.parent.parent
        active = {
            "workflow": candidate_blueprint["workflow"], "version": candidate_blueprint["version"],
            "path": str(candidate), "promoted_at": datetime.now(timezone.utc).isoformat(),
            "certification_digest": canonical_digest(candidate_cert), "optimization_digest": canonical_digest(report),
        }
        atomic_write(workflow_root / "active.json", json.dumps(active, indent=2) + "\n")
        report["decision"] = "promoted"
        report["promotion_executed"] = True
        atomic_write(output, json.dumps(report, indent=2) + "\n")
        atomic_write(candidate / "optimization" / "latest.json", json.dumps(report, indent=2) + "\n")
    print(output)
    return 0 if eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())
