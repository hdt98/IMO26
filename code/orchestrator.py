#!/usr/bin/env python3
"""
IMO 2026 direct solver orchestrator.

Harness-agnostic background script implementing the
solve -> self-improve -> verify -> classify -> correct loop
using OpenAI-compatible chat completions.

Usage:
    python3 orchestrator.py \
        --problem problems/imo2026_p1.txt \
        --api-url http://host:port/v1/chat/completions \
        --api-key TOKEN \
        --model MODEL_NAME \
        --run-dir /tmp/imo26-run \
        --output solutions/imo2026_p1.md

Environment fallbacks: IMO_SOLVER_API_URL, IMO_SOLVER_TOKEN, IMO_SOLVER_MODEL

"""

import argparse
import hashlib
import json
import os
import signal
import sys
import subprocess
import re
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
    correction_prompt,
    empirical_probe_prompt,
    cas_verify_prompt,
    cas_compute_prompt,
    verification_system_prompt,
    verification_reminder,
    classifier_prompt,
    refinement_prompt,
)

# -- Constants --

MAX_ITERATIONS = 30
MAX_ERRORS = 3  # consecutive failures before run restarts (was 10)
# Lowered to 3: if the model cannot fix its approach in 3 corrections,
# it is likely on a fundamentally wrong track. A fresh SOLVE with a
# pivot hint gives a better chance than more corrections.
REQUIRED_PASSES = 5
MAX_OUTER_RUNS = 10
MAX_TOKENS = 256_000
CORRECT_MAX_TOKENS = 128_000  # Gap 1+5: cap CORRECT/REFINE to prevent oversized outputs
THINKING_BUDGET = 200_000
CORRECT_THINKING_BUDGET = 100_000  # Gap 5: smaller thinking budget for corrections
HTTP_TIMEOUT = 5400
MAX_TRANSPORT_RETRIES = 3
MAX_INFRA_RETRIES = 5  # consecutive infra errors before giving up
INFRA_BACKOFF_BASE = 30  # seconds, doubled each consecutive infra error
VALIDATION_TIMEOUT = 60  # seconds for executing empirical/CAS validation scripts
VALIDATION_MAX_TOKENS = 65536  # token budget for validation code generation
VALIDATION_THINKING_BUDGET = 4096  # thinking budget for validation code generation
CAS_MAX_RETRIES = 1  # max CORRECT retry when CAS verification fails

# Wall-clock timeout per API call. Uses threading.Timer (not signal.alarm)
# because signal.alarm delivery is delayed by 500+ seconds on macOS when the
# main thread is blocked in a C extension (SSL recv) during streaming.
# threading.Timer fires in a separate thread and checks timer.fired in the
# SSE parsing loop, providing reliable timeout regardless of main thread state.
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


