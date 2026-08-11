"""Conformance tests for the eval-artifact contract.

These tests import only the stdlib-only :mod:`conformance` package (never
:mod:`orbit_agent`), so they run in the standard ``pytest -q`` CI lane without
``dspy`` or any model provider.

They cover:
  * code-drift: the live code in ``orbit_agent/`` still matches the contract;
  * a golden (valid) artifact passes;
  * each degraded artifact is *observably* rejected with the expected reason;
  * the verifier emits evidence with the required top-level shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conformance import contract as C
from conformance import verify as V

FIXTURES = Path(__file__).parent / "fixtures" / "conformance"


def _violation_checks(report: V.Report) -> set[str]:
    return {v.check for v in report.violations}


def test_code_matches_contract_on_main():
    """The shipped code must not have drifted from the contract."""
    report = V.Report(C.CONTRACT_ID, C.CONTRACT_VERSION)
    V.run_code_drift_checks(report)
    assert report.ok, "code drifted from contract: " + "; ".join(
        f"{v.check}:{v.message}" for v in report.violations
    )


def test_valid_artifacts_pass():
    report = V.verify(
        jsonl=FIXTURES / "valid_records.jsonl",
        csv_path=FIXTURES / "valid_summary.csv",
        md_path=FIXTURES / "valid_summary.md",
        include_code_drift=False,
    )
    assert report.ok, [f"{v.check}:{v.message}" for v in report.violations]


def test_missing_required_field_is_rejected():
    report = V.verify(
        jsonl=FIXTURES / "degraded_missing_field.jsonl", include_code_drift=False
    )
    assert not report.ok
    assert "record.schema" in _violation_checks(report)
    assert any("critic_score" in v.message for v in report.violations)


def test_format_ok_lie_is_observable():
    """A record that claims format_ok but violates the rule must fail loudly."""
    report = V.verify(
        jsonl=FIXTURES / "degraded_bad_format.jsonl", include_code_drift=False
    )
    assert not report.ok
    assert "record.format_ok" in _violation_checks(report)


def test_wrong_type_and_unknown_field_rejected():
    report = V.verify(
        jsonl=FIXTURES / "degraded_bad_type.jsonl", include_code_drift=False
    )
    assert not report.ok
    msgs = " ".join(v.message for v in report.violations)
    assert "wrong type" in msgs
    assert "unknown field" in msgs


def test_bad_csv_header_rejected():
    report = V.verify(csv_path=FIXTURES / "bad_summary.csv", include_code_drift=False)
    assert not report.ok
    assert "artifact.csv" in _violation_checks(report)


def test_evidence_has_required_shape(tmp_path: Path):
    out = tmp_path / "evidence.json"
    rc = V.main(
        [
            "--jsonl",
            str(FIXTURES / "valid_records.jsonl"),
            "--evidence-out",
            str(out),
            "--quiet",
        ]
    )
    assert rc == 0
    evidence = json.loads(out.read_text())
    for key in (
        "contract",
        "contract_version",
        "inputs",
        "checks",
        "verdict",
        "violation_count",
    ):
        assert key in evidence, f"evidence missing '{key}'"
    assert evidence["contract_version"] == C.CONTRACT_VERSION
    assert evidence["verdict"] == "pass"
    # Inputs cite content hashes so a downstream operator can pin exactly what ran.
    assert all("sha256" in i for i in evidence["inputs"])
    # Checks record a decision per rule.
    assert all({"id", "status"} <= set(c) for c in evidence["checks"])


def test_main_returns_nonzero_on_violation(tmp_path: Path):
    out = tmp_path / "evidence.json"
    rc = V.main(
        [
            "--no-code-drift",
            "--jsonl",
            str(FIXTURES / "degraded_missing_field.jsonl"),
            "--evidence-out",
            str(out),
            "--quiet",
        ]
    )
    assert rc == 1
    assert json.loads(out.read_text())["verdict"] == "fail"


# --------------------------------------------------------------------------- #
# Fail-closed format-rule verification (behavioral)
# --------------------------------------------------------------------------- #
_FORMAT_EQUIVALENT = """
def _format_eval(advice, actions_lines, risks_lines):
    a_clean = [ln.strip() for ln in actions_lines if ln.strip()]
    r_clean = [ln.strip() for ln in risks_lines if ln.strip()]
    actions_count = len(a_clean)
    risks_count = len(r_clean)
    lo, hi, exact = 3, 5, 3
    format_ok = (lo <= actions_count <= hi) and (risks_count == exact) and bool(advice and advice.strip())
    return format_ok, actions_count, risks_count
