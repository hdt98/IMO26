import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "code"))

import orchestrator


class WorkflowRegressionTests(unittest.TestCase):
    def run_with_responses(self, responses):
        queued = list(responses)

        def fake_chat_completion(*args, **kwargs):
            if not queued:
                raise AssertionError("unexpected model call")
            return queued.pop(0), {"total_tokens": 1}, "stop"

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                orchestrator,
                "chat_completion",
                side_effect=fake_chat_completion,
            ),
            mock.patch.object(orchestrator, "MAX_ITERATIONS", 6),
        ):
            result = orchestrator.run_outer(
                1,
                "problem",
                "url",
                "key",
                "model",
                Path(directory),
                lean_mode="off",
                self_improve_mode="always",
                validation_enabled=False,
            )
            artifacts = sorted(
                (Path(directory) / "run_01").glob("classify_*.md")
            )
            classifications = [path.read_text() for path in artifacts]
        self.assertFalse(queued)
        return result, classifications

    def test_refinement_resets_pass_streak_for_new_candidate(self):
        responses = [
            "draft A",
            "candidate A",
            "correct\nVERDICT: yes",
            "correct\nVERDICT: yes",
            "minor gap\nVERDICT: improve",
            "candidate B",
            "correct\nVERDICT: yes",
            "correct\nVERDICT: yes",
            "critical\nVERDICT: no",
            "candidate C",
        ]
        (accepted, candidate, _), _ = self.run_with_responses(responses)
        self.assertFalse(accepted)
        self.assertIsNone(candidate)

    def test_no_verdict_breaks_consecutive_pass_streak(self):
        responses = [
            "draft",
            "candidate A",
            "correct\nVERDICT: yes",
            "correct\nVERDICT: yes",
            "critical error\nVERDICT: no",
            "candidate B",
            "correct\nVERDICT: yes",
            "correct\nVERDICT: yes",
            "critical error\nVERDICT: no",
            "candidate C",
        ]
        (accepted, candidate, _), classifications = self.run_with_responses(
            responses
        )
        self.assertEqual(
            classifications,
            ["yes", "yes", "no", "yes", "yes", "no"],
        )
        self.assertFalse(accepted)
        self.assertIsNone(candidate)

    def test_improvement_resets_critical_error_counter(self):
        responses = [
            "draft",
            "candidate A",
            "critical\nVERDICT: no",
            "candidate B",
            "minor\nVERDICT: improve",
            "candidate C",
            "critical\nVERDICT: no",
            "candidate D",
            "critical\nVERDICT: no",
            "candidate E",
            "correct\nVERDICT: yes",
            "correct\nVERDICT: yes",
        ]
        (accepted, candidate, _), classifications = self.run_with_responses(
            responses
        )
        self.assertEqual(
            classifications,
            ["no", "improve", "no", "no", "yes", "yes"],
        )
        self.assertFalse(accepted)
        self.assertIsNone(candidate)

    def test_truncated_cas_script_never_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "run_01").mkdir()
            with (
                mock.patch.object(
                    orchestrator,
                    "chat_completion",
                    return_value=(
                        'print("PARTIAL_OUTPUT")',
                        {"total_tokens": 65536},
                        "length",
                    ),
                ),
                mock.patch.object(
                    orchestrator,
                    "execute_python_code",
                    return_value=("PARTIAL_OUTPUT\n", "", 0),
                ),
            ):
                passed, details = orchestrator.cas_verify_candidate(
                    "candidate",
                    "url",
                    "key",
                    "model",
                    run_dir,
                    1,
                    1,
                )
        self.assertFalse(passed)
        self.assertIn("incomplete", details.lower())

    def test_probe_timeout_retries_from_partial_reasoning(self):
        calls = []

        def fake_chat_completion(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                raise orchestrator._WallClockTimeout(
                    "timeout",
                    partial_reasoning="use exhaustive search",
                )
            return 'print("n=1: result")', {"total_tokens": 5}, "stop"

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                orchestrator,
                "chat_completion",
                side_effect=fake_chat_completion,
            ),
            mock.patch.object(
                orchestrator,
                "execute_python_code",
                return_value=("n=1: result\n", "", 0),
            ),
        ):
            result = orchestrator.pre_solve_validation(
                "problem",
                "url",
                "key",
                "model",
                Path(directory),
                1,
            )
        self.assertEqual(result, "n=1: result")
        self.assertEqual(len(calls), 2)
        retry_messages = calls[1][0][3]
        self.assertIn(
            "use exhaustive search",
            retry_messages[-1]["content"],
        )
        self.assertEqual(
            calls[1][1]["max_tokens"],
            orchestrator.VALIDATION_MAX_TOKENS // 2,
        )

    def test_arbitrary_cas_output_never_counts_as_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "run_01").mkdir()
            with (
                mock.patch.object(
                    orchestrator,
                    "chat_completion",
                    return_value=(
                        'print("ARBITRARY")',
                        {"total_tokens": 10},
                        "stop",
                    ),
                ),
                mock.patch.object(
                    orchestrator,
                    "execute_python_code",
                    return_value=("ARBITRARY\n", "", 0),
                ),
            ):
                passed, details = orchestrator.cas_verify_candidate(
                    "candidate",
                    "url",
                    "key",
                    "model",
                    run_dir,
                    1,
                    1,
                )
        self.assertFalse(passed)
        self.assertIn("explicit pass", details.lower())

    def test_cas_compute_requires_exact_three_line_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "run_01").mkdir()
            with (
                mock.patch.object(
                    orchestrator,
                    "chat_completion",
                    return_value=("print('result')", {}, "stop"),
                ),
                mock.patch.object(
                    orchestrator,
                    "execute_python_code",
                    return_value=(
                        "IDENTITY: x=x\n"
                        "ASSUMPTIONS: none\n"
                        "RESULT: CONFIRMED\n"
                        "unlabelled extra output\n",
                        "",
                        0,
                    ),
                ),
            ):
                evidence = orchestrator.cas_compute_gap(
                    "Detailed Solution\nidentity x=x",
                    "Justification Gap: expand the identity",
                    "url",
                    "key",
                    "model",
                    run_dir,
                    1,
                    1,
                )
        self.assertIsNone(evidence)

    def test_three_profiles_accept_one_unchanged_candidate_hash(self):
        candidate = (
            "1. Summary\n"
            "a. Verdict: complete\n"
            "b. Method Sketch: direct.\n"
            "2. Detailed Solution\n"
            "A complete proof."
        )
        responses = [candidate] + [
            f"audit {index}\nVERDICT: yes"
            for index in range(3)
        ]
        queued = list(responses)

        def fake_chat_completion(*args, **kwargs):
            return queued.pop(0), {"total_tokens": 1}, "stop"

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                orchestrator,
                "chat_completion",
                side_effect=fake_chat_completion,
            ),
            mock.patch.object(orchestrator, "MAX_ITERATIONS", 3),
        ):
            accepted, result, summary = orchestrator.run_outer(
                1,
                "problem",
                "url",
                "key",
                "model",
                Path(directory),
                lean_mode="off",
                self_improve_mode="recovery",
                validation_enabled=False,
            )
        self.assertFalse(queued)
        self.assertTrue(accepted)
        self.assertEqual(result, candidate)
        self.assertEqual(
            [item["profile"] for item in summary["pass_artifacts"]],
            [item[0] for item in orchestrator.VERIFICATION_PROFILES],
        )
        self.assertEqual(
            len(
                {
                    item["candidate_sha256"]
                    for item in summary["pass_artifacts"]
                }
            ),
            1,
        )

    def test_frozen_statement_precedes_proof_and_binds_all_audits(self):
        candidate = (
            "1. Summary\n"
            "a. Verdict: complete\n"
            "b. Method Sketch: direct.\n"
            "2. Detailed Solution\n"
            "A complete proof."
        )
        responses = [
            candidate,
            "logic audit\nVERDICT: yes",
            (
                "import Mathlib\n"
                "set_option autoImplicit false\n"
                "theorem imo_problem : True := by"
            ),
            "statement fidelity audit\nVERDICT: yes",
            (
                "import Mathlib\n"
                "set_option autoImplicit false\n"
                "theorem imo_problem : True := by\n"
                "  trivial"
            ),
            "computation audit\nVERDICT: yes",
        ]
        systems = []

        def fake_chat_completion(*args, **kwargs):
            systems.append(args[3][0]["content"])
            return responses.pop(0), {"total_tokens": 1}, "stop"

        statement_hash = "a" * 64
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                orchestrator,
                "chat_completion",
                side_effect=fake_chat_completion,
            ),
            mock.patch.object(
                orchestrator,
                "execute_lean_code",
                return_value=(
                    True,
                    "Local Lean compiled. Statement SHA-256: "
                    + statement_hash,
                ),
            ),
            mock.patch.object(orchestrator, "MAX_ITERATIONS", 3),
        ):
            accepted, result, summary = orchestrator.run_outer(
                1,
                "problem",
                "url",
                "key",
                "model",
                Path(directory),
                lean_mode="required",
                lean_project=Path(directory) / "lean",
                validation_enabled=False,
                self_improve_mode="recovery",
            )
        self.assertTrue(accepted)
        self.assertEqual(result, candidate)
        self.assertFalse(responses)
        self.assertIn("IMO grader", systems[1])
        self.assertIn("Lean 4", systems[2])
        self.assertIn("IMO grader", systems[3])
        self.assertIn("Lean 4", systems[4])
        frozen_hash = orchestrator.sha256_text(
            "import Mathlib\n"
            "set_option autoImplicit false\n"
            "theorem imo_problem : True := by"
        )
        self.assertEqual(
            summary["formal_statement_sha256"],
            frozen_hash,
        )
        self.assertEqual(
            summary["elaborated_statement_sha256"],
            statement_hash,
        )
        self.assertEqual(
            {
                item["formal_statement_sha256"]
                for item in summary["pass_artifacts"]
            },
            {frozen_hash},
        )

    def test_repair_cannot_change_frozen_statement(self):
        candidate = (
            "1. Summary\n"
            "a. Verdict: complete\n"
            "b. Method Sketch: direct.\n"
            "2. Detailed Solution\n"
            "A complete proof."
        )
        frozen = (
            "import Mathlib\n"
            "set_option autoImplicit false\n"
            "theorem imo_problem : True := by"
        )
        responses = [
            candidate,
            "logic audit\nVERDICT: yes",
            frozen,
            "statement fidelity audit\nVERDICT: yes",
            frozen + "\n  fail_if_success trivial",
            (
                "import Mathlib\n"
                "set_option autoImplicit false\n"
                "theorem imo_problem : False := by\n"
                "  contradiction"
            ),
            frozen + "\n  trivial",
            "computation audit\nVERDICT: yes",
        ]
        compiled_sources = []

        def fake_chat_completion(*args, **kwargs):
            return responses.pop(0), {"total_tokens": 1}, "stop"

        def fake_execute_lean(source, *args, **kwargs):
            compiled_sources.append(source)
            if len(compiled_sources) == 1:
                return False, "compiler failure"
            return (
                True,
                "Local Lean compiled. Statement SHA-256: " + "b" * 64,
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                orchestrator,
                "chat_completion",
                side_effect=fake_chat_completion,
            ),
            mock.patch.object(
                orchestrator,
                "execute_lean_code",
                side_effect=fake_execute_lean,
            ),
            mock.patch.object(orchestrator, "MAX_ITERATIONS", 4),
        ):
            accepted, _, summary = orchestrator.run_outer(
                1,
                "problem",
                "url",
                "key",
                "model",
                Path(directory),
                lean_mode="required",
                lean_project=Path(directory) / "lean",
                validation_enabled=False,
                self_improve_mode="recovery",
            )
        self.assertTrue(accepted)
        self.assertFalse(responses)
        self.assertEqual(len(compiled_sources), 2)
        self.assertTrue(
            all(source.startswith(frozen) for source in compiled_sources)
        )
        self.assertEqual(
            summary["formal_statement_sha256"],
            orchestrator.sha256_text(frozen),
        )

    def test_rejected_statement_is_redrafted_without_mutating_candidate(self):
        candidate = (
            "1. Summary\n"
            "a. Verdict: complete\n"
            "b. Method Sketch: direct.\n"
            "2. Detailed Solution\n"
            "A complete proof."
        )
        bad_statement = (
            "import Mathlib\n"
            "set_option autoImplicit false\n"
            "theorem imo_problem : False := by"
        )
        good_statement = (
            "import Mathlib\n"
            "set_option autoImplicit false\n"
            "theorem imo_problem : True := by"
        )
        responses = [
            candidate,
            "logic audit\nVERDICT: yes",
            bad_statement,
            "weakened translation\nVERDICT: no",
            good_statement,
            "faithful translation\nVERDICT: yes",
            good_statement + "\n  trivial",
            "computation audit\nVERDICT: yes",
        ]
        model_labels = []

        def fake_chat_completion(*args, **kwargs):
            model_labels.append(args[3][0]["content"])
            return responses.pop(0), {"total_tokens": 1}, "stop"

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                orchestrator,
                "chat_completion",
                side_effect=fake_chat_completion,
            ),
            mock.patch.object(
                orchestrator,
                "execute_lean_code",
                return_value=(
                    True,
                    "Local Lean compiled. Statement SHA-256: " + "c" * 64,
                ),
            ),
            mock.patch.object(orchestrator, "MAX_ITERATIONS", 4),
        ):
            accepted, result, summary = orchestrator.run_outer(
                1,
                "problem",
                "url",
                "key",
                "model",
                Path(directory),
                lean_mode="required",
                lean_project=Path(directory) / "lean",
                validation_enabled=False,
                self_improve_mode="recovery",
            )
        self.assertTrue(accepted)
        self.assertEqual(result, candidate)
        self.assertFalse(responses)
        self.assertNotIn(
            orchestrator.step1_prompt.strip(),
            model_labels[4:],
        )
        self.assertEqual(
            [item["profile"] for item in summary["pass_artifacts"]],
            ["proof_logic", "statement_fidelity", "computation"],
        )

    def test_missing_machine_verdict_fails_closed(self):
        self.assertEqual(orchestrator.parse_verdict("The proof is correct."), "no")
        self.assertEqual(
            orchestrator.parse_verdict(
                "The proof is correct.\nVERDICT: yes"
            ),
            "yes",
        )


