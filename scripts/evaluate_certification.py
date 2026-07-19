#!/usr/bin/env python3
"""Evaluate independently produced workflow results against a compiled certification contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIMENSIONS = ("truth", "eval_quality", "cost_efficiency", "integration", "safety")
CASE_CLASSES = {"positive", "adversarial", "regression", "integration", "safety"}
HEX = set("0123456789abcdef")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def exact_keys(value: dict[str, Any], required: set[str], optional: set[str], label: str) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        raise ValueError(f"{label} keys invalid; missing={sorted(missing)} extra={sorted(extra)}")


def nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def number(value: Any, label: str, minimum: float = 0.0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{label} outside [{minimum}, {maximum}]")
    return result


def digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in HEX for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def safe_artifact(base: Path, relative: Any, label: str) -> Path:
    text = nonempty_text(relative, label)
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"{label} must be a safe relative path")
    path = (base / candidate).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as error:
        raise ValueError(f"{label} escapes the result directory") from error
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {text}")
    return path


def validate_corpus(corpus: dict[str, Any], contract: dict[str, Any], workflow: str) -> dict[str, dict[str, Any]]:
    exact_keys(corpus, {"schema_version", "workflow", "cases"}, set(), "corpus")
    if corpus["schema_version"] != "1.0" or corpus["workflow"] != workflow:
        raise ValueError("corpus identity does not match the compiled workflow")
    cases = corpus["cases"]
    minimum = contract["corpus"]["minimum_cases"]
    if not isinstance(cases, list) or len(cases) < minimum:
        raise ValueError(f"corpus requires at least {minimum} cases")
    indexed: dict[str, dict[str, Any]] = {}
    classes: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"corpus case {index} must be an object")
        exact_keys(case, {"id", "class", "input", "expected", "evidence_requirements"}, set(), f"corpus case {index}")
        case_id = nonempty_text(case["id"], f"corpus case {index}.id")
        case_class = case["class"]
        if case_class not in CASE_CLASSES:
            raise ValueError(f"corpus case {case_id} has invalid class")
        requirements = case["evidence_requirements"]
        if not isinstance(requirements, list) or not requirements or any(not isinstance(item, str) or not item for item in requirements):
            raise ValueError(f"corpus case {case_id} evidence_requirements must be non-empty strings")
        if len(requirements) != len(set(requirements)) or case_id in indexed:
            raise ValueError(f"corpus case {case_id} contains duplicate identifiers")
        indexed[case_id] = case
        classes.add(case_class)
    required_classes = set(contract["corpus"]["required_classes"])
    if not required_classes <= classes:
        raise ValueError(f"corpus missing required classes: {sorted(required_classes - classes)}")
    return indexed


def validate_rubrics(harness: Path, contract: dict[str, Any], acceptance_ids: set[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    rubrics: dict[str, dict[str, Any]] = {}
    digests: dict[str, str] = {}
    criterion_dimensions: dict[str, str] = {}
    for judge in contract["judges"]:
        judge_id = judge["id"]
        path = harness / "examples" / "workspace" / judge["rubric_path"]
        rubric = read_json(path)
        exact_keys(rubric, {"schema_version", "judge_id", "instructions", "criteria"}, set(), f"rubric {judge_id}")
        if rubric["schema_version"] != "1.1" or rubric["judge_id"] != judge_id:
            raise ValueError(f"rubric {judge_id} identity mismatch")
        nonempty_text(rubric["instructions"], f"rubric {judge_id}.instructions")
        criteria = rubric["criteria"]
        if not isinstance(criteria, list) or not criteria:
            raise ValueError(f"rubric {judge_id} has no criteria")
        seen: set[str] = set()
        for criterion in criteria:
            if not isinstance(criterion, dict):
                raise ValueError(f"rubric {judge_id} criterion must be an object")
            exact_keys(criterion, {"id", "dimension", "description", "weight", "anchors"}, set(), f"rubric {judge_id} criterion")
            criterion_id = nonempty_text(criterion["id"], f"rubric {judge_id} criterion.id")
            if criterion_id in seen or criterion_id in criterion_dimensions:
                raise ValueError(f"criterion id must be globally unique: {criterion_id}")
            dimension = criterion["dimension"]
            if dimension not in DIMENSIONS:
                raise ValueError(f"criterion {criterion_id} has invalid dimension")
            number(criterion["weight"], f"criterion {criterion_id}.weight", minimum=0.000001)
            anchors = criterion["anchors"]
            if not isinstance(anchors, dict) or set(anchors) != {"0", "5", "10"} or any(not isinstance(value, str) or not value for value in anchors.values()):
                raise ValueError(f"criterion {criterion_id} requires exact 0/5/10 anchors")
            seen.add(criterion_id)
            criterion_dimensions[criterion_id] = dimension
        rubrics[judge_id] = rubric
        digests[judge_id] = canonical_digest(rubric)
    declared_dimensions = contract["dimensions"]
    dimension_references: list[str] = []
    for dimension in DIMENSIONS:
        declared = declared_dimensions[dimension]
        if not isinstance(declared, list) or not declared or len(declared) != len(set(declared)):
            raise ValueError(f"contract dimension {dimension} must contain unique acceptance criterion ids")
        dimension_references.extend(declared)
    if set(dimension_references) != acceptance_ids or len(dimension_references) != len(set(dimension_references)):
        raise ValueError("contract dimensions must map every acceptance criterion exactly once")
    return rubrics, digests


def validate_results(
    results_path: Path,
    results: dict[str, Any],
    workflow: str,
    version: str,
    contract: dict[str, Any],
    contract_digest: str,
    corpus: dict[str, Any],
    corpus_cases: dict[str, dict[str, Any]],
    rubrics: dict[str, dict[str, Any]],
    rubric_digests: dict[str, str],
) -> dict[str, Any]:
    exact_keys(results, {"schema_version", "workflow", "version", "contract_digest", "corpus_digest", "rubric_digests", "validator", "cases"}, set(), "results")
    if results["schema_version"] != "1.2" or results["workflow"] != workflow or results["version"] != version:
        raise ValueError("result identity does not match the evaluated workflow version")
    if digest(results["contract_digest"], "results.contract_digest") != contract_digest:
        raise ValueError("results were not produced against this certification contract")
    if digest(results["corpus_digest"], "results.corpus_digest") != canonical_digest(corpus):
        raise ValueError("results were not produced against this corpus")
    if results["rubric_digests"] != rubric_digests:
        raise ValueError("results rubric digests do not match the compiled rubrics")
    validator = results["validator"]
    if not isinstance(validator, dict):
        raise ValueError("results.validator must be an object")
    exact_keys(validator, {"independent", "provider", "model", "run_id", "executed_at"}, set(), "results.validator")
    if validator["independent"] is not True:
        raise ValueError("validation must be independently produced")
    for field in ("provider", "model", "run_id", "executed_at"):
        nonempty_text(validator[field], f"results.validator.{field}")

    records = results["cases"]
    if not isinstance(records, list):
        raise ValueError("results.cases must be an array")
    indexed: dict[str, dict[str, Any]] = {}
    base = results_path.parent
    declared_gates = set(contract["deterministic_gates"])
    declared_judges = {judge["id"]: judge for judge in contract["judges"]}
    dimension_totals = {dimension: [0.0, 0.0] for dimension in DIMENSIONS}
    passed = 0
    deterministic_failures: list[str] = []
    judge_failures: list[str] = []

    for record in records:
        if not isinstance(record, dict):
            raise ValueError("result case must be an object")
        exact_keys(record, {"id", "deterministic_gates", "judge_results", "cost", "latency_ms", "evidence"}, set(), "result case")
        case_id = nonempty_text(record["id"], "result case.id")
        if case_id in indexed or case_id not in corpus_cases:
            raise ValueError(f"result case is duplicate or unknown: {case_id}")
        gates = record["deterministic_gates"]
        if not isinstance(gates, dict) or set(gates) != declared_gates or any(type(value) is not bool for value in gates.values()):
            raise ValueError(f"case {case_id} must report every declared deterministic gate exactly once")
        judges = record["judge_results"]
        if not isinstance(judges, dict) or set(judges) != set(declared_judges):
            raise ValueError(f"case {case_id} must report every declared judge exactly once")
        evidence = record["evidence"]
        if not isinstance(evidence, dict):
            raise ValueError(f"case {case_id}.evidence must be an object")
        required_evidence = set(corpus_cases[case_id]["evidence_requirements"])
        if not required_evidence <= set(evidence):
            raise ValueError(f"case {case_id} missing evidence: {sorted(required_evidence - set(evidence))}")
        for evidence_id, artifact in evidence.items():
            if not isinstance(artifact, dict):
                raise ValueError(f"case {case_id} evidence {evidence_id} must be an artifact reference")
            exact_keys(artifact, {"path", "sha256"}, set(), f"case {case_id} evidence {evidence_id}")
            path = safe_artifact(base, artifact["path"], f"case {case_id} evidence {evidence_id}.path")
            if file_digest(path) != digest(artifact["sha256"], f"case {case_id} evidence {evidence_id}.sha256"):
                raise ValueError(f"case {case_id} evidence {evidence_id} digest mismatch")

        case_judges_pass = True
        for judge_id, judge_result in judges.items():
            if not isinstance(judge_result, dict):
                raise ValueError(f"case {case_id} judge {judge_id} must be an object")
            exact_keys(judge_result, {"score", "criterion_scores", "rubric_digest", "artifact_path", "artifact_digest", "evidence"}, set(), f"case {case_id} judge {judge_id}")
            if digest(judge_result["rubric_digest"], f"case {case_id} judge {judge_id}.rubric_digest") != rubric_digests[judge_id]:
                raise ValueError(f"case {case_id} judge {judge_id} rubric digest mismatch")
            artifact = safe_artifact(base, judge_result["artifact_path"], f"case {case_id} judge {judge_id}.artifact_path")
            if file_digest(artifact) != digest(judge_result["artifact_digest"], f"case {case_id} judge {judge_id}.artifact_digest"):
                raise ValueError(f"case {case_id} judge {judge_id} artifact digest mismatch")
            judge_evidence = judge_result["evidence"]
            required_fields = set(declared_judges[judge_id]["evidence_fields"])
            if not isinstance(judge_evidence, dict) or not required_fields <= set(judge_evidence):
                raise ValueError(f"case {case_id} judge {judge_id} missing declared evidence fields")
            criteria = rubrics[judge_id]["criteria"]
            criterion_scores = judge_result["criterion_scores"]
            expected_criteria = {criterion["id"] for criterion in criteria}
            if not isinstance(criterion_scores, dict) or set(criterion_scores) != expected_criteria:
                raise ValueError(f"case {case_id} judge {judge_id} must score every rubric criterion exactly once")
            weighted_score = 0.0
            total_weight = 0.0
            for criterion in criteria:
                criterion_id = criterion["id"]
                criterion_score = number(criterion_scores[criterion_id], f"case {case_id} criterion {criterion_id}", maximum=10)
                weight = float(criterion["weight"])
                weighted_score += criterion_score * weight
                total_weight += weight
                dimension_totals[criterion["dimension"]][0] += criterion_score * weight
                dimension_totals[criterion["dimension"]][1] += weight
            reported_score = number(judge_result["score"], f"case {case_id} judge {judge_id}.score", maximum=10)
            calculated_score = weighted_score / total_weight
            if abs(reported_score - calculated_score) > 0.01:
                raise ValueError(f"case {case_id} judge {judge_id} aggregate score is not rubric-derived")
            if reported_score < float(declared_judges[judge_id]["threshold"]):
                judge_failures.append(f"{case_id}:{judge_id}")
                case_judges_pass = False
        number(record["cost"], f"case {case_id}.cost")
        number(record["latency_ms"], f"case {case_id}.latency_ms")
        failed_gates = [gate_id for gate_id, value in gates.items() if not value]
        deterministic_failures.extend(f"{case_id}:{gate_id}" for gate_id in failed_gates)
        if not failed_gates and case_judges_pass:
            passed += 1
        indexed[case_id] = record

    if set(indexed) != set(corpus_cases):
        raise ValueError(f"results must cover the frozen corpus exactly; missing={sorted(set(corpus_cases) - set(indexed))}")
    dimensions = {
        dimension: (dimension_totals[dimension][0] / dimension_totals[dimension][1] if dimension_totals[dimension][1] else 0.0)
        for dimension in DIMENSIONS
    }
    threshold = float(contract["promotion"]["minimum_dimension_score"])
    dimension_failures = [dimension for dimension, value in dimensions.items() if value < threshold]
    return {
        "version": version,
        "pass_rate": passed / len(records),
        "passed": passed,
        "total": len(records),
        "deterministic_failures": deterministic_failures,
        "judge_failures": judge_failures,
        "dimension_failures": dimension_failures,
        "dimensions": dimensions,
        "total_cost": sum(float(record["cost"]) for record in records),
        "average_latency_ms": sum(float(record["latency_ms"]) for record in records) / len(records),
        "validator": validator,
    }


def percent_change(candidate: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0 if candidate == 0 else float("inf")
    return ((candidate - baseline) / baseline) * 100.0


def validate_decision(path: Path, workflow: str, version: str, contract_digest: str, results_digest: str) -> dict[str, Any]:
    decision = read_json(path)
    required = {"schema_version", "decision", "operator", "rationale", "workflow", "candidate_version", "results_digest", "contract_digest", "decided_at"}
    exact_keys(decision, required, set(), "operator decision")
    if decision["schema_version"] != "1.0" or decision["decision"] not in {"promote", "reject"}:
        raise ValueError("operator decision schema or value is invalid")
    if decision["workflow"] != workflow or decision["candidate_version"] != version:
        raise ValueError("operator decision targets the wrong workflow version")
    if decision["contract_digest"] != contract_digest or decision["results_digest"] != results_digest:
        raise ValueError("operator decision is not bound to these immutable inputs")
    nonempty_text(decision["operator"], "operator decision.operator")
    if len(nonempty_text(decision["rationale"], "operator decision.rationale")) < 10:
        raise ValueError("operator decision.rationale must contain at least 10 characters")
    nonempty_text(decision["decided_at"], "operator decision.decided_at")
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--baseline-results")
    parser.add_argument("--operator-decision")
    parser.add_argument("--output")
    args = parser.parse_args()

    harness = Path(args.harness).expanduser().resolve()
    config = read_json(harness / "harness.json")
    blueprint = read_json(harness / "workflow.blueprint.json")
    contract_path = harness / "certification-contract.json"
    if not contract_path.is_file() or not config.get("certification_contract", {}).get("enabled"):
        raise SystemExit("compiled workflow has no certification contract")
    contract = read_json(contract_path)
    contract_digest = canonical_digest(contract)
    if contract_digest != config["certification_contract"].get("digest"):
        raise SystemExit("certification contract digest mismatch")

    corpus_path = harness / "examples" / "workspace" / contract["corpus"]["path"]
    corpus = read_json(corpus_path)
    corpus_cases = validate_corpus(corpus, contract, blueprint["workflow"])
    acceptance_ids = {criterion["id"] for criterion in blueprint["acceptance_criteria"]}
    rubrics, rubric_digests = validate_rubrics(harness, contract, acceptance_ids)

    results_path = Path(args.results).expanduser().resolve()
    results = read_json(results_path)
    candidate = validate_results(
        results_path, results, blueprint["workflow"], blueprint["version"], contract, contract_digest,
        corpus, corpus_cases, rubrics, rubric_digests,
    )

    baseline_summary = None
    regression_failures: list[str] = []
    baseline_version = contract["replay"].get("baseline_version")
    cost_change = None
    latency_change = None
    if baseline_version is not None:
        if not args.baseline_results:
            raise SystemExit("certification contract requires --baseline-results")
        baseline_path = Path(args.baseline_results).expanduser().resolve()
        baseline_results = read_json(baseline_path)
        baseline_summary = validate_results(
            baseline_path, baseline_results, blueprint["workflow"], baseline_version, contract, contract_digest,
            corpus, corpus_cases, rubrics, rubric_digests,
        )
        cost_change = percent_change(candidate["total_cost"], baseline_summary["total_cost"])
        latency_change = percent_change(candidate["average_latency_ms"], baseline_summary["average_latency_ms"])
        if candidate["pass_rate"] < baseline_summary["pass_rate"]:
            regression_failures.append("pass_rate")
        for dimension in DIMENSIONS:
            if candidate["dimensions"][dimension] < baseline_summary["dimensions"][dimension]:
                regression_failures.append(dimension)
        if cost_change > float(contract["replay"]["max_cost_regression_percent"]):
            regression_failures.append("cost")
        if latency_change > float(contract["replay"]["max_latency_regression_percent"]):
            regression_failures.append("latency")

    minimum_pass_rate = float(contract["promotion"]["minimum_pass_rate"])
    mechanical_pass = (
        candidate["pass_rate"] >= minimum_pass_rate
        and not candidate["deterministic_failures"]
        and not candidate["judge_failures"]
        and not candidate["dimension_failures"]
        and not regression_failures
    )
    results_digest = canonical_digest(results)
    decision = validate_decision(
        Path(args.operator_decision).expanduser().resolve(), blueprint["workflow"], blueprint["version"], contract_digest, results_digest
    ) if args.operator_decision else None
    if not mechanical_pass:
        status = "failed"
    elif decision is None:
        status = "awaiting_operator"
    elif decision["decision"] == "promote":
        status = "certified"
    else:
        status = "rejected"

    report = {
        "schema_version": "1.1",
        "workflow": blueprint["workflow"],
        "version": blueprint["version"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "promotion_eligible": status == "certified",
        "mechanical_pass": mechanical_pass,
        "operator_decision": decision,
        "contract_digest": contract_digest,
        "corpus_digest": canonical_digest(corpus),
        "results_digest": results_digest,
        "rubric_digests": rubric_digests,
        "candidate": candidate,
        "baseline": baseline_summary,
        "regression": {
            "baseline_version": baseline_version,
            "cost_change_percent": cost_change,
            "latency_change_percent": latency_change,
            "failures": sorted(set(regression_failures)),
        },
    }
    output = Path(args.output).expanduser().resolve() if args.output else harness / "certification" / "latest-contract-evaluation.json"
    atomic_write(output, report)
    print(output)
    return 0 if report["promotion_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
