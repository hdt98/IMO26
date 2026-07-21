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
import sys
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
    verification_system_prompt,
    verification_reminder,
    classifier_prompt,
)

# -- Constants --

MAX_ITERATIONS = 30
MAX_ERRORS = 10
REQUIRED_PASSES = 5
MAX_OUTER_RUNS = 10
MAX_TOKENS = 256_000
HTTP_TIMEOUT = 3600
MAX_TRANSPORT_RETRIES = 3
TEMPERATURE = 0.1


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


# -- API --

def chat_completion(api_url, api_key, model, messages):
    """Return (content, usage, finish_reason) from an OpenAI-compatible call."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }
    last_error = None
    for attempt in range(1, MAX_TRANSPORT_RETRIES + 1):
        try:
            resp = requests.post(
                api_url, headers=headers, json=payload, timeout=HTTP_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage", {})
            finish = choice.get("finish_reason", "unknown")
            return content, usage, finish
        except Exception as exc:
            last_error = exc
            if attempt < MAX_TRANSPORT_RETRIES:
                time.sleep(5 * attempt)
    raise RuntimeError(
        f"API call failed after {MAX_TRANSPORT_RETRIES} attempts: {last_error}"
    )


# -- Prompt builders --

DNL = "\n\n"
DIV = "\n" + "=" * 70 + "\n"


def build_solver_messages(problem):
    user = DIV + "### Problem ###" + DNL + problem.strip()
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": user},
    ]


def build_self_improvement_messages(problem, solution):
    user = DIV + "### Problem ###" + DNL + problem.strip()
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": user},
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
        DIV + "### Problem ###" + DNL + problem.strip() + DNL
        + DIV + "### Solution ###" + DNL + detailed + DNL
        + verification_reminder.strip()
    )
    return [
        {"role": "system", "content": verification_system_prompt.strip()},
        {"role": "user", "content": user},
    ]


def build_classifier_messages(verification):
    user = classifier_prompt.strip() + DNL + verification.strip()
    return [{"role": "user", "content": user}]


def build_correction_messages(problem, solution, verification):
    bug_report = extract_section(verification, "Detailed Verification", after=False)
    if not bug_report:
        bug_report = verification.strip()
    user = (
        DIV + "### Problem ###" + DNL + problem.strip() + DNL
        + DIV + "### Current Solution ###" + DNL + solution.strip() + DNL
        + correction_prompt.strip() + DNL
        + DIV + "### Bug Report ###" + DNL + bug_report.strip()
    )
    return [
        {"role": "system", "content": step1_prompt.strip()},
        {"role": "user", "content": user},
    ]


# -- Solver loop --

def run_outer(outer_run, problem, api_url, api_key, model, run_dir):
    """Run one outer attempt. Returns (accepted, candidate, summary)."""
    subdir = run_dir / f"run_{outer_run:02d}"
    subdir.mkdir(exist_ok=True)

    total_tokens = 0
    candidate_num = 0
    verify_num = 0
    pass_artifacts = []

    def call(messages, label):
        nonlocal total_tokens
        content, usage, finish = chat_completion(api_url, api_key, model, messages)
        tokens = usage.get("total_tokens", 0)
        total_tokens += tokens
        log_progress(run_dir, f"RUN {outer_run} {label}: {tokens} tokens finish={finish}")
        if finish == "length":
            log_progress(run_dir, f"RUN {outer_run} {label}: WARNING truncated (max_tokens)")
        return content

    # -- SOLVE --
    log_progress(run_dir, f"RUN {outer_run} SOLVE: start")
    solution = call(build_solver_messages(problem), "SOLVE")
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
    correct_count = 1  # matches agent.py: initial pass counts as 1

    if "yes" in good_verify.lower():
        pass_artifacts.append({
            "candidate": f"candidate_{candidate_num:02d}.md",
            "verify": f"verify_{verify_num - 1:02d}.md",
            "classify": f"classify_{verify_num - 1:02d}.md",
        })
        log_progress(run_dir, f"RUN {outer_run} initial PASS (1/{REQUIRED_PASSES})")
    else:
        log_progress(run_dir, f"RUN {outer_run} initial FAIL (errors=0)")

    # -- CORRECT / VERIFY / CLASSIFY loop --
    for i in range(MAX_ITERATIONS):
        save_state(run_dir, {
            "outer_run": outer_run,
            "iteration": i + 1,
            "consecutive_passes": correct_count,
            "error_count": error_count,
            "accepted": False,
            "status": "running",
        })

        if "yes" not in good_verify.lower():
            correct_count = 0
            error_count += 1
            pass_artifacts.clear()

            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} CORRECT: start (errors={error_count})",
            )
            candidate = call(
                build_correction_messages(problem, candidate, verification),
                f"ITER {i+1} CORRECT",
            )
            candidate_num += 1
            save_text(subdir, f"candidate_{candidate_num:02d}.md", candidate)

        # VERIFY
        log_progress(run_dir, f"RUN {outer_run} ITER {i+1} VERIFY: start")
        verification = call(
            build_verifier_messages(problem, candidate),
            f"ITER {i+1} VERIFY",
        )
        save_text(subdir, f"verify_{verify_num:02d}.md", verification)

        # CLASSIFY
        log_progress(run_dir, f"RUN {outer_run} ITER {i+1} CLASSIFY: start")
        classification = call(
            build_classifier_messages(verification),
            f"ITER {i+1} CLASSIFY",
        )
        save_text(subdir, f"classify_{verify_num:02d}.md", classification)
        verify_num += 1

        good_verify = classification

        if "yes" in good_verify.lower():
            correct_count += 1
            error_count = 0
            pass_artifacts.append({
                "candidate": f"candidate_{candidate_num:02d}.md",
                "verify": f"verify_{verify_num - 1:02d}.md",
                "classify": f"classify_{verify_num - 1:02d}.md",
            })
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} PASS ({correct_count}/{REQUIRED_PASSES})",
            )
        else:
            log_progress(
                run_dir,
                f"RUN {outer_run} ITER {i+1} FAIL "
                f"(passes={correct_count} errors={error_count})",
            )

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
            return False, None, {"total_tokens": total_tokens}

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
    return False, None, {"total_tokens": total_tokens}


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

    if (args.run_dir / "state.json").exists():
        print(
            f"ERROR: run directory already has state.json: {args.run_dir}",
            file=sys.stderr,
        )
        sys.exit(1)

    log_progress(
        args.run_dir,
        f"ORCHESTRATOR START: problem={args.problem.name} model={args.model}",
    )

    for outer_run in range(1, MAX_OUTER_RUNS + 1):
        log_progress(args.run_dir, f"OUTER_RUN {outer_run}/{MAX_OUTER_RUNS}")
        save_state(args.run_dir, {
            "outer_run": outer_run,
            "status": "starting",
        })

        try:
            accepted, candidate, summary = run_outer(
                outer_run,
                problem,
                args.api_url,
                args.api_key,
                args.model,
                args.run_dir,
            )
        except Exception as exc:
            log_progress(args.run_dir, f"RUN {outer_run} ERROR: {exc}")
            traceback.print_exc()
            save_state(args.run_dir, {
                "outer_run": outer_run,
                "status": "error",
                "error": str(exc),
            })
            continue

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
    sys.exit(1)


if __name__ == "__main__":
    main()

