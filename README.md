# IMO 2026 Direct Solver Harness

A durable background workflow for producing and auditing IMO solutions with
model-generated proofs, three distinct verifier profiles, sandboxed
computational evidence, and optional Lean/AXLE formal verification.

## Requirements

- Python 3.10 or newer with `requests`
- macOS `sandbox-exec` for generated Python and local Lean execution
- Lean 4 and pinned Mathlib, installed by `bash scripts/setup_lean.sh`
- Optional AXLE client from `requirements-axle.txt` (Python 3.11+)

The default workflow never executes model-generated Python or Lean directly on
the host. If `sandbox-exec` is unavailable, use `--validation-mode off` and
`--lean-mode off`; the orchestrator refuses an unsandboxed fallback.

## Setup

```sh
python3 -m pip install -r requirements.txt
bash scripts/setup_lean.sh

export IMO_SOLVER_API_URL="https://model-host/v1/chat/completions"
export IMO_SOLVER_TOKEN="..."
export IMO_SOLVER_MODEL="..."
```

The model token is read only from `IMO_SOLVER_TOKEN` or a mode-0600
`--api-key-file`. It must never be committed or placed on a command line.

AXLE is optional:

```sh
python3 -m pip install -r requirements-axle.txt
export AXLE_API_KEY="..."
```

## Repository layout

```text
code/         orchestrator and authoritative model prompts
lean/         pinned Lean 4 and Mathlib project
problems/     P1-P6 problem statements
prompts/      Codex/Claude launch prompts and templates
scripts/      Lean setup and safe process cleanup
tests/        workflow, sandbox, Lean, and AXLE regressions
workflows/    harness-specific launch and monitoring contracts
```

## Flow

1. `EMPIRICAL_PROBE` optionally generates a small-case script. The script is
   policy-checked, sandboxed, output-bounded, and preserved with stdout/stderr.
   Its output is explicitly labeled untrusted evidence.
2. `SOLVE` produces the first informal candidate.
3. `SELF_IMPROVE` runs only when SOLVE is incomplete by default. Configure
   `--self-improve recovery|always|off`; `always` forces one independent review
   even when SOLVE is already complete.
4. `proof_logic` audits the complete informal argument before formal work.
5. `LEAN_STATEMENT` drafts only the `imo_problem ... := by` prefix.
   `statement_fidelity` compares that exact prefix with the natural-language
   problem. A rejection redrafts the statement without mutating the informal
   proof; a pass freezes the exact bytes and their SHA-256.
6. `LEAN_FORMALIZE` may add only the proof body after the frozen prefix.
   Repairs that alter the prefix are rejected before execution. Local Lean
   verifies the theorem, records the elaborated statement hash and axioms, and
   runs in an OS sandbox.
7. `--axle-mode fallback` may check the same frozen-statement source through
   hosted AXLE when local Lean fails; `required` requires both backends.
8. `computation` audits algebraic/computational evidence and final consistency
   among the informal proof, frozen statement, and formal report.
9. A verdict of `improve` or `no` on the mathematical audits clears every pass
   before candidate mutation. Acceptance requires all three audit artifacts
   to carry one candidate SHA-256 and one frozen-statement SHA-256, plus the
   configured formal gate.

`CAS_VERIFY` and `CAS_COMPUTE` produce preserved evidence only. Computational
output is sent to the verifier/corrector and is never appended to the solution
as a substitute for proof.

## Run

```sh
python3 code/orchestrator.py \
  --problem problems/imo2026_p1.txt \
  --run-dir /tmp/imo26-p1-$(date -u +%Y%m%dT%H%M%SZ) \
  --output solutions/imo2026_p1.md \
  --lean-mode required \
  --axle-mode off \
  --self-improve recovery \
  --validation-mode sandboxed
```

Model configuration comes from the environment. `IMO_VERIFIER_MODEL` may name
a distinct verifier model; otherwise the solver model is reused with three
different audit profiles.

## Artifacts

Each run records:

- candidates, verifier reports, derived verdicts, and candidate hashes;
- generated empirical/CAS scripts with stdout, stderr, and execution metadata;
- live reasoning and visible partial-output files flushed during streaming;
- proposed and frozen Lean statements, proof sources, local/AXLE reports,
  candidate hash, frozen-statement hash, and elaborated-statement hash;
- `usage.jsonl` with every model attempt, including retries and timeouts;
- `state.json`, `failure_ledger.json`, and the accepted `manifest.json`.

The example uses `/tmp` for disposable experiments. For retained audit trails,
point `--run-dir` at a persistent, access-controlled location; reasoning traces
can be large and may contain sensitive problem-solving context.

## Verification modes

- `--lean-mode required` (default): acceptance requires the formal gate.
- `--lean-mode best-effort`: formal reports inform the verifier but do not gate
  acceptance.
- `--lean-mode off`: no Lean source is generated or executed.
- `--axle-mode off` (default): never contact AXLE.
- `--axle-mode fallback`: use AXLE only after local Lean fails.
- `--axle-mode required`: require both local Lean and AXLE.

AXLE sends generated Lean source to Axiom's hosted service. The AXLE key is
accepted only through `AXLE_API_KEY`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests cover candidate and frozen-statement hash invariants, isolated
statement redrafting, prefix-preserving proof repair, streak resets,
fail-closed CAS handling, sandbox policy, atomic locks, Lean declaration
validation, local Lean compilation, and AXLE semantics.

## License

MIT - prompt design adapted from Lin Yang and Yichen Huang IMO solver agent.
