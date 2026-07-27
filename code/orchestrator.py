#!/usr/bin/env python3
"""
IMO 2026 direct solver orchestrator.

Harness-agnostic background script implementing a solve, evidence, audit,
formalization, and correction loop using OpenAI-compatible chat completions.

Usage:
    python3 orchestrator.py \
        --problem problems/imo2026_p1.txt \
        --run-dir /tmp/imo26-run \
        --output solutions/imo2026_p1.md

Environment fallbacks: IMO_SOLVER_API_URL, IMO_SOLVER_TOKEN, IMO_SOLVER_MODEL,
IMO_VERIFIER_MODEL, IMO_SELF_IMPROVE, IMO_VALIDATION_MODE, IMO_LEAN_MODE,
IMO_AXLE_MODE, AXLE_API_KEY, AXLE_ENVIRONMENT

"""

import argparse
import ast
import atexit
import hashlib
import json
import os
import shutil
import sys
import subprocess
import re
import stat
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from prompts import (
    step1_prompt,
    self_improvement_prompt,
    self_review_prompt,
    correction_prompt,
    empirical_probe_prompt,
    cas_verify_prompt,
    cas_compute_prompt,
    lean_statement_prompt,
    lean_formalize_prompt,
    lean_repair_prompt,
    verification_system_prompt,
    verification_reminder,
    refinement_prompt,
)

# -- Constants --

MAX_ITERATIONS = 30
MAX_ERRORS = 3
MAX_OUTER_RUNS = 10
MAX_TOKENS = 256_000
CORRECT_MAX_TOKENS = 128_000
THINKING_BUDGET = 200_000
CORRECT_THINKING_BUDGET = 100_000
HTTP_TIMEOUT = 5400
MAX_TRANSPORT_RETRIES = 3
MAX_INFRA_RETRIES = 5  # consecutive infra errors before giving up
INFRA_BACKOFF_BASE = 30  # seconds, doubled each consecutive infra error
VALIDATION_TIMEOUT = 60  # seconds for executing empirical/CAS validation scripts
VALIDATION_OUTPUT_LIMIT = 100_000
VALIDATION_MAX_TOKENS = 65536  # token budget for validation code generation
VALIDATION_THINKING_BUDGET = 4096  # thinking budget for validation code generation
LEAN_TIMEOUT = 300
LEAN_MAX_REPAIRS = 1
LEAN_MAX_TOKENS = 128_000
LEAN_THINKING_BUDGET = 64_000
FORMAL_MAX_ATTEMPTS = 2
LIVE_TRACE_INTERVAL = 5

VERIFICATION_PROFILES = (
    (
        "proof_logic",
        "Audit the complete informal proof: its logical chain, dependencies, "
        "case coverage, definitions, boundary cases, and circularity. Check "
        "that every decisive lemma is proved and the claimed conclusion follows.",
    ),
    (
        "statement_fidelity",
        "Audit quantifiers, domains, hypotheses, and the exact conclusion. "
        "When a proposed Lean statement is supplied, compare it directly with "
        "the natural-language problem and actively seek weakened, strengthened, "
        "or missing cases. Judge statement fidelity only; the proof is audited "
        "separately.",
    ),
    (
        "computation",
        "Audit every algebraic, combinatorial, and numerical transformation. "
        "Check the assumptions and coverage of supplied computational evidence, "
        "then perform a final consistency check between the informal proof and "
        "the verified frozen formal statement.",
    ),
)
REQUIRED_PASSES = len(VERIFICATION_PROFILES)

# Wall-clock timeout per API call. A timer thread closes an active response so
# a blocked streaming read is interrupted, then the caller gets both partial
# reasoning and partial visible content.
WALL_CLOCK_TIMEOUT = 5400  # max wall-clock per API call (90 min)


class _WallClockTimeout(Exception):
    """Raised when the wall-clock timer fires during an API call."""
    def __init__(self, message, partial_content="", partial_reasoning=""):
        super().__init__(message)
        self.partial_content = partial_content
        self.partial_reasoning = partial_reasoning


class InfrastructureError(Exception):
    """Raised when the API endpoint is unreachable (connection refused, DNS
    failure, etc.). Distinct from model errors (wrong answer) so the caller
    can wait with backoff instead of burning through outer runs."""


class ConfigurationError(Exception):
    """Raised for credentials, endpoint, or response-contract failures that
    retries cannot repair."""


class UsageLedger:
    """Record every model attempt and expose aggregate usage."""

    def __init__(self, path=None):
        self.path = path
        self.events = []

    def record(self, label, usage=None, finish="unknown", status="completed"):
        usage = usage or {}
        event = {
            "timestamp": now_utc(),
            "label": label,
            "status": status,
            "finish_reason": finish,
            "usage": usage,
        }
        self.events.append(event)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    @property
    def total_tokens(self):
        return sum(
            int(event["usage"].get("total_tokens", 0) or 0)
            for event in self.events
        )


class _WallClockTimer:
    """Interrupt an active streaming response when the deadline expires."""

    def __init__(self, timeout, log_fn=None):
        self._timer = None
        self._fired = False
        self._timeout = timeout
        self._log_fn = log_fn
        self._abort_fn = None

    def _fire(self):
        self._fired = True
        if self._log_fn:
            self._log_fn(f"wall-clock timer fired after {self._timeout}s")
        if self._abort_fn:
            try:
                self._abort_fn()
            except Exception:
                pass

    def start(self):
        self._fired = False
        self._timer = threading.Timer(self._timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def set_abort(self, abort_fn):
        self._abort_fn = abort_fn
        if self._fired:
            abort_fn()

    def cancel(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._abort_fn = None

    @property
    def fired(self):
        return self._fired


NEUTRAL_COMPLETE_REQUEST = (
    "Please return your complete final response now, keeping it under 2500 words. "
    "Do not restate the problem. Provide only your final answer following the required output format."
)

PRESENTATION_LIMIT_NOTE = (
    "\n\n(Presentation limit: keep your complete final response under 2500 words. "
    "This is a presentation limit only, not a mathematical constraint or hint.)"
)

PIVOT_HINT = (
    "Note: A previous attempt at this problem failed verification. "
    "Try a fundamentally different approach — different key idea, "
    "different technique, or different angle of attack."
)


# -- Utilities --

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_progress(run_dir, message):
    with open(run_dir / "progress.log", "a", encoding="utf-8") as f:
        f.write(f"[{now_utc()}] {message}\n")
        f.flush()


def save_state(run_dir, state):
    tmp = run_dir / "state.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(run_dir / "state.json")


def save_text(directory, filename, content):
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


def save_atomic_text(path, content):
    """Replace a durable text artifact without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_verdict(text):
    """Parse the verifier's required final machine-verdict line."""
    if not text:
        return "no"
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    if not lines:
        return "no"
    match = re.fullmatch(r"verdict:\s*(yes|improve|no)", lines[-1])
    return match.group(1) if match else "no"


def candidate_is_complete(candidate):
    """Recognize the required solver output shape before formal verification."""
    if not candidate or not candidate.strip():
        return False
    lowered = candidate.lower()
    summary = lowered.split("detailed solution", 1)[0]
    incomplete_markers = (
        "partial solution",
        "incomplete solution",
        "not found a complete solution",
        "not have a complete solution",
        "unable to complete",
        "could not complete",
    )
    return (
        "summary" in lowered
        and "detailed solution" in lowered
        and not any(marker in summary for marker in incomplete_markers)
    )


def acquire_output_lock(output_path):
    """Atomically claim the output lock, replacing only a stale lock."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.with_suffix(output_path.suffix + ".lock")
    for _ in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            try:
                raw_pid = lock_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot read existing output lock {lock_path}: {exc}"
                ) from exc
            try:
                old_pid = int(raw_pid)
            except ValueError:
                lock_path.unlink(missing_ok=True)
                continue
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                lock_path.unlink(missing_ok=True)
                continue
            except PermissionError as exc:
                raise RuntimeError(
                    f"Cannot inspect lock owner PID {old_pid}: {exc}"
                ) from exc
            raise RuntimeError(
                f"Another orchestrator (PID {old_pid}) is already running "
                f"for {output_path}."
            )
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            return lock_path
    raise RuntimeError(f"Could not acquire output lock: {lock_path}")


def release_output_lock(lock_path):
    try:
        owner = int(lock_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if owner == os.getpid():
        lock_path.unlink(missing_ok=True)


def _flush_stream_artifacts(reasoning_path, reasoning_parts, content_parts):
    if reasoning_path is None:
        return
    reasoning_path.parent.mkdir(parents=True, exist_ok=True)
    reasoning_path.write_text(
        "".join(reasoning_parts),
        encoding="utf-8",
    )
    reasoning_path.with_suffix(".partial.md").write_text(
        "".join(content_parts),
        encoding="utf-8",
    )


# -- API --

def chat_completion(api_url, api_key, model, messages, log_fn=None, max_tokens=None, thinking_budget=None, reasoning_path=None):
    """Return (content, usage, finish_reason) from an OpenAI-compatible call.

    The configured endpoint is expected to expose reasoning_content separately
    from visible content in streaming deltas. Both channels are preserved.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens if max_tokens else MAX_TOKENS,
        "thinking": {"type": "enabled", "budget_tokens": thinking_budget if thinking_budget else THINKING_BUDGET},
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    last_error = None
    for attempt in range(1, MAX_TRANSPORT_RETRIES + 1):
        content_parts = []
        reasoning_parts = []
        usage = {}
        finish = "unknown"
        try:
            wc_timer = _WallClockTimer(WALL_CLOCK_TIMEOUT, log_fn=log_fn)
            wc_timer.start()
            try:
                resp = requests.post(
                    api_url,
                    headers=headers,
                    json=payload,
                    timeout=(30, HTTP_TIMEOUT),
                    stream=True,
                )
                wc_timer.set_abort(resp.close)
                if resp.status_code in (400, 401, 403, 404, 422):
                    raise ConfigurationError(
                        f"Model endpoint rejected the request "
                        f"(HTTP {resp.status_code})."
                    )
                resp.raise_for_status()

                last_trace_flush = time.monotonic()
                for line in resp.iter_lines(decode_unicode=True):
                    if wc_timer.fired:
                        _flush_stream_artifacts(
                            reasoning_path, reasoning_parts, content_parts
                        )
                        raise _WallClockTimeout(
                            f"Wall-clock timeout after {WALL_CLOCK_TIMEOUT}s "
                            "(server may be sending keepalive without real data)",
                            partial_content="".join(content_parts),
                            partial_reasoning="".join(reasoning_parts),
                        )
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                    elif line.startswith("data:"):
                        data_str = line[5:]
                    else:
                        continue
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if "usage" in chunk and chunk["usage"]:
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}
                    if delta.get("reasoning_content"):
                        reasoning_parts.append(delta["reasoning_content"])
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                    if (
                        reasoning_path is not None
                        and time.monotonic() - last_trace_flush
                        >= LIVE_TRACE_INTERVAL
                    ):
                        _flush_stream_artifacts(
                            reasoning_path, reasoning_parts, content_parts
                        )
                        last_trace_flush = time.monotonic()
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish = fr
                if wc_timer.fired:
                    _flush_stream_artifacts(
                        reasoning_path, reasoning_parts, content_parts
                    )
                    raise _WallClockTimeout(
                        f"Wall-clock timeout after {WALL_CLOCK_TIMEOUT}s",
                        partial_content="".join(content_parts),
                        partial_reasoning="".join(reasoning_parts),
                    )
                content = "".join(content_parts)
            finally:
                wc_timer.cancel()
            _flush_stream_artifacts(
                reasoning_path, reasoning_parts, content_parts
            )
            return content, usage, finish
        except ConfigurationError:
            raise
        except requests.exceptions.RequestException as exc:
            last_error = exc
            _flush_stream_artifacts(
                reasoning_path, reasoning_parts, content_parts
            )
            if wc_timer.fired:
                raise _WallClockTimeout(
                    f"Wall-clock timeout after {WALL_CLOCK_TIMEOUT}s",
                    partial_content="".join(content_parts),
                    partial_reasoning="".join(reasoning_parts),
                ) from exc
            is_network_error = isinstance(
                exc,
                (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                ),
            )
            if attempt < MAX_TRANSPORT_RETRIES:
                backoff = (
                    INFRA_BACKOFF_BASE * (2 ** (attempt - 1))
                    if is_network_error
                    else 5 * attempt
                )
                if log_fn:
                    log_fn(f"attempt {attempt}/{MAX_TRANSPORT_RETRIES} failed: {exc}, retrying in {backoff}s")
                time.sleep(backoff)
            else:
                if log_fn:
                    log_fn(f"attempt {attempt}/{MAX_TRANSPORT_RETRIES} failed: {exc}, no retries left")
                raise InfrastructureError(
                    "Model endpoint unavailable after "
                    f"{MAX_TRANSPORT_RETRIES} attempts: {last_error}"
                ) from last_error
        except _WallClockTimeout as exc:
            # Do NOT retry on wall-clock timeout. The call took too long
            # (server stall or oversized request). Retrying wastes another
            # full timeout period. Let the caller handle it.
            if log_fn:
                log_fn(f"attempt {attempt}/{MAX_TRANSPORT_RETRIES} wall-clock timeout, not retrying")
            raise
        except Exception as exc:
            last_error = exc
            _flush_stream_artifacts(
                reasoning_path, reasoning_parts, content_parts
            )
            if wc_timer.fired:
                raise _WallClockTimeout(
                    f"Wall-clock timeout after {WALL_CLOCK_TIMEOUT}s",
                    partial_content="".join(content_parts),
                    partial_reasoning="".join(reasoning_parts),
                ) from exc
            if attempt < MAX_TRANSPORT_RETRIES:
                if log_fn:
                    log_fn(f"attempt {attempt}/{MAX_TRANSPORT_RETRIES} failed: {exc}, retrying")
                time.sleep(5 * attempt)
            else:
                if log_fn:
                    log_fn(f"attempt {attempt}/{MAX_TRANSPORT_RETRIES} failed: {exc}, no retries left")
    raise RuntimeError(
        f"API call failed after {MAX_TRANSPORT_RETRIES} attempts: {last_error}"
    )


# -- Prompt builders --
# Message structures retain the useful multi-turn correction pattern:
# - Correction uses multi-turn (user/assistant/user) so the model sees its own
#   previous solution as context and can fix specific issues.
# - Verifier puts all instructions in the user message with a minimal system
#   prompt and emits its own strict machine-verdict line.

DNL = "\n\n"
DIV = "\n" + "=" * 70 + "\n"


def build_solver_messages(problem, outer_run=1, pivot_hint=None, empirical_results=None, failure_context=None):
    user = problem.strip() + PRESENTATION_LIMIT_NOTE
    if empirical_results:
        user += (
            DNL
            + "### Untrusted Small-Case Evidence ###"
            + DNL
            + "Use this only for conjecture generation and sanity checks. "
            + "It is not a proof and may contain implementation mistakes."
            + DNL
            + empirical_results
        )
    if failure_context:
        user += chr(10) + chr(10) + failure_context
    if outer_run > 1:
        hint = pivot_hint if pivot_hint else PIVOT_HINT
        user += chr(10) + chr(10) + hint
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": user},
    ]


