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
    execute_axle_check,
    execute_lean_code,
    formal_backends_pass,
    lean_policy_violations,
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

    def test_axle_modes_have_explicit_acceptance_semantics(self):
        self.assertTrue(formal_backends_pass(True, None, "off"))
        self.assertTrue(formal_backends_pass(True, None, "fallback"))
        self.assertTrue(formal_backends_pass(False, True, "fallback"))
        self.assertFalse(formal_backends_pass(False, False, "fallback"))
        self.assertTrue(formal_backends_pass(True, True, "required"))
        self.assertFalse(formal_backends_pass(True, False, "required"))
        self.assertFalse(formal_backends_pass(False, True, "required"))

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
                "def unrelated : Nat := 1",
                "lean-4.28.0",
            )
            self.assertFalse(passed)
            self.assertIn("missing theorem named imo_problem", report)

            passed, report = execute_axle_check(
                "theorem imo_problem : True := by trivial",
                "lean-4.28.0",
            )
            self.assertFalse(passed)
            self.assertIn("imo_problem", report)

            FakeAxleClient.failed_declarations = []
            passed, report = execute_axle_check(
                "theorem imo_problem : True := by trivial",
                "lean-4.28.0",
            )
            self.assertTrue(passed, report)
            self.assertFalse(FakeAxleClient.last_request[2])

            FakeAxleClient.raise_error = True
            passed, report = execute_axle_check(
                "theorem imo_problem : True := by trivial",
                "lean-4.28.0",
            )
            self.assertFalse(passed)
            self.assertNotIn("test-key", report)


if __name__ == "__main__":
    unittest.main()
