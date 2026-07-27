# Claude Code workflow

Claude Code launches and monitors the durable solver. It does not solve,
grade, or rewrite the olympiad proof itself. The mathematical state machine is
the same as the Codex workflow; only process launch and monitoring differ.

## Solver state machine

1. `EMPIRICAL_PROBE` optionally creates a small-case Python program. The
   orchestrator policy-checks and runs it in an OS sandbox. Its stdout is
   untrusted evidence passed to `SOLVE`.
2. `SOLVE` creates the informal candidate.
3. In the default `--self-improve recovery` mode, `SELF_IMPROVE` runs only when
   SOLVE is empty, truncated, or structurally incomplete. It receives the full
   solver context and partial output. `always` forces a complete independent
   review, while `off` disables the stage.
4. `CAS_VERIFY` may produce sandboxed computational evidence; it never edits
   or proves the candidate by itself.
5. `proof_logic` audits the complete informal proof. A clean result unlocks
   statement translation.
6. `LEAN_STATEMENT` drafts only an `imo_problem ... := by` prefix.
   `statement_fidelity` checks its quantifiers, domains, hypotheses, and
   conclusion against the original problem. Rejection redrafts only the
   statement; a pass freezes its exact bytes and SHA-256.
7. `LEAN_FORMALIZE` and `LEAN_REPAIR` may add or change only the proof body.
   A prefix change is rejected before execution. Local Lean checks the
   declaration, elaborated statement hash, and axioms in an OS sandbox.
8. AXLE is an optional hosted checker for that same Lean source. `fallback`
   calls it only when local Lean fails; `required` requires both backends.
9. `computation` audits all algebraic/computational evidence and final
   consistency with the frozen statement and formal report.
10. `VERDICT: improve` refines the proof and `VERDICT: no` corrects it. Either
   mutation clears every pass, formal result, and hash-bound artifact.
11. Acceptance requires all three audits on one candidate SHA-256 and one
    frozen-statement SHA-256, plus the selected formal gate.

There is no independent classifier call. `classify_NN.md` is the parsed final
verdict line from `verify_NN.md`; malformed or absent verdicts become `no`.
Formalization failures receive separate retries so a translation failure is
not confused with a mathematical error.

## Setup

```sh
python3 -m pip install -r requirements.txt
bash scripts/setup_lean.sh

export IMO_SOLVER_API_URL="https://model-host/v1/chat/completions"
export IMO_SOLVER_TOKEN="..."
export IMO_SOLVER_MODEL="..."
```

Optionally set `IMO_VERIFIER_MODEL`. Do not put credentials in prompts,
source files, artifacts, or command lines. A mode-0600 `--api-key-file` is the
only supported file alternative to `IMO_SOLVER_TOKEN`.

For optional AXLE:

```sh
python3 -m pip install -r requirements-axle.txt
export AXLE_API_KEY="..."
```

The AXLE client currently requires Python 3.11 or newer.

AXLE receives generated Lean source through Axiom's hosted service. It does not
run `SOLVE`, the empirical probe, CAS checks, or mathematical verification.

Generated Python and Lean execution requires macOS `sandbox-exec`. The
orchestrator has no unsandboxed execution fallback. Disabling both validation
and Lean is supported for portability but is not equivalent assurance.

## Launch

Create a fresh run directory and launch with the Bash tool's
`run_in_background: true`. Do not use `nohup`, `&`, or `screen`.

```sh
RUN_DIR="/tmp/imo26-imo2026_p1-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_DIR"
python3 code/orchestrator.py \
  --problem problems/imo2026_p1.txt \
  --run-dir "$RUN_DIR" \
  --output solutions/imo2026_p1.md \
  --self-improve recovery \
  --validation-mode sandboxed \
  --lean-mode required \
  --axle-mode fallback \
  --axle-environment lean-4.28.0 \
  >"$RUN_DIR/stdout.log" 2>"$RUN_DIR/stderr.log"
```

Save the background task ID, PID, and run directory. The P1-P6 Claude prompts
require AXLE fallback; if the client or key is missing, surface the preflight
failure rather than weakening the policy.

## Monitor

After 30 seconds:

```sh
ps -p <pid>
tail -20 <run-dir>/progress.log
tail -20 <run-dir>/stderr.log
cat <run-dir>/state.json
```

If the PID is absent, report the crash and diagnose it. Do not attach to
another task's run. If progress contains `ERROR`, diagnose immediately rather
than waiting.

Check no more than once every ten minutes and report only completed stages,
errors, or terminal state. On every completed audit, inspect:

```text
run_XX/candidate_NN.md
run_XX/verify_YY.md
run_XX/verify_YY.json
run_XX/classify_YY.md
run_XX/candidate_NN_statement_frozen.lean
run_XX/lean_verify_NN_attempt_AA.txt
```

Summarize the mathematical finding, profile, exact verdict, candidate hash,
formal-statement fidelity, and local/AXLE status.

The stream is durable: reasoning and visible output are flushed every few
seconds. A 90-minute wall-clock timeout saves those partials and makes one
smaller continuation attempt. `usage.jsonl` accounts for every model attempt,
including retries and timeouts. A UI or Bash polling timeout does not mean the
model stream failed.

## Built-in controls

- atomic output lock with owner PID;
- bounded transport retries and exponential infrastructure backoff without
  consuming a new outer-run number;
- terminal handling for invalid credentials and endpoint contracts;
- policy and OS sandbox for generated Python and local Lean;
- exact machine-verdict parsing;
- exact candidate- and frozen-statement-hash binding for all pass artifacts;
- three distinct audits instead of repeated identical grading;
- a fresh-solve pivot after three critical failures.

Do not disable, bypass, or lower these controls during a run.

## Cleanup

Background processes can survive a stopped Claude task. List owned candidates:

```sh
bash scripts/cleanup.sh
```

Terminate only a PID or exact run directory confirmed to belong to the stopped
task:

```sh
bash scripts/cleanup.sh <pid>
bash scripts/cleanup.sh <exact-run-dir>
```

The cleanup helper validates that the PID is an IMO26 orchestrator and avoids
broad regex process killing. Never terminate another task's run.

## Completion

On acceptance the orchestrator writes the solution and `manifest.json`, with
the candidate hash, frozen and elaborated statement hashes, three audit
artifacts, model settings, and global token total. If all outer runs fail,
report that no verified solution was found. Never promote a partial artifact
or relax the acceptance threshold.