def build_self_improvement_messages(
    solver_messages,
    solution,
    recovery=True,
):
    return list(solver_messages) + [
        {"role": "assistant", "content": solution},
        {
            "role": "user",
            "content": (
                self_improvement_prompt
                if recovery
                else self_review_prompt
            ).strip(),
        },
    ]


def extract_section(text, marker, after=True):
    idx = text.find(marker)
    if idx == -1:
        return ""
    if after:
        return text[idx + len(marker):].strip()
    return text[:idx].strip()


def build_verifier_messages(
    problem,
    solution,
    formal_report=None,
    computational_report=None,
    profile=None,
    proposed_formal_statement=None,
):
    detailed = extract_section(solution, "Detailed Solution")
    if not detailed:
        detailed = solution.strip()
    user = (
        verification_system_prompt.strip() + DNL
        + DIV + "### Problem ###" + DNL + problem.strip() + DNL
        + DIV + "### Solution ###" + DNL + detailed + DNL
    )
    if formal_report:
        user += (
            DIV + "### Formal Verification Report ###" + DNL
            + formal_report.strip() + DNL
            + "A formal backend pass proves only the encoded theorem. Check that "
            + "`imo_problem` faithfully states the entire natural-language problem; "
            + "flag any weakened, altered, or incomplete formalization as a Critical Error."
            + DNL
        )
    if proposed_formal_statement:
        user += (
            DIV + "### Proposed Lean Statement (Not Yet Proved) ###" + DNL
            + proposed_formal_statement.strip() + DNL
            + "Audit only whether this exact theorem statement faithfully "
            + "encodes every quantifier, domain, hypothesis, and conclusion "
            + "of the original problem. It will be frozen only if this audit "
            + "passes."
            + DNL
        )
    if computational_report:
        user += (
            DIV + "### Untrusted Computational Evidence ###" + DNL
            + computational_report.strip() + DNL
            + "Audit the code coverage, assumptions, and relevance. Treat it "
            + "as supporting evidence only, never as a replacement for proof."
            + DNL
        )
    if profile:
        user += (
            DIV + "### Assigned Audit Profile ###" + DNL
            + profile.strip() + DNL
            + "This is your exclusive audit scope. Do not fail the candidate "
            + "for concerns assigned to another profile; report only findings "
            + "within this scope." + DNL
        )
    user += verification_reminder.strip()
    return [
        {"role": "system", "content": "You are an expert IMO grader. Follow the instructions exactly."},
        {"role": "user", "content": user},
    ]


def build_correction_messages(
    problem,
    solution,
    verification,
    computational_report=None,
):
    bug_report = verification.strip()
    user2 = correction_prompt.strip() + DNL + DIV + "### Full Verification Report ###" + DNL + bug_report
    if computational_report:
        user2 += (
            DNL + DIV + "### Untrusted Computational Evidence ###" + DNL
            + computational_report.strip()
            + DNL
            + "Use this evidence to locate the issue, but include a complete "
            + "human-readable derivation in the corrected proof."
        )
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": problem.strip()},
        {"role": "assistant", "content": solution},
        {"role": "user", "content": user2},
    ]


def build_refinement_messages(
    problem,
    solution,
    verification,
    computational_report=None,
):
    bug_report = verification.strip()
    user2 = refinement_prompt.strip() + DNL + DIV + "### Full Verification Report ###" + DNL + bug_report
    if computational_report:
        user2 += (
            DNL + DIV + "### Untrusted Computational Evidence ###" + DNL
            + computational_report.strip()
            + DNL
            + "Incorporate an explicit mathematical derivation; do not cite "
            + "the computation as the proof."
        )
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": problem.strip()},
        {"role": "assistant", "content": solution},
        {"role": "user", "content": user2},
    ]


def build_lean_statement_messages(problem):
    return [
        {
            "role": "system",
            "content": (
                "You are an expert Lean 4 and Mathlib formalizer. "
                "Output only the requested theorem-statement prefix."
            ),
        },
        {
            "role": "user",
            "content": lean_statement_prompt.strip() + DNL + problem.strip(),
        },
    ]


def build_lean_formalization_messages(
    problem,
    solution,
    frozen_statement,
):
    return [
        {
            "role": "system",
            "content": "You are an expert Lean 4 and Mathlib formalizer. Output Lean source only.",
        },
        {
            "role": "user",
            "content": (
                lean_formalize_prompt.strip() + DNL
                + frozen_statement.strip() + DNL
                + DIV + "### Original Problem ###" + DNL + problem.strip()
                + DNL + DIV + "### Informal Solution ###" + DNL
                + solution.strip()
            ),
        },
    ]


def build_lean_repair_messages(
    problem,
    solution,
    frozen_statement,
    lean_source,
    report,
):
    return [
        {
            "role": "system",
            "content": "You are an expert Lean 4 and Mathlib proof engineer. Output Lean source only.",
        },
        {
            "role": "user",
            "content": (
                lean_repair_prompt.strip() + DNL
                + DIV + "### Frozen Lean Statement Prefix ###" + DNL
                + frozen_statement.strip() + DNL
                + DIV + "### Original Problem ###" + DNL + problem.strip()
                + DNL
                + DIV + "### Informal Solution ###" + DNL + solution.strip() + DNL
                + DIV + "### Broken Lean Source ###" + DNL + lean_source.strip() + DNL
                + DIV + "### Formal Verification Report ###" + DNL + report.strip()
            ),
        },
    ]


# -- Solver loop --

SAFE_PYTHON_IMPORTS = {
    "collections",
    "decimal",
    "fractions",
    "functools",
    "heapq",
    "itertools",
    "math",
    "operator",
    "random",
    "statistics",
    "sympy",
}
FORBIDDEN_PYTHON_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "open",
    "setattr",
    "vars",
}
FORBIDDEN_PYTHON_ATTRIBUTES = {
    "environ",
    "getenv",
    "import_module",
    "popen",
    "spawn",
    "system",
}


