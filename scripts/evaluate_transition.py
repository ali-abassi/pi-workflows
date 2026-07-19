#!/usr/bin/env python3
"""Evaluate one compiled conditional-workflow transition without an LLM."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
SAFE_DECISION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LEAF_OPS = {"exists", "missing", "type_is", "equals", "not_equals", "less_than", "less_than_or_equal", "greater_than", "greater_than_or_equal", "contains", "in"}
GROUP_OPS = {"all", "any", "not"}
TYPE_NAMES = {"null", "boolean", "number", "string", "array", "object"}
FORBIDDEN_POINTER_SEGMENTS = {"__proto__", "prototype", "constructor"}


class TransitionError(ValueError):
    pass


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-JSON number {value}")))
    except FileNotFoundError as exc:
        raise TransitionError(f"missing JSON file: {path}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise TransitionError(f"invalid JSON in {path}: {exc}") from exc


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise TransitionError("non-finite numbers are not valid routing data")
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TransitionError(f"unsupported non-JSON value type: {type(value).__name__}")


def pointer_segments(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 512:
        raise TransitionError("condition path must be an RFC 6901 JSON Pointer up to 512 characters")
    result = []
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise TransitionError(f"invalid JSON Pointer escape in {pointer!r}")
        value = raw.replace("~1", "/").replace("~0", "~")
        if value in FORBIDDEN_POINTER_SEGMENTS:
            raise TransitionError(f"forbidden JSON Pointer segment: {value}")
        result.append(value)
    return result


def resolve_pointer(value: Any, pointer: str) -> tuple[bool, Any]:
    current = value
    for segment in pointer_segments(pointer):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return False, None
    return True, current


def validate_condition(condition: Any, *, depth: int = 0, counter: list[int] | None = None) -> list[str]:
    errors: list[str] = []
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > 100:
        return ["condition tree exceeds 100 nodes"]
    if depth > 8:
        return ["condition tree exceeds depth 8"]
    if not isinstance(condition, dict):
        return ["condition must be an object"]
    op = condition.get("op")
    if op in {"all", "any"}:
        if set(condition) != {"op", "args"} or not isinstance(condition.get("args"), list) or not condition["args"] or len(condition["args"]) > 50:
            return [f"{op} condition requires 1-50 args and no other fields"]
        for item in condition["args"]:
            errors.extend(validate_condition(item, depth=depth + 1, counter=counter))
        return errors
    if op == "not":
        if set(condition) != {"op", "arg"}:
            return ["not condition requires exactly one arg"]
        return validate_condition(condition["arg"], depth=depth + 1, counter=counter)
    if op not in LEAF_OPS:
        return [f"unsupported condition operator: {op!r}"]
    expected = {"op", "path"} if op in {"exists", "missing"} else {"op", "path", "value"}
    if set(condition) != expected:
        errors.append(f"{op} condition must contain exactly: {', '.join(sorted(expected))}")
    try:
        pointer_segments(condition.get("path"))
    except (TransitionError, TypeError) as exc:
        errors.append(str(exc))
    if "value" in expected:
        candidate = condition.get("value")
        try:
            candidate_type = json_type(candidate)
        except TransitionError as exc:
            errors.append(str(exc))
            candidate_type = "invalid"
        if op == "type_is" and candidate not in TYPE_NAMES:
            errors.append("type_is value must name a JSON type")
        if op in {"less_than", "less_than_or_equal", "greater_than", "greater_than_or_equal"} and candidate_type != "number":
            errors.append(f"{op} value must be a number")
        if op == "in" and candidate_type != "array":
            errors.append("in value must be an array")
        if isinstance(candidate, list) and len(candidate) > 100:
            errors.append("condition arrays may contain at most 100 values")
        if len(json.dumps(candidate, ensure_ascii=False)) > 65536:
            errors.append("condition value exceeds 64 KiB")
    return errors


def graph_errors(graph: Any) -> list[str]:
    errors: list[str] = []
    required = {"schema_version", "workflow", "stages", "initial_stage", "terminal_stages", "max_transitions", "max_visits_per_stage", "on_exhausted", "transitions"}
    if not isinstance(graph, dict) or set(graph) != required:
        return ["control-flow graph must contain exactly: " + ", ".join(sorted(required))]
    if graph.get("schema_version") != "1.0":
        errors.append("control-flow schema_version must be 1.0")
    stages = graph.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) != len(set(stages or [])) or not all(isinstance(item, str) and SAFE_ID.fullmatch(item) for item in stages):
        errors.append("stages must be a non-empty unique safe-name array")
        stages = []
    stage_set = set(stages)
    initial = graph.get("initial_stage")
    terminals = graph.get("terminal_stages")
    if initial not in stage_set:
        errors.append("initial_stage must name a declared stage")
    if not isinstance(terminals, list) or not terminals or len(terminals) != len(set(terminals or [])) or not set(terminals).issubset(stage_set):
        errors.append("terminal_stages must be a non-empty unique subset of stages")
        terminals = []
    if isinstance(graph.get("max_transitions"), bool) or not isinstance(graph.get("max_transitions"), int) or not 1 <= graph["max_transitions"] <= 1000:
        errors.append("max_transitions must be from 1 through 1000")
    visits = graph.get("max_visits_per_stage")
    if not isinstance(visits, dict) or not set(visits).issubset(stage_set) or not all(not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= 50 for value in visits.values()):
        errors.append("max_visits_per_stage must map declared stages to limits from 1 through 50")
        visits = {}
    if graph.get("on_exhausted") not in set(terminals):
        errors.append("on_exhausted must name a terminal stage")
    transitions = graph.get("transitions")
    if not isinstance(transitions, list) or not transitions:
        errors.append("transitions must be a non-empty array")
        transitions = []
    ids: set[str] = set()
    outgoing: dict[str, list[dict[str, Any]]] = {stage: [] for stage in stages}
    adjacency: dict[str, set[str]] = {stage: set() for stage in stages}
    for index, transition in enumerate(transitions, start=1):
        label = f"transitions[{index}]"
        if not isinstance(transition, dict):
            errors.append(f"{label} must be an object")
            continue
        base = {"id", "from", "to", "priority"}
        conditional = set(transition) == base | {"when"}
        default = set(transition) == base | {"default"} and transition.get("default") is True
        if not (conditional or default):
            errors.append(f"{label} must contain base fields and exactly one of when or default:true")
            continue
        transition_id = transition.get("id")
        if not isinstance(transition_id, str) or not SAFE_ID.fullmatch(transition_id) or transition_id in ids:
            errors.append(f"{label}.id must be a unique safe name")
        else:
            ids.add(transition_id)
        source, target = transition.get("from"), transition.get("to")
        if source not in stage_set or target not in stage_set:
            errors.append(f"{label} source and target must be declared stages")
            continue
        if source in set(terminals):
            errors.append(f"terminal stage {source} cannot have outgoing transitions")
        if isinstance(transition.get("priority"), bool) or not isinstance(transition.get("priority"), int) or not 0 <= transition["priority"] <= 10000:
            errors.append(f"{label}.priority must be from 0 through 10000")
        if conditional:
            errors.extend(f"{label}: {error}" for error in validate_condition(transition["when"]))
        outgoing[source].append(transition)
        adjacency[source].add(target)
    for stage in stages:
        if stage in set(terminals):
            continue
        routes = outgoing[stage]
        if not routes:
            errors.append(f"nonterminal stage {stage} has no outgoing transitions")
            continue
        defaults = [item for item in routes if item.get("default") is True]
        if len(defaults) != 1:
            errors.append(f"nonterminal stage {stage} must have exactly one default transition")
        priorities = [item.get("priority") for item in routes if "when" in item]
        if len(priorities) != len(set(priorities)):
            errors.append(f"conditional priorities from stage {stage} must be unique")
        if graph.get("on_exhausted") in stage_set:
            adjacency[stage].add(graph["on_exhausted"])
    if stages and initial in stage_set:
        reachable = {initial}
        frontier = [initial]
        while frontier:
            node = frontier.pop()
            for target in adjacency[node] - reachable:
                reachable.add(target)
                frontier.append(target)
        missing = sorted(stage_set - reachable)
        if missing:
            errors.append("unreachable stages: " + ", ".join(missing))
        for stage in sorted(reachable):
            seen, frontier = {stage}, [stage]
            terminal_reachable = stage in set(terminals)
            while frontier and not terminal_reachable:
                node = frontier.pop()
                for target in adjacency[node] - seen:
                    terminal_reachable = terminal_reachable or target in set(terminals)
                    seen.add(target)
                    frontier.append(target)
            if not terminal_reachable:
                errors.append(f"stage {stage} cannot reach a terminal stage")
        cyclic = []
        for stage in stages:
            seen, frontier = set(), list(adjacency[stage])
            while frontier:
                node = frontier.pop()
                if node == stage:
                    cyclic.append(stage)
                    break
                if node in seen:
                    continue
                seen.add(node)
                frontier.extend(adjacency[node])
        unbounded = sorted(stage for stage in cyclic if visits.get(stage, 1) <= 1)
        if unbounded:
            errors.append("cyclic stages require explicit max_visits_per_stage > 1: " + ", ".join(unbounded))
    return errors


def assert_graph(graph: Any) -> dict[str, Any]:
    errors = graph_errors(graph)
    if errors:
        raise TransitionError("invalid control-flow graph:\n- " + "\n- ".join(errors))
    return graph


def evaluate_condition(condition: dict[str, Any], data: Any) -> tuple[bool, dict[str, Any]]:
    op = condition["op"]
    if op in {"all", "any"}:
        children = [evaluate_condition(item, data) for item in condition["args"]]
        result = all(item[0] for item in children) if op == "all" else any(item[0] for item in children)
        return result, {"op": op, "result": result, "children": [item[1] for item in children]}
    if op == "not":
        child_result, child = evaluate_condition(condition["arg"], data)
        return not child_result, {"op": op, "result": not child_result, "child": child}
    present, actual = resolve_pointer(data, condition["path"])
    if op == "exists":
        return present, {"op": op, "path": condition["path"], "present": present, "result": present}
    if op == "missing":
        return not present, {"op": op, "path": condition["path"], "present": present, "result": not present}
    if not present:
        raise TransitionError(f"condition path is missing: {condition['path']}")
    actual_type = json_type(actual)
    expected = condition["value"]
    if op == "type_is":
        result = actual_type == expected
    elif op in {"equals", "not_equals"}:
        equal = actual_type == json_type(expected) and actual == expected
        result = equal if op == "equals" else not equal
    elif op in {"less_than", "less_than_or_equal", "greater_than", "greater_than_or_equal"}:
        if actual_type != "number":
            raise TransitionError(f"{op} requires a numeric value at {condition['path']}, got {actual_type}")
        result = {"less_than": actual < expected, "less_than_or_equal": actual <= expected, "greater_than": actual > expected, "greater_than_or_equal": actual >= expected}[op]
    elif op == "contains":
        if actual_type not in {"string", "array"}:
            raise TransitionError(f"contains requires a string or array at {condition['path']}, got {actual_type}")
        if actual_type == "string" and not isinstance(expected, str):
            raise TransitionError(f"contains on a string requires a string condition value at {condition['path']}")
        result = expected in actual
    elif op == "in":
        result = any(json_type(actual) == json_type(item) and actual == item for item in expected)
    else:
        raise TransitionError(f"unsupported condition operator: {op}")
    return result, {"op": op, "path": condition["path"], "present": True, "actual_type": actual_type, "actual": actual, "expected": expected, "result": result}


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        try:
            records.append(json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-JSON number {value}"))))
        except (json.JSONDecodeError, ValueError) as exc:
            raise TransitionError(f"invalid routing ledger line {line_number}: {exc}") from exc
    return records


def decision_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"decision_digest", "recorded_at"}}


def verify_ledger(run_dir: Path, graph: dict[str, Any]) -> list[dict[str, Any]]:
    records = read_ledger(run_dir / "routing" / "decisions.jsonl")
    previous = None
    seen_ids: set[str] = set()
    for record in records:
        if record.get("decision_id") in seen_ids or record.get("graph_digest") != canonical_digest(graph) or record.get("previous_decision_digest") != previous:
            raise TransitionError("routing decision chain metadata is invalid")
        if record.get("decision_digest") != canonical_digest(decision_payload(record)):
            raise TransitionError("routing decision digest is invalid")
        artifact = run_dir / str(record.get("source_artifact", ""))
        if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != record.get("source_artifact_digest"):
            raise TransitionError("routing decision source artifact changed or disappeared")
        individual = run_dir / "routing" / "decisions" / f"{record['decision_id']}.json"
        if not individual.is_file() or read_json(individual) != record:
            raise TransitionError("individual routing decision changed or disappeared")
        seen_ids.add(record["decision_id"])
        previous = record["decision_digest"]
    return records


def state_from_records(graph: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    visits = {graph["initial_stage"]: 1}
    current = graph["initial_stage"]
    for record in records:
        if record["from"] != current:
            raise TransitionError("routing ledger stage sequence is invalid")
        current = record["to"]
        visits[current] = visits.get(current, 0) + 1
    return {"graph_digest": canonical_digest(graph), "current_stage": current, "transition_count": len(records), "visits": visits, "previous_decision_digest": records[-1]["decision_digest"] if records else None}


def evaluate_transition(*, graph: dict[str, Any], run_dir: Path, stage: str, decision_id: str) -> dict[str, Any]:
    assert_graph(graph)
    if not SAFE_DECISION_ID.fullmatch(decision_id):
        raise TransitionError("decision_id must be a safe path segment")
    records = verify_ledger(run_dir, graph)
    existing = next((record for record in records if record["decision_id"] == decision_id), None)
    source_relative = f"stages/{stage}.json"
    source_path = run_dir / source_relative
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else None
    if existing:
        if existing.get("from") != stage or existing.get("source_artifact_digest") != source_digest:
            raise TransitionError("decision_id was already used for a different routing request")
        return existing
    state = state_from_records(graph, records)
    atomic_json(run_dir / "routing" / "state.json", state)
    if state["current_stage"] != stage:
        raise TransitionError(f"current routing stage is {state['current_stage']}, not {stage}")
    if stage in graph["terminal_stages"]:
        raise TransitionError(f"terminal stage {stage} has no outgoing transition")
    if not source_path.is_file():
        raise TransitionError(f"missing current stage artifact: {source_path}")
    source = read_json(source_path)
    outgoing = [item for item in graph["transitions"] if item["from"] == stage]
    evaluated = []
    matches = []
    default = None
    exhausted_reason = "max_transitions" if state["transition_count"] >= graph["max_transitions"] else None
    if exhausted_reason:
        selected, used_default = None, False
    else:
        for transition in outgoing:
            if transition.get("default") is True:
                default = transition
                continue
            matched, trace = evaluate_condition(transition["when"], source)
            evaluated.append({"transition_id": transition["id"], "to": transition["to"], "priority": transition["priority"], "matched": matched, "trace": trace})
            if matched:
                matches.append(transition)
        if matches:
            best_priority = min(item["priority"] for item in matches)
            best = [item for item in matches if item["priority"] == best_priority]
            if len(best) != 1:
                raise TransitionError("ambiguous routing: multiple matching transitions share the best priority")
            selected, used_default = best[0], False
        else:
            if default is None:
                raise TransitionError("no transition matched and no default exists")
            selected, used_default = default, True
        target_limit = graph["max_visits_per_stage"].get(selected["to"], 1)
        if state["visits"].get(selected["to"], 0) >= target_limit:
            exhausted_reason = f"max_visits:{selected['to']}"
    target = graph["on_exhausted"] if exhausted_reason else selected["to"]
    record = {
        "schema_version": "1.0", "workflow": graph["workflow"], "decision_id": decision_id,
        "sequence": len(records) + 1, "graph_digest": canonical_digest(graph), "from": stage, "to": target,
        "source_artifact": source_relative, "source_artifact_digest": source_digest,
        "evaluated": evaluated, "matched_transition_ids": [item["id"] for item in matches],
        "selected_transition_id": selected["id"] if selected else None, "selected_priority": selected["priority"] if selected else None,
        "used_default": used_default, "exhausted_reason": exhausted_reason,
        "transition_count_before": state["transition_count"], "visits_before": state["visits"],
        "previous_decision_digest": state["previous_decision_digest"], "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    record["decision_digest"] = canonical_digest(decision_payload(record))
    decision_path = run_dir / "routing" / "decisions" / f"{decision_id}.json"
    atomic_json(decision_path, record)
    ledger = run_dir / "routing" / "decisions.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    updated = state_from_records(graph, [*records, record])
    atomic_json(run_dir / "routing" / "state.json", updated)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, help="Compiled specialized harness root")
    parser.add_argument("--run", required=True, help="Run directory containing stages/<from>.json")
    parser.add_argument("--from", dest="stage", required=True, help="Current stage id")
    parser.add_argument("--decision-id", required=True, help="Stable idempotency key for this routing decision")
    args = parser.parse_args()
    harness = Path(args.harness).expanduser().resolve()
    run_dir = Path(args.run).expanduser().resolve()
    try:
        graph = read_json(harness / "control-flow.json")
        config = read_json(harness / "harness.json")
        flow = config.get("control_flow") if isinstance(config, dict) else None
        if not isinstance(flow, dict) or flow.get("enabled") is not True:
            raise TransitionError("harness does not declare enabled conditional control flow")
        if flow.get("graph_digest") != canonical_digest(graph):
            raise TransitionError("compiled control-flow graph digest mismatch")
        declared_engine = flow.get("engine")
        if declared_engine != "scripts/evaluate_transition.py":
            raise TransitionError("harness declares an unsupported transition engine path")
        engine_path = (harness / declared_engine).resolve()
        if engine_path != Path(__file__).resolve():
            raise TransitionError("transition engine must execute from the compiled harness bundle")
        if flow.get("engine_digest") != canonical_digest(engine_path.read_text()):
            raise TransitionError("compiled transition engine digest mismatch")
        decision = evaluate_transition(graph=graph, run_dir=run_dir, stage=args.stage, decision_id=args.decision_id)
    except TransitionError as exc:
        failure = {"status": "failed", "decision_id": args.decision_id, "from": args.stage, "error": str(exc), "recorded_at": datetime.now(timezone.utc).isoformat()}
        atomic_json(run_dir / "routing" / "failures" / f"{args.decision_id}.json", failure)
        print(str(exc), file=__import__("sys").stderr)
        return 1
    print(json.dumps(decision, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
