# Direct solver workflow for Codex Desktop

This workflow lets Codex Desktop launch and monitor a durable background solver.
The Desktop agent is an orchestrator only; it must not solve, grade, or rewrite
the mathematics itself.

## Setup

1. Read the problem file from problems/.
2. Read code/prompts.py and code/orchestrator.py to understand the algorithm.
3. Create a fresh run directory: /tmp/imo26-<problem-id>-<UTC-timestamp>.

## Transport

The orchestrator makes OpenAI-compatible chat completion calls directly to the
configured model endpoint. Always pass --api-url, --api-key, and --model
explicitly as command-line arguments. Do not rely on environment variable
inheritance.

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

The orchestrator encodes proven defaults: max_tokens=256000,
thinking_budget=200000, HTTP_TIMEOUT=3600, MAX_TRANSPORT_RETRIES=3,
MAX_ERRORS=3 (consecutive failures before run restart), REQUIRED_PASSES=5.

## Launch (exec_command - no screen)

Launch the orchestrator directly via exec_command. The process becomes a child
of the Codex app-server, so it is automatically cleaned up when the goal is
stopped or the app exits. No screen session is needed.

Run this command via exec_command (yield_time_ms=3000):

    python3 /Users/sonln4/IMO26/code/orchestrator.py \
      --problem /Users/sonln4/IMO26/problems/<problem-file> \
      --api-url <endpoint> \
      --api-key <token> \
      --model <model> \
      --run-dir <run-dir> \
      --output /Users/sonln4/IMO26/solutions/<problem-id>.md \
      > <run-dir>/stdout.log 2> <run-dir>/stderr.log

The exec_command call returns a session_id immediately (the process is still
running). Save this session_id for monitoring with write_stdin.

## First check after launch

CRITICAL: Wait 30 seconds after launch, then check these files:

    cat <run-dir>/progress.log
    tail <run-dir>/stderr.log

If progress.log shows any ERROR line, the orchestrator is failing. Common
causes:
  - Connection timeout to the API endpoint: wrong URL or endpoint is down
  - 401 Unauthorized: wrong token for the chosen endpoint
  - 422 Unprocessable Entity: model name not recognized

Do NOT keep waiting if you see errors. Diagnose and fix, then relaunch.
Do NOT poll for sentinel files - the orchestrator never creates them.
Files written: progress.log, state.json, stdout.log, stderr.log, and
per-run artifact directories.

## Monitoring

Monitor using write_stdin (to detect completion) and exec_command (to check
progress files).

### Monitoring loop

Repeat this cycle until the orchestrator completes:

1. Call write_stdin with the session_id, empty chars, and
   yield_time_ms=300000 (5 minutes). This blocks for up to 5 minutes.
   - If exit_code is returned: the orchestrator finished. Check results.
   - If no exit_code: still running. Continue to step 2.

2. Check progress via a separate exec_command:
       tail -5 <run-dir>/progress.log
       cat <run-dir>/state.json

3. Report significant events only (passes, errors, acceptance, failure).
   Do NOT report every polling cycle.

### State.json lag warning

state.json is saved at the TOP of each loop iteration, BEFORE the reset
logic runs. This means consecutive_passes and error_count may show stale
values from the previous iteration. The actual in-memory values are correct
but not yet written to disk. Do not be alarmed if state.json shows
consecutive_passes=3 right after a FAIL - the reset to 0 has already
happened in memory and will be reflected in the next state.json save.

### Built-in protections

The orchestrator has two built-in protections that work without agent
intervention:

1. Wall-clock timeout: signal.alarm fires after 3600 seconds (1 hour)
   per API call, regardless of server keepalive. If the server sends
   partial data that prevents the HTTP read timeout from firing, the
   alarm still triggers, causing a retry. No external watchdog needed.

2. Pivot mechanism: after 3 consecutive verification failures
   (MAX_ERRORS=3), the current run fails and a new outer run starts
   with a fresh SOLVE. The solver prompt includes a PIVOT_HINT on
   outer_run > 1, telling the model to try a fundamentally different
   approach. This prevents wasting time on wrong approaches.

### Monitoring discipline

- Poll no more than once every 5 minutes.
- Only report when an iteration completes or an error occurs.
- Do NOT create a separate monitor process or screen session.
- Do NOT write complex monitor scripts with heredocs or embedded Python.

## Resume after goal stop

If the goal was stopped and later resumed:

1. Check if the orchestrator process is still alive:
       ps aux | grep '[o]rchestrator.py.*<problem-id>'

2. If alive: resume monitoring. Find its run directory from the ps
   output and check progress.log/state.json.

3. If not alive: start a new run with a new run directory. The old
   run's artifacts are preserved for reference.

## Cleanup

When the orchestrator finishes (write_stdin returns exit_code), the
process has already exited and the session is automatically closed.
No manual cleanup is needed.

If the goal is stopped mid-flight, the Codex framework kills the
exec_command child process automatically. No orphaned processes remain.

To identify stale processes from other sessions:
    bash scripts/cleanup.sh

Kill specific stale ones by PID:
    bash scripts/cleanup.sh <pid>

## Completion

On five consecutive passes, the orchestrator copies the accepted candidate to
the output path and writes a manifest with its SHA-256 hash. Report completion
with the run directory, output path, and token summary.

If all outer runs fail, report that no verified solution was found. Never
promote a partial result or lower the acceptance threshold.