def python_policy_violations(code):
    try:
        tree = ast.parse(strip_code_fences(code))
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg}"]
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in SAFE_PYTHON_IMPORTS:
                    violations.append(f"forbidden import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root not in SAFE_PYTHON_IMPORTS:
                violations.append(f"forbidden import: {node.module}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_PYTHON_NAMES or node.id.startswith("__"):
                violations.append(f"forbidden name: {node.id}")
        elif isinstance(node, ast.Attribute) and (
            node.attr.startswith("_")
            or node.attr in FORBIDDEN_PYTHON_ATTRIBUTES
        ):
            violations.append(f"forbidden attribute: {node.attr}")
    return sorted(set(violations))


def _sandbox_profile(readable_paths, writable_paths):
    def subpath(path):
        return f"(subpath {json.dumps(str(Path(path).resolve()))})"

    resolved_home = Path.home().resolve()
    home = subpath(resolved_home)
    read_exceptions = " ".join(
        f"(require-not {subpath(path)})" for path in readable_paths
    )
    read_exceptions += (
        f" (require-not (literal {json.dumps(str(resolved_home))}))"
    )
    write_exceptions = " ".join(
        f"(require-not {subpath(path)})" for path in writable_paths
    )
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        "(deny process-fork)\n"
        f"(deny file-read* (require-all {home} {read_exceptions}))\n"
        f"(deny file-write* (require-all {write_exceptions}))\n"
    )


def _sandboxed_command(command, readable_paths, writable_paths):
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return None
    profile = _sandbox_profile(readable_paths, writable_paths)
    return [sandbox_exec, "-p", profile, *command]


def _restricted_environment(home, extra=None):
    environment = {
        "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONNOUSERSITE": "1",
    }
    if extra:
        environment.update(extra)
    return environment


def execute_python_code(
    code,
    timeout=VALIDATION_TIMEOUT,
    output_limit=VALIDATION_OUTPUT_LIMIT,
):
    """Execute policy-checked Python in an OS sandbox.

    Returns rc=-2 when no supported sandbox is available or the source violates
    policy. Generated code is never executed directly on the host.
    """
    source = strip_code_fences(code)
    violations = python_policy_violations(source)
    if violations:
        return "", "Policy failure:\n" + "\n".join(violations), -2

    with tempfile.TemporaryDirectory(prefix="imo26-python-") as directory:
        work_dir = Path(directory)
        script_path = work_dir / "validation.py"
        script_path.write_text(source, encoding="utf-8")
        readable = [
            work_dir,
            "/System",
            "/usr",
            "/opt/homebrew",
            "/private/var/db/timezone",
        ]
        command = _sandboxed_command(
            [sys.executable, "-I", str(script_path)],
            readable,
            [work_dir],
        )
        if not command:
            return "", "No supported OS sandbox is available.", -2
        try:
            stdout_path = work_dir / "stdout.txt"
            stderr_path = work_dir / "stderr.txt"
            with (
                open(stdout_path, "wb") as stdout_handle,
                open(stderr_path, "wb") as stderr_handle,
            ):
                result = subprocess.run(
                    command,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    timeout=timeout,
                    cwd=work_dir,
                    env=_restricted_environment(work_dir),
                )
            if (
                stdout_path.stat().st_size > output_limit
                or stderr_path.stat().st_size > output_limit
            ):
                return (
                    "",
                    "Sandbox output exceeded "
                    f"{output_limit} bytes and was discarded.",
                    -3,
                )
            stdout_bytes = stdout_path.read_bytes()
            stderr_bytes = stderr_path.read_bytes()
            return (
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace"),
                result.returncode,
            )
        except subprocess.TimeoutExpired:
            return "", f"Timeout after {timeout}s", -1
        except OSError as exc:
            return "", str(exc), -1


def strip_code_fences(code):
    lines = code.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _strip_lean_comments_and_strings(code):
    output = []
    index = 0
    block_depth = 0
    in_string = False
    while index < len(code):
        pair = code[index:index + 2]
        char = code[index]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                index += 2
            elif pair == "-/":
                block_depth -= 1
                index += 2
            else:
                index += 1
            output.append(" ")
            continue
        if in_string:
            if char == "\\":
                index += 2
            else:
                in_string = char != '"'
                index += 1
            output.append(" ")
            continue
        if pair == "--":
            newline = code.find("\n", index)
            if newline == -1:
                output.append(" ")
                break
            output.append("\n")
            index = newline + 1
        elif pair == "/-":
            block_depth = 1
            output.append(" ")
            index += 2
        elif char == '"':
            in_string = True
            output.append(" ")
            index += 1
        else:
            output.append(char)
            index += 1
    return "".join(output)


def validate_lean_statement_prefix(code):
    """Return a canonical freezeable theorem prefix and policy failures."""
    source = strip_code_fences(code).strip()
    violations = lean_policy_violations(source)
    if "--" in source or "/-" in source:
        violations.append("comments are not allowed in a frozen statement")
    if not re.match(
        r"\Aimport Mathlib\s*\n"
        r"set_option autoImplicit false\s*\n"
        r"theorem imo_problem\b",
        source,
    ):
        violations.append(
            "statement must contain only the required import, option, and "
            "imo_problem theorem prefix"
        )
    without_comments = _strip_lean_comments_and_strings(source)
    if len(re.findall(r"\btheorem\b", without_comments)) != 1:
        violations.append("statement must declare exactly one theorem")
    if not re.search(r":=\s*by\s*\Z", without_comments):
        violations.append("statement must end exactly at `:= by`")
    return source, sorted(set(violations))


def lean_source_preserves_frozen_statement(code, frozen_statement):
    """Require generated proof source to preserve the reviewed prefix exactly."""
    source = strip_code_fences(code).strip()
    prefix = frozen_statement.strip()
    suffix = source[len(prefix):] if source.startswith(prefix) else ""
    return (
        source.startswith(prefix)
        and bool(suffix)
        and suffix[0].isspace()
        and bool(suffix.strip())
    )


def lean_policy_violations(code):
    source = strip_code_fences(code)
    without_comments = _strip_lean_comments_and_strings(source)
    violations = []
    for token in ("sorry", "admit", "axiom", "opaque", "unsafe"):
        if re.search(rf"\b{token}\b", without_comments):
            violations.append(f"forbidden token: {token}")
    risky_patterns = {
        r"\brun_cmd\b": "command execution",
        r"\brun_tac\b": "tactic-time command execution",
        r"#(?:eval|run)\b": "evaluation command",
        r"\b(?:elab|by_elab|elab_rules|syntax|macro|macro_rules|"
        r"initialize|builtin_initialize|extern)\b":
            "metaprogramming or initialization",
        r"\bimplemented_by\b": "alternate implementation",
        r"\b(?:include_str|include_bytes)\b": "compile-time file access",
        r"\bnative_decide\b": "native code execution",
        r"\b(?:IO|System|FilePath|Process)\b": "host I/O access",
    }
    for pattern, label in risky_patterns.items():
        if re.search(pattern, without_comments):
            violations.append(f"forbidden Lean feature: {label}")
    if re.search(r"\bset_option\s+autoImplicit\s+true\b", without_comments):
        violations.append("autoImplicit may not be enabled")
    if not re.search(r"\btheorem\s+imo_problem\b", without_comments):
        violations.append("missing theorem named imo_problem")
    if not re.match(r"\s*import\s+Mathlib\b", without_comments):
        violations.append("source must begin with `import Mathlib`")
    return violations


LEAN_DECLARATION_CHECK = """

open Lean Elab Command in
run_cmd
  let env ← getEnv
  match env.find? `imo_problem with
  | some (.thmInfo info) =>
      logInfo m!"IMO_DECLARATION_OK"
      logInfo m!"IMO_STATEMENT_BEGIN\n{info.type}\nIMO_STATEMENT_END"
  | some _ => throwError "imo_problem is not a theorem"
  | none => throwError "missing theorem named imo_problem"

#print axioms imo_problem
"""


def execute_lean_code(code, source_path, lean_project, timeout=LEAN_TIMEOUT):
    """Compile a generated proof locally using elan/lake and reject proof holes."""
    source = strip_code_fences(code)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    violations = lean_policy_violations(source)
    if violations:
        return False, "Policy failure:\n" + "\n".join(f"- {item}" for item in violations)
    checked_source = source + LEAN_DECLARATION_CHECK
    source_path.write_text(checked_source, encoding="utf-8")

    lake = shutil.which("lake")
    if not lake:
        candidate = Path.home() / ".elan" / "bin" / "lake"
        if candidate.is_file():
            lake = str(candidate)
    if not lake:
        return False, "Local Lean unavailable: lake was not found. Run scripts/setup_lean.sh."
    if not (lean_project / "lakefile.lean").is_file():
        return False, f"Local Lean project unavailable: {lean_project / 'lakefile.lean'} not found."

    elan_home = Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))
    lake_env = _restricted_environment(
        Path.home(),
        {"ELAN_HOME": str(elan_home)},
    )
    try:
        lean_binary = subprocess.run(
            [lake, "env", "which", "lean"],
            cwd=lean_project,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env=lake_env,
        ).stdout.strip()
        for name in ("LEAN_PATH", "LEAN_SRC_PATH", "LEAN_SYSROOT"):
            value = subprocess.run(
                [lake, "env", "printenv", name],
                cwd=lean_project,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
                env=lake_env,
            ).stdout.strip()
            lake_env[name] = value
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not resolve the pinned Lean environment: {exc}"

    command = _sandboxed_command(
        [lean_binary, str(source_path.resolve())],
        [
            "/System",
            "/usr",
            "/opt/homebrew",
            lean_project.parent,
            lean_project,
            source_path.parent,
            elan_home,
        ],
        [source_path.parent],
    )
    if not command:
        return False, "Local Lean sandbox unavailable: sandbox-exec was not found."
    try:
        result = subprocess.run(
            command,
            cwd=lean_project,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=lake_env,
        )
    except subprocess.TimeoutExpired:
        return False, f"Local Lean timeout after {timeout}s."
    except OSError as exc:
        return False, f"Local Lean execution error: {exc}"

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, f"Local Lean failed (rc={result.returncode}):\n{output}"
    if "declaration uses 'sorry'" in output or "IMO_DECLARATION_OK" not in output:
        return False, f"Local Lean rejected a proof hole:\n{output}"
    statement_match = re.search(
        r"IMO_STATEMENT_BEGIN\s*(.*?)\s*IMO_STATEMENT_END",
        output,
        re.DOTALL,
    )
    statement = statement_match.group(1).strip() if statement_match else ""
    statement_hash = sha256_text(statement) if statement else ""
    return True, (
        "Local Lean compiled an actual theorem `imo_problem` without proof "
        f"holes. Statement SHA-256: {statement_hash or 'unavailable'}."
    ) + (
        "\nCompiler output:\n" + output if output else ""
    )


def formal_backends_pass(local_ok, axle_ok, axle_mode):
    if axle_mode == "required":
        return local_ok and axle_ok is True
    if axle_mode == "fallback":
        return local_ok or axle_ok is True
    return local_ok


def execute_axle_check(code, environment, timeout=LEAN_TIMEOUT):
    """Verify Lean source using AXLE. The caller must explicitly enable cloud use."""
    source = strip_code_fences(code)
    violations = lean_policy_violations(source)
    if violations:
        return False, "AXLE policy failure:\n" + "\n".join(
            f"- {item}" for item in violations
        )

    try:
        import asyncio
        from axle import AxleClient
    except ImportError:
        return False, "AXLE unavailable: install requirements-axle.txt."

    api_key = os.getenv("AXLE_API_KEY")
    if not api_key:
        return False, "AXLE unavailable: AXLE_API_KEY is not set."

    async def check():
        async with AxleClient(api_key=api_key) as client:
            return await client.check(
                content=source,
                environment=environment,
                ignore_imports=False,
                timeout_seconds=timeout,
            )

    try:
        result = asyncio.run(check())
    except Exception as exc:
        details = str(exc).replace(api_key, "<redacted>")
        return False, f"AXLE request failed: {type(exc).__name__}: {details}"

    failed = [str(item) for item in (result.failed_declarations or [])]
    errors = [str(item) for item in result.lean_messages.errors]
    warnings = [str(item) for item in result.lean_messages.warnings]
    tool_errors = [str(item) for item in result.tool_messages.errors]
    tool_warnings = [str(item) for item in result.tool_messages.warnings]
    report = json.dumps(
        {
            "okay": result.okay,
            "failed_declarations": failed,
            "lean_errors": errors,
            "lean_warnings": warnings,
            "tool_errors": tool_errors,
            "tool_warnings": tool_warnings,
        },
        indent=2,
        ensure_ascii=False,
    )
    return bool(result.okay and not failed), report


def save_execution_artifacts(
    directory,
    stem,
    source,
    stdout="",
    stderr="",
    metadata=None,
):
    save_text(directory, f"{stem}.py", strip_code_fences(source))
    save_text(directory, f"{stem}.stdout.txt", stdout)
    save_text(directory, f"{stem}.stderr.txt", stderr)
    if metadata is not None:
        save_text(
            directory,
            f"{stem}.json",
            json.dumps(metadata, indent=2, ensure_ascii=False),
        )