class _WallClockTimer:
    """Thread-based wall-clock timer. More reliable than signal.alarm on macOS
    during streaming I/O, where signal delivery can be delayed by 500+ seconds
    because the main thread is blocked in a C extension (SSL recv)."""
    def __init__(self, timeout, log_fn=None):
        self._timer = None
        self._fired = False
        self._timeout = timeout
        self._log_fn = log_fn

    def _fire(self):
        self._fired = True
        if self._log_fn:
            self._log_fn(f"wall-clock timer fired after {self._timeout}s")

    def start(self):
        self._fired = False
        self._timer = threading.Timer(self._timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

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


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_standalone_yes(text):
    """Accept only an unambiguous standalone 'yes' (case-insensitive, ignoring
    surrounding whitespace, quotes, and trailing punctuation). Empty, mixed, or
    qualified responses count as no."""
    if not text:
        return False
    t = text.strip().strip('"').strip("'").strip()
    t = t.rstrip(".!?。").strip()
    return t.lower() == "yes"

def is_standalone_improve(text):
    """Accept only an unambiguous standalone 'improve' (same normalization as
    is_standalone_yes)."""
    if not text:
        return False
    t = text.strip().strip('"').strip("'").strip()
    t = t.rstrip(".!?。").strip()
    return t.lower() == "improve"


# -- API --

def chat_completion(api_url, api_key, model, messages, log_fn=None, max_tokens=None, thinking_budget=None):
    """Return (content, usage, finish_reason) from an OpenAI-compatible call.

    The GLM-5.2-FP8 endpoint supports Anthropic-style thinking via the
    OpenAI-compatible API. When thinking is enabled, the model reasons first
    (consuming reasoning_tokens against the budget) then emits visible content.
    The response message includes a reasoning_content field separate from
    content. Empirically validated by the P3 run: the solver used 124650
    reasoning tokens out of the 200000 budget and completed normally.
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
        try:
            wc_timer = _WallClockTimer(WALL_CLOCK_TIMEOUT, log_fn=log_fn)
            wc_timer.start()
            try:
                resp = requests.post(
                    api_url, headers=headers, json=payload, timeout=HTTP_TIMEOUT,
                    stream=True,
                )
                resp.raise_for_status()

                # Parse SSE stream: accumulate content and capture usage +
                # finish_reason from chunks. Streaming keeps the connection
                # alive, preventing the server from closing it before the
                # full response is generated.
                content_parts = []
                reasoning_parts = []
                usage = {}
                finish = "unknown"
                for line in resp.iter_lines(decode_unicode=True):
                    if wc_timer.fired:
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
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish = fr
                content = "".join(content_parts)
            finally:
                wc_timer.cancel()
            return content, usage, finish
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            is_conn_error = isinstance(exc, requests.exceptions.ConnectionError)
            if attempt < MAX_TRANSPORT_RETRIES:
                backoff = INFRA_BACKOFF_BASE * (2 ** (attempt - 1)) if is_conn_error else 5 * attempt
                if log_fn:
                    log_fn(f"attempt {attempt}/{MAX_TRANSPORT_RETRIES} failed: {exc}, retrying in {backoff}s")
                time.sleep(backoff)
            else:
                if log_fn:
                    log_fn(f"attempt {attempt}/{MAX_TRANSPORT_RETRIES} failed: {exc}, no retries left")
                if is_conn_error:
                    raise InfrastructureError(
                        f"Endpoint unreachable after {MAX_TRANSPORT_RETRIES} attempts: {last_error}"
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
# Faithful to the proven message structures from the successful P3 and P6 runs.
# Key design choices validated across all 6 accepted solutions:
# - Correction uses multi-turn (user/assistant/user) so the model sees its own
#   previous solution as context and can fix specific issues.
# - Verifier puts all instructions in the user message with a minimal system
#   prompt, matching the P3 proven structure.
# - Classifier uses strict standalone "yes" detection.

DNL = "\n\n"
DIV = "\n" + "=" * 70 + "\n"


def build_solver_messages(problem, outer_run=1, pivot_hint=None, empirical_results=None, failure_context=None):
    user = problem.strip() + PRESENTATION_LIMIT_NOTE
    if empirical_results:
        user += chr(10) + chr(10) + "### Computational Ground Truth ###" + chr(10) + chr(10) + empirical_results
    if failure_context:
        user += chr(10) + chr(10) + failure_context
    if outer_run > 1:
        hint = pivot_hint if pivot_hint else PIVOT_HINT
        user += chr(10) + chr(10) + hint
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": user},
    ]


def build_self_improvement_messages(problem, solution):
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": problem.strip()},
        {"role": "assistant", "content": solution},
        {"role": "user", "content": self_improvement_prompt.strip()},
    ]


def extract_section(text, marker, after=True):
    idx = text.find(marker)
    if idx == -1:
        return ""
    if after:
        return text[idx + len(marker):].strip()
    return text[:idx].strip()


def build_verifier_messages(problem, solution):
    detailed = extract_section(solution, "Detailed Solution")
    if not detailed:
        detailed = solution.strip()
    user = (
        verification_system_prompt.strip() + DNL
        + DIV + "### Problem ###" + DNL + problem.strip() + DNL
        + DIV + "### Solution ###" + DNL + detailed + DNL
        + verification_reminder.strip()
    )
    return [
        {"role": "system", "content": "You are an expert IMO grader. Follow the instructions exactly."},
        {"role": "user", "content": user},
    ]


def build_classifier_messages(verification):
    user = classifier_prompt.strip() + DNL + verification.strip()
    return [{"role": "user", "content": user}]


def build_correction_messages(problem, solution, verification, cas_feedback=None):
    # Gap 7: Send the ENTIRE verification (Summary + Detailed Log) to the
    # corrector, not just the Summary. The detailed log contains numerical
    # counterexamples and step-by-step reasoning that the corrector needs.
    bug_report = verification.strip()
    user2 = correction_prompt.strip() + DNL + DIV + "### Full Verification Report ###" + DNL + bug_report
    if cas_feedback:
        user2 += DNL + DIV + "### CAS Verification Failure ###" + DNL + "The following algebraic claims in your previous correction FAILED computational verification:" + DNL + DNL + cas_feedback + DNL + DNL + "You MUST fix these failures. Do not assert identities without verifying them computationally."
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": problem.strip()},
        {"role": "assistant", "content": solution},
        {"role": "user", "content": user2},
    ]


def build_refinement_messages(problem, solution, verification):
    # Gap 7: Send the ENTIRE verification to the refiner.
    bug_report = verification.strip()
    user2 = refinement_prompt.strip() + DNL + DIV + "### Full Verification Report ###" + DNL + bug_report
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": problem.strip()},
        {"role": "assistant", "content": solution},
        {"role": "user", "content": user2},
    ]


# -- Solver loop --

def execute_python_code(code, timeout=VALIDATION_TIMEOUT):
    """Execute Python code in a subprocess. Returns (stdout, stderr, returncode)."""
    # Strip markdown fences if present
    lines = code.split(chr(10))
    bt3 = chr(96) * 3
    if lines and lines[0].startswith(bt3):
        lines = lines[1:]
    if lines and lines[-1].strip() == bt3:
        lines = lines[:-1]
    code = chr(10).join(lines).strip()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        f.flush()
        script_path = f.name
    try:
        result = subprocess.run(
            ["python3", script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"Timeout after {timeout}s", -1
    except Exception as e:
        return "", str(e), -1
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def pre_solve_validation(problem, api_url, api_key, model, run_dir, outer_run):
    """Core Fix A: Generate and execute a validation script that computes
    small cases before SOLVE. Returns the computational results as a string,
    or None if validation fails."""
    log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: start")
    messages = [
        {"role": "system", "content": "You are a Python programmer. Write self-contained, executable Python code only."},
        {"role": "user", "content": empirical_probe_prompt.strip() + DNL + DIV + "### Problem ###" + DNL + problem.strip()},
    ]
    try:
        content, usage, finish = chat_completion(
            api_url, api_key, model, messages,
            log_fn=lambda msg: log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: {msg}"),
            max_tokens=VALIDATION_MAX_TOKENS,
            thinking_budget=VALIDATION_THINKING_BUDGET,
        )
        log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: {usage.get("total_tokens", 0)} tokens finish={finish}")
        if finish == "length":
            # Fix 1: Universal truncation retry with feedback + halved budget
            probe_tokens = usage.get("total_tokens", 0)
            truncation_feedback = f"Your previous attempt generated {probe_tokens} tokens and was truncated. Generate much shorter code — use the simplest possible approach, under 100 lines, no comments."
            log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: truncated, retry with feedback + halved budget")
            retry_messages = list(messages) + [
                {"role": "user", "content": truncation_feedback}
            ]
            content, usage, finish = chat_completion(
                api_url, api_key, model, retry_messages,
                log_fn=lambda msg: log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: {msg}"),
                max_tokens=VALIDATION_MAX_TOKENS // 2,
                thinking_budget=VALIDATION_THINKING_BUDGET,
            )
            log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: retry {usage.get("total_tokens", 0)} tokens finish={finish}")
        stdout, stderr, rc = execute_python_code(content)
        if rc == 0 and stdout.strip():
            result = stdout.strip()
            log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: success, {len(result)} chars output")
            return result
        else:
            log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: code execution failed (rc={rc}), stderr={stderr[:200]}")
            return None
    except _WallClockTimeout as wc_exc:
        # Gap O + Gap P: Wall-clock timeout on EMPIRICAL_PROBE — retry with
        # partial content carried forward (if any) and halved budget.
        partial = wc_exc.partial_content
        log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: wall-clock timeout (partial: {len(partial)} chars), retry with halved budget")
        if partial.strip():
            retry_messages = list(messages) + [
                {"role": "assistant", "content": partial},
                {"role": "user", "content": "Continue and complete your code from where you left off. Do not repeat what you already wrote."}
            ]
        else:
            retry_messages = list(messages) + [
                {"role": "user", "content": "Your previous attempt timed out. Generate much shorter code — use the simplest possible approach, under 50 lines, no comments."}
            ]
        try:
            content, usage, finish = chat_completion(
                api_url, api_key, model, retry_messages,
                log_fn=lambda msg: log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: {msg}"),
                max_tokens=VALIDATION_MAX_TOKENS // 2,
                thinking_budget=VALIDATION_THINKING_BUDGET,
            )
            log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: timeout retry {usage.get('total_tokens', 0)} tokens finish={finish}")
            if partial.strip():
                content = partial + content
            stdout, stderr, rc = execute_python_code(content)
            if rc == 0 and stdout.strip():
                result = stdout.strip()
                log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: success after timeout retry, {len(result)} chars output")
                return result
            else:
                log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: timeout retry code execution failed (rc={rc}), stderr={stderr[:200]}")
                return None
        except Exception as e:
            log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: timeout retry error: {e}")
            return None
    except Exception as e:
        log_progress(run_dir, f"RUN {outer_run} EMPIRICAL_PROBE: error: {e}")
        return None


def cas_verify_candidate(candidate, api_url, api_key, model, run_dir, outer_run, iteration):
    """Core Fix D: Generate and execute SymPy verification code for algebraic
    claims in the candidate. Returns (all_pass, details_string)."""
    label = f"ITER {iteration}" if iteration else "initial"
    log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: start")
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
        )
        log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: {usage.get("total_tokens", 0)} tokens finish={finish}")
        stdout, stderr, rc = execute_python_code(content)
        if rc == 0 and stdout.strip():
            output = stdout.strip()
            if "NO_ALGEBRAIC_CLAIMS" in output:
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: no algebraic claims found")
                return True, ""
            elif "FAIL:" in output:
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: FAIL detected")
                return False, output
            else:
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: all claims PASS")
                return True, output
        else:
            if finish == "length":
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: code truncated (finish=length), treating as FAIL")
                return False, "CAS verification code was truncated (finish=length). Please generate shorter, more compact SymPy code."
            log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: code execution failed (rc={rc})")
            return False, f"CAS verification code crashed on execution (rc={rc}). Error: {stderr[:500]}. Please generate correct, executable SymPy code."
    except Exception as e:
        log_progress(run_dir, f"RUN {outer_run} {label} CAS_VERIFY: error: {e}")
        return False, f"CAS verification encountered an error: {e}. Please generate correct, executable SymPy code."


def cas_compute_gap(candidate, verification, api_url, api_key, model, run_dir, outer_run, iteration):
    """Fix 2: When VERIFY finds only Justification Gaps (no Critical Errors),
    attempt to directly verify the incomplete identity using SymPy.
    Returns augmented candidate if identity confirmed, None otherwise."""
    label = f"ITER {iteration}" if iteration else "initial"

    # Only attempt when verification has Justification Gap but no Critical Error
    if "Critical Error" in verification or "Justification Gap" not in verification:
        return None

    log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: start")
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
        )
        probe_tokens = usage.get('total_tokens', 0)
        log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: {probe_tokens} tokens finish={finish}")
        if finish == "length":
            # Fix 1: truncation retry with feedback + halved budget
            truncation_feedback = f"Your previous attempt generated {probe_tokens} tokens and was truncated. Generate much shorter code."
            log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: truncated, retry with halved budget")
            retry_messages = list(messages) + [{"role": "user", "content": truncation_feedback}]
            content, usage, finish = chat_completion(
                api_url, api_key, model, retry_messages,
                log_fn=lambda msg: log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: {msg}"),
                max_tokens=VALIDATION_MAX_TOKENS // 2,
                thinking_budget=VALIDATION_THINKING_BUDGET,
            )
            probe_tokens = usage.get('total_tokens', 0)
            log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: retry {probe_tokens} tokens finish={finish}")
        stdout, stderr, rc = execute_python_code(content)
        if rc == 0 and stdout.strip():
            output = stdout.strip()
            if "IDENTITY_CONFIRMED" in output:
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: IDENTITY CONFIRMED")
                augmented = candidate + chr(10) + chr(10) + "---" + chr(10)
                augmented += "**CAS-Verified Identity:** The algebraic identity flagged as incomplete in the verification report has been computationally verified using SymPy. The expression simplifies to zero under the given constraints." + chr(10) + chr(10)
                augmented += "SymPy verification output:" + chr(10) + "```" + chr(10) + output + chr(10) + "```"
                return augmented
            elif "IDENTITY_DENIED" in output:
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: IDENTITY DENIED")
                return None
            else:
                log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: inconclusive output")
                return None
        else:
            log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: code execution failed (rc={rc})")
            return None
    except Exception as e:
        log_progress(run_dir, f"RUN {outer_run} {label} CAS_COMPUTE: error: {e}")
        return None


def extract_error_locations(verification):
    """Core Fix B: Extract (step_number, error_type) pairs from verification text."""
    locations = []
    for match in re.finditer(r"\*\*Step (\d+)[^*]*\*\*.*?\*\*(Critical Error|Justification Gap)\*\*", verification, re.DOTALL):
        locations.append((int(match.group(1)), match.group(2)))
    for match in re.finditer(r"\*\*Location:.*?Step (\d+).*?\*\*\s*\n\s*\*\*Issue:\*\*\s*(Critical Error|Justification Gap)", verification, re.DOTALL):
        step = int(match.group(1))
        etype = match.group(2)
        if (step, etype) not in locations:
            locations.append((step, etype))
    return locations


def detect_structural_error(current_locations, previous_locations_list):
    """Core Fix B: Detect if error pattern suggests a structural issue.
    Returns (is_structural, reason)."""
    if not current_locations:
        return False, ""
    current_steps = {loc[0] for loc in current_locations}
    current_criticals = {loc[0] for loc in current_locations if loc[1] == "Critical Error"}
    if previous_locations_list:
        prev_steps = {loc[0] for loc in previous_locations_list[-1]}
        overlap = current_steps & prev_steps
        if overlap:
            return True, f"Same step(s) {overlap} failed in consecutive iterations"
    if 1 in current_criticals:
        return True, "Critical Error in Step 1 (foundational)"
    if previous_locations_list:
        prev_criticals = {loc[0] for loc in previous_locations_list[-1] if loc[1] == "Critical Error"}
        new_criticals = current_criticals - prev_criticals
        if new_criticals:
            return True, f"Correction introduced new Critical Error(s) in step(s) {new_criticals}"
    return False, ""


def read_failure_ledger(run_dir):
    """Core Fix C: Read the failure ledger from the run directory."""
    ledger_path = run_dir / "failure_ledger.json"
    if not ledger_path.exists():
        return []
    try:
        return json.loads(ledger_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def write_failure_ledger(run_dir, entry):
    """Core Fix C: Append an entry to the failure ledger."""
    ledger_path = run_dir / "failure_ledger.json"
    ledger = read_failure_ledger(run_dir)
    ledger.append(entry)
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")


def build_failure_ledger_context(ledger):
    """Core Fix C: Build a context string from the failure ledger for SOLVE prompt."""
    if not ledger:
        return ""
    lines = ["### Prior Attempts (Cross-Run Knowledge) ###", ""]
    for entry in ledger:
        run = entry.get("run", "?")
        reason = entry.get("failure_reason", entry.get("errors", "unknown"))
        lines.append(f"Run {run}: {reason}")
        if entry.get("empirical_results"):
            lines.append(f"  Computational ground truth: {entry['empirical_results']}")
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
    """Build a dynamic pivot hint, optionally including failure reason.
    
    Gap 2: failure_reason now includes accumulated reasons from all
    failed iterations (up to last 3), giving the model more context
    about what approaches didn't work.
    """
    hint = PIVOT_HINT
    if failure_reason:
        hint += chr(10) + chr(10) + "Previous failure reason(s): " + failure_reason
    return hint


def run_outer(outer_run, problem, api_url, api_key, model, run_dir, prev_failure_reason=None, empirical_results=None, failure_context=None):
    """Run one outer attempt. Returns (accepted, candidate, summary)."""
    subdir = run_dir / f"run_{outer_run:02d}"
    subdir.mkdir(exist_ok=True)

    total_tokens = 0
    candidate_num = 0
    verify_num = 0
    pass_artifacts = []

    def call(messages, label, max_tokens=None, thinking_budget=None):
        nonlocal total_tokens
        def log_fn(msg):
            log_progress(run_dir, f"RUN {outer_run} {label}: {msg}")
        # Fix 4: Catch wall-clock timeout — treat as truncation, don't propagate to main
        try:
            content, usage, finish = chat_completion(api_url, api_key, model, messages, log_fn=log_fn, max_tokens=max_tokens, thinking_budget=thinking_budget)
        except _WallClockTimeout as wc_exc:
            # Gap P: Use partial streaming content as context for retry instead of starting from zero
            partial = wc_exc.partial_content
            partial_reasoning = wc_exc.partial_reasoning
            log_progress(run_dir, f"RUN {outer_run} {label}: wall-clock timeout, treating as truncation (partial: {len(partial)} chars content, {len(partial_reasoning)} chars reasoning)")
            try:
                if partial.strip():
                    # Include partial content as assistant message, ask to continue
                    retry_messages = list(messages) + [
                        {"role": "assistant", "content": partial},
                        {"role": "user", "content": "Continue and complete your response from where you left off. Do not repeat what you already wrote."}
                    ]
                else:
                    # No partial content (likely still in reasoning phase) — start fresh
                    retry_messages = list(messages) + [
                        {"role": "user", "content": NEUTRAL_COMPLETE_REQUEST}
                    ]
                content2, usage2, finish2 = chat_completion(
                    api_url, api_key, model, retry_messages, log_fn=log_fn
                )
                tokens2 = usage2.get("total_tokens", 0)
                total_tokens += tokens2
                log_progress(run_dir, f"RUN {outer_run} {label}: timeout retry {tokens2} tokens finish={finish2}")
                if content2.strip():
                    if partial.strip():
                        return partial + content2
                    return content2
            except _WallClockTimeout:
                log_progress(run_dir, f"RUN {outer_run} {label}: timeout retry also timed out")
            return ""  # Empty → will fail verification → error_count increments, no full pivot
        tokens = usage.get("total_tokens", 0)
        total_tokens += tokens
        log_progress(run_dir, f"RUN {outer_run} {label}: {tokens} tokens finish={finish}")
        if finish == "length":
            # Fix 1: Universal truncation retry with feedback + halved budget
            truncation_feedback = f"Your previous attempt generated {tokens} tokens and was truncated. Be more concise — focus on the essential content and omit unnecessary detail."
            log_progress(run_dir, f"RUN {outer_run} {label}: truncated, retry with feedback + halved budget")
            retry_messages = list(messages) + [
                {"role": "user", "content": truncation_feedback + chr(10) + chr(10) + NEUTRAL_COMPLETE_REQUEST}
            ]
            retry_max_tokens = max(8192, (max_tokens or MAX_TOKENS) // 2)
            retry_thinking = min(thinking_budget or THINKING_BUDGET, retry_max_tokens - 8192)
            content2, usage2, finish2 = chat_completion(
                api_url, api_key, model, retry_messages, log_fn=log_fn,
                max_tokens=retry_max_tokens,
                thinking_budget=retry_thinking,
            )
            tokens2 = usage2.get("total_tokens", 0)
            total_tokens += tokens2
            log_progress(
                run_dir,
                f"RUN {outer_run} {label}: retry {tokens2} tokens finish={finish2}",
            )
            if content2.strip():
                return content2
        return content

    # -- SOLVE --
    log_progress(run_dir, f"RUN {outer_run} SOLVE: start")
    pivot_hint = build_pivot_hint(prev_failure_reason) if outer_run > 1 else None
    solution = call(build_solver_messages(problem, outer_run, pivot_hint, empirical_results, failure_context), "SOLVE")
    save_text(subdir, "draft.md", solution)

    # -- SELF-IMPROVE --
    log_progress(run_dir, f"RUN {outer_run} SELF_IMPROVE: start")
    solution = call(
        build_self_improvement_messages(problem, solution), "SELF_IMPROVE"
    )
    save_text(subdir, "candidate_00.md", solution)
    candidate = solution

    # -- Initial VERIFY + CLASSIFY --
    log_progress(run_dir, f"RUN {outer_run} VERIFY: initial")
    verification = call(build_verifier_messages(problem, candidate), "VERIFY")
    save_text(subdir, f"verify_{verify_num:02d}.md", verification)

    log_progress(run_dir, f"RUN {outer_run} CLASSIFY: initial")
    classification = call(build_classifier_messages(verification), "CLASSIFY")
    save_text(subdir, f"classify_{verify_num:02d}.md", classification)
    verify_num += 1

    good_verify = classification
    error_count = 0
    correct_count = 1 if is_standalone_yes(good_verify) else 0
    consecutive_no = 0
    last_good_candidate = candidate if correct_count > 0 else None
    last_failure_reason = None
    all_failure_reasons = []  # Gap 2: accumulate all failure reasons
    error_locations_history = []  # Core Fix B: track error locations across iterations

    if is_standalone_yes(good_verify):
        pass_artifacts.append({
            "candidate": f"candidate_{candidate_num:02d}.md",
            "verify": f"verify_{verify_num - 1:02d}.md",
            "classify": f"classify_{verify_num - 1:02d}.md",
        })
        log_progress(run_dir, f"RUN {outer_run} initial PASS (1/{REQUIRED_PASSES})")
    elif is_standalone_improve(good_verify):
        log_progress(run_dir, f"RUN {outer_run} initial IMPROVE (minor gaps)")
    else:
        consecutive_no = 1
        last_failure_reason = extract_failure_reason(verification)
        all_failure_reasons.append(last_failure_reason)
        log_progress(run_dir, f"RUN {outer_run} initial FAIL (errors=0)")

    # -- REFINE / CORRECT / VERIFY / CLASSIFY loop --
    for i in range(MAX_ITERATIONS):
        save_state(run_dir, {
            "outer_run": outer_run,
            "iteration": i + 1,
            "consecutive_passes": correct_count,
            "error_count": error_count,
            "accepted": False,
            "status": "running",
        })

        # Decide action based on previous classification
        if is_standalone_yes(good_verify):
            # Pass - re-verify same candidate (no modification)
            pass
        elif is_standalone_improve(good_verify):
            # Improve - non-destructive refinement to close minor gaps
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} REFINE: start (closing minor gaps)",
            )
            candidate = call(
                build_refinement_messages(problem, candidate, verification),
                f"ITER {i+1} REFINE",
                max_tokens=CORRECT_MAX_TOKENS,
                thinking_budget=CORRECT_THINKING_BUDGET,
            )
            candidate_num += 1
            save_text(subdir, f"candidate_{candidate_num:02d}.md", candidate)
        else:
            # No - check tolerance before destructive correction
            if correct_count > 0 and consecutive_no == 1:
                # Tolerance: first no after passes, re-verify before correcting
                log_progress(
                    run_dir,
                    f"RUN {outer_run} ITER {i+1} TOLERANCE: re-verifying (passes={correct_count})",
                )
            else:
                # Destructive correction with fallback to last good candidate
                correct_count = 0
                error_count += 1
                pass_artifacts.clear()
                base = last_good_candidate if last_good_candidate else candidate
                log_progress(
                    run_dir,
                    f"RUN {outer_run} ITER {i+1} CORRECT: start (errors={error_count})",
                )
                candidate = call(
                    build_correction_messages(problem, base, verification),
                    f"ITER {i+1} CORRECT",
                    max_tokens=CORRECT_MAX_TOKENS,
                    thinking_budget=CORRECT_THINKING_BUDGET,
                )
                candidate_num += 1
                save_text(subdir, f"candidate_{candidate_num:02d}.md", candidate)

                # Core Fix D: CAS self-verification after CORRECT
                cas_pass, cas_details = cas_verify_candidate(
                    candidate, api_url, api_key, model, run_dir, outer_run, i + 1
                )
                if not cas_pass and CAS_MAX_RETRIES > 0:
                    log_progress(run_dir, f"RUN {outer_run} ITER {i+1} CAS_RETRY: retrying CORRECT with CAS feedback")
                    candidate = call(
                        build_correction_messages(problem, base, verification, cas_feedback=cas_details),
                        f"ITER {i+1} CAS_RETRY",
                        max_tokens=CORRECT_MAX_TOKENS,
                        thinking_budget=CORRECT_THINKING_BUDGET,
                    )
                    candidate_num += 1
                    save_text(subdir, f"candidate_{candidate_num:02d}.md", candidate)

                # Fix 2: CAS_COMPUTE — try to close computational identity gaps
                # When previous verification had only Justification Gaps (no Critical Errors),
                # attempt direct CAS verification of the incomplete identity
                if "Justification Gap" in verification and "Critical Error" not in verification:
                    augmented = cas_compute_gap(
                        candidate, verification, api_url, api_key, model, run_dir, outer_run, i + 1
                    )
                    if augmented:
                        candidate = augmented
                        candidate_num += 1
                        save_text(subdir, f"candidate_{candidate_num:02d}.md", candidate)

        # VERIFY
        log_progress(run_dir, f"RUN {outer_run} ITER {i+1} VERIFY: start")
        verification = call(
            build_verifier_messages(problem, candidate),
            f"ITER {i+1} VERIFY",
        )
        save_text(subdir, f"verify_{verify_num:02d}.md", verification)

        # Core Fix B: Track error locations for structural error detection
        current_error_locations = extract_error_locations(verification)

        # CLASSIFY
        log_progress(run_dir, f"RUN {outer_run} ITER {i+1} CLASSIFY: start")
        classification = call(
            build_classifier_messages(verification),
            f"ITER {i+1} CLASSIFY",
        )
        save_text(subdir, f"classify_{verify_num:02d}.md", classification)
        verify_num += 1

        good_verify = classification

        # Update counters based on new classification
        if is_standalone_yes(good_verify):
            correct_count += 1
            error_count = 0
            consecutive_no = 0
            last_good_candidate = candidate
            pass_artifacts.append({
                "candidate": f"candidate_{candidate_num:02d}.md",
                "verify": f"verify_{verify_num - 1:02d}.md",
                "classify": f"classify_{verify_num - 1:02d}.md",
            })
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} PASS ({correct_count}/{REQUIRED_PASSES})",
            )
        elif is_standalone_improve(good_verify):
            error_count = 0
            consecutive_no = 0
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} IMPROVE ({correct_count}/{REQUIRED_PASSES})",
            )
        else:
            consecutive_no += 1
            last_failure_reason = extract_failure_reason(verification)
            all_failure_reasons.append(last_failure_reason)
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} FAIL "
                f"(passes={correct_count} errors={error_count})",
            )

            # Core Fix B: Structural error detection
            is_structural, structural_reason = detect_structural_error(
                current_error_locations, error_locations_history
            )
            error_locations_history.append(current_error_locations)
            if is_structural:
                log_progress(
                    run_dir,
                    f"RUN {outer_run} ITER {i+1} STRUCTURAL_ERROR: {structural_reason}",
                )
                combined_reason = " | ".join(all_failure_reasons[-3:]) if all_failure_reasons else last_failure_reason
                combined_reason += f" | STRUCTURAL: {structural_reason}"
                save_state(run_dir, {
                    "outer_run": outer_run,
                    "iteration": i + 1,
                    "consecutive_passes": correct_count,
                    "error_count": error_count,
                    "accepted": False,
                    "status": "structural_error",
                })
                return False, None, {"total_tokens": total_tokens, "failure_reason": combined_reason}

        # Check thresholds
        if correct_count >= REQUIRED_PASSES:
            log_progress(
                run_dir,
                f"RUN {outer_run} ACCEPTED: {REQUIRED_PASSES} consecutive passes",
            )
            save_state(run_dir, {
                "outer_run": outer_run,
                "iteration": i + 1,
                "consecutive_passes": correct_count,
                "error_count": error_count,
                "accepted": True,
                "status": "accepted",
            })
            return True, candidate, {
                "total_tokens": total_tokens,
                "pass_artifacts": pass_artifacts,
                "iterations": i + 1,
                "failure_reason": last_failure_reason,
            }

        if error_count >= MAX_ERRORS:
            log_progress(
                run_dir,
                f"RUN {outer_run} FAILED: {MAX_ERRORS} errors reached",
            )
            save_state(run_dir, {
                "outer_run": outer_run,
                "iteration": i + 1,
                "consecutive_passes": correct_count,
                "error_count": error_count,
                "accepted": False,
                "status": "failed",
            })
            combined_reason = " | ".join(all_failure_reasons[-3:]) if all_failure_reasons else last_failure_reason
            return False, None, {"total_tokens": total_tokens, "failure_reason": combined_reason}

    log_progress(
        run_dir, f"RUN {outer_run} EXHAUSTED: {MAX_ITERATIONS} iterations"
    )
    save_state(run_dir, {
        "outer_run": outer_run,
        "iteration": MAX_ITERATIONS,
        "consecutive_passes": correct_count,
        "error_count": error_count,
        "accepted": False,
        "status": "exhausted",
    })
    combined_reason = " | ".join(all_failure_reasons[-3:]) if all_failure_reasons else last_failure_reason
    return False, None, {"total_tokens": total_tokens, "failure_reason": combined_reason}


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
        "--api-key", default=os.getenv("IMO_SOLVER_TOKEN", "")
    )
    parser.add_argument(
        "--model", default=os.getenv("IMO_SOLVER_MODEL", "")
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not args.api_url:
        parser.error("--api-url or IMO_SOLVER_API_URL is required")
    if not args.api_key:
        parser.error("--api-key or IMO_SOLVER_TOKEN is required")
    if not args.model:
        parser.error("--model or IMO_SOLVER_MODEL is required")

    problem = args.problem.read_text(encoding="utf-8")
    args.run_dir.mkdir(parents=True, exist_ok=True)

    # Pre-launch duplicate detection: check if another orchestrator is
    # already running for the same output file.
    lock_file = args.output.with_suffix(args.output.suffix + ".lock")
    if lock_file.exists():
        try:
            old_pid = int(lock_file.read_text().strip())
            try:
                os.kill(old_pid, 0)
                print(
                    f"ERROR: Another orchestrator (PID {old_pid}) is already "
                    f"running for {args.output}. Refusing to duplicate.",
                    file=sys.stderr,
                )
                sys.exit(1)
            except (ProcessLookupError, PermissionError):
                pass  # PID is dead, take over the lock
        except (ValueError, OSError):
            pass  # Corrupt lock file, take over

    lock_file.write_text(str(os.getpid()), encoding="utf-8")

    if (args.run_dir / "state.json").exists():
        print(
            f"ERROR: run directory already has state.json: {args.run_dir}",
            file=sys.stderr,
        )
        lock_file.unlink(missing_ok=True)
        sys.exit(1)

    log_progress(
        args.run_dir,
        f"ORCHESTRATOR START: problem={args.problem.name} model={args.model}",
    )

    last_failure_reason = None
    consecutive_infra_errors = 0
    for outer_run in range(1, MAX_OUTER_RUNS + 1):
        log_progress(args.run_dir, f"OUTER_RUN {outer_run}/{MAX_OUTER_RUNS}")
        save_state(args.run_dir, {
            "outer_run": outer_run,
            "status": "starting",
        })

        try:
            # Core Fix A: Pre-SOLVE empirical validation
            empirical_results = pre_solve_validation(
                problem, args.api_url, args.api_key, args.model, args.run_dir, outer_run
            )
            # Core Fix C: Read failure ledger for cross-run knowledge
            failure_ledger = read_failure_ledger(args.run_dir)
            failure_context = build_failure_ledger_context(failure_ledger)
            accepted, candidate, summary = run_outer(
                outer_run,
                problem,
                args.api_url,
                args.api_key,
                args.model,
                args.run_dir,
                prev_failure_reason=last_failure_reason,
                empirical_results=empirical_results,
                failure_context=failure_context,
            )
            consecutive_infra_errors = 0
        except InfrastructureError as exc:
            consecutive_infra_errors += 1
            backoff = INFRA_BACKOFF_BASE * (2 ** consecutive_infra_errors)
            log_progress(
                args.run_dir,
                f"RUN {outer_run} INFRA_ERROR ({consecutive_infra_errors}/{MAX_INFRA_RETRIES}): {exc}, "
                f"waiting {backoff}s before next run",
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
                lock_file.unlink(missing_ok=True)
                print(
                    f"Endpoint unavailable after {MAX_INFRA_RETRIES} consecutive errors.",
                    file=sys.stderr,
                )
                sys.exit(1)
            time.sleep(backoff)
            # Retry the same outer run instead of advancing
            continue
        except Exception as exc:
            log_progress(args.run_dir, f"RUN {outer_run} ERROR: {exc}")
            traceback.print_exc()
            save_state(args.run_dir, {
                "outer_run": outer_run,
                "status": "error",
                "error": str(exc),
            })
            continue

        if not accepted and summary and summary.get("failure_reason"):
            last_failure_reason = summary["failure_reason"]
            # Core Fix C: Write to failure ledger for cross-run knowledge
            write_failure_ledger(args.run_dir, {
                "run": outer_run,
                "failure_reason": summary["failure_reason"],
                "empirical_results": locals().get("empirical_results"),
            })

        if accepted:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(candidate, encoding="utf-8")

            manifest = {
                "problem": str(args.problem),
                "output": str(args.output),
                "sha256": sha256_text(candidate),
                "outer_run": outer_run,
                "total_tokens": summary.get("total_tokens", 0),
                "pass_artifacts": summary.get("pass_artifacts", []),
                "timestamp": now_utc(),
            }
            (args.run_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            log_progress(
                args.run_dir,
                f"ACCEPTED: output={args.output} "
                f"sha256={manifest['sha256'][:16]}...",
            )
            print(f"Solution accepted: {args.output}")
            lock_file.unlink(missing_ok=True)
            sys.exit(0)

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
    lock_file.unlink(missing_ok=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
