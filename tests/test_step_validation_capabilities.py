import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_pi_harness import resolve_stage_tools  # noqa: E402


class StepValidationCapabilityTests(unittest.TestCase):
    def test_execute_accepts_pinned_step_validation_tool(self) -> None:
        config = {"stage_capabilities": {"execute": {"tools": ["read", "edit", "write"]}}}

        self.assertEqual(
            resolve_stage_tools(config, "execute", ["read", "edit", "write", "harness_step"]),
            ["read", "edit", "write", "harness_step"],
        )

    def test_other_extension_tools_remain_denied(self) -> None:
        config = {"stage_capabilities": {"execute": {"tools": ["read"]}}}

        with self.assertRaises(SystemExit):
            resolve_stage_tools(config, "execute", ["read", "untrusted_extension"])


if __name__ == "__main__":
    unittest.main()