def pre_solve_validation(
    problem,
    api_url,
    api_key,
    model,
    run_dir,
    outer_run,
    usage_ledger=None,
    validation_enabled=True,
):
    """Generate, sandbox, and preserve small-case evidence before SOLVE."""
    label = f"RUN {outer_run} EMPIRICAL_PROBE"
    subdir = run_dir / f"run_{outer_run:02d}"
    subdir.mkdir(exist_ok=True)
    log_progress(run_dir, f"{label}: start")
    if not validation_enabled:
        log_progress(run_dir, f"{label}: skipped by --validation-mode off")
        return None
    messages = [
        {
            "role": "system",
            "content": "Write self-contained executable Python code only.",
        },
        {
            "role": "user",
            "content": (
                empirical_probe_prompt.strip()
                + DNL + DIV + "### Problem ###" + DNL + problem.strip()
            ),
        },
    ]
    content = ""
    reasoning_path = subdir / "reasoning_EMPIRICAL_PROBE.txt"
    try:
        content, usage, finish = chat_completion(
            api_url,
            api_key,
            model,
            messages,
            log_fn=lambda message: log_progress(
                run_dir, f"{label}: {message}"
            ),
            max_tokens=VALIDATION_MAX_TOKENS,
            thinking_budget=VALIDATION_THINKING_BUDGET,
            reasoning_path=reasoning_path,
        )
        if usage_ledger:
            usage_ledger.record("EMPIRICAL_PROBE", usage, finish)
        if finish != "stop":
            partial_content = content if content.strip() else ""
            retry_messages = list(messages)
            if partial_content:
                retry_messages.append(
                    {"role": "assistant", "content": partial_content}
                )
                retry_instruction = (
                    "Continue from the exact stopping point and finish the "
                    "script without repeating prior code."
                )
            else:
                reasoning_tail = ""
                if reasoning_path.exists():
                    reasoning_tail = reasoning_path.read_text(
                        encoding="utf-8"
                    )[-10000:]
                retry_instruction = (
                    "The prior response was incomplete. Return one complete "
                    "concise script with no commentary. Stay within the "
                    "original 150-line limit."
                )
                if reasoning_tail:
                    retry_instruction += (
                        "\n\nUse this prior reasoning tail:\n"
                        + reasoning_tail
                    )
            retry_messages.append(
                {"role": "user", "content": retry_instruction}
            )
            continued_content, usage, finish = chat_completion(
                api_url,
                api_key,
                model,
                retry_messages,
                log_fn=lambda message: log_progress(
                    run_dir, f"{label} RETRY: {message}"
                ),
                max_tokens=VALIDATION_MAX_TOKENS // 2,
                thinking_budget=VALIDATION_THINKING_BUDGET,
                reasoning_path=subdir / "reasoning_EMPIRICAL_PROBE_retry.txt",
            )
            if usage_ledger:
                usage_ledger.record(
                    "EMPIRICAL_PROBE_RETRY", usage, finish
                )
            content = (
                partial_content + continued_content
                if partial_content and finish == "stop"
                else continued_content
            )
        if finish != "stop":
            save_execution_artifacts(
                subdir,
                "empirical_probe",
                content,
                metadata={"finish_reason": finish, "executed": False},
            )
            log_progress(run_dir, f"{label}: incomplete response, skipped")
            return None
        stdout, stderr, rc = execute_python_code(content)
        save_execution_artifacts(
            subdir,
            "empirical_probe",
            content,
            stdout,
            stderr,
            {"finish_reason": finish, "returncode": rc, "executed": True},
        )
        if rc == 0 and stdout.strip():
            result = stdout.strip()
            log_progress(
                run_dir,
                f"{label}: sandboxed evidence produced {len(result)} chars",
            )
            return result
        log_progress(
            run_dir,
            f"{label}: sandboxed execution failed (rc={rc})",
        )
    except _WallClockTimeout as exc:
        if usage_ledger:
            usage_ledger.record(
                "EMPIRICAL_PROBE",
                finish="timeout",
                status="timeout",
            )
        save_execution_artifacts(
            subdir,
            "empirical_probe_partial",
            exc.partial_content,
            metadata={
                "finish_reason": "timeout",
                "reasoning_chars": len(exc.partial_reasoning),
                "executed": False,
            },
        )
        log_progress(
            run_dir,
            f"{label}: wall-clock timeout "
            f"({len(exc.partial_content)} content chars, "
            f"{len(exc.partial_reasoning)} reasoning chars); retrying",
        )
        if exc.partial_content.strip():
            retry_messages = list(messages) + [
                {
                    "role": "assistant",
                    "content": exc.partial_content,
                },
                {
                    "role": "user",
                    "content": (
                        "Continue from the exact stopping point and finish the "
                        "Python script. Do not repeat prior code."
                    ),
                },
            ]
        elif exc.partial_reasoning.strip():
            reasoning_tail = exc.partial_reasoning[-10000:]
            retry_messages = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        "The previous attempt timed out during reasoning. "
                        "Using this reasoning tail, return only one complete "
                        "concise Python script:\n\n" + reasoning_tail
                    ),
                },
            ]
        else:
            retry_messages = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        "Return one complete concise Python script now, with "
                        "no commentary."
                    ),
                },
            ]
        try:
            continued_content, usage, finish = chat_completion(
                api_url,
                api_key,
                model,
                retry_messages,
                log_fn=lambda message: log_progress(
                    run_dir, f"{label} TIMEOUT_RETRY: {message}"
                ),
                max_tokens=VALIDATION_MAX_TOKENS // 2,
                thinking_budget=VALIDATION_THINKING_BUDGET,
                reasoning_path=(
                    subdir / "reasoning_EMPIRICAL_PROBE_timeout_retry.txt"
                ),
            )
            if usage_ledger:
                usage_ledger.record(
                    "EMPIRICAL_PROBE_TIMEOUT_RETRY",
                    usage,
                    finish,
                )
            content = (
                exc.partial_content + continued_content
                if exc.partial_content.strip() and finish == "stop"
                else continued_content
            )
            if finish != "stop":
                save_execution_artifacts(
                    subdir,
                    "empirical_probe_timeout_retry",
                    content,
                    metadata={
                        "finish_reason": finish,
                        "executed": False,
                    },
                )
                log_progress(
                    run_dir,
                    f"{label}: timeout retry incomplete, skipped",
                )
                return None
            stdout, stderr, rc = execute_python_code(content)
            save_execution_artifacts(
                subdir,
                "empirical_probe",
                content,
                stdout,
                stderr,
                {
                    "finish_reason": finish,
                    "returncode": rc,
                    "executed": True,
                    "source": "timeout_retry",
                },
            )
            if rc == 0 and stdout.strip():
                result = stdout.strip()
                log_progress(
                    run_dir,
                    f"{label}: timeout retry produced "
                    f"{len(result)} chars of sandboxed evidence",
                )
                return result
            log_progress(
                run_dir,
                f"{label}: timeout retry execution failed (rc={rc})",
            )
        except _WallClockTimeout as retry_exc:
            if usage_ledger:
                usage_ledger.record(
                    "EMPIRICAL_PROBE_TIMEOUT_RETRY",
                    finish="timeout",
                    status="timeout",
                )
            save_execution_artifacts(
                subdir,
                "empirical_probe_timeout_retry_partial",
                retry_exc.partial_content,
                metadata={
                    "finish_reason": "timeout",
                    "reasoning_chars": len(
                        retry_exc.partial_reasoning
                    ),
                    "executed": False,
                },
            )
            log_progress(
                run_dir,
                f"{label}: timeout retry also timed out, skipped",
            )
    except (InfrastructureError, ConfigurationError):
        raise
    except Exception as exc:
        log_progress(run_dir, f"{label}: error: {exc}")
    return None


def cas_verify_candidate(
    candidate,
    api_url,
    api_key,
    model,
    run_dir,
    outer_run,
    iteration,
    usage_ledger=None,
    validation_enabled=True,
):
    """Generate and sandbox a SymPy audit; return fail-closed evidence."""
    label = f"ITER {iteration}" if iteration else "initial"
    log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: start")
    if not validation_enabled:
        return True, "CAS verification skipped by --validation-mode off."
    subdir = run_dir / f"run_{outer_run:02d}"
    artifact_stem = f"cas_verify_{iteration:02d}"
    detailed = extract_section(candidate, "Detailed Solution")
    if not detailed:
        detailed = candidate.strip()
    messages = [
        {"role": "system", "content": "You are a Python programmer specializing in SymPy. Write self-contained, executable Python code only."},
        {"role": "user", "content": cas_verify_prompt.strip() + DNL + detailed},
    ]
    try:
        content, usage, finish = chat_completion(
            api_url, api_key, model, messages,
            log_fn=lambda msg: log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: {msg}"),
            max_tokens=VALIDATION_MAX_TOKENS,
            thinking_budget=VALIDATION_THINKING_BUDGET,
            reasoning_path=run_dir / f"run_{outer_run:02d}" / f"reasoning_CAS_VERIFY_{label.replace(' ', '_')}.txt",
        )
        if usage_ledger:
            usage_ledger.record(f"{label} CAS_VERIFY", usage, finish)
        if finish != "stop":
            save_execution_artifacts(
                subdir,
                artifact_stem,
                content,
                metadata={"finish_reason": finish, "executed": False},
            )
            return False, (
                f"CAS verification response was incomplete "
                f"(finish={finish}); it was not executed."
            )
        stdout, stderr, rc = execute_python_code(content)
        save_execution_artifacts(
            subdir,
            artifact_stem,
            content,
            stdout,
            stderr,
            {"finish_reason": finish, "returncode": rc, "executed": True},
        )
        if rc == 0 and stdout.strip():
            lines = [line.strip() for line in stdout.splitlines() if line.strip()]
            if lines == ["NO_ALGEBRAIC_CLAIMS"]:
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: no algebraic claims found")
                return True, "CAS extraction reported no algebraic claims."
            if any(line.startswith("FAIL:") for line in lines):
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: FAIL detected")
                return False, stdout.strip()
            if lines and all(
                line.startswith("PASS:")
                and line[len("PASS:"):].strip()
                for line in lines
            ):
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: all claims PASS")
                return True, stdout.strip()
            return False, (
                "CAS output did not contain explicit PASS/FAIL lines."
            )
        return False, (
            f"CAS script failed in the sandbox (rc={rc}): {stderr[:500]}"
        )
    except (InfrastructureError, ConfigurationError):
        raise
    except Exception as exc:
        log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: error: {exc}")
        return False, f"CAS verification error: {exc}"


