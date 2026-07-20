"""Regression tests for the runner promises the README sells.

These cover the three things that would most embarrass the project if they
broke silently: a drifted model pin, a stale cache hit after the quality
contract changed, and the two hand-maintained `build_deps` copies disagreeing
about what will run.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CLI = SCRIPTS / "piw.py"
EXAMPLES = ROOT / "examples" / "workflows"

sys.path.insert(0, str(SCRIPTS))

import graph as pygraph  # noqa: E402
import run_steps  # noqa: E402


def fake_pi(directory: Path, provider: str, model: str, text: str = "ok") -> Path:
    """A stub `pi` that reports whichever provider/model it is told to."""
    binary = directory / "pi"
    binary.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"type\":\"message_end\",\"message\":{\"role\":\"assistant\","
        f'\"provider\":\"{provider}\",\"model\":\"{model}\",\"stopReason\":\"stop\",'
        f'\"content\":[{{\"type\":\"text\",\"text\":\"{text}\"}}],'
        "\"usage\":{\"input\":5,\"output\":2,\"totalTokens\":7,"
        "\"cost\":{\"total\":0.001}}}}'\n"
        "printf '%s\\n' '{\"type\":\"agent_settled\"}'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


class CacheContractTests(unittest.TestCase):
    """A cache hit skips the model, the schema check, and the judge.

    So anything those three depend on has to be in the key, or a step can pass
    a bar it was never actually held to.
    """

    def _key(self, step: dict) -> str:
        return run_steps.cache_key(step, {"model": "test/luna"}, "same prompt")

    def test_raising_the_judge_threshold_invalidates_the_cache(self) -> None:
        lenient = {"id": "draft", "judge": {"prompt": "score it", "score": 1.0}}
        strict = {"id": "draft", "judge": {"prompt": "score it", "score": 9.0}}
        self.assertNotEqual(
            self._key(lenient), self._key(strict),
            "raising the judge threshold must not reuse an artifact judged at the old bar",
        )

    def test_changing_the_judge_prompt_invalidates_the_cache(self) -> None:
        before = {"id": "draft", "judge": {"prompt": "score it", "score": 8.0}}
        after = {"id": "draft", "judge": {"prompt": "score it harshly", "score": 8.0}}
        self.assertNotEqual(self._key(before), self._key(after))

    def test_tightening_the_schema_invalidates_the_cache(self) -> None:
        loose = {"id": "draft", "schema": {"type": "object"}}
        tight = {"id": "draft", "schema": {"type": "object", "required": ["verdict"]}}
        self.assertNotEqual(self._key(loose), self._key(tight))

    def test_an_unchanged_contract_still_hits_the_cache(self) -> None:
        step = {"id": "draft", "judge": {"prompt": "score it", "score": 8.0}}
        self.assertEqual(self._key(dict(step)), self._key(dict(step)))

    def test_a_step_without_qa_is_unaffected(self) -> None:
        self.assertEqual(self._key({"id": "draft"}), self._key({"id": "draft"}))


class ModelPinTests(unittest.TestCase):
    """The README promises a drifted model fails rather than answering quietly."""

    def _run(self, pinned: str, served_provider: str, served_model: str):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fake_pi(binary_dir, served_provider, served_model)
            steps = root / "steps.yaml"
            steps.write_text(yaml.safe_dump({
                "version": 1, "workflow": "pin-check", "model": pinned,
                "input": {"required": True, "description": "pin fixture"},
                "steps": [{"id": "call", "prompt": "Answer {input}", "gate": 'test -s "$OUT"'}],
            }, sort_keys=False), encoding="utf-8")
            environment = {
                "PATH": f"{binary_dir}:/usr/bin:/bin",
                "HOME": str(root),
                "PI_WORKFLOWS_ROOTS": str(root),
            }
            return subprocess.run(
                [sys.executable, str(CLI), "run", str(steps), "--input", "hello", "--json"],
                capture_output=True, text=True, env=environment, timeout=120,
            )

    def test_a_served_model_matching_the_pin_passes(self) -> None:
        result = self._run("test/luna", "test", "luna")
        self.assertIn('"ok":true', result.stdout, result.stdout + result.stderr)

    def test_a_drifted_model_fails_the_step(self) -> None:
        result = self._run("test/luna", "other", "sol")
        self.assertIn('"ok":false', result.stdout, result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 0)

    def test_a_drifted_model_never_reports_success(self) -> None:
        """The dangerous failure is a pass, not a confusing message."""
        result = self._run("test/luna", "test", "terra")
        self.assertNotIn('"ok":true', result.stdout, result.stdout + result.stderr)


class GraphParityTests(unittest.TestCase):
    """`graph.build_deps` is a hand-maintained copy of the runner's.

    If they drift, `piw graph`, `piw validate`, and the Studio all display a
    graph different from the one that executes — the single thing this product
    cannot get wrong. This replaces the test the docstring in graph.py names.
    """

    def test_every_shipped_example_resolves_identically_in_both_copies(self) -> None:
        # Both return (deps, extra). Only `deps` is the shared contract: the
        # canvas's second element is the implicit-edge set it draws differently,
        # the runner's is its previous-step map. `deps` is what must never drift.
        workflows = sorted(EXAMPLES.glob("*/steps.yaml"))
        self.assertGreater(len(workflows), 0, "no examples found to compare")
        for path in workflows:
            with self.subTest(workflow=path.parent.name):
                spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                steps = spec.get("steps") or []
                self.assertEqual(
                    pygraph.build_deps(steps)[0], run_steps.build_deps(steps)[0],
                    f"{path.parent.name}: the canvas and the runner disagree about dependencies",
                )

    def test_the_parity_check_would_actually_catch_a_drift(self) -> None:
        """Guard the guard: a contrived divergence must fail the comparison."""
        steps = [{"id": "a"}, {"id": "b", "needs": ["a"]}]
        canvas = pygraph.build_deps(steps)[0]
        tampered = {**canvas, "b": set()}
        self.assertNotEqual(tampered, run_steps.build_deps(steps)[0])


if __name__ == "__main__":
    unittest.main()
