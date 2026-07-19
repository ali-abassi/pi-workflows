#!/usr/bin/env python3
"""Render one deterministic Pi harness run as a compact static HTML tracker."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any


GOOD = {"passed", "done", "approved", "verified", "true", "0"}
BAD = {"failed", "error", "invalid", "false"}


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    candidates = [run_dir / "tracker.jsonl", run_dir / "events" / "harness.jsonl"]
    candidates += sorted((run_dir / "events").glob("pi-*-hooks.jsonl")) if (run_dir / "events").is_dir() else []
    seen: set[str] = set()
    events: list[dict[str, Any]] = []
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip() or line in seen:
                continue
            seen.add(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event["_source_file"] = str(path.relative_to(run_dir))
            events.append(event)
    return sorted(events, key=lambda item: str(item.get("timestamp", "")))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def esc(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True)
    return html.escape(str(value), quote=True)


def status_class(status: Any) -> str:
    lowered = str(status or "unknown").lower()
    return "ok" if lowered in GOOD else "bad" if lowered in BAD else "neutral" if lowered == "skipped" else "warn"


def dot(status: Any, label: Any = None) -> str:
    text = label if label is not None else status
    return f'<span class="dotline"><span class="dot {status_class(status)}"></span><span>{esc(text)}</span></span>'


def audit_outcome(passed: Any, score: Any) -> str:
    verdict = "PASS" if passed is True else "FAIL" if passed is False else "UNKNOWN"
    return f"{verdict} · {score}" if score is not None else verdict


def render_audit_step(run_dir: Path, index: int) -> tuple[str, str] | None:
    review = read_json(run_dir / "reviews" / f"{index:02d}.json", {}) or {}
    trace = read_json(run_dir / "inputs" / "traces" / f"{index:02d}.json", {}) or {}
    if not isinstance(review, dict) or not review.get("accuracy") or not isinstance(trace, dict):
        return None
    accuracy = str(review.get("accuracy"))
    error_class = str(review.get("error_class") or "none").replace("_", " ")
    accuracy_status = "passed" if accuracy == "correct" else "failed" if accuracy == "incorrect" else "pending"
    original = trace.get("original_score") if isinstance(trace.get("original_score"), dict) else {}
    confidence = review.get("confidence")
    confidence_text = f"{float(confidence) * 100:.0f}%" if isinstance(confidence, (int, float)) else "—"
    original_outcome = audit_outcome(original.get("passed"), original.get("score"))
    expected_outcome = audit_outcome(review.get("expected_pass"), review.get("expected_score"))
    summary = (
        f'<span class="audit-summary"><span class="audit-summary-top"><strong>Trace {index:02d}</strong>'
        f'<span class="outcome-flow">{esc(original_outcome)} → {esc(expected_outcome)}</span>'
        f'{dot(accuracy_status, f"{accuracy} · {error_class}")}</span>'
        f'<span class="audit-preview">{esc(review.get("review"))}</span></span>'
        f'<span class="verified-note">quality {esc(review.get("quality_score"))}/10 · confidence {esc(confidence_text)}<br>review verified</span>'
    )
    evidence_html = "".join(
        f'<blockquote><span class="evidence-source">{esc(item.get("source"))}</span><code>{esc(item.get("quote"))}</code><p>{esc(item.get("why"))}</p></blockquote>'
        for item in review.get("evidence", []) if isinstance(item, dict)
    ) or '<p class="muted">No grounded evidence recorded.</p>'
    issues_html = "".join(f'<li>{esc(item)}</li>' for item in review.get("issues", [])) or '<li>None recorded.</li>'
    trace_text = trace.get("rendered_scorer_trace")
    trace_display = trace_text if isinstance(trace_text, str) else f"Trace unavailable: {trace.get('trace_error') or 'unknown error'}"
    body = f'''
<div class="audit-body">
  <div class="decision-grid">
    <div><span>Original scorer</span><strong>{esc(audit_outcome(original.get("passed"), original.get("score")))}</strong><p>{esc(original.get("reason") or "No scorer reason recorded.")}</p></div>
    <div><span>Audit judgment</span><strong class="{status_class(accuracy_status)}">{esc(accuracy.upper())} · {esc(error_class)}</strong><p>{esc(review.get("review"))}</p></div>
    <div><span>Expected result</span><strong>{esc(audit_outcome(review.get("expected_pass"), review.get("expected_score")))}</strong><p>Confidence {esc(confidence_text)} · context {esc(review.get("context_sufficiency"))}</p></div>
  </div>
  <div class="ratings">
    <span>Overall quality <b>{esc(review.get("quality_score"))}/10</b></span>
    <span>Reasoning <b>{esc(review.get("reasoning_quality"))}/10</b></span>
    <span>Evidence coverage <b>{esc(review.get("evidence_coverage"))}/10</b></span>
  </div>
  <h3>Grounded evidence</h3>{evidence_html}
  <div class="audit-columns"><div><h3>Problems found</h3><ul>{issues_html}</ul></div><div><h3>Recommended change</h3><p>{esc(review.get("recommended_change") or "No change recommended.")}</p></div></div>
  <div class="trace-meta">Run {esc(trace.get("run_id"))} · Subtask {esc(trace.get("subtask_id"))} · {esc(trace.get("run_at"))}</div>
  <details class="full-trace"><summary>View full persisted scorer trace</summary><pre>{esc(trace_display)}</pre><a href="inputs/traces/{index:02d}.json">Open source trace JSON</a> · <a href="reviews/{index:02d}.json">Open review JSON</a></details>
</div>'''
    return summary, body


def render_root_cause_step(run_dir: Path, index: int) -> tuple[str, str] | None:
    diagnosis = read_json(run_dir / "diagnoses" / f"{index:02d}.json", {}) or {}
    trace = read_json(run_dir / "inputs" / "traces" / f"{index:02d}.json", {}) or {}
    if not isinstance(diagnosis, dict) or diagnosis.get("status") not in {"diagnosed", "unresolved"} or not isinstance(trace, dict):
        return None
    diagnosed = diagnosis["status"] == "diagnosed"
    outcome_status = "passed" if diagnosed else "pending"
    category = str(diagnosis.get("root_cause_category") or "unknown").replace("_", " ")
    diagnosis_status = str(diagnosis["status"])
    confidence = diagnosis.get("confidence")
    confidence_text = f"{float(confidence) * 100:.0f}%" if isinstance(confidence, (int, float)) else "—"
    context = ((diagnosis.get("_harness") or {}).get("trace_context") or {})
    coverage = context.get("coverage")
    coverage_text = f"{float(coverage) * 100:.1f}%" if isinstance(coverage, (int, float)) else "—"
    summary = (
        f'<span class="audit-summary"><span class="audit-summary-top"><strong>Failed task {index:02d}</strong>'
        f'{dot(outcome_status, diagnosis_status + " · " + category)}</span>'
        f'<span class="audit-preview">{esc(diagnosis.get("root_cause"))}</span></span>'
        f'<span class="verified-note">confidence {esc(confidence_text)}<br>context {esc(coverage_text)}</span>'
    )
    original = trace.get("original_score") if isinstance(trace.get("original_score"), dict) else {}
    evidence_html = "".join(
        f'<blockquote><span class="evidence-source">{esc(item.get("source"))}</span><code>{esc(item.get("quote"))}</code><p>{esc(item.get("why"))}</p></blockquote>'
        for item in diagnosis.get("evidence", []) if isinstance(item, dict)
    ) or '<p class="muted">No task evidence recorded.</p>'
    factors = "".join(f"<li>{esc(item)}</li>" for item in diagnosis.get("contributing_factors", [])) or "<li>None recorded.</li>"
    uncertainties = "".join(f"<li>{esc(item)}</li>" for item in diagnosis.get("remaining_uncertainty", [])) or "<li>None recorded.</li>"
    fixes = "".join(
        f'<div class="fix-card"><span>{esc(item.get("layer"))}</span><strong>{esc(item.get("change"))}</strong><p>{esc(item.get("rationale"))}</p><small>Impact · {esc(item.get("expected_impact"))}<br>Risk · {esc(item.get("risk"))}<br>Verify · {esc(item.get("verification"))}</small></div>'
        for item in diagnosis.get("fixes", []) if isinstance(item, dict)
    ) or '<p class="muted">No fix proposed because the case is unresolved.</p>'
    trace_text = trace.get("rendered_scorer_trace")
    trace_display = trace_text if isinstance(trace_text, str) else f"Trace unavailable: {trace.get('trace_error') or 'unknown error'}"
    body = f'''
<div class="audit-body rca-body">
  <div class="decision-grid">
    <div><span>Failed-score selector</span><strong>{esc(audit_outcome(original.get("passed"), original.get("score")))}</strong><p>{esc(original.get("reason") or "No scorer reason recorded.")}</p></div>
    <div><span>Expected behavior</span><strong>EXPECTED</strong><p>{esc(diagnosis.get("expected_behavior"))}</p></div>
    <div><span>Actual behavior</span><strong class="bad">ACTUAL</strong><p>{esc(diagnosis.get("actual_behavior"))}</p></div>
  </div>
  <h3>Root cause</h3><div class="root-cause-callout"><strong>{esc(category)}</strong><p>{esc(diagnosis.get("root_cause"))}</p></div>
  <h3>Grounded evidence</h3>{evidence_html}
  <div class="audit-columns"><div><h3>Contributing factors</h3><ul>{factors}</ul></div><div><h3>Remaining uncertainty</h3><ul>{uncertainties}</ul></div></div>
  <h3>Proposed fixes</h3><div class="fix-grid">{fixes}</div>
  <div class="trace-meta">Run {esc(trace.get("run_id"))} · Subtask {esc(trace.get("subtask_id"))} · context coverage {esc(coverage_text)} · {esc(diagnosis.get("context_sufficiency"))}</div>
  <details class="full-trace"><summary>View complete persisted scorer trace</summary><pre>{esc(trace_display)}</pre><a href="inputs/traces/{index:02d}.json">Open source trace JSON</a> · <a href="diagnoses/{index:02d}.json">Open diagnosis JSON</a> · <a href="context/{index:02d}.json">Open context selection JSON</a></details>
</div>'''
    return summary, body


def render_aggregate_findings(run_dir: Path) -> str:
    aggregate = read_json(run_dir / "stages" / "aggregate.json", {}) or {}
    metrics = read_json(run_dir / "validation" / "metrics.json", {}) or {}
    if not isinstance(aggregate, dict) or not aggregate.get("summary"):
        return ""

    def finding_group(title: str, key: str, opened: bool = False) -> str:
        values = aggregate.get(key) if isinstance(aggregate.get(key), list) else []
        items = "".join(f"<li>{esc(item)}</li>" for item in values) or "<li>None recorded.</li>"
        return f'<details class="finding-group"{" open" if opened else ""}><summary>{esc(title)} · {len(values)}</summary><ul>{items}</ul></details>'

    root_cause_mode = metrics.get("selection") == "failed_root_cause"
    failed_only = metrics.get("selection") == "failed_only"
    accuracy = metrics.get("failed_decision_accuracy_rate") if failed_only else metrics.get("estimated_accuracy_rate")
    accuracy_text = f"{float(accuracy) * 100:.0f}%" if isinstance(accuracy, (int, float)) else "—"
    if root_cause_mode:
        categories = metrics.get("root_cause_categories") or {}
        top_category = max(categories, key=categories.get) if categories else "—"
        metric_cards = [
            ("Failed tasks", metrics.get("trace_count", "—")),
            ("Diagnosed", metrics.get("diagnosed_count", "—")),
            ("Unresolved", metrics.get("unresolved_count", "—")),
            ("Average confidence", f'{float(metrics.get("average_confidence", 0)) * 100:.0f}%' if isinstance(metrics.get("average_confidence"), (int, float)) else "—"),
            ("Top root cause", str(top_category).replace("_", " ")),
            ("Root-cause classes", len(categories)),
            ("Evidence verification", "passed"),
            ("Synthesis confidence", f'{aggregate.get("score", "—")}/10'),
        ]
    elif failed_only:
        metric_cards = [
            ("Failed decisions reviewed", metrics.get("failed_decision_count", "—")),
            ("Valid failures", metrics.get("valid_failure_count", "—")),
            ("Invalid failures", metrics.get("invalid_failure_count", "—")),
            ("Failed-decision accuracy", accuracy_text),
            ("False-negative candidates", metrics.get("false_negative_count", "—")),
            ("Unjudgeable", metrics.get("unjudgeable_count", "—")),
            ("Average quality", f'{metrics.get("average_quality_score", "—")}/10'),
            ("Audit confidence", f'{aggregate.get("score", "—")}/10'),
        ]
    else:
        metric_cards = [
            ("Estimated accuracy", accuracy_text),
            ("Correct", metrics.get("correct_count", "—")),
            ("Incorrect", metrics.get("incorrect_count", "—")),
            ("False positives", metrics.get("false_positive_count", "—")),
            ("False negatives", metrics.get("false_negative_count", "—")),
            ("Unjudgeable", metrics.get("unjudgeable_count", "—")),
            ("Average quality", f'{metrics.get("average_quality_score", "—")}/10'),
            ("Audit confidence", f'{aggregate.get("score", "—")}/10'),
        ]
    cards = "".join(f'<div><span>{esc(label)}</span><strong>{esc(value)}</strong></div>' for label, value in metric_cards)
    return f'''
<div class="section-head"><h2>Aggregate findings</h2><span class="section-note">Sol synthesis · {"failed-task root causes · " if root_cause_mode else "failed decisions only · " if failed_only else ""}mechanically checked metrics</span></div>
<section class="panel aggregate-findings">
  <div class="metric-grid">{cards}</div>
  <p class="aggregate-summary">{esc(aggregate.get("summary"))}</p>
  <div class="finding-grid">
    {finding_group("Systemic issues", "systemic_issues", True)}
    {finding_group("Recommendations", "recommendations", True)}
    {finding_group("Strengths", "strengths")}
    {finding_group("Residual risks", "residual_risk")}
  </div>
  <a class="raw-link" href="stages/aggregate.json">Open aggregate JSON</a> · <a class="raw-link" href="validation/metrics.json">Open mechanical metrics JSON</a>
</section>'''


def verify_seal(run_dir: Path) -> str:
    seal = read_json(run_dir / "integrity" / "run-seal.json", {}) or {}
    artifacts = seal.get("artifacts")
    if not isinstance(artifacts, list):
        return "unsealed"
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return "invalid"
        path = run_dir / item["path"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
            return "invalid"
    return "verified"


def render(run_dir: Path) -> Path:
    manifest = read_json(run_dir / "manifest.json", {}) or {}
    state = read_json(run_dir / "state.json", {}) or {}
    validation = read_json(run_dir / "validation" / "final_validation.json", {}) or {}
    criteria_validation = read_json(run_dir / "validation" / "criteria.json", {}) or {}
    failure = read_json(run_dir / "failure.json", {}) or {}
    stream = read_json(run_dir / "stream_state.json", {}) or {}
    events = read_events(run_dir)
    route_decisions = read_jsonl(run_dir / "routing" / "decisions.jsonl")
    routing_failures = sorted((run_dir / "routing" / "failures").glob("*.json")) if (run_dir / "routing" / "failures").is_dir() else []
    workflow = manifest.get("workflow") or state.get("workflow") or "unknown-workflow"
    task_id = manifest.get("task_id") or state.get("task_id") or run_dir.parent.name
    run_id = manifest.get("run_id") or state.get("run_id") or run_dir.name
    verdict = validation.get("status") or state.get("status") or failure.get("status") or "unknown"
    hook_policy = validation.get("hook_policy") or {}
    write_activity = validation.get("write_activity") or {}
    write_coverage = validation.get("model_write_coverage") or {}
    step_review = validation.get("step_validation") or read_json(run_dir / "validation" / "step-validation.json", {}) or {}
    seal_status = verify_seal(run_dir)
    aggregate_html = render_aggregate_findings(run_dir)
    blocked = [event for event in events if event.get("blocked") is True]
    stage_events = [event for event in events if event.get("type") in {"state", "pi_stage_start", "pi_stage_end", "verification", "failure", "plan_reused"}]
    tool_events = [event for event in events if str(event.get("type", "")).startswith("tool_")]

    required_steps = manifest.get("required_steps") or []
    step_payloads = []
    for index, name in enumerate(required_steps, start=1):
        payload = read_json(run_dir / "stages" / "task-steps" / f"{index:02d}.json", {}) or {}
        reported = payload.get("status") or "pending"
        verified = (
            reported == "verified"
            or payload.get("mechanically_verified") is True
            or (reported == "done" and validation.get("status") == "passed")
        )
        model_validation = payload.get("model_validation") or {}
        step_payloads.append({
            "index": index,
            "name": name,
            "status": "verified" if verified else reported,
            "evidence": payload.get("evidence"),
            "model_validation": model_validation,
        })
    verified_steps = sum(step["status"] == "verified" for step in step_payloads)

    seen_stages = {str(event.get("stage")) for event in stage_events if event.get("stage")}
    artifact_stages = {
        "intake": run_dir / "stages" / "intake.json",
        "plan": run_dir / "stages" / "plan.json",
        "approval": run_dir / "stages" / "approval.json",
        "execute": run_dir / "stages" / "execute.json",
        "judge": run_dir / "stages" / "judge.json",
        "report": run_dir / "final_report.md",
    }
    seen_stages.update(stage for stage, path in artifact_stages.items() if path.exists())
    if any((run_dir / "validation").glob("attempt-*.json")):
        seen_stages.add("verify")
    if any((run_dir / "stages").glob("repair-*.json")):
        seen_stages.add("repair")
    lifecycle = []
    default_stages = ["intake", "plan", "approval", "execute", "verify", "repair", "judge", "report"]
    declared_stages = manifest.get("stages")
    ordered_stages = declared_stages if isinstance(declared_stages, list) and declared_stages and all(isinstance(item, str) for item in declared_stages) else default_stages
    stage_labels = {
        "resolve": "Resolve scorer",
        "fetch": "Fetch traces",
        "review-25": "Review 25 traces",
        "mechanical-verify": "Verify reviews",
        "aggregate": "Aggregate findings",
        "report": "Report",
        **(manifest.get("stage_labels") if isinstance(manifest.get("stage_labels"), dict) else {}),
    }
    current_stage = str(state.get("stage") or "")
    terminal_pass = str(verdict).lower() in GOOD
    current_index = ordered_stages.index(current_stage) if current_stage in ordered_stages else None
    routed_stages = {str(item.get("from")) for item in route_decisions} | {str(item.get("to")) for item in route_decisions}
    for index, stage in enumerate(ordered_stages):
        if terminal_pass and not route_decisions:
            stage_status = "passed"
        elif route_decisions and stage not in routed_stages:
            stage_status = "skipped"
        elif stage == "repair" and stage not in seen_stages:
            stage_status = "skipped"
        elif stage == current_stage and verdict in BAD:
            stage_status = "failed"
        elif stage == current_stage and verdict in {"blocked", "waiting", "running"}:
            stage_status = verdict
        elif route_decisions and stage in routed_stages:
            stage_status = "passed"
        elif current_index is not None and index < current_index:
            stage_status = "passed"
        elif stage not in seen_stages:
            stage_status = "pending"
        else:
            stage_status = "passed"
        label = stage_labels.get(stage) or stage.replace("-", " ")
        lifecycle.append(f'<div class="life-step {status_class(stage_status)}"><span class="life-dot"></span><span>{esc(label)}</span></div>')

    problems = []
    if failure:
        problems.append(f"{failure.get('error_type', 'failure')}: {failure.get('error', '')}")
    for item in hook_policy.get("blocked", []):
        problems.append(f"{item.get('tool')} blocked in {item.get('stage')}: {item.get('reason')}")
    for root in write_coverage.get("missing_roots", []):
        problems.append(f"Missing model write coverage for {root}")
    for failure_path in routing_failures:
        route_failure = read_json(failure_path, {}) or {}
        problems.append(f"Routing failed at {route_failure.get('from')}: {route_failure.get('error')}")
    if seal_status == "invalid":
        problems.append("The evidence seal is invalid; a sealed artifact changed or disappeared.")

    step_cards = []
    for step in step_payloads:
        root_cause_step = render_root_cause_step(run_dir, step["index"])
        if root_cause_step:
            root_summary, root_body = root_cause_step
            step_cards.append(
                f'<details class="step audit-step root-cause-step"><summary><span class="step-num">{step["index"]:02d}</span>'
                f'{root_summary}</summary>{root_body}</details>'
            )
            continue
        audit_step = render_audit_step(run_dir, step["index"])
        if audit_step:
            audit_summary, audit_body = audit_step
            step_cards.append(
                f'<details class="step audit-step"><summary><span class="step-num">{step["index"]:02d}</span>'
                f'{audit_summary}</summary>{audit_body}</details>'
            )
            continue
        model_validation = step.get("model_validation") or {}
        luna = ""
        if model_validation:
            guidance = model_validation.get("guidance") or []
            model_label = model_validation.get("model_label") or "Luna"
            luna = (
                f'<div class="luna">{esc(model_label)} · {esc(model_validation.get("score", "—"))}/10 · {esc(model_validation.get("review") or "reviewed")}</div>'
                + (f'<div class="guidance">Guidance · {esc(" · ".join(str(item) for item in guidance))}</div>' if guidance else "")
            )
        step_cards.append(
            f'<details class="step"><summary><span class="step-num">{step["index"]:02d}</span>'
            f'{dot(step["status"], step["name"])}</summary><div class="evidence">{esc(step["evidence"] or "No evidence recorded yet.")}{luna}</div></details>'
        )

    recent = []
    for event in [*stage_events, *tool_events][-14:]:
        event_status = "failed" if event.get("blocked") is True or event.get("isError") is True else "passed"
        detail = event.get("reason") or event.get("path") or event.get("status") or event.get("next_action") or event.get("toolName") or event.get("type")
        recent.append(
            f'<li>{dot(event_status, "")}<span class="event-name">{esc(event.get("stage") or "system")} · {esc(event.get("type"))}</span>'
            f'<span class="event-detail">{esc(detail)}</span><time>{esc(event.get("timestamp"))}</time></li>'
        )

    models = manifest.get("models") or {}
    model_summary = " · ".join(f"{key}: {value}" for key, value in models.items())
    steps_state = "passed" if required_steps and verified_steps == len(required_steps) else "pending" if required_steps else "passed"
    checks = [
        ("Verifier", validation.get("mechanical_verification"), validation.get("mechanical_verification") or "pending"),
        ("Criteria", criteria_validation.get("status"), f"{criteria_validation.get('passed', 0)} / {criteria_validation.get('total', len(manifest.get('acceptance_criteria') or []))}"),
        ("Hooks", "passed" if not blocked else "failed", f"{len(blocked)} blocked"),
        ("Writes", write_coverage.get("status") or write_activity.get("status"), write_activity.get("status") or "pending"),
        ("Steps", steps_state, f"{verified_steps} / {len(required_steps)}"),
        (step_review.get("label") or "Step review", step_review.get("status"), f"{step_review.get('accepted', 0)} / {step_review.get('required', len(required_steps))}" if step_review.get("enabled") else "off"),
        ("Judge", validation.get("judge_accepted"), f"{validation.get('judge_score', '—')} / 10"),
        ("Seal", seal_status, seal_status),
    ]
    if route_decisions or routing_failures:
        last_route = route_decisions[-1] if route_decisions else {}
        checks.insert(1, ("Route", "failed" if routing_failures else "passed", f"{last_route.get('from', '—')} → {last_route.get('to', '—')}"))
    check_html = "".join(f'<div class="check"><span>{esc(name)}</span>{dot(status, label)}</div>' for name, status, label in checks)
    criterion_cards = []
    for index, criterion in enumerate(criteria_validation.get("criteria") or manifest.get("acceptance_criteria") or [], start=1):
        if not isinstance(criterion, dict):
            criterion = {"id": f"criterion-{index}", "description": str(criterion), "status": "pending", "evidence": []}
        evidence = criterion.get("evidence") or []
        evidence_text = " · ".join(
            f"{item.get('verifier_id')}: {'passed' if item.get('passed') else 'failed'} ({item.get('artifact')})"
            for item in evidence if isinstance(item, dict)
        ) or "No criterion-level verifier evidence recorded yet."
        criterion_cards.append(
            f'<details class="step"><summary><span class="step-num">C{index:02d}</span>'
            f'{dot(criterion.get("status") or "pending", criterion.get("description") or criterion.get("id"))}</summary>'
            f'<div class="evidence"><b>{esc(criterion.get("id"))}</b> · {esc(evidence_text)}</div></details>'
        )
    live_html = ""
    if stream.get("active"):
        live_html = (
            '<div class="panel live"><span class="pulse"></span><strong>Live</strong>'
            f'<span>{esc(stream.get("stage"))}</span><span>{esc(stream.get("last_event"))}</span>'
            f'<span>{esc(stream.get("last_tool") or "model")}</span>'
            f'<span>{stream.get("text_characters", 0):,} output chars</span>'
            f'<span>{stream.get("tool_calls_started", 0)} tools</span></div>'
        )
    routing_html = ""
    if route_decisions:
        route_rows = "".join(
            f'<li>{dot("passed", "")}<span class="event-name">{esc(item.get("from"))} → {esc(item.get("to"))}</span>'
            f'<span class="event-detail">{esc(item.get("selected_transition_id"))}{" · default" if item.get("used_default") else ""}{" · " + esc(item.get("exhausted_reason")) if item.get("exhausted_reason") else ""}</span>'
            f'<time>#{esc(item.get("sequence"))}</time></li>'
            for item in route_decisions
        )
        routing_html = f'<details class="panel more" open><summary>Selected route · {len(route_decisions)} transitions</summary><ul>{route_rows}</ul></details>'
    refresh_meta = '<meta http-equiv="refresh" content="2">' if stream.get("active") else ""

    html_text = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{refresh_meta}
<title>{esc(task_id)} · {esc(workflow)}</title>
<style>
:root{{--bg:#f4f5f7;--panel:#fff;--ink:#15171a;--muted:#747983;--line:#e4e6ea;--soft:#f8f9fa;--ok:#22a447;--bad:#dc3c3c;--warn:#d69216}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1040px;margin:auto;padding:28px 22px 44px}} a{{color:inherit;text-decoration:none}} a:hover{{text-decoration:underline}}
.topbar{{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:22px}} .crumb{{color:var(--muted)}} .crumb strong{{color:var(--ink);font-weight:600}}
.back{{padding:7px 10px;border:1px solid var(--line);border-radius:7px;background:var(--panel)}}
.hero{{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:18px}} .eyebrow{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}}
h1{{font-size:24px;line-height:1.2;letter-spacing:-.025em;margin:0}} .runid{{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);margin-top:7px}}
.verdict{{font-size:14px;font-weight:650;padding-top:6px}} .panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px}}
.checks{{display:grid;grid-template-columns:repeat(8,1fr);margin-bottom:12px;overflow:hidden}} .check{{padding:11px 12px;border-left:1px solid var(--line)}} .check:first-child{{border-left:0}}
.check>span:first-child{{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}} .dotline{{display:inline-flex;align-items:center;gap:7px;min-width:0}}
.dot{{width:8px;height:8px;border-radius:50%;flex:0 0 8px;background:var(--warn)}} .dot.ok{{background:var(--ok)}} .dot.bad{{background:var(--bad)}}
.lifecycle{{display:grid;grid-template-columns:repeat(var(--stage-count),1fr);padding:12px;margin-bottom:12px}} .life-step{{position:relative;text-align:center;color:var(--muted);font-size:11px}}
.life-step:before{{content:"";position:absolute;top:5px;left:-50%;right:50%;height:1px;background:var(--line)}} .life-step:first-child:before{{display:none}}
.life-dot{{position:relative;z-index:1;display:block;width:10px;height:10px;border-radius:50%;background:var(--warn);margin:0 auto 6px;box-shadow:0 0 0 4px var(--panel)}}
.life-step.ok .life-dot{{background:var(--ok)}} .life-step.bad .life-dot{{background:var(--bad)}} .section-head{{display:flex;justify-content:space-between;align-items:baseline;margin:20px 0 8px}}
.life-step.neutral .life-dot{{background:#c6c9cf}}
h2{{font-size:13px;margin:0}} .section-note{{color:var(--muted);font-size:12px}} .steps{{display:grid;grid-template-columns:1fr;gap:7px}}
.step{{background:var(--panel);border:1px solid var(--line);border-radius:8px}} .step>summary{{display:grid;grid-template-columns:30px 1fr auto;align-items:center;gap:8px;padding:9px 10px;cursor:pointer;list-style:none}}
.step summary::-webkit-details-marker{{display:none}} .step-num{{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted)}} .evidence{{border-top:1px solid var(--line);padding:9px 40px;color:var(--muted);background:var(--soft)}}
.luna{{margin-top:8px;color:var(--ink)}}.guidance{{margin-top:3px;color:var(--warn)}}
.audit-summary{{display:block;min-width:0}} .audit-summary-top{{display:flex;align-items:center;gap:12px;min-width:0}} .audit-summary strong{{font-weight:600;white-space:nowrap}} .outcome-flow{{font:11px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);white-space:nowrap}} .audit-preview{{display:block;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}} .verified-note{{color:var(--muted);font-size:11px;text-align:right;white-space:nowrap}}
.audit-body{{border-top:1px solid var(--line);padding:14px 16px 16px;background:var(--soft)}} .decision-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
.decision-grid>div{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:11px}} .decision-grid span,.trace-meta{{display:block;color:var(--muted);font-size:11px}} .decision-grid strong{{display:block;margin:4px 0}} .decision-grid strong.ok{{color:var(--ok)}} .decision-grid strong.bad{{color:var(--bad)}} .decision-grid p{{margin:0;color:var(--muted)}}
.ratings{{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}} .ratings span{{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:5px 9px;color:var(--muted)}} .ratings b{{color:var(--ink)}}
.audit-body h3{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:14px 0 6px}} blockquote{{margin:6px 0;background:var(--panel);border-left:3px solid var(--line);padding:9px 11px}} .evidence-source{{display:block;color:var(--muted);font-size:10px;margin-bottom:4px}} blockquote code{{white-space:pre-wrap;color:var(--ink)}} blockquote p{{margin:5px 0 0;color:var(--muted)}}
.audit-columns{{display:grid;grid-template-columns:1fr 1fr;gap:16px}} .audit-body ul{{list-style:disc;margin:0;padding:0 0 0 18px}} .audit-body li{{display:list-item;border:0;padding:2px 0}} .audit-columns p{{margin:0}} .trace-meta{{margin-top:14px}}
.full-trace{{margin-top:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}} .full-trace>summary{{display:block;padding:9px 11px;cursor:pointer;color:var(--ink)}} .full-trace pre{{max-height:520px;overflow:auto;margin:0;border-top:1px solid var(--line);padding:12px;white-space:pre-wrap;word-break:break-word;font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:#101215;color:#e8eaed}} .full-trace a{{display:inline-block;margin:9px 0 9px 11px;text-decoration:underline;color:var(--muted)}}
.aggregate-findings{{padding:14px}} .metric-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}} .metric-grid>div{{border:1px solid var(--line);border-radius:8px;padding:9px;background:var(--soft)}} .metric-grid span{{display:block;color:var(--muted);font-size:11px}} .metric-grid strong{{display:block;font-size:17px;margin-top:2px}} .aggregate-summary{{font-size:14px;line-height:1.55;margin:14px 2px}}
.finding-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}} .finding-group{{border:1px solid var(--line);border-radius:8px;background:var(--soft)}} .finding-group>summary{{padding:9px 10px;cursor:pointer;font-weight:600}} .finding-group ul{{list-style:disc;margin:0;padding:0 14px 10px 28px}} .finding-group li{{display:list-item;border:0;padding:3px 0}} .raw-link{{display:inline-block;margin-top:12px;color:var(--muted);text-decoration:underline}}
.root-cause-callout{{border-left:3px solid var(--bad);background:var(--panel);padding:10px 12px}} .root-cause-callout p{{margin:4px 0 0}} .fix-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}} .fix-card{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}} .fix-card>span{{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}} .fix-card>strong{{display:block;margin:4px 0}} .fix-card p{{margin:0 0 6px}} .fix-card small{{color:var(--muted)}}
.attention{{padding:11px 12px;margin-top:12px;border-left:3px solid var(--bad);color:var(--bad)}} .all-clear{{padding:10px 12px;margin-top:12px;color:var(--muted)}}
.live{{display:flex;align-items:center;gap:12px;padding:9px 12px;margin-bottom:12px;color:var(--muted)}}.live strong{{color:var(--ink)}}.pulse{{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 rgba(34,164,71,.4);animation:pulse 1.5s infinite}}@keyframes pulse{{70%{{box-shadow:0 0 0 7px rgba(34,164,71,0)}}100%{{box-shadow:0 0 0 0 rgba(34,164,71,0)}}}}
.more{{margin-top:12px}} .more>summary{{cursor:pointer;padding:11px 12px;color:var(--muted)}} ul{{list-style:none;margin:0;padding:0 12px 8px}}
li{{display:grid;grid-template-columns:16px 180px 1fr auto;gap:7px;padding:7px 0;border-top:1px solid var(--line)}} .event-detail,time{{color:var(--muted)}} time{{font-size:11px}}
.meta{{padding:0 12px 12px;color:var(--muted);overflow-wrap:anywhere}} .meta div{{padding-top:5px}} .meta b{{color:var(--ink);font-weight:500}}
@media(max-width:780px){{main{{padding:18px 14px}}.checks{{grid-template-columns:repeat(2,1fr)}}.check:nth-child(3n+1){{border-left:1px solid var(--line)}}.check:nth-child(odd){{border-left:0}}.steps{{grid-template-columns:1fr}}.lifecycle{{overflow:auto;grid-template-columns:repeat(var(--stage-count),90px)}}li{{grid-template-columns:16px 1fr}}.event-detail,time{{grid-column:2}}.hero{{display:block}}.verdict{{margin-top:10px}}.decision-grid,.audit-columns,.finding-grid,.fix-grid{{grid-template-columns:1fr}}.metric-grid{{grid-template-columns:repeat(2,1fr)}}.verified-note{{display:none}}.audit-summary-top{{align-items:flex-start;flex-direction:column;gap:3px}}.outcome-flow{{white-space:normal}}}}
</style></head><body><main>
<nav class="topbar"><div class="crumb"><strong>{esc(workflow)}</strong> / run detail</div><a class="back" href="../../index.html">← All runs</a></nav>
<section class="hero"><div><div class="eyebrow">Task</div><h1>{esc(task_id)}</h1><div class="runid">{esc(run_id)}</div></div><div class="verdict">{dot(verdict, str(verdict).upper())}</div></section>
{live_html}
<section class="panel checks">{check_html}</section>
<section class="panel lifecycle" style="--stage-count:{len(ordered_stages)}" aria-label="Workflow lifecycle">{''.join(lifecycle)}</section>
{routing_html}
{f'<div class="panel attention">{"<br>".join(esc(item) for item in problems)}</div>' if problems else '<div class="panel all-clear">No failed checks, blocked tools, or missing write coverage.</div>'}
{aggregate_html}
<div class="section-head"><h2>Acceptance criteria</h2><span class="section-note">{criteria_validation.get('passed', 0)} of {criteria_validation.get('total', len(manifest.get('acceptance_criteria') or []))} mechanically verified</span></div>
<section class="steps">{''.join(criterion_cards) if criterion_cards else '<div class="panel all-clear">Criterion evidence is pending.</div>'}</section>
<div class="section-head"><h2>Required steps</h2><span class="section-note">{verified_steps} of {len(required_steps)} verified</span></div>
<section class="steps">{''.join(step_cards) if step_cards else '<div class="panel all-clear">This workflow has no required-step contract.</div>'}</section>
<details class="panel more"><summary>Recent activity · {len(events)} events</summary><ul>{''.join(recent) or '<li>No events recorded.</li>'}</ul></details>
<details class="panel more"><summary>Run metadata</summary><div class="meta"><div><b>Run path</b> · {esc(run_dir)}</div><div><b>Updated</b> · {esc(state.get('updated_at'))}</div><div><b>Models</b> · {esc(model_summary)}</div><div><b>Spec</b> · {esc(manifest.get('spec_sha256'))}</div><div><b>Implementation</b> · {esc(manifest.get('implementation_digest'))}</div></div></details>
</main></body></html>"""
    out = run_dir / "tracker.html"
    out.write_text(html_text)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Harness run directory")
    args = parser.parse_args()
    print(render(Path(args.run).expanduser().resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