def cas_compute_gap(
    candidate,
    verification,
    api_url,
    api_key,
    model,
    run_dir,
    outer_run,
    iteration,
    usage_ledger=None,
    validation_enabled=True,
):
    """Produce preserved computational evidence without modifying the proof."""
    label = f"ITER {iteration}" if iteration else "initial"

    if (
        not validation_enabled
        or "Critical Error" in verification
        or "Justification Gap" not in verification
        or not re.search(
            r"identity|algebra|expand|comput|polynomial|simplif",
            verification,
            re.IGNORECASE,
        )
    ):
        return None

    log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: start")
    subdir = run_dir / f"run_{outer_run:02d}"
    artifact_stem = f"cas_compute_{iteration:02d}"
    detailed = extract_section(candidate, "Detailed Solution")
    if not detailed:
        detailed = candidate.strip()
    messages = [
        {"role": "system", "content": "You are a Python programmer specializing in SymPy. Write self-contained, executable Python code only."},
        {"role": "user", "content": cas_compute_prompt.strip() + DNL + detailed + DNL + DIV + "### Verification Report ###" + DNL + verification.strip()},
    ]
    try:
        content, usage, finish = chat_completion(
            api_url, api_key, model, messages,
            log_fn=lambda msg: log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: {msg}"),
            max_tokens=VALIDATION_MAX_TOKENS,
            thinking_budget=VALIDATION_THINKING_BUDGET,
            reasoning_path=run_dir / f"run_{outer_run:02d}" / f"reasoning_CAS_COMPUTE_{label.replace(' ', '_')}.txt",
        )
        if usage_ledger:
            usage_ledger.record(f"{label} CAS_COMPUTE", usage, finish)
        if finish == "length":
            log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: truncated, retry with halved budget")
            retry_messages = list(messages)
            if content.strip():
                retry_messages.append(
                    {"role": "assistant", "content": content}
                )
                retry_instruction = (
                    "Continue from the exact stopping point and finish the "
                    "script without repeating prior code."
                )
            else:
                retry_instruction = "Return a complete shorter script."
            retry_messages.append(
                {"role": "user", "content": retry_instruction}
            )
            partial_content = content if content.strip() else ""
            continued_content, usage, finish = chat_completion(
                api_url, api_key, model, retry_messages,
                log_fn=lambda msg: log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: {msg}"),
                max_tokens=VALIDATION_MAX_TOKENS // 2,
                thinking_budget=VALIDATION_THINKING_BUDGET,
                reasoning_path=run_dir / f"run_{outer_run:02d}" / f"reasoning_CAS_COMPUTE_{label.replace(' ', '_')}_retry.txt",
            )
            if usage_ledger:
                usage_ledger.record(
                    f"{label} CAS_COMPUTE_RETRY", usage, finish
                )
            content = (
                partial_content + continued_content
                if partial_content and finish == "stop"
                else continued_content
            )
        if finish != "stop":
            save_execution_artifacts(
                subdir,
                artifact_stem,
                content,
                metadata={"finish_reason": finish, "executed": False},
            )
            return None
        stdout, stderr, rc = execute_python_code(content)
        save_execution_artifacts(
            subdir,
            artifact_stem,
            content,
            stdout,
            stderr,
            {"finish_reason": finish, "returncode": rc, "executed": True},
        )
        if rc == 0 and stdout.strip():
            output = stdout.strip()
            lines = [
                line.strip()
                for line in output.splitlines()
                if line.strip()
            ]
            if (
                len(lines) == 3
                and lines[0].startswith("IDENTITY:")
                and lines[0][len("IDENTITY:"):].strip()
                and lines[1].startswith("ASSUMPTIONS:")
                and lines[2] == "RESULT: CONFIRMED"
            ):
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: IDENTITY CONFIRMED")
                return (
                    "Sandboxed SymPy evidence (not a proof):\n" + output
                )
            if (
                len(lines) == 3
                and lines[0].startswith("IDENTITY:")
                and lines[0][len("IDENTITY:"):].strip()
                and lines[1].startswith("ASSUMPTIONS:")
                and lines[2].startswith("RESULT: DENIED:")
            ):
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: IDENTITY DENIED")
                return "Sandboxed SymPy evidence found a denial:\n" + output
        log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: inconclusive")
    except (InfrastructureError, ConfigurationError):
        raise
    except Exception as exc:
        log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: error: {exc}")
    return None


