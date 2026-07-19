#!/usr/bin/env python3
"""Execute identical replay cases against two certified workflow versions."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parent.parent
RUNNER = SKILL_ROOT / "scripts" / "run_pi_harness.py"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def cost_from_events(run_dir: Path) -> float | None:
    totals: list[float] = []
    saw_usage = False
    for path in sorted((run_dir / "events").glob("pi-*.jsonl")):
        final_by_response: dict[str, float] = {}
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "message_end":
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            cost = ((message.get("usage") or {}).get("cost") or {}).get("total")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                saw_usage = True
                response = message.get("responseId") or f"{path.name}:{len(final_by_response)}"
                final_by_response[str(response)] = float(cost)
        totals.extend(final_by_response.values())
    return round(sum(totals), 8) if saw_usage else None


def run_side(harness: Path, spec: Path, case_id: str, side: str) -> dict[str, Any]:
    task = read_json(spec)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    gate_id = f"replay-{case_id}-{side}-{stamp}-gate"
    positive_id = f"replay-{case_id}-{side}-{stamp}-positive"
    base = ["python3", str(RUNNER), "--harness", str(harness), "--spec", str(spec)]
    started = time.monotonic()
    gate = subprocess.run(base + ["--run-id", gate_id], cwd=harness, text=True, capture_output=True, check=False)
    gate_run = harness / "runs" / task["task_id"] / gate_id
    if gate.returncode != 3 or not (gate_run / "stages" / "plan.json").is_file():
        return {"passed": False, "judge_score": None, "duration_seconds": round(time.monotonic() - started, 3), "cost_usd": cost_from_events(gate_run), "repair_attempts": None, "failure": "approval_gate_failed"}
    positive = subprocess.run(
        base + ["--run-id", positive_id, "--approve-execution", "--approved-plan-artifact", str(gate_run / "stages" / "plan.json")],
        cwd=harness, text=True, capture_output=True, check=False,
    )
    run_dir = harness / "runs" / task["task_id"] / positive_id
    validation_path = run_dir / "validation" / "final_validation.json"
    validation = read_json(validation_path) if validation_path.is_file() else {}
    return {
        "passed": positive.returncode == 0 and validation.get("status") == "passed",
        "judge_score": validation.get("judge_score"),
        "duration_seconds": round(time.monotonic() - started, 3),
        "cost_usd": cost_from_events(gate_run) + cost_from_events(run_dir) if cost_from_events(gate_run) is not None and cost_from_events(run_dir) is not None else None,
        "repair_attempts": validation.get("repair_attempts", 0),
        "run_dir": str(run_dir),
        "failure": None if positive.returncode == 0 else (positive.stderr[-1000:] or f"exit {positive.returncode}"),
    }


def resolve_spec(harness: Path, value: str) -> Path:
    path = Path(value)
    return path.expanduser().resolve() if path.is_absolute() else (harness / path).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--corpus", required=True, help="JSON with case_id and baseline_spec/candidate_spec paths")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    baseline = Path(args.baseline).expanduser().resolve()
    candidate = Path(args.candidate).expanduser().resolve()
    baseline_cert = read_json(baseline / "certification" / "latest.json")
    candidate_cert = read_json(candidate / "certification" / "latest.json")
    if baseline_cert.get("status") != "fixture_certified" or candidate_cert.get("status") != "fixture_certified":
        raise SystemExit("both workflow versions must be fixture_certified before replay")
    baseline_blueprint = read_json(baseline / "workflow.blueprint.json")
    candidate_blueprint = read_json(candidate / "workflow.blueprint.json")
    if baseline_blueprint.get("workflow") != candidate_blueprint.get("workflow"):
        raise SystemExit("baseline and candidate must belong to the same workflow")
    corpus = read_json(Path(args.corpus).expanduser().resolve())
    cases = corpus.get("cases", []) if isinstance(corpus, dict) else []
    if not cases:
        raise SystemExit("replay corpus must contain cases")
    results = []
    for item in cases:
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or not case_id or not all(char.isalnum() or char in "-_" for char in case_id):
            raise SystemExit(f"unsafe replay case_id: {case_id!r}")
        baseline_spec = resolve_spec(baseline, item.get("baseline_spec", "examples/task.json"))
        candidate_spec = resolve_spec(candidate, item.get("candidate_spec", "examples/task.json"))
        results.append({
            "case_id": case_id,
            "baseline": run_side(baseline, baseline_spec, case_id, "baseline"),
            "candidate": run_side(candidate, candidate_spec, case_id, "candidate"),
        })
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema_version": "1.0", "created_at": datetime.now(timezone.utc).isoformat(), "cases": results}, indent=2) + "\n")
    print(output)
    return 0 if all(item["baseline"]["passed"] and item["candidate"]["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
