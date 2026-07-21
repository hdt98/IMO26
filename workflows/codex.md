# Direct solver workflow for Codex Desktop

This workflow lets Codex Desktop launch and monitor a durable background solver.
The Desktop agent is an orchestrator only; it must not solve, grade, or rewrite
the mathematics itself.

## Setup

1. Read the problem file from problems/.
2. Read code/prompts.py and code/orchestrator.py to understand the algorithm.
3. Create a fresh run directory: /tmp/imo26-<problem-id>-<UTC-timestamp>.
4. Write the orchestrator script to the run directory (or use the repo copy).

## Transport

The orchestrator makes OpenAI-compatible chat completion calls directly to the
configured model endpoint. It reads credentials from environment variables:

- IMO_SOLVER_API_URL - the chat completions endpoint
- IMO_SOLVER_TOKEN - bearer token
- IMO_SOLVER_MODEL - model name

Never print or persist the token. If credentials live in ~/.claude/settings.json
under env, extract them at runtime without exposing values.

Use max_tokens=256000 with thinking_budget=200000 (Anthropic-style thinking
via the OpenAI-compatible API), a 3600-second HTTP timeout, and no more than
three transport retries. These values are proven from the P3 run: the solver
used 124650 reasoning tokens out of the 200000 budget and completed normally
with finish_reason=stop. The orchestrator script already encodes these defaults.

## Launch

Launch the orchestrator inside a detached screen session so it survives
exec_command yielding:

    screen -dmS imo_solver bash -c       'cd <run-dir> &&        IMO_SOLVER_TOKEN=<token>        IMO_SOLVER_API_URL=<endpoint>        IMO_SOLVER_MODEL=<model>        python3 orchestrator.py          --problem <repo>/problems/<problem-file>          --run-dir <run-dir>          --output <repo>/solutions/<problem-id>.md          > stdout.log 2> stderr.log'

## Monitoring - Codex Desktop specific

CRITICAL: Do NOT use sleep inside exec_command. The exec_command tool yields
after at most 30 seconds and the PTY closure kills the process. This creates a
busy-wait loop that fills the context with polling noise and triggers context
compaction.

Instead, use one of these patterns:

### Pattern A: Screen-based monitor (recommended)

Create a monitor screen session that writes progress to a file:

    screen -dmS monitor bash -c       'while true; do tail -1 <run-dir>/progress.log > <run-dir>/monitor_latest.txt 2>/dev/null; sleep 600; done'

Then check periodically (every 10-15 minutes):

    cat <run-dir>/monitor_latest.txt

### Pattern B: Long for-loop with nohup

    nohup bash -c 'for i in $(seq 1 30); do sleep 60; tail -1 <run-dir>/progress.log; done' > <run-dir>/monitor_output.txt 2>&1 &

Then check: cat <run-dir>/monitor_output.txt

### Monitoring discipline

- Check state no more than once every 10 minutes.
- Only report when an iteration completes or an error occurs.
- Do NOT write complex monitor scripts with heredocs or embedded Python;
  they break due to shell escaping issues.
- Use tail -5 <run-dir>/progress.log and cat <run-dir>/state.json for quick checks.
- The progress.log file has one line per state transition; tail it to see what
  happened since the last check.

## Context management

The orchestrator writes a one-line summary to progress.log on every state
transition. This is the primary monitoring interface. After each check, read
state.json for full state (outer_run, iteration, consecutive_passes,
error_count, accepted, status).

## Completion

On five consecutive passes, the orchestrator copies the accepted candidate to
the output path and writes a final manifest with its SHA-256 hash. The agent
may then report completion with the run directory, output path, and token
summary.

If all outer runs fail, report that no verified solution was found. Never
promote a partial result or lower the acceptance threshold.
