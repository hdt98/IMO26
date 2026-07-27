import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "code"))

from orchestrator import (
    build_lean_repair_messages,
    build_verifier_messages,
    execute_axle_check,
    execute_lean_code,
    formal_backends_pass,
    lean_policy_violations,
    lean_source_preserves_frozen_statement,
    validate_lean_statement_prefix,
)


class LeanVerifierTests(unittest.TestCase):
    def test_rejects_proof_holes(self):
        code = "import Mathlib\ntheorem imo_problem : False := by sorry\n"
        self.assertIn("forbidden token: sorry", lean_policy_violations(code))

    def test_requires_named_theorem(self):
        code = "import Mathlib\ntheorem different_name : True := by trivial\n"
        self.assertIn(
            "missing theorem named imo_problem", lean_policy_violations(code)
        )

    def test_compiles_valid_mathlib_proof_locally(self):
        code = (
            "import Mathlib\n"
            "set_option autoImplicit false\n"
            "theorem imo_problem : 1 + 1 = 2 := by norm_num\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            passed, report = execute_lean_code(
                code,
                Path(directory) / "proof.lean",
                REPO_ROOT / "lean",
            )
        self.assertTrue(passed, report)
        self.assertRegex(report, r"Statement SHA-256: [0-9a-f]{64}")

    def test_rejects_theorem_name_inside_string_literal(self):
        code = (
            "import Mathlib\n"
            'def bait : String := "theorem imo_problem"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            passed, report = execute_lean_code(
                code,
                Path(directory) / "bait.lean",
                REPO_ROOT / "lean",
            )
        self.assertFalse(passed)
        self.assertIn("missing theorem named imo_problem", report)

    def test_rejects_generated_lean_command_execution(self):
        code = (
            "import Mathlib\n"
            "run_cmd IO.println \"not allowed\"\n"
            "theorem imo_problem : True := by trivial\n"
        )
        violations = lean_policy_violations(code)
        self.assertIn(
            "forbidden Lean feature: command execution",
            violations,
        )
        tactic_violations = lean_policy_violations(
            "import Mathlib\n"
            "theorem imo_problem : True := by\n"
            "  run_tac Lean.Elab.Tactic.closeMainGoalUsing `True.intro\n"
        )
        self.assertIn(
            "forbidden Lean feature: tactic-time command execution",
            tactic_violations,
        )

    def test_axle_modes_have_explicit_acceptance_semantics(self):
        self.assertTrue(formal_backends_pass(True, None, "off"))
        self.assertTrue(formal_backends_pass(True, None, "fallback"))
        self.assertTrue(formal_backends_pass(False, True, "fallback"))
        self.assertFalse(formal_backends_pass(False, False, "fallback"))
        self.assertTrue(formal_backends_pass(True, True, "required"))
        self.assertFalse(formal_backends_pass(True, False, "required"))
        self.assertFalse(formal_backends_pass(False, True, "required"))

    def test_formal_reports_use_backend_neutral_prompt_labels(self):
        report = "Local Lean: FAIL\nAXLE (fallback): PASS"
        frozen_statement = (
            "import Mathlib\n"
            "set_option autoImplicit false\n"
            "theorem imo_problem : True := by"
        )
        verifier_prompt = build_verifier_messages(
            "Prove the problem.",
            "Detailed Solution\nA proof.",
            report,
        )[1]["content"]
        repair_prompt = build_lean_repair_messages(
            "Prove the problem.",
            "A proof.",
            frozen_statement,
            "theorem imo_problem : True := by trivial",
            report,
        )[1]["content"]

        self.assertIn("### Formal Verification Report ###", verifier_prompt)
        self.assertIn("### Formal Verification Report ###", repair_prompt)
        self.assertIn("### Frozen Lean Statement Prefix ###", repair_prompt)
        self.assertIn("passes the configured formal", repair_prompt)
        self.assertNotIn("### Local Lean Report ###", repair_prompt)

    def test_statement_prefix_is_freezeable_only_before_proof_body(self):
        frozen_statement = (
            "import Mathlib\n"
            "set_option autoImplicit false\n"
            "theorem imo_problem (n : Nat) : n = n := by"
        )
        source, violations = validate_lean_statement_prefix(
            frozen_statement
        )
        self.assertEqual(source, frozen_statement)
        self.assertEqual(violations, [])

        _, violations = validate_lean_statement_prefix(
            frozen_statement + "\n  rfl"
        )
        self.assertIn(
            "statement must end exactly at `:= by`",
            violations,
        )

    def test_proof_source_must_preserve_frozen_statement_token_boundary(self):
        frozen_statement = (
            "import Mathlib\n"
            "set_option autoImplicit false\n"
            "theorem imo_problem : True := by"
        )
        self.assertTrue(
            lean_source_preserves_frozen_statement(
                frozen_statement + "\n  trivial",
                frozen_statement,
            )
        )
        self.assertFalse(
            lean_source_preserves_frozen_statement(
                frozen_statement + "_contra\n  trivial",
                frozen_statement,
            )
        )
        self.assertFalse(
            lean_source_preserves_frozen_statement(
                frozen_statement.replace("True", "False")
                + "\n  contradiction",
                frozen_statement,
            )
        )

    def test_axle_rejects_failed_declarations_even_when_code_compiles(self):
        class FakeAxleClient:
            failed_declarations = ["imo_problem"]
            last_request = None
            raise_error = False

            def __init__(self, api_key):
                self.api_key = api_key

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def check(
                self, content, environment, ignore_imports, timeout_seconds
            ):
                type(self).last_request = (
                    content,
                    environment,
                    ignore_imports,
                    timeout_seconds,
                )
                if self.raise_error:
                    raise RuntimeError(f"request failed with {self.api_key}")
                messages = types.SimpleNamespace(errors=[], warnings=[])
                return types.SimpleNamespace(
                    okay=True,
                    failed_declarations=self.failed_declarations,
                    lean_messages=messages,
                    tool_messages=messages,
                )

        axle_module = types.ModuleType("axle")
        axle_module.AxleClient = FakeAxleClient
        with (
            mock.patch.dict(sys.modules, {"axle": axle_module}),
            mock.patch.dict(os.environ, {"AXLE_API_KEY": "test-key"}),
        ):
            passed, report = execute_axle_check(
                "import Mathlib\ndef unrelated : Nat := 1",
                "lean-4.28.0",
            )
            self.assertFalse(passed)
            self.assertIn("missing theorem named imo_problem", report)

            passed, report = execute_axle_check(
                "import Mathlib\ntheorem imo_problem : True := by trivial",
                "lean-4.28.0",
            )
            self.assertFalse(passed)
            self.assertIn("imo_problem", report)

            FakeAxleClient.failed_declarations = []
            passed, report = execute_axle_check(
                "import Mathlib\ntheorem imo_problem : True := by trivial",
                "lean-4.28.0",
            )
            self.assertTrue(passed, report)
            self.assertFalse(FakeAxleClient.last_request[2])

            FakeAxleClient.raise_error = True
            passed, report = execute_axle_check(
                "import Mathlib\ntheorem imo_problem : True := by trivial",
                "lean-4.28.0",
            )
            self.assertFalse(passed)
            self.assertNotIn("test-key", report)


if __name__ == "__main__":
    unittest.main()
