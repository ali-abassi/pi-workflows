#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from compile_workflow import compile_blueprint  # noqa: E402


def canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CertificationContractTests(unittest.TestCase):
    def test_compiled_contract_requires_independent_evidence_and_operator_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            blueprint = copy.deepcopy(json.loads((SKILL / "examples" / "workflow-blueprint.json").read_text()))
            blueprint["workflow"] = "certification-contract-test"
            blueprint["schema_version"] = "1.3"
            classes = ["positive", "adversarial", "regression", "integration", "safety"]
            criteria = [
                ("truth-score", "truth"),
                ("eval-score", "eval_quality"),
                ("cost-score", "cost_efficiency"),
                ("integration-score", "integration"),
                ("safety-score", "safety"),
            ]
            acceptance_criteria = [
                (f"requirement-{dimension.replace('_', '-')}", dimension)
                for _, dimension in criteria
            ]
            blueprint["acceptance_criteria"] = [
                {"id": criterion_id, "description": f"Fixture satisfies {dimension}."}
                for criterion_id, dimension in acceptance_criteria
            ]
            blueprint["verifiers"][0]["covers"] = [criterion_id for criterion_id, _ in acceptance_criteria]
            corpus = {
                "schema_version": "1.0",
                "workflow": blueprint["workflow"],
                "cases": [
                    {
                        "id": f"{case_class}-case",
                        "class": case_class,
                        "input": {"text": case_class},
                        "expected": {"outcome": "pass"},
                        "evidence_requirements": ["output"],
                    }
                    for case_class in classes
                ],
            }
            rubric = {
                "schema_version": "1.1",
                "judge_id": "independent-judge",
                "instructions": "Score only the immutable evidence supplied for each case.",
                "criteria": [
                    {
                        "id": criterion_id,
                        "dimension": dimension,
                        "description": f"Evaluate {dimension} from cited evidence.",
                        "weight": 1,
                        "anchors": {
                            "0": "No valid evidence supports the requirement.",
                            "5": "Evidence partially supports the requirement.",
                            "10": "Evidence fully supports the requirement.",
                        },
                    }
                    for criterion_id, dimension in criteria
                ],
            }
            blueprint["certification_contract"] = {
                "corpus": {
                    "path": "certification/corpus.json",
                    "minimum_cases": 5,
                    "required_classes": classes,
                },
                "deterministic_gates": ["verify-result"],
                "judges": [
                    {
                        "id": "independent-judge",
                        "model": "fixture-provider/fixture-model",
                        "rubric_path": "certification/rubric.json",
                        "threshold": 9,
                        "evidence_fields": ["analysis", "verdict"],
                    }
                ],
                "dimensions": {
                    dimension: [criterion_id]
                    for criterion_id, dimension in acceptance_criteria
                },
                "replay": {
                    "baseline_version": None,
                    "same_corpus": True,
                    "max_cost_regression_percent": 10,
                    "max_latency_regression_percent": 20,
                },
                "promotion": {
                    "minimum_pass_rate": 1,
                    "minimum_dimension_score": 9,
                    "require_independent_validation": True,
                    "require_operator_decision": True,
                    "block_on_regression": True,
                },
            }
            blueprint["task_template"]["fixture_files"]["certification/corpus.json"] = json.dumps(corpus, indent=2) + "\n"
            blueprint["task_template"]["fixture_files"]["certification/rubric.json"] = json.dumps(rubric, indent=2) + "\n"
            blueprint_path = repo / "blueprint.json"
            blueprint_path.write_text(json.dumps(blueprint))

            harness = compile_blueprint(blueprint_path, repo)
            config = json.loads((harness / "harness.json").read_text())
            contract = json.loads((harness / "certification-contract.json").read_text())
            self.assertTrue(config["certification_contract"]["enabled"])
            self.assertEqual(config["certification_contract"]["digest"], canonical_digest(contract))
            self.assertTrue((harness / "schemas" / "certification-result.schema.json").is_file())
            self.assertTrue((harness / "schemas" / "certification-decision.schema.json").is_file())
            self.assertIn("evaluate_certification.py", (harness / "workflow.md").read_text())

            results_dir = repo / "independent-results"
            results_dir.mkdir()
            rubric_digest = canonical_digest(rubric)
            case_results = []
            for case in corpus["cases"]:
                case_id = case["id"]
                output_path = results_dir / f"{case_id}-output.txt"
                output_path.write_text("verified output\n")
                judge_path = results_dir / f"{case_id}-judge.json"
                judge_path.write_text(json.dumps({"analysis": "all evidence verified", "verdict": "pass"}) + "\n")
                case_results.append(
                    {
                        "id": case_id,
                        "deterministic_gates": {"verify-result": True},
                        "judge_results": {
                            "independent-judge": {
                                "score": 10,
                                "criterion_scores": {criterion_id: 10 for criterion_id, _ in criteria},
                                "rubric_digest": rubric_digest,
                                "artifact_path": judge_path.name,
                                "artifact_digest": file_digest(judge_path),
                                "evidence": {"analysis": "all evidence verified", "verdict": "pass"},
                            }
                        },
                        "cost": 0.01,
                        "latency_ms": 10,
                        "evidence": {"output": {"path": output_path.name, "sha256": file_digest(output_path)}},
                    }
                )
            results = {
                "schema_version": "1.2",
                "workflow": blueprint["workflow"],
                "version": blueprint["version"],
                "contract_digest": canonical_digest(contract),
                "corpus_digest": canonical_digest(corpus),
                "rubric_digests": {"independent-judge": rubric_digest},
                "validator": {
                    "independent": True,
                    "provider": "fixture-provider",
                    "model": "fixture-model",
                    "run_id": "fixture-run",
                    "executed_at": "2026-07-16T00:00:00Z",
                },
                "cases": case_results,
            }
            results_path = results_dir / "results.json"
            results_path.write_text(json.dumps(results, indent=2) + "\n")
            evaluator = harness / "scripts" / "evaluate_certification.py"

            awaiting = subprocess.run(
                ["python3", str(evaluator), "--harness", str(harness), "--results", str(results_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(awaiting.returncode, 1, awaiting.stderr)
            report = json.loads((harness / "certification" / "latest-contract-evaluation.json").read_text())
            self.assertTrue(report["mechanical_pass"])
            self.assertEqual(report["status"], "awaiting_operator")
            self.assertFalse(report["promotion_eligible"])

            decision = {
                "schema_version": "1.0",
                "decision": "promote",
                "operator": "fixture-operator",
                "rationale": "All frozen cases and dimensions passed without regression.",
                "workflow": blueprint["workflow"],
                "candidate_version": blueprint["version"],
                "contract_digest": canonical_digest(contract),
                "results_digest": canonical_digest(results),
                "decided_at": "2026-07-16T00:01:00Z",
            }
            decision_path = results_dir / "decision.json"
            decision_path.write_text(json.dumps(decision, indent=2) + "\n")
            promoted = subprocess.run(
                [
                    "python3",
                    str(evaluator),
                    "--harness",
                    str(harness),
                    "--results",
                    str(results_path),
                    "--operator-decision",
                    str(decision_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(promoted.returncode, 0, promoted.stderr)
            report = json.loads((harness / "certification" / "latest-contract-evaluation.json").read_text())
            self.assertEqual(report["status"], "certified")
            self.assertTrue(report["promotion_eligible"])
            self.assertEqual(report["candidate"]["pass_rate"], 1)
            self.assertTrue(all(score == 10 for score in report["candidate"]["dimensions"].values()))


if __name__ == "__main__":
    unittest.main()