"""

_FORMAT_BEHAVIOR_CHANGED = _FORMAT_EQUIVALENT.replace(
    "lo, hi, exact = 3, 5, 3", "lo, hi, exact = 3, 6, 3"
)

_FORMAT_UNEVALUABLE = """
def _format_eval(advice, actions_lines, risks_lines):
    x = SOME_UNDEFINED_NAME  # noqa: F821
    return True, 3, 3
"""


def _format_check(src: str) -> V.Report:
    report = V.Report(C.CONTRACT_ID, C.CONTRACT_VERSION)
    V.check_format_rule_drift(report, src=src)
    return report


def test_format_rule_passes_on_behavior_preserving_refactor():
    # Named constants instead of literals: robust behavioral check still passes.
    assert _format_check(_FORMAT_EQUIVALENT).ok


def test_format_rule_detects_behavior_change():
    report = _format_check(_FORMAT_BEHAVIOR_CHANGED)
    assert not report.ok
    assert "format.rules" in _violation_checks(report)


def test_format_rule_fails_closed_when_unverifiable():
    # The reviewer's core concern: inability to verify must FAIL, not skip/pass.
    report = _format_check(_FORMAT_UNEVALUABLE)
    assert not report.ok
    assert "format.rules" in _violation_checks(report)
    assert not any(c["status"] == "skip" for c in report.checks_run)


# --------------------------------------------------------------------------- #
# Typer-aware CLI surface verification
# --------------------------------------------------------------------------- #
_CLI_OK = """
import typer

app = typer.Typer()
eval_app = typer.Typer()
models_app = typer.Typer()


@models_app.command("list")
def models_list(provider: str = typer.Option("openai")):
    ...


@eval_app.command("run")
def eval_run(dataset: str = typer.Option("d"), out: str = typer.Option("o")):
    ...


@eval_app.command("report")
def eval_report(results_path: str = typer.Argument(...)):
    ...


@eval_app.command("grade")
def eval_grade(dataset: str = typer.Option("d"), results_path: str = typer.Option("r"), out: str = typer.Option("o")):
    ...


@eval_app.command("summary")
def eval_summary(input_path: str = typer.Option("i"), csv_out: str = typer.Option("c"), md_out: str = typer.Option("m")):
    ...
"""


def _cli_check(src: str) -> V.Report:
    report = V.Report(C.CONTRACT_ID, C.CONTRACT_VERSION)
    V.check_cli_surface_drift(report, src=src)
    return report


def test_cli_surface_passes_on_faithful_declaration():
    assert _cli_check(_CLI_OK).ok


def test_cli_flag_rename_detected():
    renamed = _CLI_OK.replace(
        'def eval_run(dataset: str = typer.Option("d")',
        'def eval_run(dataset: str = typer.Option("d", "--data")',
    )
    report = _cli_check(renamed)
    assert not report.ok
    assert any("--data" in v.message for v in report.violations)


def test_cli_option_argument_swap_detected():
    swapped = _CLI_OK.replace(
        "def eval_report(results_path: str = typer.Argument(...))",
        'def eval_report(results_path: str = typer.Option("r"))',
    )
    report = _cli_check(swapped)
    assert not report.ok
    assert any("positional argument" in v.message for v in report.violations)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
