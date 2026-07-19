from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "serve_workflow.py"


class WorkflowUiTests(unittest.TestCase):
    def test_studio_runs_the_canonical_engine_and_returns_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workflow = Path(raw)
            steps = workflow / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1,
                "workflow": "ui-proof",
                "input": {"required": True, "description": "Name to preserve"},
                "steps": [{
                    "id": "copy",
                    "cmd": 'cat "$INPUT"',
                    "gate": 'test -s "$OUT"',
                }],
            }, sort_keys=False), encoding="utf-8")
            env = {**os.environ, "PI_WORKFLOWS_ROOTS": raw, "LOOPS_PORT": "1"}
            process = subprocess.Popen(
                [sys.executable, str(SERVER), str(steps), "--port", "0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                assert process.stdout is not None
                line = process.stdout.readline().strip()
                match = re.search(r"(http://127\.0\.0\.1:\d+)$", line)
                self.assertIsNotNone(match, line)
                base = match.group(1)
                with urllib.request.urlopen(base, timeout=5) as response:
                    page = response.read().decode()
                    self.assertIn("Pi Workflows Studio", page)
                    self.assertIn("ui-proof", page)
                    self.assertIn("Content-Security-Policy", response.headers)
                token = json.loads(re.search(
                    r'<script id="piw-boot" type="application/json">(.*?)</script>', page, re.S,
                ).group(1))["token"]

                request = urllib.request.Request(
                    f"{base}/api/run",
                    data=json.dumps({"content": "Ada"}).encode(),
                    headers={"Content-Type": "application/json", "X-Piw-Token": token},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    session = json.loads(response.read())["session"]

                payload = {}
                for _ in range(80):
                    with urllib.request.urlopen(f"{base}/api/status?session={session}&after=0", timeout=5) as response:
                        payload = json.loads(response.read())
                    if payload.get("done"):
                        break
                    time.sleep(0.05)
                self.assertTrue(payload.get("done"), payload)
                self.assertEqual(payload["exit"], 0, payload.get("error"))
                self.assertEqual(payload["output"], "Ada")
                self.assertTrue(payload["detail"]["run"]["ok"])
                self.assertIn("run_end", {event["t"] for event in payload["events"]})
                with urllib.request.urlopen(base, timeout=5) as response:
                    restored = response.read().decode()
                self.assertIn('"latest":{"detail"', restored)
                self.assertIn('"output":"Ada"', restored)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