def read_failure_ledger(run_dir):
    """Read cross-run failure context from this run directory."""
    ledger_path = run_dir / "failure_ledger.json"
    if not ledger_path.exists():
        return []
    try:
        return json.loads(ledger_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def write_failure_ledger(run_dir, entry):
    """Append cross-run failure context with an atomic file replacement."""
    ledger_path = run_dir / "failure_ledger.json"
    ledger = read_failure_ledger(run_dir)
    ledger.append(entry)
    save_atomic_text(
        ledger_path,
        json.dumps(ledger, indent=2, ensure_ascii=False),
    )


def build_failure_ledger_context(ledger):
    """Build concise prior-attempt context for a fresh SOLVE request."""
    if not ledger:
        return ""
    lines = ["### Prior Attempts (Cross-Run Knowledge) ###", ""]
    for entry in ledger:
        run = entry.get("run", "?")
        reason = entry.get("failure_reason", entry.get("errors", "unknown"))
        lines.append(f"Run {run}: {reason}")
        if entry.get("empirical_results"):
            lines.append(
                "  Prior untrusted small-case evidence: "
                f"{entry['empirical_results']}"
            )
        lines.append("")
    return chr(10).join(lines)

def extract_failure_reason(verification):
    """Extract a failure reason from the verification text.
    
    Gets the Summary section (Final Verdict + List of Findings) which
    contains the key information about what went wrong. The Detailed
    Verification Log is excluded to keep the pivot hint concise.
    """
    # Try to get everything from "Final Verdict" to "Detailed Verification"
    verdict = extract_section(verification, "Final Verdict", after=True)
    if not verdict:
        return verification.strip()[:500]
    # Cut at "Detailed Verification" if present (only keep Summary)
    idx = verdict.find("Detailed Verification")
    if idx != -1:
        verdict = verdict[:idx].strip()
    return verdict[:500].strip()


def build_pivot_hint(failure_reason=None):
    """Build a different-approach hint with concise prior failures."""
    hint = PIVOT_HINT
    if failure_reason:
        hint += chr(10) + chr(10) + "Previous failure reason(s): " + failure_reason
    return hint


def run_outer(
    outer_run,
    problem,
    api_url,
    api_key,
    model,
    run_dir,
    prev_failure_reason=None,
    empirical_results=None,
    failure_context=None,
    lean_mode="required",
    lean_project=None,
    axle_mode="off",
    axle_environment="lean-4.28.0",
    self_improve_mode="recovery",
    validation_enabled=True,
    usage_ledger=None,
    verifier_model=None,
):
    """Run one outer attempt. Returns (accepted, candidate, summary)."""
    if lean_mode == "off" and axle_mode != "off":
        raise ValueError("AXLE requires local Lean formalization")

    subdir = run_dir / f"run_{outer_run:02d}"
    subdir.mkdir(exist_ok=True)
    usage_ledger = usage_ledger or UsageLedger()
    verifier_model = verifier_model or model
    candidate_num = 0
    verify_num = 0
    pass_artifacts = []
    formal_cache = {}
    formal_gate_required = lean_mode == "required" or axle_mode == "required"
    _reasoning_seq = [0]

    def call(messages, label, max_tokens=None, thinking_budget=None):
        def log_fn(msg):
            log_progress(run_dir, f"RUN {outer_run} {label}: {msg}")

        call_model = (
            verifier_model
            if label.startswith("VERIFY ")
            else model
        )
        resolved_max_tokens = max_tokens or MAX_TOKENS
        resolved_thinking = thinking_budget or THINKING_BUDGET
        try:
            _reasoning_seq[0] += 1
            safe_label = label.replace(" ", "_")
            rpath = subdir / f"reasoning_{_reasoning_seq[0]:02d}_{safe_label}.txt"
            content, usage, finish = chat_completion(
                api_url,
                api_key,
                call_model,
                messages,
                log_fn=log_fn,
                max_tokens=resolved_max_tokens,
                thinking_budget=resolved_thinking,
                reasoning_path=rpath,
            )
            usage_ledger.record(
                f"RUN {outer_run} {label}",
                usage,
                finish,
            )
        except _WallClockTimeout as wc_exc:
            partial = wc_exc.partial_content
            partial_reasoning = wc_exc.partial_reasoning
            usage_ledger.record(
                f"RUN {outer_run} {label}",
                finish="timeout",
                status="timeout",
            )
            save_text(subdir, f"partial_{safe_label}.md", partial)
            log_progress(
                run_dir,
                f"RUN {outer_run} {label}: wall-clock timeout "
                f"({len(partial)} content chars, "
                f"{len(partial_reasoning)} reasoning chars)",
            )
            try:
                if partial.strip():
                    retry_messages = list(messages) + [
                        {"role": "assistant", "content": partial},
                        {"role": "user", "content": "Continue and complete your response from where you left off. Do not repeat what you already wrote."}
                    ]
                elif partial_reasoning.strip():
                    reasoning_tail = partial_reasoning[-10000:]
                    retry_messages = list(messages) + [
                        {"role": "user", "content": "Your previous attempt spent a long time reasoning but produced no final output. Here is the tail of your reasoning:" + chr(10) + chr(10) + reasoning_tail + chr(10) + chr(10) + "Based on this reasoning, write the complete solution now. Do not repeat the reasoning — output only the final solution."}
                    ]
                else:
                    # No partial content (likely still in reasoning phase) — start fresh
                    retry_messages = list(messages) + [
                        {"role": "user", "content": NEUTRAL_COMPLETE_REQUEST}
                    ]
                _reasoning_seq[0] += 1
                rpath_wc = subdir / f"reasoning_{_reasoning_seq[0]:02d}_{safe_label}_wc_retry.txt"
                retry_max_tokens = max(8192, resolved_max_tokens // 2)
                retry_thinking = min(
                    resolved_thinking,
                    retry_max_tokens - 8192,
                )
                content2, usage2, finish2 = chat_completion(
                    api_url,
                    api_key,
                    call_model,
                    retry_messages,
                    log_fn=log_fn,
                    max_tokens=retry_max_tokens,
                    thinking_budget=retry_thinking,
                    reasoning_path=rpath_wc,
                )
                usage_ledger.record(
                    f"RUN {outer_run} {label} TIMEOUT_RETRY",
                    usage2,
                    finish2,
                )
                if finish2 == "stop" and content2.strip():
                    if partial.strip():
                        return partial + content2
                    return content2
            except _WallClockTimeout as retry_exc:
                usage_ledger.record(
                    f"RUN {outer_run} {label} TIMEOUT_RETRY",
                    finish="timeout",
                    status="timeout",
                )
                save_text(
                    subdir,
                    f"partial_{safe_label}_retry.md",
                    retry_exc.partial_content,
                )
                log_progress(run_dir, f"RUN {outer_run} {label}: timeout retry also timed out")
            return ""

        log_progress(
            run_dir,
            f"RUN {outer_run} {label}: "
            f"{usage.get('total_tokens', 0)} tokens finish={finish}",
        )
        if finish != "stop":
            log_progress(
                run_dir,
                f"RUN {outer_run} {label}: incomplete response, retrying",
            )
            retry_messages = list(messages)
            if content.strip():
                retry_messages.append(
                    {"role": "assistant", "content": content}
                )
                retry_instruction = (
                    "The response ended before completion. Continue from "
                    "the exact stopping point and finish concisely. Do not "
                    "repeat prior text."
                )
            else:
                reasoning_tail = ""
                if rpath.exists():
                    reasoning_tail = rpath.read_text(
                        encoding="utf-8"
                    )[-10000:]
                retry_instruction = (
                    "The response ended during reasoning. Produce the "
                    "complete requested output now."
                )
                if reasoning_tail:
                    retry_instruction += (
                        "\n\nUse this prior reasoning tail:\n"
                        + reasoning_tail
                    )
            retry_messages.append(
                {"role": "user", "content": retry_instruction}
            )
            retry_max_tokens = max(8192, resolved_max_tokens // 2)
            retry_thinking = min(
                resolved_thinking,
                retry_max_tokens - 8192,
            )
            _reasoning_seq[0] += 1
            rpath_tr = subdir / f"reasoning_{_reasoning_seq[0]:02d}_{safe_label}_trunc_retry.txt"
            content2, usage2, finish2 = chat_completion(
                api_url,
                api_key,
                call_model,
                retry_messages,
                log_fn=log_fn,
                max_tokens=retry_max_tokens,
                thinking_budget=retry_thinking,
                reasoning_path=rpath_tr,
            )
            usage_ledger.record(
                f"RUN {outer_run} {label} INCOMPLETE_RETRY",
                usage2,
                finish2,
            )
            if finish2 == "stop" and content2.strip():
                return content + content2 if content.strip() else content2
            log_progress(
                run_dir,
                f"RUN {outer_run} {label}: retry incomplete "
                f"(finish={finish2}), failing closed",
            )
            return ""
        return content

    def draft_formal_statement(
        current_candidate,
        current_candidate_num,
        attempt,
    ):
        candidate_key = sha256_text(current_candidate)
        label = f"CANDIDATE {current_candidate_num:02d}"
        log_progress(
            run_dir,
            f"RUN {outer_run} {label} LEAN_STATEMENT "
            f"attempt {attempt}: start",
        )
        save_state(run_dir, {
            "outer_run": outer_run,
            "candidate_sha256": candidate_key,
            "statement_attempt": attempt,
            "consecutive_passes": len(pass_artifacts),
            "error_count": error_count,
            "accepted": False,
            "status": "drafting_formal_statement",
        })
        proposed = call(
            build_lean_statement_messages(problem),
            f"{label} LEAN_STATEMENT",
            max_tokens=LEAN_MAX_TOKENS,
            thinking_budget=LEAN_THINKING_BUDGET,
        )
        proposed, violations = validate_lean_statement_prefix(proposed)
        source_name = (
            f"candidate_{current_candidate_num:02d}"
            f"_statement_attempt_{attempt:02d}.lean"
        )
        save_text(subdir, source_name, proposed)
        if violations:
            report = "Statement policy failure:\n" + "\n".join(
                f"- {item}" for item in violations
            )
            log_progress(
                run_dir,
                f"RUN {outer_run} {label} LEAN_STATEMENT: "
                "invalid prefix",
            )
            save_text(
                subdir,
                f"lean_statement_{current_candidate_num:02d}"
                f"_attempt_{attempt:02d}.txt",
                report,
            )
            return None, None, report
        statement_hash = sha256_text(proposed)
        report = (
            f"Candidate SHA-256: {candidate_key}" + DNL
            + f"Proposed statement SHA-256: {statement_hash}" + DNL
            + "This statement has not yet been proved or frozen." + DNL
            + DIV + "### Proposed Lean Statement ###" + DNL + proposed
        )
        save_text(
            subdir,
            f"lean_statement_{current_candidate_num:02d}"
            f"_attempt_{attempt:02d}.txt",
            report,
        )
        return proposed, statement_hash, report

    def formal_check(
        current_candidate,
        current_candidate_num,
        frozen_statement,
        frozen_statement_hash,
        attempt,
    ):
        if lean_mode == "off":
            return (
                True,
                "Formal verification disabled by --lean-mode off.",
                None,
                None,
                None,
            )

        candidate_key = sha256_text(current_candidate)
        cache_key = (candidate_key, frozen_statement_hash)
        if cache_key in formal_cache:
            return formal_cache[cache_key]

        label = f"CANDIDATE {current_candidate_num:02d}"
        log_progress(
            run_dir,
            f"RUN {outer_run} {label} LEAN_FORMALIZE "
            f"attempt {attempt}: start",
        )
        save_state(run_dir, {
            "outer_run": outer_run,
            "candidate_sha256": candidate_key,
            "formal_statement_sha256": frozen_statement_hash,
            "formal_attempt": attempt,
            "consecutive_passes": len(pass_artifacts),
            "error_count": error_count,
            "accepted": False,
            "status": "formalizing",
        })
        source = call(
            build_lean_formalization_messages(
                problem,
                current_candidate,
                frozen_statement,
            ),
            f"{label} LEAN_FORMALIZE",
            max_tokens=LEAN_MAX_TOKENS,
            thinking_budget=LEAN_THINKING_BUDGET,
        )
        source_name = (
            f"candidate_{current_candidate_num:02d}"
            f"_formal_{attempt:02d}.lean"
        )
        source_path = subdir / source_name

        def check_backends(lean_source, lean_source_path):
            if not lean_source_preserves_frozen_statement(
                lean_source,
                frozen_statement,
            ):
                save_text(
                    subdir,
                    lean_source_path.name,
                    strip_code_fences(lean_source),
                )
                return (
                    False,
                    False,
                    None,
                    "Frozen statement mismatch: generated Lean source changed "
                    "the reviewed theorem prefix and was not executed.",
                    None,
                )
            local_ok, local_report = execute_lean_code(
                lean_source, lean_source_path, lean_project, timeout=LEAN_TIMEOUT
            )
            axle_ok = None
            axle_report = "AXLE not requested."
            if axle_mode == "required" or (
                axle_mode == "fallback" and not local_ok
            ):
                axle_ok, axle_report = execute_axle_check(
                    lean_source, axle_environment, timeout=LEAN_TIMEOUT
                )
            passed = formal_backends_pass(local_ok, axle_ok, axle_mode)
            statement_match = re.search(
                r"Statement SHA-256: ([0-9a-f]{64})",
                local_report,
            )
            statement_hash = (
                statement_match.group(1) if statement_match else None
            )
            combined_report = (
                f"Local Lean: {'PASS' if local_ok else 'FAIL'}" + DNL
                + local_report + DNL + DIV
                + f"AXLE ({axle_mode}): "
                + (
                    "SKIPPED"
                    if axle_ok is None
                    else ("PASS" if axle_ok else "FAIL")
                )
                + DNL + axle_report
            )
            return (
                passed,
                local_ok,
                axle_ok,
                combined_report,
                statement_hash,
            )

        passed, local_ok, axle_ok, report, statement_hash = check_backends(
            source, source_path
        )

        if not passed and LEAN_MAX_REPAIRS and not local_ok:
            log_progress(run_dir, f"RUN {outer_run} {label} LEAN_REPAIR: start")
            source = call(
                build_lean_repair_messages(
                    problem,
                    current_candidate,
                    frozen_statement,
                    source,
                    report,
                ),
                f"{label} LEAN_REPAIR",
                max_tokens=LEAN_MAX_TOKENS,
                thinking_budget=LEAN_THINKING_BUDGET,
            )
            source_name = (
                f"candidate_{current_candidate_num:02d}"
                f"_formal_{attempt:02d}_repair.lean"
            )
            source_path = subdir / source_name
            (
                passed,
                local_ok,
                axle_ok,
                report,
                statement_hash,
            ) = check_backends(source, source_path)

        status = "PASS" if passed else "FAIL"
        report = (
            f"Formal verification: {status} "
            f"(local Lean with AXLE mode {axle_mode})." + DNL
            + f"Candidate SHA-256: {candidate_key}" + DNL
            + f"Frozen statement SHA-256: {frozen_statement_hash}" + DNL
            + f"Elaborated statement SHA-256: "
            + (statement_hash or "unavailable") + DNL
            + report + DNL + DIV + "### Lean Source ###" + DNL
            + strip_code_fences(source)
        )
        report_name = (
            f"lean_verify_{current_candidate_num:02d}"
            f"_attempt_{attempt:02d}.txt"
        )
        save_text(subdir, report_name, report)
        log_progress(
            run_dir,
            f"RUN {outer_run} {label} LEAN_VERIFY: {status} "
            f"(local={'PASS' if local_ok else 'FAIL'}, "
            f"axle={'SKIP' if axle_ok is None else ('PASS' if axle_ok else 'FAIL')})",
        )
        result = (
            passed,
            report,
            source_name,
            report_name,
            statement_hash,
        )
        if passed:
            formal_cache[cache_key] = result
        return result

    log_progress(run_dir, f"RUN {outer_run} SOLVE: start")
    save_state(run_dir, {
        "outer_run": outer_run,
        "accepted": False,
        "status": "solving",
    })
    pivot_hint = build_pivot_hint(prev_failure_reason) if outer_run > 1 else None
    solver_messages = build_solver_messages(
        problem,
        outer_run,
        pivot_hint,
        empirical_results,
        failure_context,
    )
    solution = call(solver_messages, "SOLVE")
    save_text(subdir, "draft.md", solution)

    should_self_improve = self_improve_mode == "always" or (
        self_improve_mode == "recovery"
        and not candidate_is_complete(solution)
    )
    if should_self_improve:
        log_progress(run_dir, f"RUN {outer_run} SELF_IMPROVE: recovery start")
        save_state(run_dir, {
            "outer_run": outer_run,
            "accepted": False,
            "status": "self_improving",
        })
        improved = call(
            build_self_improvement_messages(
                solver_messages,
                solution,
                recovery=self_improve_mode == "recovery",
            ),
            "SELF_IMPROVE",
        )
        if improved.strip():
            solution = improved
    else:
        log_progress(
            run_dir,
            f"RUN {outer_run} SELF_IMPROVE: skipped ({self_improve_mode})",
        )
    save_text(subdir, "candidate_00.md", solution)
    candidate = solution
    candidate_hash = sha256_text(candidate)
    profile_index = 0
    error_count = 0
    last_failure_reason = None
    all_failure_reasons = []
    formal_ok = lean_mode == "off"
    formal_report = None
    lean_source_name = None
    lean_report_name = None
    proposed_statement = None
    proposed_statement_hash = None
    frozen_statement = None
    formal_statement_hash = None
    elaborated_statement_hash = None
    statement_attempt = 0
    formal_attempt = 0
    computational_report = ""
    save_state(run_dir, {
        "outer_run": outer_run,
        "candidate_sha256": candidate_hash,
        "consecutive_passes": 0,
        "error_count": 0,
        "accepted": False,
        "status": "cas_verifying",
    })
    cas_ok, computational_report = cas_verify_candidate(
        candidate,
        api_url,
        api_key,
        model,
        run_dir,
        outer_run,
        0,
        usage_ledger=usage_ledger,
        validation_enabled=validation_enabled,
    )
    if not cas_ok:
        computational_report = "CAS verification failed:\n" + computational_report

    for i in range(MAX_ITERATIONS):
        save_state(run_dir, {
            "outer_run": outer_run,
            "iteration": i + 1,
            "candidate_sha256": candidate_hash,
            "verification_profile": VERIFICATION_PROFILES[
                profile_index
            ][0],
            "consecutive_passes": len(pass_artifacts),
            "error_count": error_count,
            "total_tokens": usage_ledger.total_tokens,
            "accepted": False,
            "status": "running",
        })

        if (
            profile_index == 1
            and lean_mode != "off"
            and proposed_statement is None
        ):
            statement_attempt += 1
            (
                proposed_statement,
                proposed_statement_hash,
                _statement_report,
            ) = draft_formal_statement(
                candidate,
                candidate_num,
                statement_attempt,
            )
            if proposed_statement is None:
                if statement_attempt < FORMAL_MAX_ATTEMPTS:
                    continue
                save_state(run_dir, {
                    "outer_run": outer_run,
                    "iteration": i + 1,
                    "candidate_sha256": candidate_hash,
                    "consecutive_passes": len(pass_artifacts),
                    "error_count": error_count,
                    "accepted": False,
                    "status": "formal_statement_failed",
                })
                return False, None, {
                    "total_tokens": usage_ledger.total_tokens,
                    "failure_reason": (
                        "The model could not produce a structurally valid "
                        "Lean theorem statement for fidelity review."
                    ),
                    "candidate_sha256": candidate_hash,
                }

        if (
            profile_index == 2
            and lean_mode != "off"
            and formal_report is None
        ):
            if not frozen_statement or not formal_statement_hash:
                raise AssertionError(
                    "formal proof requested before statement freeze"
                )
            formal_attempt += 1
            (
                formal_ok,
                formal_report,
                lean_source_name,
                lean_report_name,
                elaborated_statement_hash,
            ) = formal_check(
                candidate,
                candidate_num,
                frozen_statement,
                formal_statement_hash,
                formal_attempt,
            )
            if not formal_ok and formal_gate_required:
                if formal_attempt < FORMAL_MAX_ATTEMPTS:
                    log_progress(
                        run_dir,
                        f"RUN {outer_run} CANDIDATE {candidate_num:02d}: "
                        "formalization failed; retrying without changing "
                        "the informal proof",
                    )
                    save_state(run_dir, {
                        "outer_run": outer_run,
                        "iteration": i + 1,
                        "candidate_sha256": candidate_hash,
                        "verification_profile": VERIFICATION_PROFILES[
                            profile_index
                        ][0],
                        "consecutive_passes": len(pass_artifacts),
                        "error_count": error_count,
                        "accepted": False,
                        "status": "formal_retry",
                    })
                    formal_report = None
                    elaborated_statement_hash = None
                    continue
                log_progress(
                    run_dir,
                    f"RUN {outer_run} CANDIDATE {candidate_num:02d}: "
                    "formalization attempts exhausted",
                )
                save_state(run_dir, {
                    "outer_run": outer_run,
                    "iteration": i + 1,
                    "candidate_sha256": candidate_hash,
                    "consecutive_passes": len(pass_artifacts),
                    "error_count": error_count,
                    "accepted": False,
                    "status": "formal_failed",
                })
                return False, None, {
                    "total_tokens": usage_ledger.total_tokens,
                    "failure_reason": (
                        "The informal candidate passed an audit but could not "
                        "be formalized after isolated formalization retries."
                    ),
                    "candidate_sha256": candidate_hash,
                }

        profile_name, profile_instruction = VERIFICATION_PROFILES[
            profile_index
        ]
        log_progress(
            run_dir,
            f"RUN {outer_run} ITER {i+1} VERIFY "
            f"profile={profile_name}: start",
        )
        save_state(run_dir, {
            "outer_run": outer_run,
            "iteration": i + 1,
            "candidate_sha256": candidate_hash,
            "verification_profile": profile_name,
            "consecutive_passes": len(pass_artifacts),
            "error_count": error_count,
            "accepted": False,
            "status": "verifying",
        })
        verification = call(
            build_verifier_messages(
                problem,
                candidate,
                (
                    formal_report
                    if profile_name == "computation"
                    else None
                ),
                (
                    computational_report
                    if profile_name == "computation"
                    else None
                ),
                profile_instruction,
                proposed_formal_statement=(
                    proposed_statement
                    if profile_name == "statement_fidelity"
                    else None
                ),
            ),
            f"VERIFY {profile_name}",
        )
        formal_expected = profile_index == 2 and lean_mode != "off"
        if formal_gate_required and formal_expected and not formal_ok:
            verification += (
                DNL + DIV + "### Required Formal Verification Gate Failure ###" + DNL
                + "The candidate cannot be accepted until the configured formal "
                + "verification backends verify a faithful formalization of the "
                + "full problem." + DNL
                + formal_report
            )
        save_text(subdir, f"verify_{verify_num:02d}.md", verification)
        verdict = parse_verdict(verification)
        if (
            formal_gate_required
            and formal_expected
            and not formal_ok
            and verdict == "yes"
        ):
            verdict = "no"
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1}: verdict overridden by "
                "required formal gate",
            )
        save_text(subdir, f"classify_{verify_num:02d}.md", verdict)
        audit_statement_hash = formal_statement_hash
        if (
            profile_name == "statement_fidelity"
            and lean_mode != "off"
        ):
            audit_statement_hash = proposed_statement_hash
        metadata_name = f"verify_{verify_num:02d}.json"
        save_text(
            subdir,
            metadata_name,
            json.dumps(
                {
                    "candidate_sha256": candidate_hash,
                    "profile": profile_name,
                    "verdict": verdict,
                    "formal_statement_sha256": audit_statement_hash,
                    "elaborated_statement_sha256": (
                        elaborated_statement_hash
                    ),
                },
                indent=2,
            ),
        )
        verify_num += 1

        if verdict == "yes":
            error_count = 0
            if (
                profile_name == "statement_fidelity"
                and lean_mode != "off"
            ):
                if not proposed_statement or not proposed_statement_hash:
                    raise AssertionError(
                        "statement fidelity passed without a proposed statement"
                    )
                frozen_statement = proposed_statement
                formal_statement_hash = proposed_statement_hash
                frozen_name = (
                    f"candidate_{candidate_num:02d}_statement_frozen.lean"
                )
                save_text(subdir, frozen_name, frozen_statement)
                for artifact in pass_artifacts:
                    artifact["formal_statement_sha256"] = (
                        formal_statement_hash
                    )
                    metadata_path = subdir / artifact["metadata"]
                    metadata = json.loads(
                        metadata_path.read_text(encoding="utf-8")
                    )
                    metadata["formal_statement_sha256"] = (
                        formal_statement_hash
                    )
                    save_atomic_text(
                        metadata_path,
                        json.dumps(metadata, indent=2),
                    )
                log_progress(
                    run_dir,
                    f"RUN {outer_run} CANDIDATE {candidate_num:02d} "
                    f"LEAN_STATEMENT FROZEN: "
                    f"{formal_statement_hash[:16]}...",
                )
            pass_artifacts.append({
                "candidate": f"candidate_{candidate_num:02d}.md",
                "candidate_sha256": candidate_hash,
                "profile": profile_name,
                "verify": f"verify_{verify_num - 1:02d}.md",
                "classify": f"classify_{verify_num - 1:02d}.md",
                "metadata": metadata_name,
                "lean_source": lean_source_name,
                "lean_report": lean_report_name,
                "formal_statement_sha256": (
                    formal_statement_hash
                    if lean_mode != "off"
                    else None
                ),
                "elaborated_statement_sha256": (
                    elaborated_statement_hash
                ),
            })
            if any(
                artifact["candidate_sha256"] != candidate_hash
                for artifact in pass_artifacts
            ):
                raise AssertionError(
                    "pass artifacts span multiple candidate hashes"
                )
            profile_index += 1
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} PASS "
                f"profile={profile_name} "
                f"({len(pass_artifacts)}/{REQUIRED_PASSES})",
            )
            save_state(run_dir, {
                "outer_run": outer_run,
                "iteration": i + 1,
                "candidate_sha256": candidate_hash,
                "verification_profile": profile_name,
                "consecutive_passes": len(pass_artifacts),
                "error_count": error_count,
                "accepted": False,
                "status": "profile_passed",
            })
            if len(pass_artifacts) >= REQUIRED_PASSES:
                if formal_gate_required and not formal_ok:
                    raise AssertionError(
                        "formal gate was not satisfied at acceptance"
                    )
                if lean_mode != "off":
                    if not formal_statement_hash:
                        raise AssertionError(
                            "accepted candidate has no frozen statement hash"
                        )
                    if any(
                        artifact["formal_statement_sha256"]
                        != formal_statement_hash
                        for artifact in pass_artifacts
                    ):
                        raise AssertionError(
                            "pass artifacts span multiple formal statements"
                        )
                log_progress(
                    run_dir,
                    f"RUN {outer_run} VERIFIED: {REQUIRED_PASSES} "
                    "distinct audits of one candidate hash",
                )
                save_state(run_dir, {
                    "outer_run": outer_run,
                    "iteration": i + 1,
                    "candidate_sha256": candidate_hash,
                    "consecutive_passes": len(pass_artifacts),
                    "error_count": error_count,
                    "accepted": False,
                    "status": "verified",
                })
                return True, candidate, {
                    "total_tokens": usage_ledger.total_tokens,
                    "pass_artifacts": pass_artifacts,
                    "iterations": i + 1,
                    "failure_reason": last_failure_reason,
                    "candidate_sha256": candidate_hash,
                    "formal_statement_sha256": formal_statement_hash,
                    "elaborated_statement_sha256": (
                        elaborated_statement_hash
                    ),
                }
            continue

        if (
            profile_name == "statement_fidelity"
            and lean_mode != "off"
        ):
            last_failure_reason = extract_failure_reason(verification)
            proposed_statement = None
            proposed_statement_hash = None
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} LEAN_STATEMENT: "
                f"fidelity verdict={verdict}; redrafting without changing "
                "the informal candidate",
            )
            if statement_attempt < FORMAL_MAX_ATTEMPTS:
                save_state(run_dir, {
                    "outer_run": outer_run,
                    "iteration": i + 1,
                    "candidate_sha256": candidate_hash,
                    "verification_profile": profile_name,
                    "verdict": verdict,
                    "statement_attempt": statement_attempt,
                    "consecutive_passes": len(pass_artifacts),
                    "error_count": error_count,
                    "accepted": False,
                    "status": "formal_statement_retry",
                })
                continue
            save_state(run_dir, {
                "outer_run": outer_run,
                "iteration": i + 1,
                "candidate_sha256": candidate_hash,
                "verification_profile": profile_name,
                "verdict": verdict,
                "statement_attempt": statement_attempt,
                "consecutive_passes": len(pass_artifacts),
                "error_count": error_count,
                "accepted": False,
                "status": "formal_statement_failed",
            })
            return False, None, {
                "total_tokens": usage_ledger.total_tokens,
                "failure_reason": (
                    "The informal candidate passed the proof-logic audit, "
                    "but no faithful Lean theorem statement was approved "
                    "within the isolated statement-drafting attempts."
                ),
                "candidate_sha256": candidate_hash,
            }

        last_failure_reason = extract_failure_reason(verification)
        all_failure_reasons.append(last_failure_reason)
        pass_artifacts.clear()
        profile_index = 0
        if verdict == "improve":
            error_count = 0
            next_status = "refining"
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} REFINE: "
                "pass streak reset before mutation",
            )
        else:
            error_count += 1
            next_status = "correcting"
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} CORRECT: "
                f"errors={error_count}, pass streak reset",
            )
        save_state(run_dir, {
            "outer_run": outer_run,
            "iteration": i + 1,
            "candidate_sha256": candidate_hash,
            "verification_profile": profile_name,
            "verdict": verdict,
            "consecutive_passes": 0,
            "error_count": error_count,
            "accepted": False,
            "status": next_status,
        })
        if verdict == "no" and error_count >= MAX_ERRORS:
            combined_reason = " | ".join(all_failure_reasons[-3:])
            return False, None, {
                "total_tokens": usage_ledger.total_tokens,
                "failure_reason": combined_reason,
            }

        evidence = cas_compute_gap(
            candidate,
            verification,
            api_url,
            api_key,
            model,
            run_dir,
            outer_run,
            i + 1,
            usage_ledger=usage_ledger,
            validation_enabled=validation_enabled,
        )
        combined_evidence = "\n\n".join(
            item for item in (computational_report, evidence) if item
        )
        if verdict == "improve":
            candidate = call(
                build_refinement_messages(
                    problem,
                    candidate,
                    verification,
                    combined_evidence,
                ),
                f"ITER {i+1} REFINE",
                max_tokens=CORRECT_MAX_TOKENS,
                thinking_budget=CORRECT_THINKING_BUDGET,
            )
        else:
            candidate = call(
                build_correction_messages(
                    problem,
                    candidate,
                    verification,
                    combined_evidence,
                ),
                f"ITER {i+1} CORRECT",
                max_tokens=CORRECT_MAX_TOKENS,
                thinking_budget=CORRECT_THINKING_BUDGET,
            )

        candidate_num += 1
        save_text(subdir, f"candidate_{candidate_num:02d}.md", candidate)
        candidate_hash = sha256_text(candidate)
        save_state(run_dir, {
            "outer_run": outer_run,
            "iteration": i + 1,
            "candidate_sha256": candidate_hash,
            "consecutive_passes": 0,
            "error_count": error_count,
            "accepted": False,
            "status": "cas_verifying",
        })
        formal_ok = lean_mode == "off"
        formal_report = None
        lean_source_name = None
        lean_report_name = None
        proposed_statement = None
        proposed_statement_hash = None
        frozen_statement = None
        formal_statement_hash = None
        elaborated_statement_hash = None
        statement_attempt = 0
        formal_attempt = 0
        cas_ok, computational_report = cas_verify_candidate(
            candidate,
            api_url,
            api_key,
            model,
            run_dir,
            outer_run,
            i + 1,
            usage_ledger=usage_ledger,
            validation_enabled=validation_enabled,
        )
        if not cas_ok:
            computational_report = (
                "CAS verification failed:\n" + computational_report
            )

    log_progress(
        run_dir, f"RUN {outer_run} EXHAUSTED: {MAX_ITERATIONS} iterations"
    )
    save_state(run_dir, {
        "outer_run": outer_run,
        "iteration": MAX_ITERATIONS,
        "candidate_sha256": candidate_hash,
        "consecutive_passes": len(pass_artifacts),
        "error_count": error_count,
        "accepted": False,
        "status": "exhausted",
    })
    combined_reason = " | ".join(all_failure_reasons[-3:]) if all_failure_reasons else last_failure_reason
    return False, None, {
        "total_tokens": usage_ledger.total_tokens,
        "failure_reason": combined_reason,
    }


