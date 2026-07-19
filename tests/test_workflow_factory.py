#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path


SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from certify_workflow import run_command as run_certification_command, static_gates  # noqa: E402
from compile_workflow import compile_blueprint  # noqa: E402
from draft_workflow_blueprint import assistant_text, extract_object  # noqa: E402
from evaluate_transition import TransitionError, canonical_digest as transition_digest, evaluate_transition, graph_errors  # noqa: E402
from run_replay_corpus import cost_from_events  # noqa: E402
from render_harness_tracker import render as render_tracker  # noqa: E402
from run_pi_harness import acquire_workspace_lock, build_pi_stage_command, clean_allowed_write_paths, criterion_results, load_approved_plan, resolve_stage_tools, run_verifiers, seal_and_render, stage_completed, stage_result_from_events, terminate_process_group, validate_pi_event_stream, validate_resolved_path_policy, verify_run_seal, write_run_seal  # noqa: E402
from validate_peer_exchange import validate_exchange  # noqa: E402
from workflow_factory_common import GENERIC_MUTATION_LIFECYCLE, PI_COMPATIBILITY, assert_supported_pi_version, bounded_pi_json_flags, prepare_pi_runtime_dir, validate_blueprint  # noqa: E402


class WorkflowFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.blueprint = json.loads((SKILL / "examples" / "workflow-blueprint.json").read_text())
        self.blueprint["workflow"] = "factory-unit-test"

    def write_blueprint(self, root: Path, blueprint: dict | None = None) -> Path:
        path = root / "blueprint.json"
        path.write_text(json.dumps(blueprint or self.blueprint))
        return path

    def write_approval_run(self, root: Path) -> tuple[Path, Path]:
        harness = root / "harness"
        run = harness / "runs" / "fixture-task" / "approval-run"
        plan = run / "stages" / "plan.json"
        plan.parent.mkdir(parents=True)
        plan.write_text(json.dumps({
            "summary": "approved plan", "steps": [], "files_expected_to_change": [],
            "risks": [], "verification_mapping": [],
        }) + "\n")
        (run / "manifest.json").write_text(json.dumps({
            "workflow": "fixture-workflow", "task_id": "fixture-task", "spec_sha256": "spec",
            "input_snapshot_digest": "inputs", "implementation_digest": "implementation",
        }) + "\n")
        (run / "state.json").write_text(json.dumps({"stage": "approval", "status": "blocked"}) + "\n")
        (run / "stages" / "approval.json").write_text(json.dumps({
            "status": "not_approved", "spec_sha256": "spec", "input_snapshot_digest": "inputs",
            "implementation_digest": "implementation", "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        }) + "\n")
        write_run_seal(run)
        return harness, plan

    def test_compiles_versioned_bundle_and_passes_static_certification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            harness = compile_blueprint(self.write_blueprint(repo), repo)
            gates, blueprint, task = static_gates(harness)
            self.assertTrue(all(item["status"] == "passed" for item in gates), gates)
            self.assertEqual(blueprint["version"], "1.0.0")
            self.assertEqual(task["objective_contract"], self.blueprint["objective_contract"])
            self.assertEqual(task["acceptance_criteria"], self.blueprint["acceptance_criteria"])
            self.assertEqual(task["verification_contracts"][0]["id"], "verify-result")
            self.assertEqual(task["verification_contracts"][0]["covers"], ["result-exists", "result-uppercase"])
            self.assertEqual(task["allowed_tools"], ["read", "edit", "write"])
            self.assertEqual(task["step_validation"], {"enabled": False})
            self.assertEqual(json.loads((harness / "harness.json").read_text())["stage_capabilities"]["judge"], {"tools": []})
            self.assertTrue((harness / "examples" / "workspace" / "input.txt").is_file())

    def test_blueprint_model_pins_compile_and_certify(self) -> None:
        value = copy.deepcopy(self.blueprint)
        value["models"] = {
            role: "openai/gpt-5.3-codex"
            for role in ("intake", "plan", "execute", "repair", "judge")
        }
        self.assertEqual(validate_blueprint(value), [])
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            harness = compile_blueprint(self.write_blueprint(repo, value), repo)
            config = json.loads((harness / "harness.json").read_text())
            gates, _, _ = static_gates(harness)
            self.assertEqual(config["models"], value["models"])
            self.assertTrue(all(item["status"] == "passed" for item in gates), gates)
        del value["models"]["judge"]
        self.assertIn(
            "models must contain exactly intake, plan, execute, repair, and judge",
            validate_blueprint(value),
        )
    def test_stage_capabilities_are_validated_and_enforced(self) -> None:
        value = copy.deepcopy(self.blueprint)
        value["stage_capabilities"] = {
            "intake": {"tools": []},
            "plan": {"tools": []},
            "execute": {"tools": ["read", "write"]},
            "repair": {"tools": ["read", "write"]},
            "judge": {"tools": []},
        }
        self.assertEqual(validate_blueprint(value), [])
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            harness = compile_blueprint(self.write_blueprint(repo, value), repo)
            config = json.loads((harness / "harness.json").read_text())
            task = json.loads((harness / "examples" / "task.json").read_text())
            self.assertEqual(task["allowed_tools"], ["read", "write"])
            self.assertEqual(resolve_stage_tools(config, "execute", ["read"]), ["read"])
            with self.assertRaisesRegex(SystemExit, "outside the compiled execute"):
                resolve_stage_tools(config, "execute", ["edit"])
        value["stage_capabilities"]["unknown"] = {"tools": []}
        self.assertTrue(any("undeclared stage" in error for error in validate_blueprint(value)))

    def test_peer_collaboration_compiles_and_validates_correlated_exchange(self) -> None:
        blueprint = json.loads((SKILL / "examples" / "pr-review-peer-blueprint.json").read_text())
        self.assertEqual(validate_blueprint(blueprint), [])
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            harness = compile_blueprint(self.write_blueprint(repo, blueprint), repo)
            runtime = harness / "scripts" / "run.py"
            runtime.write_text("#!/usr/bin/env python3\n")
            gates, _, _ = static_gates(harness)
            self.assertTrue(all(item["status"] == "passed" for item in gates), gates)
            config = json.loads((harness / "harness.json").read_text())
            contract = json.loads((harness / "peer-collaboration.json").read_text())
            self.assertTrue(config["peer_collaboration"]["enabled"])
            self.assertEqual(contract["required_responses"], 2)
            request = {
                "schema_version": "1.0", "message_id": "request-1", "correlation_id": "job-1",
                "stage": "review", "sender": "controller", "recipient": "security-reviewer", "hop": 0,
                "sent_at": "2026-07-12T12:00:00Z", "payload": {"artifact": "pr.json"},
            }
            response = {
                "schema_version": "1.0", "message_id": "response-1", "correlation_id": "job-1",
                "in_reply_to": "request-1", "stage": "review", "sender": "security-reviewer",
                "recipient": "controller", "hop": 0, "completed_at": "2026-07-12T12:00:01Z",
                "status": "completed", "summary": "One finding", "findings": [{
                    "id": "auth-1", "severity": "high", "claim": "Authorization is missing",
                    "evidence": [{"source": "pr.json", "locator": "src/auth.ts:12"}],
                }], "error": None,
            }
            receipt = validate_exchange(contract, request, response, len(json.dumps(response).encode()))
            self.assertEqual((receipt["status"], receipt["finding_count"]), ("completed", 1))
            response["sender"] = "correctness-reviewer"
            with self.assertRaisesRegex(ValueError, "reverse the request route"):
                validate_exchange(contract, request, response, len(json.dumps(response).encode()))

    def test_peer_collaboration_rejects_unbounded_or_shared_auth_topologies(self) -> None:
        value = json.loads((SKILL / "examples" / "pr-review-peer-blueprint.json").read_text())
        value["peer_collaboration"]["transport"] = {
            "kind": "http_sse", "authentication": "shared_bearer", "endpoint_env": "PI_PEER_URL",
        }
        self.assertTrue(any("per_agent_token or mtls" in error for error in validate_blueprint(value)))
        value = json.loads((SKILL / "examples" / "pr-review-peer-blueprint.json").read_text())
        value["peer_collaboration"]["limits"]["max_messages"] = 1000
        self.assertTrue(any("max_messages" in error for error in validate_blueprint(value)))

    def test_rejects_missing_verifier_coverage(self) -> None:
        value = copy.deepcopy(self.blueprint)
        value["verifiers"][0]["covers"] = ["result-exists"]
        errors = validate_blueprint(value)
        self.assertTrue(any("lack verifier coverage" in item for item in errors), errors)

    def test_rejects_unsafe_paths(self) -> None:
        value = copy.deepcopy(self.blueprint)
        value["task_template"]["allowed_write_paths"] = ["../escape.txt"]
        errors = validate_blueprint(value)
        self.assertTrue(any("safe relative path" in item for item in errors), errors)

    def test_rejects_write_ancestor_of_immutable_input(self) -> None:
        value = copy.deepcopy(self.blueprint)
        value["task_template"]["fixture_files"] = {"data/input.txt": "do not delete\n"}
        value["task_template"]["inputs"] = ["data/input.txt"]
        value["task_template"]["immutable_paths"] = ["data/input.txt"]
        value["task_template"]["allowed_write_paths"] = ["data"]
        errors = validate_blueprint(value)
        self.assertTrue(any("overlap" in item for item in errors), errors)

    def test_cleanup_refuses_overlap_without_deleting_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            immutable = workdir / "data" / "input.txt"
            immutable.parent.mkdir()
            immutable.write_text("do not delete\n")
            with self.assertRaises(SystemExit):
                clean_allowed_write_paths(workdir, ["data"], ["data/input.txt"])
            self.assertEqual(immutable.read_text(), "do not delete\n")

    def test_cleanup_refuses_symlink_alias_to_immutable_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary)
            immutable = workdir / "data" / "input.txt"
            immutable.parent.mkdir()
            immutable.write_text("do not delete\n")
            (workdir / "alias").symlink_to(workdir / "data", target_is_directory=True)
            spec = {"inputs": ["data/input.txt"], "immutable_paths": ["data/input.txt"], "allowed_write_paths": ["alias"]}
            with self.assertRaises(SystemExit):
                validate_resolved_path_policy(workdir, spec)
            with self.assertRaises(SystemExit):
                clean_allowed_write_paths(workdir, ["alias"], ["data/input.txt"])
            self.assertEqual(immutable.read_text(), "do not delete\n")

    def test_rejects_unexecutable_generic_lifecycle(self) -> None:
        value = copy.deepcopy(self.blueprint)
        value["lifecycle"] = list(reversed(GENERIC_MUTATION_LIFECYCLE))
        errors = validate_blueprint(value)
        self.assertTrue(any("exactly match" in item for item in errors), errors)

    def test_criterion_results_retain_verifier_provenance(self) -> None:
        spec = {"acceptance_criteria": self.blueprint["acceptance_criteria"]}
        verification = {
            "attempt": 0,
            "verifiers": [{"index": 1, "id": "verify-result", "covers": ["result-exists", "result-uppercase"], "passed": True, "exit_code": 0}],
        }
        result = criterion_results(spec, verification)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["passed"], 2)
        self.assertEqual(result["criteria"][0]["evidence"][0]["verifier_id"], "verify-result")

    def test_run_seal_rejects_tampered_approval_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            plan = run / "stages" / "plan.json"
            plan.parent.mkdir()
            plan.write_text('{"summary":"approved"}\n')
            write_run_seal(run)
            self.assertTrue(verify_run_seal(run))
            plan.write_text('{"summary":"tampered"}\n')
            self.assertFalse(verify_run_seal(run))

    def test_run_seal_rejects_partial_inventory_and_added_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            write_run_seal(run)
            self.assertFalse(verify_run_seal(run))
            (run / "manifest.json").write_text("{}\n")
            write_run_seal(run)
            seal = run / "integrity" / "run-seal.json"
            seal.write_text(json.dumps({"artifact_count": 0, "artifacts": [], "digest": hashlib.sha256(b"[]").hexdigest()}) + "\n")
            self.assertFalse(verify_run_seal(run))
            write_run_seal(run)
            (run / "unsealed-evidence.json").write_text("{}\n")
            self.assertFalse(verify_run_seal(run))
            (run / "unsealed-evidence.json").unlink()
            write_run_seal(run)
            (run / "tracker.html").write_text("derived but authoritative at seal time\n")
            self.assertFalse(verify_run_seal(run))

    def test_terminal_tracker_is_bound_into_final_seal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "harness" / "runs" / "task" / "run"
            run.mkdir(parents=True)
            (run.parent.parent.parent / "harness.json").write_text(json.dumps({"workflow": "fixture"}) + "\n")
            (run / "manifest.json").write_text(json.dumps({"workflow": "fixture", "task_id": "task", "run_id": "run"}) + "\n")
            seal_and_render(run)
            self.assertTrue((run / "tracker.html").is_file())
            self.assertTrue(verify_run_seal(run))

    def test_approved_plan_requires_canonical_sealed_blocked_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness, plan = self.write_approval_run(Path(temporary))
            loaded = load_approved_plan(
                approved_plan_path=plan, harness=harness, workflow="fixture-workflow", task_id="fixture-task",
                spec_digest="spec", input_digest="inputs", implementation_digest="implementation",
            )
            self.assertEqual(loaded["summary"], "approved plan")
            state = plan.parent.parent / "state.json"
            state.write_text(json.dumps({"stage": "approval", "status": "running"}) + "\n")
            write_run_seal(plan.parent.parent)
            with self.assertRaises(SystemExit):
                load_approved_plan(
                    approved_plan_path=plan, harness=harness, workflow="fixture-workflow", task_id="fixture-task",
                    spec_digest="spec", input_digest="inputs", implementation_digest="implementation",
                )
            with self.assertRaises(SystemExit):
                load_approved_plan(
                    approved_plan_path=plan, harness=harness, workflow="fixture-workflow", task_id="other-task",
                    spec_digest="spec", input_digest="inputs", implementation_digest="implementation",
                )

    def test_verifiers_receive_immutable_input_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary) / "workdir"
            run_dir = Path(temporary) / "run"
            workdir.mkdir()
            (run_dir / "validation").mkdir(parents=True)
            (run_dir / "tracker.jsonl").touch()
            source = workdir / "input.txt"
            source.write_text("source\n")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            spec = {
                "workdir": str(workdir),
                "inputs": ["input.txt"],
                "immutable_paths": ["input.txt"],
                "required_steps": [],
                "verification_contracts": [{
                    "id": "baseline-visible",
                    "covers": ["baseline-visible"],
                    "timeout_seconds": 10,
                    "command": ["python3", "-c", "import json,os; assert json.loads(os.environ['WORKFLOW_BASELINE_SHA256_JSON'])['input.txt'] == '" + digest + "'"],
                }],
            }
            result = run_verifiers(
                spec,
                run_dir,
                0,
                {"status": "completed", "completed_steps": []},
                {"files": [{"path": "input.txt", "sha256": digest, "size": source.stat().st_size}], "digest": digest},
            )
            self.assertEqual(result["status"], "passed")

    def test_pi_compatibility_is_bounded_and_runtime_flags_are_isolated(self) -> None:
        assert_supported_pi_version("0.80.5", PI_COMPATIBILITY)
        assert_supported_pi_version("0.80.7", PI_COMPATIBILITY)
        assert_supported_pi_version("0.80.10", PI_COMPATIBILITY)
        with self.assertRaises(ValueError):
            assert_supported_pi_version("0.80.4", PI_COMPATIBILITY)
        with self.assertRaises(ValueError):
            assert_supported_pi_version("0.80.11", PI_COMPATIBILITY)
        with self.assertRaises(ValueError):
            assert_supported_pi_version("0.80.6-beta.1", PI_COMPATIBILITY)
        flags = bounded_pi_json_flags("fixed")
        for flag in ("--no-approve", "--offline", "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-themes", "--system-prompt"):
            self.assertIn(flag, flags)
        assert_supported_pi_version("omp/17.0.0", PI_COMPATIBILITY, "/usr/local/bin/omp")
        with self.assertRaises(ValueError):
            assert_supported_pi_version("17.0.1", PI_COMPATIBILITY, "/usr/local/bin/omp")
        omp_flags = bounded_pi_json_flags("fixed", "/usr/local/bin/omp")
        for flag in ("--mode", "--no-session", "--no-extensions", "--no-skills", "--system-prompt"):
            self.assertIn(flag, omp_flags)
        for unsupported in ("--no-approve", "--offline", "--no-context-files", "--no-prompt-templates", "--no-themes"):
            self.assertNotIn(unsupported, omp_flags)

    def test_stage_command_pins_provider_model_thinking_and_result_tool(self) -> None:
        with patch("run_pi_harness.pi_path", return_value="/fake/pi"):
            command = build_pi_stage_command(
                "openai-codex/gpt-5.6-luna",
                "high",
                "Return the result.",
                ["read"],
            )
        self.assertEqual(command[command.index("--provider") + 1], "openai-codex")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertEqual(command[command.index("--thinking") + 1], "high")
        self.assertEqual(command[command.index("--tools") + 1], "read,submit_stage_result")
        self.assertEqual(command[-1], "Return the result.")

    def test_stage_result_tool_is_terminal(self) -> None:
        extension = (SKILL / "extensions" / "stage-result.ts").as_uri()
        script = f"""
process.env.HARNESS_STAGE = "intake";
const stageResult = (await import({json.dumps(extension)})).default;
let registered;
stageResult({{ registerTool(tool) {{ registered = tool; }} }});
if (!registered || registered.name !== "submit_stage_result" || registered.terminate !== true) {{
  throw new Error("stage result tool must terminate the turn");
}}
"""
        execution = subprocess.run(["bun", "-e", script], cwd=SKILL, text=True, capture_output=True, timeout=30)
        self.assertEqual(execution.returncode, 0, execution.stderr)

    def test_process_group_termination_escalates_after_grace_period(self) -> None:
        process = MagicMock()
        process.pid = 4242
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired(["pi"], 0.01), -signal.SIGKILL]
        with patch("run_pi_harness.os.killpg") as killpg:
            terminate_process_group(process, grace_seconds=0.01)
        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(4242, signal.SIGTERM),
                unittest.mock.call(4242, signal.SIGKILL),
            ],
        )

    def test_certification_timeout_terminates_the_process_group(self) -> None:
        process = MagicMock()
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["pi"], 1),
            ("partial output", "partial error"),
        ]
        with patch("certify_workflow.subprocess.Popen", return_value=process), patch(
            "certify_workflow.terminate_process_group"
        ) as terminate:
            result = run_certification_command(["pi"], Path("/tmp"), timeout=1)
        terminate.assert_called_once_with(process)
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr)

    def test_tracker_renders_criterion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "harness" / "runs" / "task" / "run"
            (run / "validation").mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"workflow": "fixture", "task_id": "task", "run_id": "run"}) + "\n")
            (run / "validation" / "criteria.json").write_text(json.dumps({
                "status": "passed", "passed": 1, "total": 1,
                "criteria": [{"id": "output-correct", "description": "Output is correct", "status": "passed", "evidence": [{"verifier_id": "verify-output", "passed": True, "artifact": "validation/attempt-0-command-1.json"}]}],
            }) + "\n")
            output = render_tracker(run)
            rendered = output.read_text()
            self.assertIn("Acceptance criteria", rendered)
            self.assertIn("output-correct", rendered)
            self.assertIn("verify-output: passed", rendered)

    def test_workspace_lock_refuses_second_mutating_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / "harness"
            workdir = root / "workspace"
            workdir.mkdir()
            lock = acquire_workspace_lock(harness, workdir)
            try:
                script = (
                    "import sys; from pathlib import Path; "
                    f"sys.path.insert(0, {str(SCRIPTS)!r}); "
                    "from run_pi_harness import acquire_workspace_lock; "
                    f"acquire_workspace_lock(Path({str(harness / 'other')!r}), Path({str(workdir)!r}))"
                )
                alternate_tmp = root / "alternate-tmp"
                alternate_tmp.mkdir()
                result = subprocess.run(
                    ["python3", "-c", script], text=True, capture_output=True, check=False,
                    env={**os.environ, "TMPDIR": str(alternate_tmp)},
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already owned", result.stderr)
            finally:
                lock.close()


class ConditionalTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.blueprint = json.loads((SKILL / "examples" / "pr-review-conditional-blueprint.json").read_text())
        self.graph = {"schema_version": "1.0", "workflow": self.blueprint["workflow"], "stages": self.blueprint["lifecycle"], **self.blueprint["control_flow"]}

    def write_stage(self, run: Path, stage: str, value: dict) -> None:
        path = run / "stages" / f"{stage}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n")

    def write_blueprint(self, root: Path, blueprint: dict | None = None) -> Path:
        path = root / "blueprint.json"
        path.write_text(json.dumps(blueprint or self.blueprint))
        return path

    def test_conditional_blueprint_and_graph_are_valid(self) -> None:
        self.assertEqual(validate_blueprint(self.blueprint), [])
        self.assertEqual(graph_errors(self.graph), [])

    def test_pi_protocol_and_global_runtime_policy_fail_closed(self) -> None:
        good = "\n".join([
            json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "{}"}]}}),
            json.dumps({"type": "agent_settled"}),
        ])
        self.assertEqual(validate_pi_event_stream(good, "")["stop_reason"], "stop")
        error = json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "provider failed"}})
        with self.assertRaises(RuntimeError):
            validate_pi_event_stream(error, "")
        with self.assertRaises(ValueError):
            validate_pi_event_stream("not-json", "")
        with self.assertRaises(RuntimeError):
            validate_pi_event_stream(good, "Extension error (tool_call): broken")
        for stop_reason in (None, "length", "toolUse", "mystery"):
            incomplete = json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": stop_reason, "content": [{"type": "text", "text": "{}"}]}})
            with self.assertRaises(RuntimeError):
                validate_pi_event_stream(incomplete, "")
        with self.assertRaisesRegex(RuntimeError, "agent_settled"):
            validate_pi_event_stream(good.splitlines()[0], "")
        runtime, policy = prepare_pi_runtime_dir("unit-test-runtime-policy")
        self.assertEqual(json.loads((runtime / "settings.json").read_text()), {key: value for key, value in policy.items() if key != "auth"})
        self.assertFalse((runtime / "models.json").exists())
        self.assertFalse(policy["retry"]["enabled"])
        self.assertFalse(policy["compaction"]["enabled"])

    def test_pi_protocol_accepts_tool_call_terminated_stage_result(self) -> None:
        tool_call_message = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [{"type": "toolCall", "toolName": "submit_stage_result"}],
            },
        })
        tool_execution_end = json.dumps({
            "type": "tool_execution_end",
            "toolName": "submit_stage_result",
            "isError": False,
            "result": {
                "details": {
                    "stage": "judge",
                    "submitted": True,
                    "accepted": True,
                    "score": 10,
                    "criteria": [],
                    "evidence": [],
                    "residual_risk": [],
                },
            },
        })
        settled = json.dumps({"type": "agent_settled"})
        event = "\n".join([tool_call_message, tool_execution_end, settled])
        protocol = validate_pi_event_stream(event, "", expected_stage="judge")
        self.assertEqual(protocol["stop_reason"], "toolUse")
        self.assertTrue(protocol["structured_result"])
        with self.assertRaises(RuntimeError):
            validate_pi_event_stream(event, "")

    def test_pi_protocol_rejects_model_pin_drift(self) -> None:
        message = {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "content": [{"type": "text", "text": "{}"}],
            },
        }
        event = "\n".join([json.dumps(message), json.dumps({"type": "agent_settled"})])
        validate_pi_event_stream(event, "", expected_model="openai-codex/gpt-5.6-luna")
        with self.assertRaisesRegex(RuntimeError, "model pin drifted"):
            validate_pi_event_stream(event, "", expected_model="openai-codex/gpt-5.6-sol")

    def test_pi_protocol_rejects_empty_final_assistant_message(self) -> None:
        prior = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "{\"accepted\": true}"}],
            },
        })
        empty_final = json.dumps({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [],
            },
        })
        settled = json.dumps({"type": "agent_settled"})
        with self.assertRaises(ValueError):
            validate_pi_event_stream(f"{prior}\n{empty_final}\n{settled}", "")

    def test_stage_result_preserves_judge_rejection(self) -> None:
        event = json.dumps({
            "type": "tool_execution_end",
            "toolName": "submit_stage_result",
            "isError": False,
            "result": {
                "details": {
                    "stage": "judge",
                    "submitted": True,
                    "accepted": False,
                    "score": 3,
                    "criteria": [],
                    "evidence": [],
                    "residual_risk": ["criterion failed"],
                },
            },
        })
        result = stage_result_from_events(event, "judge")
        self.assertFalse(result["accepted"])
        self.assertNotIn("submitted", result)

    def test_stage_result_accepts_numbered_repair_stage(self) -> None:
        event = json.dumps({
            "type": "tool_execution_end",
            "toolName": "submit_stage_result",
            "isError": False,
            "result": {
                "details": {
                    "stage": "repair",
                    "submitted": True,
                    "status": "completed",
                    "diagnosis": "fixed verifier failure",
                    "changes": ["updated result.txt"],
                    "residual_risk": [],
                    "completed_steps": ["repair"],
                },
            },
        })
        result = stage_result_from_events(event, "repair-1")
        self.assertEqual(result["status"], "completed")

    def test_stage_result_rejects_unexpected_stage(self) -> None:
        event = json.dumps({
            "type": "tool_execution_end",
            "toolName": "submit_stage_result",
            "isError": False,
            "result": {
                "details": {
                    "stage": "plan",
                    "submitted": True,
                },
            },
        })
        with self.assertRaisesRegex(ValueError, "expected 'execute', received 'plan'"):
            stage_result_from_events(event, "execute")

    def test_stage_result_requires_submission_marker(self) -> None:
        event = json.dumps({
            "type": "tool_execution_end",
            "toolName": "submit_stage_result",
            "result": {"details": {"stage": "judge", "accepted": True}},
        })
        with self.assertRaisesRegex(ValueError, "submitted result"):
            stage_result_from_events(event, "judge")

    def test_step_validator_rejects_incomplete_or_drifted_pi_streams(self) -> None:
        guard = (SKILL / "extensions" / "harness-guard.ts").as_uri()
        script = f"""
const {{ strictValidatorVerdict }} = await import({json.dumps(guard)});
const message = (stopReason, provider = "openai-codex", model = "gpt-5.6-luna") => JSON.stringify({{
  type: "message_end",
  message: {{
    role: "assistant",
    stopReason,
    provider,
    model,
    content: [{{ type: "text", text: '{{"accepted":true,"score":10,"review":"complete","guidance":[],"evidence_quality":"strong","confidence":1}}' }}],
  }},
}});
const settled = JSON.stringify({{ type: "agent_settled" }});
const expectFailure = (raw, pattern) => {{
  try {{
    strictValidatorVerdict(raw, "openai-codex", "gpt-5.6-luna");
    throw new Error("expected parser failure");
  }} catch (error) {{
    if (error.message === "expected parser failure" || !pattern.test(error.message)) throw error;
  }}
}};
const verdict = strictValidatorVerdict(`${{message("stop")}}\\n${{settled}}`, "openai-codex", "gpt-5.6-luna");
if (verdict.accepted !== true || verdict.score !== 10) throw new Error("valid verdict was not accepted");
expectFailure(message("stop"), /agent_settled/);
expectFailure(`${{message("length")}}\n${{settled}}`, /unsuccessfully/);
expectFailure(`${{message("stop", "openai-codex", "gpt-5.6-sol")}}\n${{settled}}`, /model drifted/);
"""
        execution = subprocess.run(["bun", "-e", script], cwd=SKILL, text=True, capture_output=True, timeout=30)
        self.assertEqual(execution.returncode, 0, execution.stderr)

    def test_step_attempt_budget_survives_extension_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            attempt_dir = run_dir / "step-validation" / "01"
            attempt_dir.mkdir(parents=True)
            for attempt in range(1, 4):
                (attempt_dir / f"attempt-{attempt:02d}.json").write_text("{}\n")
            guard = (SKILL / "extensions" / "harness-guard.ts").as_uri()
            script = f"""
process.env.HARNESS_RUN_DIR = {json.dumps(str(run_dir))};
process.env.HARNESS_REQUIRED_STEPS = '["inspect"]';
process.env.HARNESS_STEP_VALIDATION = JSON.stringify({{
  enabled: true,
  mode: "gate",
  max_attempts_per_step: 3,
}});
const guard = await import({json.dumps(guard)});
let registered;
const pi = {{
  registerTool(tool) {{ registered = tool; }},
  on() {{}},
  exec() {{ throw new Error("validator must not run after budget exhaustion"); }},
}};
guard.default(pi);
const result = await registered.execute("test", {{
  index: 1,
  name: "inspect",
  summary: "done",
  evidence: "receipt",
}});
if (result.details.review !== "Maximum validation attempts exhausted.") throw new Error(JSON.stringify(result));
"""
            execution = subprocess.run(["bun", "-e", script], cwd=SKILL, text=True, capture_output=True, timeout=30)
            self.assertEqual(execution.returncode, 0, execution.stderr)

    def test_sanitized_runtime_uses_only_explicit_profile_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile"
            global_agent = root / "home" / ".pi" / "agent"
            profile.mkdir()
            global_agent.mkdir(parents=True)
            (profile / "auth.json").write_text("{}\n")
            global_auth = global_agent / "auth.json"
            global_auth.write_text('{"openai-codex":{"type":"oauth"}}\n')
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(profile)}), patch(
                "workflow_factory_common.Path.home", return_value=root / "home"
            ):
                runtime, policy = prepare_pi_runtime_dir(f"auth-empty:{temporary}")
                self.assertFalse((runtime / "auth.json").exists())
                self.assertEqual(
                    policy["auth"],
                    {"source": "none", "fallback_used": False, "bridged_by": "none"},
                )
                source_auth = profile / "auth.json"
                source_auth.write_text('{"openai-codex":{"type":"oauth"}}\n')
                runtime, policy = prepare_pi_runtime_dir(f"auth-explicit:{temporary}")
            self.assertEqual((runtime / "auth.json").resolve(), source_auth.resolve())
            self.assertEqual(
                policy["auth"],
                {"source": "configured", "fallback_used": False, "bridged_by": "symlink"},
            )

    def test_sanitized_runtime_allows_explicit_home_auth_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile"
            global_agent = root / "home" / ".pi" / "agent"
            profile.mkdir()
            global_agent.mkdir(parents=True)
            (profile / "auth.json").write_text("{}\n")
            global_auth = global_agent / "auth.json"
            global_auth.write_text('{"openai-codex":{"type":"oauth"}}\n')
            with patch.dict(
                os.environ,
                {
                    "PI_CODING_AGENT_DIR": str(profile),
                    "PI_WORKFLOW_AUTH_FALLBACK": "1",
                },
            ), patch("workflow_factory_common.Path.home", return_value=root / "home"):
                runtime, policy = prepare_pi_runtime_dir(f"auth-fallback:{temporary}")
            self.assertEqual((runtime / "auth.json").resolve(), global_auth.resolve())
            self.assertEqual(
                policy["auth"],
                {"source": "home_fallback", "fallback_used": True, "bridged_by": "symlink"},
            )

    def test_unknown_blueprint_fields_fail_instead_of_disappearing(self) -> None:
        value = copy.deepcopy(self.blueprint)
        value["transitions"] = [{"from": "classify", "to": "report"}]
        errors = validate_blueprint(value)
        self.assertTrue(any("unsupported blueprint fields" in item for item in errors), errors)

    def test_nested_unknown_blueprint_fields_fail_closed(self) -> None:
        mutations = [
            ("runtime", lambda value: value["runtime"].update({"mystery": True})),
            ("acceptance_criteria", lambda value: value["acceptance_criteria"][0].update({"mystery": True})),
            ("verifiers", lambda value: value["verifiers"][0].update({"mystery": True})),
            ("task_template", lambda value: value["task_template"].update({"mystery": True})),
            ("context_policy", lambda value: value["context_policy"].update({"mystery": True})),
            ("operations", lambda value: value["operations"].update({"mystery": True})),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                value = copy.deepcopy(self.blueprint)
                mutate(value)
                self.assertNotEqual(validate_blueprint(value), [])

    def test_generic_runtime_rejects_conditional_graph(self) -> None:
        value = json.loads((SKILL / "examples" / "workflow-blueprint.json").read_text())
        value["schema_version"] = "1.1"
        value["control_flow"] = self.blueprint["control_flow"]
        errors = validate_blueprint(value)
        self.assertTrue(any("only by specialized" in item for item in errors), errors)

    def test_routes_pr_by_priority_and_records_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_stage(run, "select", {"selected": True})
            first = evaluate_transition(graph=self.graph, run_dir=run, stage="select", decision_id="select-1")
            self.assertEqual(first["to"], "classify")
            self.write_stage(run, "classify", {"change_type": "code", "diff_lines": 6001})
            second = evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="classify-1")
            self.assertEqual(second["to"], "chunked-review")
            self.assertEqual(second["matched_transition_ids"], ["large-route", "code-route"])
            self.assertEqual(second["selected_transition_id"], "large-route")
            self.assertEqual(second["previous_decision_digest"], first["decision_digest"])
            self.assertEqual(evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="classify-1"), second)

    def test_default_route_is_explicit_and_missing_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_stage(run, "select", {"selected": True})
            evaluate_transition(graph=self.graph, run_dir=run, stage="select", decision_id="select-1")
            self.write_stage(run, "classify", {"change_type": "unknown", "diff_lines": 10})
            decision = evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="classify-default")
            self.assertEqual(decision["to"], "blocked")
            self.assertTrue(decision["used_default"])
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_stage(run, "select", {"selected": True})
            evaluate_transition(graph=self.graph, run_dir=run, stage="select", decision_id="select-1")
            self.write_stage(run, "classify", {"change_type": "code"})
            with self.assertRaises(TransitionError):
                evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="missing-field")

    def test_invalid_graphs_reject_ambiguity_targets_and_unbounded_cycles(self) -> None:
        ambiguous = copy.deepcopy(self.graph)
        next(item for item in ambiguous["transitions"] if item["id"] == "code-route")["priority"] = 20
        self.assertTrue(any("priorities" in item for item in graph_errors(ambiguous)))
        unknown = copy.deepcopy(self.graph)
        unknown["transitions"][0]["to"] = "does-not-exist"
        self.assertTrue(any("declared stages" in item for item in graph_errors(unknown)))
        cyclic = copy.deepcopy(self.graph)
        next(item for item in cyclic["transitions"] if item["id"] == "full-review-done")["to"] = "classify"
        self.assertTrue(any("max_visits" in item for item in graph_errors(cyclic)))
        unsafe = copy.deepcopy(self.graph)
        next(item for item in unsafe["transitions"] if item["id"] == "code-route")["when"]["path"] = "/__proto__/route"
        self.assertTrue(any("forbidden" in item for item in graph_errors(unsafe)))

    def test_graph_rejects_duplicate_terminals_and_boolean_limits(self) -> None:
        invalids = []
        duplicate_terminals = copy.deepcopy(self.graph)
        duplicate_terminals["terminal_stages"].append(duplicate_terminals["terminal_stages"][0])
        invalids.append(duplicate_terminals)
        boolean_transitions = copy.deepcopy(self.graph)
        boolean_transitions["max_transitions"] = True
        invalids.append(boolean_transitions)
        boolean_visits = copy.deepcopy(self.graph)
        boolean_visits["max_visits_per_stage"]["classify"] = True
        invalids.append(boolean_visits)
        boolean_priority = copy.deepcopy(self.graph)
        boolean_priority["transitions"][0]["priority"] = True
        invalids.append(boolean_priority)
        for graph in invalids:
            self.assertNotEqual(graph_errors(graph), [])

    def test_graph_source_and_decision_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_stage(run, "select", {"selected": True})
            evaluate_transition(graph=self.graph, run_dir=run, stage="select", decision_id="select-1")
            (run / "stages" / "select.json").write_text('{"selected":false}\n')
            with self.assertRaises(TransitionError):
                evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="classify-1")
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_stage(run, "select", {"selected": True})
            evaluate_transition(graph=self.graph, run_dir=run, stage="select", decision_id="select-1")
            path = run / "routing" / "decisions" / "select-1.json"
            value = json.loads(path.read_text())
            value["to"] = "report"
            path.write_text(json.dumps(value))
            with self.assertRaises(TransitionError):
                evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="classify-1")

    def test_non_json_numbers_and_type_mismatches_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_stage(run, "select", {"selected": True})
            evaluate_transition(graph=self.graph, run_dir=run, stage="select", decision_id="select-1")
            (run / "stages" / "classify.json").write_text('{"change_type":"code","diff_lines":NaN}\n')
            with self.assertRaises(TransitionError):
                evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="nan")
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_stage(run, "select", {"selected": True})
            evaluate_transition(graph=self.graph, run_dir=run, stage="select", decision_id="select-1")
            self.write_stage(run, "classify", {"change_type": "code", "diff_lines": "many"})
            with self.assertRaises(TransitionError):
                evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="wrong-type")

    def test_bounded_cycle_routes_exhaustion_to_terminal(self) -> None:
        graph = {
            "schema_version": "1.0", "workflow": "bounded-loop", "stages": ["review", "report", "blocked"],
            "initial_stage": "review", "terminal_stages": ["report", "blocked"], "max_transitions": 4,
            "max_visits_per_stage": {"review": 2}, "on_exhausted": "blocked",
            "transitions": [
                {"id": "review-again", "from": "review", "to": "review", "priority": 10, "when": {"op": "equals", "path": "/again", "value": True}},
                {"id": "review-done", "from": "review", "to": "report", "priority": 100, "default": True},
            ],
        }
        self.assertEqual(graph_errors(graph), [])
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_stage(run, "review", {"again": True})
            first = evaluate_transition(graph=graph, run_dir=run, stage="review", decision_id="review-1")
            self.assertEqual(first["to"], "review")
            second = evaluate_transition(graph=graph, run_dir=run, stage="review", decision_id="review-2")
            self.assertEqual(second["to"], "blocked")
            self.assertEqual(second["exhausted_reason"], "max_visits:review")
    def test_compiler_preserves_and_hashes_control_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            blueprint = repo / "blueprint.json"
            blueprint.write_text(json.dumps(self.blueprint))
            harness = compile_blueprint(blueprint, repo)
            compiled = json.loads((harness / "control-flow.json").read_text())
            config = json.loads((harness / "harness.json").read_text())
            self.assertEqual(compiled, self.graph)
            self.assertEqual(config["control_flow"]["graph_digest"], transition_digest(compiled))
            self.assertTrue((harness / "scripts" / "evaluate_transition.py").is_file())
            (harness / "scripts" / "run.py").write_text("#!/usr/bin/env python3\n")
            gates, _, _ = static_gates(harness)
            self.assertTrue(all(item["status"] == "passed" for item in gates), gates)
            engine = harness / "scripts" / "evaluate_transition.py"
            engine.write_text(engine.read_text() + "\n# tampered\n")
            gates, _, _ = static_gates(harness)
            self.assertEqual(next(item for item in gates if item["name"] == "transition_engine_integrity")["status"], "failed")

    def test_compiled_cli_enforces_graph_and_engine_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            harness = compile_blueprint(self.write_blueprint(repo), repo)
            run = repo / "run"
            self.write_stage(run, "select", {"selected": True})
            engine = harness / "scripts" / "evaluate_transition.py"
            command = ["python3", str(engine), "--harness", str(harness), "--run", str(run), "--from", "select", "--decision-id", "first"]
            self.assertEqual(subprocess.run(command, text=True, capture_output=True, check=False).returncode, 0)
            graph_path = harness / "control-flow.json"
            graph = json.loads(graph_path.read_text())
            graph["transitions"][0]["to"] = "blocked"
            graph_path.write_text(json.dumps(graph))
            tampered_graph = subprocess.run(command[:-1] + ["graph-tamper"], text=True, capture_output=True, check=False)
            self.assertNotEqual(tampered_graph.returncode, 0)
            self.assertIn("graph digest mismatch", tampered_graph.stderr)
            graph_path.write_text(json.dumps(self.graph))
            engine.write_text(engine.read_text() + "\n# tampered\n")
            tampered_engine = subprocess.run(command[:-1] + ["engine-tamper"], text=True, capture_output=True, check=False)
            self.assertNotEqual(tampered_engine.returncode, 0)
            self.assertIn("engine digest mismatch", tampered_engine.stderr)

    def test_schema_restricts_control_flow_to_specialized_schema_1_1_through_1_3(self) -> None:
        schema = json.loads((SKILL / "schemas" / "workflow-blueprint.schema.json").read_text())
        rule = schema["allOf"][0]
        self.assertEqual(rule["if"]["required"], ["control_flow"])
        self.assertEqual(rule["then"]["properties"]["schema_version"]["enum"], ["1.1", "1.2", "1.3"])
        self.assertEqual(rule["then"]["properties"]["runtime"]["properties"]["kind"]["const"], "specialized")

    def test_schema_1_2_can_combine_control_flow_and_peer_collaboration(self) -> None:
        value = copy.deepcopy(self.blueprint)
        peer = json.loads((SKILL / "examples" / "pr-review-peer-blueprint.json").read_text())["peer_collaboration"]
        peer["stages"] = ["full-review"]
        value["schema_version"] = "1.2"
        value["peer_collaboration"] = peer
        self.assertEqual(validate_blueprint(value), [])

    def test_tracker_uses_selected_route_and_skips_unvisited_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "harness" / "runs" / "pr" / "run-1"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({
                "workflow": self.graph["workflow"], "task_id": "pr", "run_id": "run-1", "stages": self.graph["stages"],
            }) + "\n")
            self.write_stage(run, "select", {"selected": True})
            evaluate_transition(graph=self.graph, run_dir=run, stage="select", decision_id="select-1")
            self.write_stage(run, "classify", {"change_type": "code", "diff_lines": 640})
            evaluate_transition(graph=self.graph, run_dir=run, stage="classify", decision_id="classify-1")
            rendered = render_tracker(run).read_text()
            self.assertIn("Selected route · 2 transitions", rendered)
            self.assertIn("classify → full-review", rendered)
            self.assertIn('life-step neutral"><span class="life-dot"></span><span>lightweight review</span>', rendered)

    def test_refuses_same_version_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            path = self.write_blueprint(repo)
            compile_blueprint(path, repo)
            with self.assertRaises(SystemExit):
                compile_blueprint(path, repo)

    def test_cli_requires_approval_of_exact_blueprint_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            blueprint = self.write_blueprint(repo)
            command = [sys.executable, str(SCRIPTS / "compile_workflow.py"), "--blueprint", str(blueprint), "--repo", str(repo)]
            blocked = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(blocked.returncode, 3)
            review = json.loads(blocked.stdout)
            self.assertEqual(review["status"], "awaiting_approval")
            self.assertFalse((repo / ".codex" / "workflows").exists())

            approved = subprocess.run(
                [*command, "--approved-plan-sha256", review["plan_sha256"]],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertTrue(Path(approved.stdout.strip()).is_dir())

    def test_extracts_blueprint_from_pi_json_stream(self) -> None:
        expected = {"schema_version": "1.0", "workflow": "example"}
        stream = json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(expected)}]}})
        self.assertEqual(extract_object(assistant_text(stream)), expected)

    def test_extracts_final_reported_cost_without_counting_stream_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            events = run / "events"
            events.mkdir()
            message = {"role": "assistant", "responseId": "one", "usage": {"cost": {"total": 0.125}}}
            (events / "pi-plan.jsonl").write_text(
                json.dumps({"type": "message_update", "message": message}) + "\n" +
                json.dumps({"type": "message_end", "message": message}) + "\n"
            )
            self.assertEqual(cost_from_events(run), 0.125)

    def test_repaired_stage_is_terminal_success(self) -> None:
        self.assertTrue(stage_completed({"status": "repaired"}))
        self.assertTrue(stage_completed({"status": "repaired_unverified_build"}))
        self.assertFalse(stage_completed({"status": "repair_failed"}))

    def test_optimization_requires_complete_replay_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            baseline_value = copy.deepcopy(self.blueprint)
            baseline_value["version"] = "1.0.0"
            candidate_value = copy.deepcopy(self.blueprint)
            candidate_value["version"] = "1.1.0"
            baseline_path = repo / "baseline.json"
            candidate_path = repo / "candidate.json"
            baseline_path.write_text(json.dumps(baseline_value))
            candidate_path.write_text(json.dumps(candidate_value))
            baseline = compile_blueprint(baseline_path, repo)
            candidate = compile_blueprint(candidate_path, repo)
            for harness in (baseline, candidate):
                (harness / "certification").mkdir()
                (harness / "certification" / "latest.json").write_text(json.dumps({"status": "fixture_certified"}))
            replay = repo / "replay.json"
            replay.write_text(json.dumps({"cases": [{
                "case_id": "one",
                "baseline": {"passed": True, "judge_score": 9, "duration_seconds": 10},
                "candidate": {"passed": True, "judge_score": 10, "duration_seconds": 9}
            }]}))
            result = subprocess.run([
                "python3", str(SCRIPTS / "compare_workflow_versions.py"), "--baseline", str(baseline),
                "--candidate", str(candidate), "--replay-results", str(replay)
            ], text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 1)
            report = json.loads((candidate / "optimization" / "latest.json").read_text())
            self.assertEqual(report["decision"], "rejected")
            self.assertFalse(report["eligible_for_promotion"])
            self.assertFalse(report["promotion_executed"])
            self.assertEqual(next(item for item in report["gates"] if item["name"] == "cost_budget")["status"], "failed")

    def test_optimization_promotes_only_complete_non_regressing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            baseline_value = copy.deepcopy(self.blueprint)
            baseline_value["version"] = "1.0.0"
            candidate_value = copy.deepcopy(self.blueprint)
            candidate_value["version"] = "1.1.0"
            baseline_path = repo / "baseline.json"
            candidate_path = repo / "candidate.json"
            baseline_path.write_text(json.dumps(baseline_value))
            candidate_path.write_text(json.dumps(candidate_value))
            baseline = compile_blueprint(baseline_path, repo)
            candidate = compile_blueprint(candidate_path, repo)
            for harness in (baseline, candidate):
                (harness / "certification").mkdir()
                (harness / "certification" / "latest.json").write_text(json.dumps({"status": "fixture_certified"}))
            replay = repo / "replay.json"
            replay.write_text(json.dumps({"cases": [{
                "case_id": "one",
                "baseline": {"passed": True, "judge_score": 9, "duration_seconds": 10, "cost_usd": 0.10, "repair_attempts": 1},
                "candidate": {"passed": True, "judge_score": 10, "duration_seconds": 9, "cost_usd": 0.09, "repair_attempts": 0}
            }]}))
            result = subprocess.run([
                "python3", str(SCRIPTS / "compare_workflow_versions.py"), "--baseline", str(baseline),
                "--candidate", str(candidate), "--replay-results", str(replay), "--promote"
            ], text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((candidate / "optimization" / "latest.json").read_text())
            self.assertEqual(report["decision"], "promoted")
            self.assertTrue(report["eligible_for_promotion"])
            self.assertTrue(report["promotion_executed"])
            active = json.loads((candidate.parent.parent / "active.json").read_text())
            self.assertEqual(active["version"], "1.1.0")


if __name__ == "__main__":
    unittest.main()
