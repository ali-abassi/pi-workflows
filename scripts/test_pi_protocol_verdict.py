#!/usr/bin/env python3
"""Regression test for the mechanical verify/execute stages accepting a zero
exit code even when a command's stdout is a genuine Pi JSON-protocol event
stream that recorded a non-"stop" outcome (aborted, errored, failed retry,
malformed mid-stream, or an extension error).

Bug this guards: run_verifiers() (the "verify" stage) and
run_supervisor_commands() (execute-stage supervisor commands) used to judge
every verification_contracts/execution_commands subprocess purely on
`proc.returncode == 0`. A command whose stdout is itself Pi's JSONL protocol
(an LLM-judge-style verifier, or a wrapper that shells out to `pi --mode
json`) can exit 0 even though the underlying model turn's final stopReason
was "aborted" rather than "stop" -- Pi's CLI does not itself fail the process
in that case. That let a mechanically "passed" verify stage launder a run
whose real evidence was an aborted model turn.

Fix: pi_jsonl_verdict() inspects a command's stdout, and when it unambiguously
looks like Pi protocol output (a confirmed assistant message_end event),
applies the same fail-closed rules already used for run_pi()-driven stages
(non-"stop" stopReason, extension_error, failed auto_retry, malformed JSONL
line all fail it) regardless of the process exit code. Ordinary verifier
commands (pytest, domain assert scripts, shell tests) that never emit Pi
protocol are left on exit-code-only semantics -- pi_jsonl_verdict returns
None for them and callers fall back unchanged.

Run directly: python3 test_pi_protocol_verdict.py
Exits 0 and prints "OK" on success; raises AssertionError and exits nonzero
on any regression.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RUNNER = Path(__file__).resolve().parent / "run_pi_harness.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_pi_harness", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_pi_harness"] = mod
    spec.loader.exec_module(mod)
    return mod


def write_script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def main() -> int:
    mod = load_runner()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        settled = "print(json.dumps({'type': 'agent_settled'}))\n"
        scripts = {
            "plain_pass.py": "import sys; print('all checks passed'); sys.exit(0)\n",
            "plain_fail.py": "import sys; print('assertion failed', file=sys.stderr); sys.exit(1)\n",
            "genuine_pass.py": (
                "import json, sys\n"
                "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}}))\n"
                + settled
                + "sys.exit(0)\n"
            ),
            "aborted_but_exit0.py": (
                "import json, sys\n"
                "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'aborted'}}))\n"
                + settled
                + "sys.exit(0)\n"
            ),
            "malformed_mid_exit0.py": (
                "import json, sys\n"
                "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}}))\n"
                "print('not json at all')\n"
                + settled
                + "sys.exit(0)\n"
            ),
            "extension_error_exit0.py": (
                "import json, sys\n"
                "print(json.dumps({'type': 'extension_error', 'detail': 'boom'}))\n"
                "print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}}))\n"
                + settled
                + "sys.exit(0)\n"
            ),
        }
        paths = {name: write_script(tmp_path, name, body) for name, body in scripts.items()}

        def verify_case(name: str, script: str, expect_passed: bool) -> None:
            run_dir = tmp_path / f"run-{name}"
            spec = {
                "workdir": str(workdir),
                "acceptance_criteria": [{"id": "c1", "text": "x"}],
                "verification_contracts": [
                    {"id": "v1", "covers": ["c1"], "command": ["python3", str(paths[script])], "timeout_seconds": 30}
                ],
                "required_steps": [],
            }
            result = mod.run_verifiers(spec, run_dir, 0, {"completed_steps": []}, {"files": []})
            actual = result["verifiers"][0]["passed"]
            assert actual == expect_passed, (
                f"run_verifiers[{name}]: expected passed={expect_passed}, got {actual} "
                f"(exit_code={result['verifiers'][0]['exit_code']}, "
                f"pi_protocol={result['verifiers'][0]['pi_protocol']})"
            )
            assert result["status"] == ("passed" if expect_passed else "failed")

        # Ordinary (non-Pi) verifier commands: unaffected, judged on exit code alone.
        verify_case("plain-pass", "plain_pass.py", True)
        verify_case("plain-fail", "plain_fail.py", False)

        # Genuine Pi protocol output with stopReason == "stop": still passes.
        verify_case("genuine-pi-stop", "genuine_pass.py", True)

        # The reported bug: exit code 0, but the Pi JSONL final stop reason is
        # "aborted". Must now fail closed instead of being silently accepted.
        verify_case("aborted-exit0", "aborted_but_exit0.py", False)

        # Malformed JSONL after a confirmed Pi signal, and an extension_error
        # event -- both must fail closed even with exit code 0.
        verify_case("malformed-mid-exit0", "malformed_mid_exit0.py", False)
        verify_case("extension-error-exit0", "extension_error_exit0.py", False)

        # Same fail-closed behavior must hold for execute-stage supervisor
        # commands, and the loop must stop at the first non-passed command.
        run_dir = tmp_path / "run-supervisor"
        spec = {
            "workdir": str(workdir),
            "execution_commands": [
                ["python3", str(paths["aborted_but_exit0.py"])],
                ["python3", str(paths["plain_pass.py"])],
            ],
        }
        result = mod.run_supervisor_commands(spec, run_dir)
        assert result["status"] == "failed", "execution_commands must fail closed on an aborted Pi stop reason"
        assert len(result["commands"]) == 1, "must short-circuit at the first non-passed command"

        # Direct unit coverage of the verdict helper itself.
        assert mod.pi_jsonl_verdict("") is None
        assert mod.pi_jsonl_verdict("not json\nplain text\n") is None
        ok = mod.pi_jsonl_verdict(
            "\n".join([
                json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}),
                json.dumps({"type": "agent_settled"}),
            ])
        )
        assert ok is not None and ok["passed"] is True and ok["is_pi_protocol"] is True
        aborted = mod.pi_jsonl_verdict(
            "\n".join([
                json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "aborted"}}),
                json.dumps({"type": "agent_settled"}),
            ])
        )
        assert aborted is not None and aborted["passed"] is False and aborted["stop_reason"] == "aborted"

    print("OK: verify/execute stages fail closed on aborted/malformed/extension-error Pi JSONL even with exit code 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
