# Direct solver workflow for Claude Code

This workflow lets Claude Code launch and monitor a durable background solver.
The Desktop agent is an orchestrator only; it must not solve, grade, or rewrite
the mathematics itself.

## Setup

1. Read the problem file from problems/.
2. Read code/prompts.py and code/orchestrator.py to understand the algorithm.
3. Create a fresh run directory: /tmp/imo26-<problem-id>-<UTC-timestamp>.
4. Use the repo copy of orchestrator.py directly; do not regenerate it.

## Transport

The orchestrator makes OpenAI-compatible chat completion calls directly to the
configured model endpoint. It reads credentials from environment variables
or command-line flags:

- --api-url (or IMO_SOLVER_API_URL) - the chat completions endpoint
- --api-key (or IMO_SOLVER_TOKEN)  - bearer token
- --model   (or IMO_SOLVER_MODEL)  - model name

Two endpoints are available. Choose one and use its matching token:

  Cloud endpoint (from Claude settings):
    - API URL: ~/.claude/settings.json env "ANTHROPIC_BASE_URL" + /chat/completions
    - Token: ~/.claude/settings.json env "ANTHROPIC_AUTH_TOKEN"
    - Model: ~/.claude/settings.json env "ANTHROPIC_MODEL"

  Direct IP endpoint:
    - API URL: http://165.245.166.41:30000/v1/chat/completions
    - Token: onenx-dev-JgZ0YeSTHeTVh057uomgjF02
    - Model: GLM-5.2-FP8

Never print or persist the token in source files.

The orchestrator already encodes max_tokens=256000 with thinking_budget=200000
(Anthropic-style thinking via the OpenAI-compatible API), a 3600-second HTTP
timeout, and no more than three transport retries. These values are proven from
the P3 run: the solver used 124650 reasoning tokens out of the 200000 budget
and completed normally with finish_reason=stop.

## Launch

Before launching, kill any stale orchestrator process for this problem:

    pkill -f "orchestrator.py.*<problem-id>" 2>/dev/null

Launch the orchestrator as a detached background process so it survives
Bash command timeouts:

    nohup python3 code/orchestrator.py \
        --problem problems/imo2026_p1.txt \
        --api-url "$OPENAI_API_URL" \
        --api-key "$OPENAI_API_KEY" \
        --model "$MODEL_NAME" \
        --run-dir /tmp/imo26-imo2026_p1-TIMESTAMP \
        --output solutions/imo2026_p1.md \
        > /tmp/imo26-run/stdout.log 2> /tmp/imo26-run/stderr.log &

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
them.

## Monitoring - Claude Code specific

Claude Code supports long-running Bash commands. Use a Bash for-loop with
sleep to poll the progress log without busy-waiting:

    for i in $(seq 1 60); do
        sleep 60
        tail -5 /tmp/imo26-run/progress.log 2>/dev/null
        echo "---"
    done

This runs for up to 60 minutes in a single Bash call. Check the output after
it completes, then start another monitoring loop if needed.

### Process liveness check

CRITICAL: Before reading progress.log, check if the orchestrator process is
still alive. The nohup process may have been killed or crashed without
updating state.json.

    ps -p <pid>   # PID from the launch step

If the PID is gone, the orchestrator died. RESTART it immediately with the
same arguments. Do NOT monitor or piggyback on another session's run — each
session must own and manage its own orchestrator independently. "Do not
silently duplicate active requests" refers to YOUR session's own active
request only; another session running the same problem does not count as
duplication.

### Monitoring discipline

- Check state no more than once every 10 minutes of wall clock.
- Only report when an iteration completes, an error occurs, or the run
  finishes.
- Always check progress.log and stderr.log. Do NOT look for sentinel files.
- Use tail -5 <run-dir>/progress.log and cat <run-dir>/state.json for
  quick checks.
- The progress.log file has one line per state transition; tail it to see
  what happened since the last check.
- Do NOT write complex monitor scripts with heredocs or embedded Python.

## Context and budget management

CRITICAL - 4-call budget: Claude Code may abort the turn after 4 consecutive
responses that do not include a tool call. Every response MUST include at
least one tool call (e.g., a Bash command). Do not spend multiple responses
on extended thinking alone. Break work into small phases: read files, create
the run directory, launch the orchestrator, start a monitoring loop, check
results - and act on each one immediately.

CRITICAL - output token limit: If you encounter "Claude's response exceeded
the 64000 output token maximum", set CLAUDE_CODE_MAX_OUTPUT_TOKENS=65536 in
the Claude Code environment. But the preferred fix is to keep responses short:
launch the background process, then use concise monitoring loops rather than
generating long inline analysis.

## Cleanup

After the goal is achieved or failed, the agent should clean up its own
orchestrator process (kill <pid>). Do NOT kill processes that belong to
other sessions. If the goal was stopped mid-flight, stale processes can be
identified with:

    bash scripts/cleanup.sh

This lists all active processes with PIDs and run directories. Kill specific
stale ones by PID or run directory:

    bash scripts/cleanup.sh <pid>
    bash scripts/cleanup.sh <run-dir>

## Completion

On five consecutive passes, the orchestrator copies the accepted candidate to
the output path and writes a final manifest with its SHA-256 hash. The agent
may then report completion with the run directory, output path, and token
summary.

If all outer runs fail, report that no verified solution was found. Never
promote a partial result or lower the acceptance threshold.
