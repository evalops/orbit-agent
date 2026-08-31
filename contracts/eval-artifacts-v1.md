# Conformance Contract: Eval Artifacts

- **Contract ID:** `eval-artifacts`
- **Version:** `1.0.0` (semver; see [Versioning](#versioning))
- **Status:** Active
- **Machine-readable source of truth:** [`conformance/contract.py`](../conformance/contract.py)
- **Verifier:** [`conformance/verify.py`](../conformance/verify.py)
- **Resolves:** promotes the latent doc/API promises flagged in issue #31 into
  explicit, enforced requirements.

## Why this exists

Orbit Agent's eval subsystem is not just an internal test harness. It emits
**artifacts that other actors consume**: a JSONL record per scenario, a per-scenario
CSV, a Markdown table, and a run-summary dict. `eval report`, `eval grade`, and
`eval summary` all read the JSONL that `eval run` wrote, and an operator (or a
downstream EvalOps agent) may parse the CSV/MD or shell out to the documented CLI.

Today those shapes are *implied* by the code and *described* in prose in the
README (Features, Commands, and "Evals & Self-Grading" sections). Nothing stops a
routine refactor from renaming a field, flipping a format rule, or dropping a CSV
column — a change that passes the existing unit tests but silently breaks every
downstream consumer. This contract turns those implied promises into normative
requirements and adds a deterministic checker that fails when the code drifts.

## Repo-specific workflow under contract

```
scenarios.yaml ──eval run──▶ EvalRecord JSONL ──┬─ eval report  ─▶ run-summary dict
                                                ├─ eval grade   ─▶ rubric grades JSONL
                                                └─ eval summary ─▶ CSV + Markdown tables
```

The contract governs the **interfaces between these stages and their external
consumers**, not the advice content itself (which is model-dependent and lives
under the rubric/eval logic, not here).

## Correctness / threat model

The failure this contract defends against is **silent artifact drift**: a change
that keeps producing well-formed-looking output while breaking a documented
promise a consumer relies on. Concretely:

- A consumer parsing JSONL depends on the field set, names, and types of
  `EvalRecord`. Renaming/removing/retyping a field breaks it.
- `format_ok` is a trust signal. If it can be `true` while the response violates
  the stated rule (3–5 actions, exactly 3 risks, non-empty advice), every
  quality gate keyed on it is compromised. Degraded output must be **observable**,
  not silently accepted.
- CSV consumers are positional; column **order** is part of the contract.
- Operator scripts and agents shell out to documented CLI flags; renaming a flag
  is a breaking change even if the Python function still works.

This is a correctness/compatibility contract, not a security boundary — it
assumes the repo's own source is trusted and simply pins its observable surface.

## Normative requirements

All requirements are enforced by `conformance/verify.py`. "MUST" language is
normative. Source-of-truth locations are cited so future edits update both the
code and the contract in the same change.

### R1 — `EvalRecord` JSONL schema
*(source: `orbit_agent/evals.py::EvalRecord`, `run_evals`)*

Every record MUST be a JSON object with these required fields and JSON types:
`scenario_id` (string), `prompt` (string), `timestamp` (number), `latency_ms`
(number), `advice` (string), `actions` (array), `metric_to_watch` (string),
`risks` (array), `critic_score` (integer), `critic_feedback` (string),
`format_ok` (boolean), `actions_count` (integer), `risks_count` (integer).
`overlap_ratio` (number) is OPTIONAL and MAY be `null`. Unknown fields are a
violation (add them via a MINOR bump first).

### R2 — Count consistency
*(source: `run_evals`)*

`actions_count` MUST equal `len(actions)` and `risks_count` MUST equal
`len(risks)` within a record.

### R3 — Format rule and `format_ok`
*(source: `orbit_agent/evals.py::_format_eval`)*

The "good response" rule is: `3 ≤ actions_count ≤ 5` **and** `risks_count == 3`
**and** advice is non-empty. `format_ok` MUST equal the truth of this rule for
the record. A record whose `format_ok` disagrees with the rule is a
degraded-mode violation (see [Negative modes](#negative-modes)).

### R4 — Run-summary keys
*(source: `orbit_agent/evals.py::summarize_results`)*

A non-empty summary MUST contain exactly: `count`, `format_ok_rate`,
`avg_critic_score`, `avg_latency_ms`, `avg_playbook_overlap`.

### R5 — CSV summary schema
*(source: `orbit_agent/evals.py::export_summary_csv`)*

The header MUST be, in order: `scenario_id, count, avg_critic_score,
avg_overlap, avg_latency_ms`.

### R6 — Markdown summary schema
*(source: `orbit_agent/evals.py::export_summary_md`)*

The header row cells MUST be, in order: `Scenario, Count, Avg Score, Overlap,
Latency (ms)`.

### R7 — CLI surface
*(source: README Commands/Evals; `orbit_agent/cli.py`)*

These commands MUST exist, registered on the documented Typer app, each exposing
the documented **CLI flag** (for options) or remaining a **positional argument**:
`models list` (`--provider`); `eval run` (`--dataset`, `--out`);
`eval report` (`results_path` positional argument); `eval grade`
(`--dataset`, `--results-path`, `--out`); `eval summary` (`--input-path`,
`--csv-out`, `--md-out`).

The verifier checks the Typer *declaration*, not just the Python parameter name:
an explicitly renamed flag (`typer.Option("--data", ...)`) or an
option⇄argument swap is a violation even when the parameter name is unchanged.

## Conformance check

The verifier runs two check classes, both pure standard library (no `dspy`, no
model provider, deterministic):

1. **Code-drift** — compares the shapes declared in `orbit_agent/evals.py` and
   `orbit_agent/cli.py` to the contract, catching an incompatible change before
   it merges. These checks are **fail-closed**: a required rule the verifier
   cannot confirm is a violation, never a silent pass. There is no "skip" for a
   contracted guarantee. To keep fail-closed from firing on harmless refactors,
   the format rule (R3) is verified **behaviorally** — the `_format_eval`
   function is extracted, executed in isolation, and probed at its boundaries —
   so any equivalent rewrite still passes and only a real behavior change (or a
   function that cannot be evaluated at all) fails. The CLI check (R7) inspects
   the Typer command/option/argument declarations, not just parameter names.
2. **Artifact validation** — validates a concrete `.jsonl` / `.csv` / `.md`
   against R1–R6, including R2/R3 internal consistency.

### Run it

```bash
# Code-drift only (fast; no artifacts needed):
python -m conformance.verify

# Validate real artifacts too:
python -m conformance.verify \
  --jsonl .orbit/evals/personas.jsonl \
  --csv reports/personas.csv \
  --md reports/personas.md \
  --evidence-out .orbit/conformance/evidence.json

# Or via make:
make contract
```

In CI it runs automatically as part of `pytest -q`
(`tests/test_contract_conformance.py`), which needs none of the model
dependencies and so also works in a minimal lane.

### Evidence

Every run writes an evidence JSON (default `.orbit/conformance/evidence.json`)
recording, for a downstream agent/operator to cite:

- **inputs** — each file inspected, with its SHA-256;
- **checks** — every rule evaluated, with `pass`/`fail`/`skip` and a detail line
  (the decisions);
- **verdict** and **violation_count** — the outputs.

## Negative modes

The fixtures under `tests/fixtures/conformance/` deliberately include degraded
artifacts so failure is observable rather than silently accepted:

- `degraded_missing_field.jsonl` — a required field is absent (R1).
- `degraded_bad_format.jsonl` — `format_ok` is `true` while the rule is violated (R3).
- `degraded_bad_type.jsonl` — a field has the wrong type and an unknown field is present (R1).
- `bad_summary.csv` — renamed/reordered columns (R5).

Each is asserted to fail with its expected reason in
`tests/test_contract_conformance.py`. The same test module also covers the
code-drift side: a behavior-preserving refactor of `_format_eval` still passes,
a real rule change fails, an unevaluable `_format_eval` fails **closed** (not
skipped), and a renamed CLI flag or an option⇄argument swap fails.

## Versioning

Semver over the **artifact surface**, independent of the package version:

- **MAJOR** — rename/remove/retype a field, reorder CSV columns, change a format
  rule bound, rename a CLI flag, or any change that breaks an existing consumer.
- **MINOR** — strictly additive (e.g. a new OPTIONAL record field).
- **PATCH** — clarifications with no wire effect.

Bump `CONTRACT_VERSION` in `conformance/contract.py` and this document together,
in the same change that alters the code.

## EvalOps linkage / standalone rationale

This repo currently ships **standalone**: it has no import-time dependency on a
shared EvalOps platform library, and the contract is enforced locally through the
repo's own CI. That is deliberate for now — the artifacts here (JSONL records,
per-scenario summaries, and the evidence document) are exactly the
machine-checkable, evidence-emitting shape a platform-level observability layer
would ingest, so the evidence JSON is designed to be a clean hand-off point if
`evalops` later centralizes conformance. Until then, the primitive lives in-repo.

The design follows a common thread in recent AI-observability and governance work
— that autonomous/agentic systems need machine-checkable contracts and emitted
evidence rather than prose promises, so downstream consumers can verify rather
than trust. We adopt that assumption narrowly: a versioned artifact contract plus
per-run evidence, enforced in CI. No claim is made here about the broader
governance frameworks in that literature.
