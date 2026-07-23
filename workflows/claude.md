# Direct solver workflow for Claude Code

This workflow lets Claude Code launch and monitor a durable background solver.
The Desktop agent is an orchestrator only; it must not solve, grade, or rewrite
the mathematics itself.

## Setup

1. Read the problem file from problems/.
2. Read code/prompts.py and code/orchestrator.py to understand the algorithm.
3. Create a fresh run directory: /tmp/imo26-<problem-id>-<UTC-timestamp>.

## Transport

The orchestrator makes OpenAI-compatible chat completion calls directly to the
configured model endpoint. It reads credentials from command-line flags:

- --api-url  - the chat completions endpoint
- --api-key  - bearer token
- --model    - model name

Direct IP endpoint:
    - API URL: http://165.245.166.41:30000/v1/chat/completions
    - Token: onenx-dev-JgZ0YeSTHeTVh057uomgjF02
    - Model: GLM-5.2-FP8

Never print or persist the token in source files.

The orchestrator already encodes max_tokens=256000 with thinking_budget=200000,
a 5400-second HTTP timeout, a 5400-second wall-clock timeout (threading.Timer),
and no more than three transport retries. These values are proven from
the P3 run: the solver used 124650 reasoning tokens out of the 200000 budget
and completed normally with finish_reason=stop.

The orchestrator uses streaming mode (stream=True) with SSE chunk parsing
and stream_options={"include_usage": true} to capture token counts. Streaming
keeps the connection alive during long generation (20-60 min per SOLVE call)
and prevents the server from closing idle non-streaming connections.

## Launch (run_in_background - no nohup, no screen)

Launch the orchestrator using the Bash tool with run_in_background: true.
This gives Claude Code a backgroundTaskId for tracking and writes output to
a managed file. The process survives session stops by design - this is
desired for long-running orchestrator runs that may take 30+ minutes.

    python3 code/orchestrator.py \
        --problem problems/imo2026_p1.txt \
        --api-url "$API_URL" \
        --api-key "$API_KEY" \
        --model "$MODEL_NAME" \
        --run-dir /tmp/imo26-imo2026_p1-TIMESTAMP \
        --output solutions/imo2026_p1.md

Set run_in_background: true in the Bash tool call. Do NOT use nohup, &, or
screen - run_in_background: true provides proper task tracking and process
management. Save the backgroundTaskId, the PID, and the run directory path.

To get the PID after launch, run:
    ps aux | grep '[o]rchestrator.py.*<problem-id>'

## First check after launch

CRITICAL: Wait 30 seconds after launch, then check these files:

    cat <run-dir>/progress.log
    tail <run-dir>/stderr.log

If progress.log shows any ERROR line, the orchestrator is failing. Common
causes:
  - Connection timeout to the API endpoint: wrong URL or endpoint is down
  - 401 Unauthorized: wrong token for the chosen endpoint
  - 422 Unprocessable Entity: model name not recognized

Do NOT keep waiting if you see errors. Diagnose and fix the issue, then
relaunch. Do NOT poll for sentinel files - the orchestrator never creates
them.

## Monitoring

Monitor using the Bash tool to check progress files. Claude Code will notify
you when the background task completes.

### Monitoring loop

Repeat this cycle until the orchestrator completes:

1. Use a Bash command to check progress:
       tail -5 <run-dir>/progress.log
       cat <run-dir>/state.json

2. Check process liveness:
       ps -p <pid>

3. Report significant events only (passes, errors, acceptance, failure).
   Do NOT report every polling cycle.

### Process liveness check

CRITICAL: Before reading progress.log, check if the orchestrator process is
still alive. The background process may have been killed or crashed without
updating state.json.

    ps -p <pid>   # PID from the launch step

If the PID is gone, the orchestrator died. RESTART it immediately with the
same arguments. Do NOT monitor or piggyback on another session's run - each
session must own and manage its own orchestrator independently. "Do not
silently duplicate active requests" refers to YOUR session's own active
request only; another session running the same problem does not count as
duplication.

### State.json lag warning

state.json is saved at the TOP of each loop iteration, BEFORE the reset
logic runs. This means consecutive_passes and error_count may show stale
values from the previous iteration. The actual in-memory values are correct
but not yet written to disk. Do not be alarmed if state.json shows
consecutive_passes=3 right after a FAIL - the reset to 0 has already
happened in memory and will be reflected in the next state.json save.

### Built-in protections

The orchestrator has built-in protections that work without agent
intervention:

1. Wall-clock timeout: threading.Timer fires after 5400 seconds (90 minutes)
   per API call, regardless of server keepalive. If the server sends
   partial data that prevents the HTTP read timeout from firing, the
   timer still triggers, aborting the call without retry. The failed run is then treated as a regular failure, triggering the pivot mechanism if needed.

2. Infrastructure error detection: connection errors (endpoint down,
   DNS failure) are detected separately from model errors. The
   orchestrator waits with exponential backoff (30s, 60s, 120s) before
   retrying, instead of burning through outer runs. After 5 consecutive
   infrastructure errors, it terminates with ENDPOINT_UNAVAILABLE status.

3. Duplicate run prevention: a lock file (<output>.lock) prevents two
   orchestrators from running for the same problem simultaneously.

4. Three-tier classifier: the classifier outputs "yes" (clean pass),
   "improve" (minor gaps, conclusion valid - triggers non-destructive
   refinement without resetting pass count), or "no" (critical error -
   triggers destructive correction).

5. Tolerance: first "no" after passes triggers a re-verify before
   destructive correction, handling stochastic false negatives.

6. Pivot mechanism: after 3 consecutive verification failures
   (MAX_ERRORS=3), the current run fails and a new outer run starts
   with a fresh SOLVE. The solver prompt includes a PIVOT_HINT on
   outer_run > 1, telling the model to try a fundamentally different
   approach. This prevents wasting time on wrong approaches.

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

## Resume after goal stop

If the goal was stopped and later resumed:

1. Check if the orchestrator process is still alive:
       ps aux | grep '[o]rchestrator.py.*<problem-id>'

2. If alive: resume monitoring. Find its run directory from the ps
   output and check progress.log/state.json.

3. If not alive: start a new run with a new run directory. The old
   run's artifacts are preserved for reference.

Note: Backgrounded processes survive goal stops by design. This is
desired - the orchestrator should keep running even if the Claude Code
session is interrupted. The process is NOT automatically killed when
the goal is stopped.

## Cleanup

When the orchestrator finishes (progress.log shows ACCEPTED or FAILED),
kill the background process:

    kill <pid>

If the goal was stopped mid-flight, stale processes can be identified with:

    bash scripts/cleanup.sh

Kill specific stale ones by PID or run directory:

    bash scripts/cleanup.sh <pid>
    bash scripts/cleanup.sh <run-dir>

Do NOT kill processes that belong to other sessions.

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

## About the 600000ms timeout

The Anthropic SDK's default timeout is 600000ms (10 minutes) for non-streaming
API requests. This does NOT affect the orchestrator, which makes its own API
calls directly to the model endpoint with a 5400-second timeout. Claude Code
uses streaming with API_TIMEOUT_MS=3000000 (50 minutes), so its own API calls
are not affected either. The 600000ms timeout is NOT a blocker for this
workflow.

## Completion

On five consecutive passes, the orchestrator copies the accepted candidate to
the output path and writes a final manifest with its SHA-256 hash. The agent
may then report completion with the run directory, output path, and token
summary.

If all outer runs fail, report that no verified solution was found. Never
promote a partial result or lower the acceptance threshold.
