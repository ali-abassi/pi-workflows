#!/usr/bin/env python3
"""Sync one Pi harness run into a durable SQLite ledger and aggregate HTML tracker."""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from contextlib import contextmanager


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_timestamp(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def dot(status: Any, label: Any = None) -> str:
    lowered = str(status or "unknown").lower()
    cls = "ok" if lowered in {"passed", "done", "verified"} else "bad" if lowered in {"failed", "error", "invalid"} else "warn"
    return f'<span class="dotline"><span class="dot {cls}"></span>{esc(label if label is not None else status)}</span>'


def harness_from_run(run_dir: Path) -> Path:
    # <harness>/runs/<task_id>/<run_id>
    if run_dir.parent.parent.name != "runs":
        raise SystemExit(f"run path does not match <harness>/runs/<task>/<run>: {run_dir}")
    return run_dir.parent.parent.parent


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
          workflow TEXT NOT NULL,
          task_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          run_path TEXT NOT NULL,
          started_at TEXT,
          updated_at TEXT NOT NULL,
          stage TEXT,
          status TEXT NOT NULL,
          next_action TEXT,
          approval_status TEXT,
          spec_sha256 TEXT,
          input_snapshot_digest TEXT,
          mechanical_verification TEXT,
          judge_accepted INTEGER,
          judge_score REAL,
          repair_attempts INTEGER DEFAULT 0,
          steps_verified INTEGER DEFAULT 0,
          steps_total INTEGER DEFAULT 0,
          steps_reviewed INTEGER DEFAULT 0,
          step_review_average REAL,
          step_review_status TEXT,
          blocked_count INTEGER DEFAULT 0,
          tool_calls INTEGER DEFAULT 0,
          seal_status TEXT,
          error TEXT,
          PRIMARY KEY (workflow, task_id, run_id)
        );
        CREATE INDEX IF NOT EXISTS runs_updated_at ON runs(updated_at DESC);
        CREATE INDEX IF NOT EXISTS runs_task_id ON runs(task_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS steps (
          workflow TEXT NOT NULL,
          task_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          step_index INTEGER NOT NULL,
          name TEXT NOT NULL,
          status TEXT NOT NULL,
          evidence TEXT,
          mechanically_verified INTEGER NOT NULL DEFAULT 0,
          artifact_path TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (workflow, task_id, run_id, step_index)
        );
        CREATE INDEX IF NOT EXISTS steps_run ON steps(workflow, task_id, run_id, step_index);
        CREATE TABLE IF NOT EXISTS registry_metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    migrations = {
        "seal_status": "TEXT",
        "steps_reviewed": "INTEGER DEFAULT 0",
        "step_review_average": "REAL",
        "step_review_status": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
    return connection


def bind_registry(connection: sqlite3.Connection, workflow: str) -> None:
    existing = connection.execute("SELECT value FROM registry_metadata WHERE key='workflow'").fetchone()
    if existing and existing[0] != workflow:
        raise SystemExit(f"registry is bound to workflow {existing[0]!r}, not {workflow!r}")
    foreign = [row[0] for row in connection.execute("SELECT DISTINCT workflow FROM runs WHERE workflow != ?", (workflow,))]
    if foreign:
        raise SystemExit(f"registry contains foreign workflow rows: {', '.join(foreign)}")
    connection.execute(
        "INSERT INTO registry_metadata(key,value) VALUES('workflow',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (workflow,),
    )
    connection.commit()


@contextmanager
def registry_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_record(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = read_json(run_dir / "manifest.json", {}) or {}
    state = read_json(run_dir / "state.json", {}) or {}
    validation = read_json(run_dir / "validation" / "final_validation.json", {}) or {}
    failure = read_json(run_dir / "failure.json", {}) or {}
    hook_policy = validation.get("hook_policy") or {}
    step_review = validation.get("step_validation") or read_json(run_dir / "validation" / "step-validation.json", {}) or {}
    if not step_review:
        accepted_records = [read_json(path, {}) or {} for path in sorted((run_dir / "step-validation").glob("*/accepted.json"))] if (run_dir / "step-validation").is_dir() else []
        scores = [record.get("review", {}).get("score") for record in accepted_records]
        scores = [float(score) for score in scores if isinstance(score, (int, float))]
        step_review = {
            "status": "running" if accepted_records else "disabled",
            "accepted": sum(record.get("validator_accepted") is True for record in accepted_records),
            "average_score": round(sum(scores) / len(scores), 2) if scores else None,
        }
    seal_status = verify_seal(run_dir)
    status = validation.get("status") or state.get("status") or failure.get("status") or "unknown"
    required_steps = manifest.get("required_steps") or []
    steps = []
    for index, name in enumerate(required_steps, start=1):
        artifact = run_dir / "stages" / "task-steps" / f"{index:02d}.json"
        payload = read_json(artifact, {}) or {}
        step_status = payload.get("status") or "pending"
        verified = (
            step_status == "verified"
            or payload.get("mechanically_verified") is True
            or (step_status == "done" and validation.get("status") == "passed")
        )
        steps.append({
            "index": index,
            "name": name,
            "status": "verified" if verified else step_status,
            "evidence": payload.get("evidence"),
            "mechanically_verified": verified,
            "artifact_path": str(artifact),
        })
    record = {
        "workflow": manifest.get("workflow") or state.get("workflow") or "unknown",
        "task_id": manifest.get("task_id") or state.get("task_id") or run_dir.parent.name,
        "run_id": manifest.get("run_id") or state.get("run_id") or run_dir.name,
        "run_path": str(run_dir),
        "started_at": manifest.get("started_at") or (run_dir.stat().st_mtime_ns and datetime.fromtimestamp(run_dir.stat().st_ctime, timezone.utc).isoformat()),
        "updated_at": state.get("updated_at") or validation.get("checked_at") or utc_now(),
        "stage": state.get("stage"),
        "status": status,
        "next_action": state.get("next_action"),
        "approval_status": (manifest.get("approval") or {}).get("status"),
        "spec_sha256": manifest.get("spec_sha256"),
        "input_snapshot_digest": manifest.get("input_snapshot_digest"),
        "mechanical_verification": validation.get("mechanical_verification"),
        "judge_accepted": validation.get("judge_accepted"),
        "judge_score": validation.get("judge_score"),
        "repair_attempts": validation.get("repair_attempts", 0),
        "steps_verified": sum(1 for step in steps if step["mechanically_verified"]),
        "steps_total": len(required_steps),
        "steps_reviewed": step_review.get("accepted", 0),
        "step_review_average": step_review.get("average_score"),
        "step_review_status": step_review.get("status"),
        "blocked_count": hook_policy.get("blocked_count", 0),
        "tool_calls": hook_policy.get("tool_calls", 0),
        "seal_status": seal_status,
        "error": failure.get("error"),
    }
    return record, steps


def verify_seal(run_dir: Path) -> str:
    seal = read_json(run_dir / "integrity" / "run-seal.json", {}) or {}
    artifacts = seal.get("artifacts")
    if not isinstance(artifacts, list):
        return "unsealed"
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return "invalid"
        path = run_dir / item["path"]
        if not path.is_file():
            return "invalid"
        import hashlib
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
            return "invalid"
    return "verified"


def sync_run(connection: sqlite3.Connection, record: dict[str, Any], steps: list[dict[str, Any]], expected_workflow: str) -> None:
    if record["workflow"] != expected_workflow:
        raise SystemExit(
            f"refusing to mix workflow {record['workflow']!r} into tracker for {expected_workflow!r}: {record['run_path']}"
        )
    columns = list(record)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column not in {"workflow", "task_id", "run_id"})
    connection.execute(
        f"INSERT INTO runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT(workflow, task_id, run_id) DO UPDATE SET {updates}",
        [record[column] for column in columns],
    )
    connection.execute(
        "DELETE FROM steps WHERE workflow=? AND task_id=? AND run_id=?",
        (record["workflow"], record["task_id"], record["run_id"]),
    )
    for step in steps:
        connection.execute(
            """INSERT INTO steps
               (workflow, task_id, run_id, step_index, name, status, evidence,
                mechanically_verified, artifact_path, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["workflow"], record["task_id"], record["run_id"], step["index"],
                step["name"], step["status"], step["evidence"], int(step["mechanically_verified"]),
                step["artifact_path"], record["updated_at"],
            ),
        )
    connection.commit()


def dashboard_data(connection: sqlite3.Connection, workflow: str) -> dict[str, Any]:
    totals = dict(connection.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) AS passed,
                  SUM(CASE WHEN status IN ('failed','error') THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS gated,
                  SUM(CASE WHEN status NOT IN ('passed','failed','blocked','error') THEN 1 ELSE 0 END) AS active,
                  SUM(steps_verified) AS steps_verified,
                  SUM(steps_total) AS steps_total,
                  SUM(steps_reviewed) AS steps_reviewed,
                  AVG(step_review_average) AS step_review_average
           FROM runs WHERE workflow=?""",
        (workflow,),
    ).fetchone())
    tasks = [dict(row) for row in connection.execute(
        """SELECT task_id, COUNT(*) AS runs,
                  SUM(CASE WHEN status='passed' THEN 1 ELSE 0 END) AS passed,
                  SUM(CASE WHEN status IN ('failed','error') THEN 1 ELSE 0 END) AS failed,
                  MAX(updated_at) AS last_updated
           FROM runs WHERE workflow=? GROUP BY task_id ORDER BY last_updated DESC""",
        (workflow,),
    )]
    runs = [dict(row) for row in connection.execute(
        "SELECT * FROM runs WHERE workflow=? ORDER BY updated_at DESC LIMIT 2000",
        (workflow,),
    )]
    totals = {key: value or 0 for key, value in totals.items()}
    totals["success_rate"] = round(100 * totals["passed"] / totals["total"], 1) if totals["total"] else 0
    return {"generated_at": utc_now(), "workflow": workflow, "totals": totals, "tasks": tasks, "runs": runs}


def render_dashboard(harness: Path, data: dict[str, Any]) -> str:
    workflow = data["workflow"]
    totals = data["totals"]
    task_rows = "".join(
        f"<tr><td><strong>{esc(row['task_id'])}</strong></td><td>{row['runs']}</td><td>{dot('passed' if row['passed'] else 'unknown', row['passed'])}</td>"
        f"<td>{dot('failed' if row['failed'] else 'passed', row['failed'])}</td><td class='muted'>{esc(format_timestamp(row['last_updated']))}</td></tr>"
        for row in data["tasks"]
    )
    run_rows = []
    runs_root = harness / "runs"
    for row in data["runs"]:
        tracker = Path(row["run_path"]) / "tracker.html"
        try:
            href = tracker.relative_to(runs_root).as_posix()
        except ValueError:
            href = "#"
        steps_total = row["steps_total"] or 0
        steps_verified = row["steps_verified"] or 0
        progress = round(100 * steps_verified / steps_total) if steps_total else 0
        steps_html = (
            f'<div class="progress"><span style="width:{progress}%"></span></div><small>{steps_verified}/{steps_total}</small>'
            if steps_total else '<span class="muted">—</span>'
        )
        review_html = (
            dot(row["step_review_status"], f"{row['step_review_average']}/10")
            if row["step_review_average"] is not None else '<span class="muted">—</span>'
        )
        run_rows.append(
            f'<tr data-status="{esc(row["status"])}" data-search="{esc((row["task_id"] + " " + row["run_id"] + " " + row["status"] + " " + (row["stage"] or "")).lower())}">'
            f'<td class="signal">{dot(row["status"], "")}</td><td><a class="task" href="{esc(href)}">{esc(row["task_id"])}</a><div class="runid">{esc(row["run_id"])}</div></td>'
            f'<td>{esc(row["stage"])}</td><td>{esc(row["status"])}</td><td class="steps-cell">{steps_html}</td><td>{review_html}</td>'
            f'<td>{dot(row["seal_status"], row["seal_status"])}</td><td class="muted updated">{esc(format_timestamp(row["updated_at"]))}</td></tr>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(workflow)} · workflow tracker</title>
<style>
:root{{--bg:#f4f5f7;--panel:#fff;--ink:#15171a;--muted:#747983;--line:#e4e6ea;--soft:#f8f9fa;--ok:#22a447;--bad:#dc3c3c;--warn:#d69216}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1180px;margin:auto;padding:30px 22px 44px}} header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:22px}}
.eyebrow{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:5px}} h1{{font-size:26px;line-height:1.2;letter-spacing:-.025em;margin:0}}
.scope{{display:inline-flex;align-items:center;gap:7px;margin-top:9px;color:var(--muted)}} .scope .dot{{background:var(--ok)}} .generated{{font-size:11px;color:var(--muted);padding-top:7px}}
.metrics{{display:grid;grid-template-columns:repeat(6,1fr);overflow:hidden;margin-bottom:12px}} .metric{{padding:12px 14px;border-left:1px solid var(--line)}}.metric:first-child{{border-left:0}}
.label{{font-size:11px;color:var(--muted)}} .value{{font-size:20px;line-height:1.25;margin-top:3px}} .panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px}}
.toolbar{{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:11px 12px;border-bottom:1px solid var(--line)}} .filters{{display:flex;gap:5px;flex-wrap:wrap}}
button{{border:1px solid transparent;border-radius:7px;background:transparent;color:var(--muted);padding:6px 9px;cursor:pointer}}button:hover{{background:var(--soft)}}button.active{{background:var(--ink);color:#fff}}
input{{width:min(320px,100%);border:1px solid var(--line);border-radius:7px;padding:7px 9px;background:#fff;color:var(--ink)}}
.muted{{color:var(--muted)}} .dotline{{display:inline-flex;align-items:center;gap:7px}} .dot{{width:8px;height:8px;border-radius:50%;background:var(--warn);flex:0 0 8px}}
.dot.ok{{background:var(--ok)}} .dot.bad{{background:var(--bad)}} .wide{{overflow:auto}} table{{width:100%;border-collapse:collapse}} th,td{{padding:9px 10px;border-top:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:var(--soft)}} tbody tr:hover{{background:#fafafa}} .signal{{width:24px;padding-right:0}}
a{{color:inherit;text-decoration:none}}a:hover{{text-decoration:underline}} .task{{font-weight:600}} .runid{{font:10px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);margin-top:2px}}
.steps-cell{{min-width:110px}} .progress{{width:72px;height:4px;border-radius:4px;background:var(--line);overflow:hidden;display:inline-block;margin-right:7px;vertical-align:middle}}.progress span{{display:block;height:100%;background:var(--ok)}}small{{color:var(--muted)}}
.updated{{font-size:11px}} details{{margin-top:12px;padding:11px 12px}} summary{{cursor:pointer;color:var(--muted)}} details table{{margin-top:9px}}
@media(max-width:760px){{main{{padding:18px 14px}}header{{display:block}}.generated{{margin-top:8px}}.metrics{{grid-template-columns:repeat(2,1fr)}}.metric{{border-top:1px solid var(--line)}}.toolbar{{align-items:stretch;flex-direction:column}}input{{width:100%}}}}
</style></head><body><main>
<header><div><div class="eyebrow">Workflow tracker</div><h1>{esc(workflow)}</h1><div class="scope"><span class="dot"></span>Isolated ledger · only {esc(workflow)} runs are admitted</div></div><div class="generated">Updated {esc(format_timestamp(data['generated_at']))}</div></header>
<section class="panel metrics">
<div class="metric"><div class="label">Runs</div><div class="value">{totals['total']}</div></div>
<div class="metric"><div class="label">Passed</div><div class="value">{dot('passed', totals['passed'])}</div></div>
<div class="metric"><div class="label">Failed</div><div class="value">{dot('failed' if totals['failed'] else 'passed', totals['failed'])}</div></div>
<div class="metric"><div class="label">Gated / active</div><div class="value">{dot('unknown', totals['gated'] + totals['active'])}</div></div>
<div class="metric"><div class="label">Verified steps</div><div class="value">{totals['steps_verified']} / {totals['steps_total']}</div></div>
<div class="metric"><div class="label">Steps reviewed</div><div class="value">{totals['steps_reviewed']}</div></div>
</section>
<section class="panel"><div class="toolbar"><div class="filters"><button class="active" data-filter="all">All {totals['total']}</button><button data-filter="passed">Passed {totals['passed']}</button><button data-filter="failed">Failed {totals['failed']}</button><button data-filter="blocked">Gated {totals['gated']}</button></div><input id="search" placeholder="Search task or run" aria-label="Search runs"></div><div class="wide"><table><thead><tr><th></th><th>Task / run</th><th>Stage</th><th>Status</th><th>Verified steps</th><th>Step review</th><th>Evidence seal</th><th>Updated</th></tr></thead><tbody id="runs">{''.join(run_rows)}</tbody></table></div></section>
<details class="panel"><summary>Task totals · {len(data['tasks'])} tasks</summary><div class="wide"><table><thead><tr><th>Task</th><th>Runs</th><th>Passed</th><th>Failed</th><th>Latest</th></tr></thead><tbody>{task_rows}</tbody></table></div></details>
<script>let status='all';const search=document.getElementById('search'),rows=[...document.querySelectorAll('#runs tr')];function apply(){{const q=search.value.toLowerCase();rows.forEach(r=>r.hidden=!r.dataset.search.includes(q)||(status!=='all'&&r.dataset.status!==status))}}search.addEventListener('input',apply);document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{{document.querySelector('[data-filter].active').classList.remove('active');b.classList.add('active');status=b.dataset.filter;apply()}}));</script>
</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run", help="Harness run directory to sync")
    target.add_argument("--harness", help="Harness root; sync every existing run")
    args = parser.parse_args()
    if args.run:
        run_dirs = [Path(args.run).expanduser().resolve()]
        harness = harness_from_run(run_dirs[0])
    else:
        harness = Path(args.harness).expanduser().resolve()
        run_dirs = sorted(path for path in (harness / "runs").glob("*/*") if (path / "manifest.json").is_file())
    harness_config = read_json(harness / "harness.json", {}) or {}
    expected_workflow = harness_config.get("workflow")
    if not isinstance(expected_workflow, str) or not expected_workflow:
        raise SystemExit(f"harness.json must declare a non-empty workflow name: {harness}")
    database = harness / "runs" / "harness.sqlite3"
    with registry_lock(harness / "runs" / ".registry.lock"):
        with connect(database) as connection:
            bind_registry(connection, expected_workflow)
            for run_dir in run_dirs:
                record, steps = run_record(run_dir)
                sync_run(connection, record, steps, expected_workflow)
            data = dashboard_data(connection, expected_workflow)
        atomic_write(harness / "runs" / "index.json", json.dumps(data, indent=2, sort_keys=True) + "\n")
        atomic_write(harness / "runs" / "index.html", render_dashboard(harness, data))
    print(harness / "runs" / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
