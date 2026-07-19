#!/usr/bin/env python3
"""Regression test for certify_workflow.pi_invocation_guard_findings().

Companion to test_pi_protocol_verdict.py. That test proves run_pi_harness.py's
shared run_verifiers()/run_supervisor_commands() fail closed on a zero exit
code paired with an aborted/errored Pi JSONL stop reason. But
certify_workflow.py's "pi_protocol_regression" gate only re-runs that test
against the shared library -- it never inspects the harness actually being
certified. A `specialized` runtime supplies its own entrypoint and is free to
shell out to Pi with its own subprocess + JSONL-parsing code (as
competitor-analysis and product-planning both do in this repo). If a
specialized harness's own reimplementation ever stopped checking stopReason,
certification would still report "passed" because pi_protocol_regression never
looks at that harness's code.

pi_invocation_guard_findings() closes that gap: for `specialized` runtimes, it
statically scans every script for a direct Pi subprocess invocation and
requires it to either import the shared fail-closed helpers or contain its own
explicit aborted/error stopReason check.

Run directly: python3 test_pi_invocation_guard.py
Exits 0 and prints "OK" on success; raises AssertionError and exits nonzero on
any regression.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

CERTIFY = Path(__file__).resolve().parent / "certify_workflow.py"


def load_certify():
    spec = importlib.util.spec_from_file_location("certify_workflow", CERTIFY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["certify_workflow"] = mod
    spec.loader.exec_module(mod)
    return mod


UNGUARDED_REIMPLEMENTATION = '''
import subprocess

def call_pi(prompt):
    proc = subprocess.Popen([self.pi, "--mode", "json", prompt], stdout=subprocess.PIPE)
    stdout, _ = proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"pi exited {proc.returncode}")
    return extract_json(stdout)
'''

GUARDED_VIA_SHARED_HELPER = '''
import subprocess
from run_pi_harness import validate_pi_event_stream

def call_pi(prompt):
    proc = subprocess.Popen([self.pi, "--mode", "json", prompt], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"pi exited {proc.returncode}")
    return validate_pi_event_stream(stdout, stderr)
'''

GUARDED_VIA_OWN_CHECK = '''
import subprocess

def assistant_text(raw):
    for line in raw.splitlines():
        message = json.loads(line).get("message") or {}
        if message.get("stopReason") in {"error", "aborted"}:
            raise ValueError("Pi call failed: " + str(message.get("stopReason")))
    return final

def call_pi(prompt):
    proc = subprocess.Popen([self.pi, "--mode", "json", prompt], stdout=subprocess.PIPE)
    stdout, _ = proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"pi exited {proc.returncode}")
    return assistant_text(stdout)
'''

NO_PI_INVOCATION_AT_ALL = '''
import subprocess

def run_pytest():
    proc = subprocess.run(["pytest"], capture_output=True)
    return proc.returncode == 0
'''


def write_harness(root: Path, label: str, script_name: str, body: str) -> Path:
    harness = root / f"harness-{label}"
    scripts = harness / "scripts"
    scripts.mkdir(parents=True)
    (scripts / script_name).write_text(body)
    return harness


def main() -> int:
    mod = load_certify()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Specialized + direct Pi invocation + no guard at all: must be flagged.
        bad = write_harness(tmp_path, "unguarded", "run.py", UNGUARDED_REIMPLEMENTATION)
        findings = mod.pi_invocation_guard_findings(bad, {"kind": "specialized"})
        assert len(findings) == 1, f"expected exactly one finding for unguarded reimplementation, got {findings}"
        assert findings[0]["file"] == "scripts/run.py"

        # Specialized + direct Pi invocation + imports the shared fail-closed
        # helper (this is exactly what competitor-analysis/common.py does):
        # must NOT be flagged.
        good_shared = write_harness(tmp_path, "guarded-shared", "common.py", GUARDED_VIA_SHARED_HELPER)
        assert mod.pi_invocation_guard_findings(good_shared, {"kind": "specialized"}) == []

        # Specialized + direct Pi invocation + its own explicit aborted/error
        # stopReason check (this is exactly what product-planning/run.py's
        # assistant_text() does): must NOT be flagged.
        good_own = write_harness(tmp_path, "guarded-own-check", "run.py", GUARDED_VIA_OWN_CHECK)
        assert mod.pi_invocation_guard_findings(good_own, {"kind": "specialized"}) == []

        # No Pi invocation at all (an ordinary pytest wrapper): never flagged,
        # regardless of runtime kind.
        no_pi = write_harness(tmp_path, "no-pi-invocation", "verify.py", NO_PI_INVOCATION_AT_ALL)
        assert mod.pi_invocation_guard_findings(no_pi, {"kind": "specialized"}) == []

        # generic_mutation runtimes are out of scope for this gate even if the
        # (synthetic, contrived) script content looks unguarded -- they always
        # route through the shared, already-tested run_pi_harness.py.
        generic = write_harness(tmp_path, "generic-mutation-scope", "run.py", UNGUARDED_REIMPLEMENTATION)
        assert mod.pi_invocation_guard_findings(generic, {"kind": "generic_mutation"}) == []

    print("OK: specialized_pi_invocation_guard flags unguarded direct Pi calls and leaves guarded/non-specialized code alone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