class SafetyUtilityTests(unittest.TestCase):
    def test_self_improve_recovery_detects_explicitly_partial_solution(self):
        partial = (
            "1. Summary\n"
            "I have not found a complete solution, but proved a lemma.\n"
            "2. Detailed Solution\n"
            "Proof of the lemma."
        )
        complete = (
            "1. Summary\n"
            "I have successfully solved the problem.\n"
            "2. Detailed Solution\n"
            "Complete proof."
        )
        self.assertFalse(orchestrator.candidate_is_complete(partial))
        self.assertTrue(orchestrator.candidate_is_complete(complete))
        solver_messages = [{"role": "user", "content": "problem"}]
        recovery_prompt = orchestrator.build_self_improvement_messages(
            solver_messages,
            partial,
            recovery=True,
        )[-1]["content"]
        review_prompt = orchestrator.build_self_improvement_messages(
            solver_messages,
            complete,
            recovery=False,
        )[-1]["content"]
        self.assertIn("empty, truncated", recovery_prompt)
        self.assertIn("Independently audit", review_prompt)

    def test_wall_clock_timeout_aborts_stream_and_preserves_partial_reasoning(
        self,
    ):
        closed = threading.Event()

        class StreamingResponse:
            status_code = 200

            def close(self):
                closed.set()

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=True):
                yield (
                    'data: {"choices":[{"delta":'
                    '{"reasoning_content":"partial thought"}}]}'
                )
                closed.wait(timeout=1)

        with (
            mock.patch.object(orchestrator, "WALL_CLOCK_TIMEOUT", 0.02),
            mock.patch.object(
                orchestrator.requests,
                "post",
                return_value=StreamingResponse(),
            ),
        ):
            with self.assertRaises(orchestrator._WallClockTimeout) as raised:
                orchestrator.chat_completion(
                    "url",
                    "key",
                    "model",
                    [{"role": "user", "content": "problem"}],
                )
        self.assertEqual(raised.exception.partial_reasoning, "partial thought")

    def test_generated_python_policy_rejects_host_access(self):
        violations = orchestrator.python_policy_violations(
            "import os\nprint(os.environ)"
        )
        self.assertIn("forbidden import: os", violations)
        indirect = orchestrator.python_policy_violations(
            'import random\nrandom._os.system("echo unsafe")'
        )
        self.assertIn("forbidden attribute: _os", indirect)
        self.assertIn("forbidden attribute: system", indirect)

    def test_generated_python_output_is_bounded(self):
        stdout, stderr, returncode = orchestrator.execute_python_code(
            'print("x" * 100)',
            output_limit=16,
        )
        self.assertEqual(stdout, "")
        self.assertIn("exceeded 16 bytes", stderr)
        self.assertEqual(returncode, -3)

    def test_atomic_lock_creates_missing_output_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "missing" / "solution.md"
            lock = orchestrator.acquire_output_lock(output)
            try:
                self.assertTrue(lock.exists())
                with self.assertRaises(RuntimeError):
                    orchestrator.acquire_output_lock(output)
            finally:
                orchestrator.release_output_lock(lock)
            self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