# -- Main --

def main():
    parser = argparse.ArgumentParser(
        description="IMO 2026 direct solver orchestrator"
    )
    parser.add_argument("--problem", type=Path, required=True)
    parser.add_argument(
        "--api-url", default=os.getenv("IMO_SOLVER_API_URL", "")
    )
    parser.add_argument(
        "--api-key-file",
        type=Path,
        help=(
            "Read the model API token from a mode-0600 file. Prefer the "
            "IMO_SOLVER_TOKEN environment variable."
        ),
    )
    parser.add_argument(
        "--model", default=os.getenv("IMO_SOLVER_MODEL", "")
    )
    parser.add_argument(
        "--verifier-model",
        default=os.getenv("IMO_VERIFIER_MODEL", ""),
        help="Optional distinct model for the three verifier profiles.",
    )
    parser.add_argument(
        "--self-improve",
        choices=("recovery", "always", "off"),
        default=os.getenv("IMO_SELF_IMPROVE", "recovery"),
        help="Run SELF_IMPROVE only for incomplete SOLVE output by default.",
    )
    parser.add_argument(
        "--validation-mode",
        choices=("sandboxed", "off"),
        default=os.getenv("IMO_VALIDATION_MODE", "sandboxed"),
        help="Run empirical/CAS Python only inside the supported OS sandbox.",
    )
    parser.add_argument(
        "--lean-mode",
        choices=("required", "best-effort", "off"),
        default=os.getenv("IMO_LEAN_MODE", "required"),
        help="Local-first formal verification policy. Default: required.",
    )
    parser.add_argument(
        "--lean-project",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "lean",
        help="Lake project containing the pinned local Mathlib environment.",
    )
    parser.add_argument(
        "--axle-mode",
        choices=("off", "fallback", "required"),
        default=os.getenv("IMO_AXLE_MODE", "off"),
        help=(
            "Hosted AXLE policy: off (default), fallback when local Lean fails, "
            "or required in addition to local Lean."
        ),
    )
    parser.add_argument(
        "--axle-environment",
        default=os.getenv("AXLE_ENVIRONMENT", "lean-4.28.0"),
        help="AXLE Lean environment name. Default: lean-4.28.0.",
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not args.api_url:
        parser.error("--api-url or IMO_SOLVER_API_URL is required")
    api_key = os.getenv("IMO_SOLVER_TOKEN", "")
    if args.api_key_file:
        try:
            mode = stat.S_IMODE(args.api_key_file.stat().st_mode)
            if mode & 0o077:
                parser.error("--api-key-file must have mode 0600")
            api_key = args.api_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            parser.error(f"cannot read --api-key-file: {exc}")
    if not api_key:
        parser.error("IMO_SOLVER_TOKEN or --api-key-file is required")
    if not args.model:
        parser.error("--model or IMO_SOLVER_MODEL is required")
    if args.axle_mode != "off":
        if args.lean_mode == "off":
            parser.error("--axle-mode requires local Lean formalization")
        if not os.getenv("AXLE_API_KEY"):
            parser.error("AXLE_API_KEY is required when --axle-mode is enabled")
        try:
            __import__("axle")
        except ImportError:
            parser.error(
                "AXLE client unavailable; install requirements-axle.txt"
            )
    args.lean_project = args.lean_project.resolve()
    if (
        args.validation_mode == "sandboxed"
        and not shutil.which("sandbox-exec")
    ):
        parser.error(
            "--validation-mode sandboxed requires sandbox-exec; use off "
            "rather than executing generated Python unsandboxed"
        )
    if args.lean_mode != "off":
        if not (args.lean_project / "lakefile.lean").is_file():
            parser.error(
                f"local Lean project not initialized: {args.lean_project}; "
                "run scripts/setup_lean.sh"
            )
        if not shutil.which("lake") and not (
            Path.home() / ".elan" / "bin" / "lake"
        ).is_file():
            parser.error("local Lean not installed; run scripts/setup_lean.sh")
        if not shutil.which("sandbox-exec"):
            parser.error(
                "local Lean verification requires sandbox-exec on this host"
            )

    problem = args.problem.read_text(encoding="utf-8")
    args.run_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock_file = acquire_output_lock(args.output)
    except RuntimeError as exc:
        parser.error(str(exc))
    atexit.register(release_output_lock, lock_file)

    if (args.run_dir / "state.json").exists():
        print(
            f"ERROR: run directory already has state.json: {args.run_dir}",
            file=sys.stderr,
        )
        release_output_lock(lock_file)
        sys.exit(1)

    log_progress(
        args.run_dir,
        f"ORCHESTRATOR START: problem={args.problem.name} model={args.model} "
        f"verifier_model={args.verifier_model or args.model} "
        f"self_improve={args.self_improve} "
        f"validation_mode={args.validation_mode} "
        f"lean_mode={args.lean_mode} axle_mode={args.axle_mode} "
        f"axle_environment={args.axle_environment}",
    )

    last_failure_reason = None
    consecutive_infra_errors = 0
    usage_ledger = UsageLedger(args.run_dir / "usage.jsonl")
    outer_run = 1
    while outer_run <= MAX_OUTER_RUNS:
        log_progress(args.run_dir, f"OUTER_RUN {outer_run}/{MAX_OUTER_RUNS}")
        save_state(args.run_dir, {
            "outer_run": outer_run,
            "status": "starting",
        })

        try:
            empirical_results = pre_solve_validation(
                problem,
                args.api_url,
                api_key,
                args.model,
                args.run_dir,
                outer_run,
                usage_ledger=usage_ledger,
                validation_enabled=args.validation_mode == "sandboxed",
            )
            failure_ledger = read_failure_ledger(args.run_dir)
            failure_context = build_failure_ledger_context(failure_ledger)
            accepted, candidate, summary = run_outer(
                outer_run,
                problem,
                args.api_url,
                api_key,
                args.model,
                args.run_dir,
                prev_failure_reason=last_failure_reason,
                empirical_results=empirical_results,
                failure_context=failure_context,
                lean_mode=args.lean_mode,
                lean_project=args.lean_project,
                axle_mode=args.axle_mode,
                axle_environment=args.axle_environment,
                self_improve_mode=args.self_improve,
                validation_enabled=args.validation_mode == "sandboxed",
                usage_ledger=usage_ledger,
                verifier_model=args.verifier_model or None,
            )
            consecutive_infra_errors = 0
        except InfrastructureError as exc:
            consecutive_infra_errors += 1
            backoff = INFRA_BACKOFF_BASE * (
                2 ** (consecutive_infra_errors - 1)
            )
            log_progress(
                args.run_dir,
                f"RUN {outer_run} INFRA_ERROR ({consecutive_infra_errors}/{MAX_INFRA_RETRIES}): {exc}, "
                f"waiting {backoff}s before retrying the same run",
            )
            save_state(args.run_dir, {
                "outer_run": outer_run,
                "status": "infra_error",
                "error": str(exc),
                "consecutive_infra_errors": consecutive_infra_errors,
            })
            if consecutive_infra_errors >= MAX_INFRA_RETRIES:
                log_progress(
                    args.run_dir,
                    f"ENDPOINT_UNAVAILABLE: {MAX_INFRA_RETRIES} consecutive infrastructure errors",
                )
                save_state(args.run_dir, {
                    "outer_run": outer_run,
                    "status": "endpoint_unavailable",
                })
                release_output_lock(lock_file)
                print(
                    f"Endpoint unavailable after {MAX_INFRA_RETRIES} consecutive errors.",
                    file=sys.stderr,
                )
                sys.exit(1)
            time.sleep(backoff)
            continue
        except ConfigurationError as exc:
            log_progress(args.run_dir, f"CONFIGURATION_ERROR: {exc}")
            save_state(args.run_dir, {
                "outer_run": outer_run,
                "status": "configuration_error",
                "error": str(exc),
            })
            release_output_lock(lock_file)
            print(str(exc), file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            log_progress(args.run_dir, f"RUN {outer_run} ERROR: {exc}")
            traceback.print_exc()
            save_state(args.run_dir, {
                "outer_run": outer_run,
                "status": "error",
                "error": str(exc),
            })
            outer_run += 1
            continue

        if not accepted and summary and summary.get("failure_reason"):
            last_failure_reason = summary["failure_reason"]
            write_failure_ledger(args.run_dir, {
                "run": outer_run,
                "failure_reason": summary["failure_reason"],
                "empirical_results": empirical_results,
            })

        if accepted:
            manifest = {
                "problem": str(args.problem),
                "output": str(args.output),
                "sha256": sha256_text(candidate),
                "outer_run": outer_run,
                "total_tokens": usage_ledger.total_tokens,
                "pass_artifacts": summary.get("pass_artifacts", []),
                "formal_statement_sha256": summary.get(
                    "formal_statement_sha256"
                ),
                "elaborated_statement_sha256": summary.get(
                    "elaborated_statement_sha256"
                ),
                "lean_mode": args.lean_mode,
                "axle_mode": args.axle_mode,
                "axle_environment": args.axle_environment,
                "self_improve_mode": args.self_improve,
                "validation_mode": args.validation_mode,
                "solver_model": args.model,
                "verifier_model": args.verifier_model or args.model,
                "usage_ledger": "usage.jsonl",
                "timestamp": now_utc(),
            }
            try:
                save_atomic_text(args.output, candidate)
                save_atomic_text(
                    args.run_dir / "manifest.json",
                    json.dumps(manifest, indent=2, ensure_ascii=False),
                )
            except OSError as exc:
                log_progress(
                    args.run_dir,
                    f"PERSISTENCE_ERROR: {exc}",
                )
                save_state(args.run_dir, {
                    "outer_run": outer_run,
                    "candidate_sha256": manifest["sha256"],
                    "accepted": False,
                    "status": "persistence_error",
                    "error": str(exc),
                })
                print(
                    f"Verified solution could not be persisted: {exc}",
                    file=sys.stderr,
                )
                sys.exit(1)

            save_state(args.run_dir, {
                "outer_run": outer_run,
                "candidate_sha256": manifest["sha256"],
                "formal_statement_sha256": manifest[
                    "formal_statement_sha256"
                ],
                "elaborated_statement_sha256": manifest[
                    "elaborated_statement_sha256"
                ],
                "consecutive_passes": REQUIRED_PASSES,
                "accepted": True,
                "status": "accepted",
            })
            log_progress(
                args.run_dir,
                f"ACCEPTED: output={args.output} "
                f"sha256={manifest['sha256'][:16]}...",
            )
            print(f"Solution accepted: {args.output}")
            release_output_lock(lock_file)
            sys.exit(0)
        outer_run += 1

    log_progress(
        args.run_dir,
        f"ALL_RUNS_FAILED: {MAX_OUTER_RUNS} runs exhausted",
    )
    save_state(args.run_dir, {
        "outer_run": MAX_OUTER_RUNS,
        "status": "all_failed",
    })
    print(
        f"No verified solution found after {MAX_OUTER_RUNS} runs.",
        file=sys.stderr,
    )
    release_output_lock(lock_file)
    sys.exit(1)


if __name__ == "__main__":
    main()
