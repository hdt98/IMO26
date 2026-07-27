# IMO 2026 Direct Solver Harness

A harness for solving IMO 2026 problems with a durable background workflow that
uses long-request model calls (solve, self-improve, formalize, verify, classify,
correct) with local Lean verification and 5 consecutive independent passes as
the acceptance threshold.

## Quick start

1. Clone this repo.
2. Put problem files in problems/ (already included for P1 through P6).
3. Run `bash scripts/setup_lean.sh` once. This installs Lean through `elan` and
   downloads the pinned Mathlib cache for fully local proof checking.
4. Point your coding agent (Claude Code or Codex Desktop) at the repo and give
   it one of the goal prompts from prompts/.

The agent reads the workflow contract for its harness
(workflows/claude.md or workflows/codex.md), writes an orchestrator script
using the prompts from code/prompts.py, launches it as a detached background
process, and monitors until completion.

## Repo layout

problems/             IMO 2026 problem statements (P1-P6)
code/prompts.py       Authoritative role prompts (solver, verifier, etc.)
code/orchestrator.py  Background orchestrator template (OpenAI-compatible API)
lean/                 Pinned local Lean 4 + Mathlib Lake project
scripts/setup_lean.sh One-time local Lean/Mathlib setup
workflows/            Harness-specific workflow contracts
  claude.md           Claude Code: Bash long-polling, max_tokens recovery
  codex.md            Codex Desktop: screen-based monitoring, no busy-waiting
prompts/              Goal prompts to paste into each harness

## How it works

The workflow runs entirely in a background Python script. The desktop agent
launches it and monitors progress; it never holds the long model calls itself.

Each iteration:
1. SOLVE - one long-request call with the problem and solver prompt
2. SELF-IMPROVE - a second call with the solver output for refinement
3. LEAN_FORMALIZE - translate the candidate into a faithful `imo_problem`
4. LEAN_VERIFY - compile locally with Mathlib; repair the formalization once
5. VERIFY - grade the informal proof and audit formal-statement fidelity
6. CLASSIFY - return yes/improve/no on the combined report
7. CORRECT - if classified no, use the report to fix the proof

Five consecutive yes classifications on an unchanged candidate accepts it,
and the default `--lean-mode required` additionally requires a local Lean pass.
Generated `.lean` sources and compiler reports are saved in each run directory.
Three correction failures or 30 iterations fails the outer run.
Up to 10 fresh outer runs are attempted.

## Requirements

Python 3.10+ with the requests library, plus the local Lean environment created
by `bash scripts/setup_lean.sh`. Runtime proof checking is local and requires no
account or hosted proof service. The orchestrator reads model API credentials
from the environment or command line; never from the repo.

Formal verification modes:

- `--lean-mode required` (default): a candidate cannot be accepted without a
  local Lean pass.
- `--lean-mode best-effort`: always attempt Lean and feed its report to the
  verifier, but do not make compilation an acceptance gate.
- `--lean-mode off`: explicitly disable Lean.

## License

MIT - prompt design adapted from Lin Yang and Yichen Huang IMO solver agent.
