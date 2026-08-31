"""Normative constants for the orbit-agent eval-artifact conformance contract.

This module is the machine-readable single source of truth. The prose contract
in ``contracts/eval-artifacts-v1.md`` explains *why*; this file pins the *what*
so it can be enforced in code. Every constant here corresponds to a documented
or implied promise about an artifact that a downstream operator or agent may
consume.

Versioning policy (semver over the artifact surface, not the code):
  * MAJOR - a rename, removal, type change, reordering of CSV columns, change to
    a format rule bound, or any other change that breaks a consumer parsing an
    existing artifact.
  * MINOR - a strictly additive change (e.g. a new OPTIONAL record field).
  * PATCH - clarifications with no wire effect.
"""

from __future__ import annotations

CONTRACT_ID = "eval-artifacts"
CONTRACT_VERSION = "1.0.0"

# --- EvalRecord: one JSONL line per scenario evaluation -----------------------
# Source of truth in code: orbit_agent/evals.py::EvalRecord and run_evals().
# Required fields MUST be present on every record with the given JSON type.
# Types use JSON vocabulary: "string", "number", "integer", "boolean", "array".
EVAL_RECORD_REQUIRED_FIELDS: dict[str, str] = {
    "scenario_id": "string",
    "prompt": "string",
    "timestamp": "number",
    "latency_ms": "number",
    "advice": "string",
    "actions": "array",
    "metric_to_watch": "string",
    "risks": "array",
    "critic_score": "integer",
    "critic_feedback": "string",
    "format_ok": "boolean",
    "actions_count": "integer",
    "risks_count": "integer",
}

# Optional fields MAY be absent or null; when present they MUST match the type.
EVAL_RECORD_OPTIONAL_FIELDS: dict[str, str] = {
    "overlap_ratio": "number",
}

# --- Format rules: the implied "good response" shape --------------------------
# Source of truth in code: orbit_agent/evals.py::_format_eval().
# These bounds are normative: report/grade/summary and downstream consumers rely
# on format_ok reflecting exactly this rule. If the code bound changes, the
# contract MUST be bumped (MAJOR) and this constant updated in the same change.
FORMAT_RULES: dict[str, int | bool] = {
    "actions_min": 3,
    "actions_max": 5,
    "risks_exact": 3,
    "advice_nonempty": True,
}

# --- summarize_results(): keys of the run/report summary dict ------------------
# Source of truth in code: orbit_agent/evals.py::summarize_results().
# The non-empty branch MUST emit exactly these keys (order irrelevant).
RUN_SUMMARY_KEYS: frozenset[str] = frozenset(
    {
        "count",
        "format_ok_rate",
        "avg_critic_score",
        "avg_latency_ms",
        "avg_playbook_overlap",
    }
)

# --- CSV summary schema -------------------------------------------------------
# Source of truth in code: orbit_agent/evals.py::export_summary_csv().
# Column ORDER is normative (CSV consumers are positional-friendly).
SUMMARY_CSV_COLUMNS: tuple[str, ...] = (
    "scenario_id",
    "count",
    "avg_critic_score",
    "avg_overlap",
    "avg_latency_ms",
)

# --- Markdown summary schema --------------------------------------------------
# Source of truth in code: orbit_agent/evals.py::export_summary_md().
# The header cells (between the outer pipes) are normative and ordered.
SUMMARY_MD_HEADER_CELLS: tuple[str, ...] = (
    "Scenario",
    "Count",
    "Avg Score",
    "Overlap",
    "Latency (ms)",
)

# --- CLI surface --------------------------------------------------------------
# Documented in README.md (Commands + Evals sections). Each entry pins the
# *Typer command surface*, not merely the Python signature: the Typer app the
# command is registered on (``app``), its sub-name, and for every documented
# parameter its kind (option vs positional argument) and its resolved CLI flag.
# The verifier confirms the Typer declaration, so an explicitly renamed flag or
# an option/argument swap is caught even when the Python parameter name is
# unchanged. Renaming any of these silently breaks documented usage and any
# operator script or agent that shells out to these commands.
#
# "flag" is the long CLI flag Typer exposes: an explicit override passed to
# typer.Option(...) when present, otherwise "--" + param with underscores as
# hyphens (e.g. results_path -> --results-path). Arguments are positional and
# have no flag.
CLI_SURFACE: tuple[dict[str, object], ...] = (
    {
        "command": "models list",
        "func": "models_list",
        "app": "models_app",
        "sub": "list",
        "options": ({"param": "provider", "flag": "--provider"},),
        "arguments": (),
    },
    {
        "command": "eval run",
        "func": "eval_run",
        "app": "eval_app",
        "sub": "run",
        "options": (
            {"param": "dataset", "flag": "--dataset"},
            {"param": "out", "flag": "--out"},
        ),
        "arguments": (),
    },
    {
        "command": "eval report",
        "func": "eval_report",
        "app": "eval_app",
        "sub": "report",
        "options": (),
        "arguments": ({"param": "results_path"},),
    },
    {
        "command": "eval grade",
        "func": "eval_grade",
        "app": "eval_app",
        "sub": "grade",
        "options": (
            {"param": "dataset", "flag": "--dataset"},
            {"param": "results_path", "flag": "--results-path"},
            {"param": "out", "flag": "--out"},
        ),
        "arguments": (),
    },
    {
        "command": "eval summary",
        "func": "eval_summary",
        "app": "eval_app",
        "sub": "summary",
        "options": (
            {"param": "input_path", "flag": "--input-path"},
            {"param": "csv_out", "flag": "--csv-out"},
            {"param": "md_out", "flag": "--md-out"},
        ),
        "arguments": (),
    },
)
