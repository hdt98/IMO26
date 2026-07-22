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

Two endpoints are available. Choose one and use its matching token:

  Cloud endpoint (from Codex config):
    - API URL: ~/.codex/config.toml [model_providers.custom] base_url + /chat/completions
    - Token: ~/.codex/auth.json key "OPENAI_API_KEY"
    - Model: ~/.codex/config.toml top-level "model"

  Direct IP endpoint:
    - API URL: http://165.245.166.41:30000/v1/chat/completions
    - Token: onenx-dev-JgZ0YeSTHeTVh057uomgjF02
    - Model: GLM-5.2-FP8

Never print or persist the token. Extract it at runtime without exposing values.

Use max_tokens=256000 with thinking_budget=200000 (Anthropic-style thinking
via the OpenAI-compatible API), a 3600-second HTTP timeout, and no more than
three transport retries. These values are proven from the P3 run: the solver
used 124650 reasoning tokens out of the 200000 budget and completed normally
with finish_reason=stop. The orchestrator script already encodes these defaults.

## Launch

Launch the orchestrator inside a detached screen session so it survives
exec_command yielding:

    screen -S imo_<problem-id> -X quit 2>/dev/null

    screen -dmS imo_<problem-id> bash -c \
      'cd <run-dir> && \
       IMO_SOLVER_TOKEN=<token> \
       IMO_SOLVER_API_URL=<endpoint> \
       IMO_SOLVER_MODEL=<model> \
       python3 orchestrator.py \
         --problem <repo>/problems/<problem-file> \
         --api-url <endpoint> \
         --api-key <token> \
         --model <model> \
         --run-dir <run-dir> \
         --output <repo>/solutions/<problem-id>.md \
         > stdout.log 2> stderr.log'

Always pass --api-url, --api-key, and --model explicitly as command-line
arguments. Do not rely on environment variable inheritance.

## First check after launch

CRITICAL: Wait 30 seconds after launch, then check these files before doing
anything else:

    cat <run-dir>/progress.log
    tail <run-dir>/stderr.log

If progress.log shows any ERROR line, the orchestrator is failing. Common
causes:
  - Connection timeout to the API endpoint: wrong URL or endpoint is down
  - 401 Unauthorized: wrong token for the chosen endpoint
  - 422 Unprocessable Entity: model name not recognized

Do NOT keep waiting if you see errors. Diagnose and fix the issue, then
relaunch. Do NOT poll for sentinel files — the orchestrator never creates
them. The only files it writes are: progress.log, state.json, stdout.log,
stderr.log, and per-run artifact directories.

## Monitoring - Codex Desktop specific

CRITICAL: Do NOT use sleep inside exec_command. The exec_command tool yields
after at most 30 seconds and the PTY closure kills the process. This creates a
busy-wait loop that fills the context with polling noise and triggers context
compaction.

Do NOT create a separate "monitor" screen session. Check progress directly:

    tail -5 <run-dir>/progress.log
    cat <run-dir>/state.json

### Process liveness check

CRITICAL: Before reading progress.log, check if the orchestrator process is
still alive. The screen session may have been killed, taking the process
with it, without updating state.json.

    ps -p <pid>   # PID from the launch step

If the PID is gone, the orchestrator died. RESTART it immediately in a new
screen session with the same arguments. Do NOT monitor or piggyback on
another session's run — each session must own and manage its own orchestrator
independently. "Do not silently duplicate active requests" refers to YOUR
session's own active request only; another session running the same problem
does not count as duplication.

### Monitoring discipline

- Check state no more than once every 10 minutes.
- Only report when an iteration completes or an error occurs.
- Always check progress.log and stderr.log. Do NOT look for sentinel files.
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

## Cleanup

After the goal is achieved or failed, the agent should clean up its own
processes:

    kill <orchestrator-pid>
    screen -S <screen-name> -X quit 2>/dev/null

Do NOT kill processes that belong to other sessions. If the goal was stopped
mid-flight (not completed), stale processes can be identified with:

    bash scripts/cleanup.sh

This lists all active orchestrator processes, screen sessions, and monitoring
loops with their PIDs and run directories. Kill specific stale ones by PID:

    bash scripts/cleanup.sh <pid>

Or by run directory:

    bash scripts/cleanup.sh <run-dir>

## Completion

On five consecutive passes, the orchestrator copies the accepted candidate to
the output path and writes a final manifest with its SHA-256 hash. The agent
may then report completion with the run directory, output path, and token
summary.

If all outer runs fail, report that no verified solution was found. Never
promote a partial result or lower the acceptance threshold.
