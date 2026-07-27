import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "code"))

from orchestrator import execute_lean_code, lean_policy_violations


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


if __name__ == "__main__":
    unittest.main()
