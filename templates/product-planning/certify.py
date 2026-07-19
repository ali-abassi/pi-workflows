#!/usr/bin/env python3
"""Run the deterministic fixture certification for a compiled planning harness."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args()
    harness = Path(args.harness).expanduser().resolve()
    static = json.loads((harness / "static-certification.json").read_text()) if (harness / "static-certification.json").is_file() else {"status": "missing"}
    with tempfile.TemporaryDirectory(prefix="product-planning-cert-") as temporary:
        output = Path(temporary) / "runs"
        run = subprocess.run([
            "python3", str(harness / "scripts/run.py"), "--backend", "fixture",
            "--idea", str(harness / "examples/idea.json"), "--run-id", "specialized-certification",
            "--output-root", str(output), "--max-improvement-rounds", "1", "--max-model-calls", "128",
        ], text=True, capture_output=True, check=False, timeout=120)
        run_dir = Path(run.stdout.strip()) if run.returncode == 0 else None
        verification = subprocess.run(
            ["python3", str(harness / "scripts/run.py"), "--verify-run", str(run_dir)],
            text=True, capture_output=True, check=False, timeout=30,
        ) if run_dir else None
        report = {
            "schema_version": "1.0", "status": "passed" if static.get("status") == "passed" and run.returncode == 0 and verification and verification.returncode == 0 else "failed",
            "static_status": static.get("status"),
            "fixture_exit_code": run.returncode, "fixture_stderr": run.stderr[-2000:],
            "verification_exit_code": verification.returncode if verification else None,
            "verification_output": verification.stdout[-2000:] if verification else None,
        }
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
