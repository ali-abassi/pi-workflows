#!/usr/bin/env python3
"""Run a deterministic workflow over N inputs — the "do this task 500 times" mode.

Usage:
  python3 run_batch.py steps.yaml --inputs inputs.jsonl --input-file idea.md
  python3 run_batch.py steps.yaml --inputs inputs/ --input-file brief.md --parallel 3

Inputs:
  - .jsonl file: one {"id": "...", "content": "..."} per line, or
  - a directory: every *.txt/*.md file is one item (id = filename stem), or
  - .txt file: one item per non-empty line (id = line number).

Each item gets an isolated work dir (batch-<ts>/items/<id>/) with a copy of the
workflow yaml, the item content written to --input-file, and the workflow's
cache/ dir symlinked in (shared across items: identical prompts are free).
Emits batch-report.md + batch.json: per item pass/fail, QA verdict, tokens,
cost, wall seconds; plus totals. Exit 0 only if every item passed.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

RUNNER = Path(__file__).parent / "run_steps.py"


def load_items(src: Path) -> list[dict]:
    if src.is_dir():
        return [{"id": f.stem, "content": f.read_text()}
                for f in sorted(src.iterdir()) if f.suffix in (".txt", ".md")]
    if src.suffix == ".jsonl":
        items = []
        for i, line in enumerate(src.read_text().splitlines()):
            if line.strip():
                obj = json.loads(line)
                items.append({"id": str(obj.get("id", i + 1)), "content": obj["content"]})
        return items
    return [{"id": str(i + 1), "content": line}
            for i, line in enumerate(src.read_text().splitlines()) if line.strip()]


def run_item(steps_yaml: Path, item: dict, batch_dir: Path, input_file: str,
             extra_args: list[str], model: str | None = None) -> dict:
    work = batch_dir / "items" / re.sub(r"[^A-Za-z0-9._-]", "_", item["id"])
    work.mkdir(parents=True, exist_ok=True)
    yaml_copy = work / steps_yaml.name
    text = steps_yaml.read_text()
    if model:  # eval mode: override the top-level default model only
        text = re.sub(r"(?m)^model:.*$", f"model: {model}", text, count=1)
    yaml_copy.write_text(text)
    (work / input_file).write_text(item["content"])
    shared_cache = steps_yaml.parent / "cache"
    shared_cache.mkdir(exist_ok=True)
    link = work / "cache"
    if not link.exists():
        link.symlink_to(shared_cache)
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, str(RUNNER), yaml_copy.name, *extra_args],
                          cwd=work, text=True, capture_output=True, check=False)
    wall = time.monotonic() - t0
    (work / "run.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    result = {"id": item["id"], "exit": proc.returncode, "passed": proc.returncode == 0,
              "wall_s": round(wall, 1), "tokens": 0, "cost": 0.0, "qa": None,
              "judge_scores": [float(s) for s in re.findall(r"judge score ([0-9.]+)", proc.stdout)],
              "dir": str(work)}
    runs = sorted((work / "runs").glob("*/ledger.json")) if (work / "runs").exists() else []
    if runs:
        ledger = json.loads(runs[-1].read_text())
        result["tokens"] = sum(e.get("total", 0) for e in ledger)
        result["cost"] = round(sum(e.get("cost", 0.0) for e in ledger), 4)
        qa_file = runs[-1].parent / "qa.md"
        if qa_file.exists():
            m = re.search(r'"verdict"\s*:\s*"(pass|fail)"', qa_file.read_text())
            result["qa"] = m.group(1) if m else "unparseable"
    return result


def write_report(results: list[dict], batch_dir: Path, label: str) -> None:
    (batch_dir / "batch.json").write_text(json.dumps(results, indent=1))
    ok = [r for r in results if r["passed"]]
    lines = [f"# Batch report — {label}",
             f"{len(ok)}/{len(results)} passed · total ${sum(r['cost'] for r in results):.4f} · "
             f"{sum(r['tokens'] for r in results)} tok · wall sum {sum(r['wall_s'] for r in results):.0f}s", "",
             "| item | passed | QA | judge scores | tokens | cost | wall |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | {r['qa'] or '-'} | "
                     f"{','.join(map(str, r['judge_scores'])) or '-'} | {r['tokens']} | ${r['cost']:.4f} | {r['wall_s']}s |")
    (batch_dir / "batch-report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch-run a deterministic workflow")
    ap.add_argument("steps_file", type=Path)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--input-file", required=True, help="filename each item's content is written to")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--out", type=Path, help="batch output dir")
    ap.add_argument("--limit", type=int, help="run only the first N items")
    args, extra = ap.parse_known_args()

    items = load_items(args.inputs)
    if args.limit:
        items = items[:args.limit]
    if not items:
        raise SystemExit("no items found")
    batch_dir = (args.out or args.steps_file.parent /
                 f"batch-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}").resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    print(f"batch: {len(items)} item(s) · parallel={args.parallel} · dir={batch_dir}", flush=True)

    results = []
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = {pool.submit(run_item, args.steps_file.resolve(), item, batch_dir,
                            args.input_file, extra): item["id"] for item in items}
        for fut in cf.as_completed(futs):
            r = fut.result()
            results.append(r)
            print(f"  {r['id']}: {'PASS' if r['passed'] else 'FAIL'} · ${r['cost']:.4f} · {r['wall_s']}s", flush=True)
    results.sort(key=lambda r: r["id"])
    write_report(results, batch_dir, args.steps_file.name)
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
