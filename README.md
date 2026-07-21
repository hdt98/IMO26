# IMO 2026 Direct Solver Harness

A harness for solving IMO 2026 problems with a durable background workflow that
uses long-request model calls (solve, self-improve, verify, classify, correct)
with 5 consecutive independent passes as the acceptance threshold.

## Quick start

1. Clone this repo.
2. Put problem files in problems/ (already included for P1 through P6).
3. Point your coding agent (Claude Code or Codex Desktop) at the repo and give
   it one of the goal prompts from prompts/.

The agent reads the workflow contract for its harness
(workflows/claude.md or workflows/codex.md), writes an orchestrator script
using the prompts from code/prompts.py, launches it as a detached background
process, and monitors until completion.

## Repo layout

problems/             IMO 2026 problem statements (P1-P6)
code/prompts.py       Authoritative role prompts (solver, verifier, etc.)
code/orchestrator.py  Background orchestrator template (OpenAI-compatible API)
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
3. VERIFY - a fresh stateless call grading the candidate
4. CLASSIFY - a fresh stateless call returning yes/no on the verifier report
5. CORRECT - if classified no, a fresh call with the bug report to fix

Five consecutive yes classifications on an unchanged candidate accepts it.
Ten consecutive no classifications or 30 iterations fails the outer run.
Up to 10 fresh outer runs are attempted.

## Requirements

Python 3.10+ with the requests library. The orchestrator reads API credentials
from the environment or a local config file; never from the repo.

## License

MIT - prompt design adapted from Lin Yang and Yichen Huang IMO solver agent.
