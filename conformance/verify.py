"""Verifier for the eval-artifact conformance contract.

Two classes of check:

1. **Code-drift checks** (fail-closed): compare the shapes declared in
   ``orbit_agent/evals.py`` and ``orbit_agent/cli.py`` to the contract. This is
   what detects an *incompatible behavior change* before it ships - e.g. someone
   renames a record field, flips the ``risks == 3`` rule, or drops a CSV column.

   Fail-closed means: a required rule that the verifier *cannot confirm* is a
   violation, not a pass. There is no "skip" escape hatch for the required
   drift checks - inability to verify a contracted guarantee fails the run.
   To keep that honest without breaking on innocent refactors, the format rule
   is verified *behaviorally* (the extracted ``_format_eval`` is executed and
   probed at its boundaries) rather than by pattern-matching its source, so any
   equivalent rewrite that preserves behavior still passes, and only a genuine
   behavior change - or a function that cannot be evaluated at all - fails.

2. **Artifact checks**: validate a concrete ``.jsonl`` / ``.csv`` / ``.md``
   produced by the eval pipeline against the contract, including internal
   consistency (a record that claims ``format_ok`` but violates the format rule
   is a degraded/observable failure, not a silent pass).

Everything is pure standard library so the check runs without ``dspy`` or any
model provider. Run it with::

    python -m conformance.verify                      # code-drift only
    python -m conformance.verify --jsonl results.jsonl --csv s.csv --md s.md
    python -m conformance.verify --evidence-out .orbit/conformance/evidence.json

Exit code is non-zero when any violation is found.
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import hashlib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contract as C

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALS_SRC = REPO_ROOT / "orbit_agent" / "evals.py"
CLI_SRC = REPO_ROOT / "orbit_agent" / "cli.py"

_JSON_TYPE_CHECK = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
}


@dataclass(frozen=True)
class Violation:
    check: str  # stable check id, e.g. "record.schema"
    target: str  # what was inspected, e.g. "orbit_agent/evals.py" or "row 3"
    message: str


@dataclass
class Report:
    contract_id: str
    contract_version: str
    violations: list[Violation] = field(default_factory=list)
    checks_run: list[dict[str, Any]] = field(default_factory=list)
    inputs: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def record_check(
        self, check: str, target: str, status: str, detail: str = ""
    ) -> None:
        self.checks_run.append(
            {"id": check, "target": target, "status": status, "detail": detail}
        )

    def add(self, check: str, target: str, message: str) -> None:
        self.violations.append(Violation(check, target, message))

    def evidence(self) -> dict[str, Any]:
        return {
            "contract": self.contract_id,
            "contract_version": self.contract_version,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "tool": "conformance.verify",
            "inputs": self.inputs,
            "checks": self.checks_run,
            "violations": [
                {"check": v.check, "target": v.target, "message": v.message}
                for v in self.violations
            ],
            "verdict": "pass" if self.ok else "fail",
            "violation_count": len(self.violations),
        }


# --------------------------------------------------------------------------- #
# Static parsing helpers
# --------------------------------------------------------------------------- #
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _parse_src(src: str, filename: str = "<contract-src>") -> ast.Module:
    return ast.parse(src, filename=filename)


def _extract_function_source(src: str, name: str) -> str | None:
    """Return the exact source text of a top-level or nested function ``name``."""
    tree = _parse_src(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(src, node)
            if segment:
                return segment
    return None


def _exec_callable(func_src: str, name: str):
    """Execute an isolated function definition and return the callable.

    A ``from __future__ import annotations`` preamble is prepended so that
    annotations referencing names not in the isolated namespace (e.g. ``List``)
    are treated as strings and never evaluated. The target repo's own source is
    trusted here - this is the same trust boundary as importing it would be, but
    without pulling the module's heavy (dspy) import graph.
    """
    namespace: dict[str, Any] = {}
    exec("from __future__ import annotations\n" + func_src, namespace)  # noqa: S102
    fn = namespace.get(name)
    if not callable(fn):
        raise TypeError(f"{name} did not define a callable")
    return fn


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _find_func(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _dataclass_fields(cls: ast.ClassDef) -> dict[str, tuple[str, bool]]:
    """Return {field_name: (annotation_str, has_default)}."""
    out: dict[str, tuple[str, bool]] = {}
    for n in cls.body:
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out[n.target.id] = (ast.unparse(n.annotation), n.value is not None)
    return out


def _annotation_to_json_type(annotation: str) -> str | None:
    a = annotation.replace(" ", "")
    # strip an Optional wrapper: "X|None" or "Optional[X]"
    a = a.removesuffix("|None")
    if a.startswith("Optional[") and a.endswith("]"):
        a = a[len("Optional[") : -1]
    mapping = {
        "str": "string",
        "float": "number",
        "int": "integer",
        "bool": "boolean",
    }
    if a in mapping:
        return mapping[a]
    if a.startswith(("List[", "list[")):
        return "array"
    return None


# --------------------------------------------------------------------------- #
# Code-drift checks
# --------------------------------------------------------------------------- #
def check_record_schema_drift(report: Report) -> None:
    target = "orbit_agent/evals.py::EvalRecord"
    tree = _parse(EVALS_SRC)
    cls = _find_class(tree, "EvalRecord")
    if cls is None:
        report.add("record.schema", target, "EvalRecord dataclass not found")
        report.record_check("record.schema", target, "fail", "class missing")
        return

    fields = _dataclass_fields(cls)
    required = dict(C.EVAL_RECORD_REQUIRED_FIELDS)
    optional = dict(C.EVAL_RECORD_OPTIONAL_FIELDS)
    contract_names = set(required) | set(optional)
    code_names = set(fields)

    for missing in sorted(contract_names - code_names):
        report.add(
            "record.schema", target, f"contract field '{missing}' missing from code"
        )
    for extra in sorted(code_names - contract_names):
        report.add(
            "record.schema",
            target,
            f"code field '{extra}' is not in the contract (bump contract MINOR to add it)",
        )

    for name, expected_type in required.items():
        if name not in fields:
            continue
        annotation, has_default = fields[name]
        if has_default:
            report.add(
                "record.schema",
                target,
                f"required field '{name}' has a default in code but is required by contract",
            )
        got = _annotation_to_json_type(annotation)
        if got != expected_type:
            report.add(
                "record.schema",
                target,
                f"field '{name}' type is '{annotation}' (json:{got}); contract requires {expected_type}",
            )

    for name, expected_type in optional.items():
        if name not in fields:
            continue
        annotation, has_default = fields[name]
        if not has_default:
            report.add(
                "record.schema",
                target,
                f"optional field '{name}' has no default in code but is optional by contract",
            )
        got = _annotation_to_json_type(annotation)
        if got != expected_type:
            report.add(
                "record.schema",
                target,
                f"optional field '{name}' type is '{annotation}' (json:{got}); contract requires {expected_type}",
            )

    status = (
        "pass"
        if not any(v.check == "record.schema" for v in report.violations)
        else "fail"
    )
    report.record_check(
        "record.schema", target, status, f"{len(fields)} code fields checked"
    )


def _probe_format_bounds(fn) -> dict[str, int | bool]:
    """Derive the format rule *behaviorally* by exercising the real function.

    ``_format_eval(advice, actions_lines, risks_lines)`` cleans and counts its
    line inputs, so passing N plain lines yields count N. We sweep the action
    and risk counts, observe where ``format_ok`` is True, and read the accepted
    band back out. This is robust to any source rewrite that preserves behavior;
    it raises ValueError only when the observed behavior is not a clean rule
    (which is a genuine, fail-closed problem), and propagates any exception from
    calling the extracted function (also fail-closed).
    """

    def lines(n: int) -> list[str]:
        return [f"line{i}" for i in range(n)]

    def is_ok(advice: str, a: int, r: int) -> bool:
        result = fn(advice, lines(a), lines(r))
        # Contract R2: the function returns (format_ok, actions_count, risks_count).
        ok = result[0] if isinstance(result, tuple) else result
        return bool(ok)

    sweep = range(9)
    accepted_actions = sorted(a for a in sweep if is_ok("advice", a, 3))
    accepted_risks = sorted(r for r in sweep if is_ok("advice", 3, r))

    if not accepted_actions:
        raise ValueError("no action count is ever accepted")
    lo, hi = accepted_actions[0], accepted_actions[-1]
    if accepted_actions != list(range(lo, hi + 1)):
        raise ValueError(
            f"accepted action counts are not a contiguous band: {accepted_actions}"
        )
    if len(accepted_risks) != 1:
        raise ValueError(
            f"expected exactly one accepted risk count, got {accepted_risks}"
        )

    advice_required = not is_ok("", lo, accepted_risks[0]) and is_ok(
        "advice", lo, accepted_risks[0]
    )

    return {
        "actions_min": lo,
        "actions_max": hi,
        "risks_exact": accepted_risks[0],
        "advice_nonempty": advice_required,
    }


def check_format_rule_drift(report: Report, src: str | None = None) -> None:
    target = "orbit_agent/evals.py::_format_eval"
    src = src if src is not None else EVALS_SRC.read_text(encoding="utf-8")
    func_src = _extract_function_source(src, "_format_eval")
    if func_src is None:
        report.add("format.rules", target, "_format_eval not found")
        report.record_check("format.rules", target, "fail", "function missing")
        return
    try:
        fn = _exec_callable(func_src, "_format_eval")
        bounds = _probe_format_bounds(fn)
    except Exception as e:  # noqa: BLE001 - fail-closed on any evaluation error
        # Fail-closed: a required rule we cannot evaluate is a violation.
        report.add(
            "format.rules",
            target,
            f"could not verify format rule behaviorally ({type(e).__name__}: {e})",
        )
        report.record_check("format.rules", target, "fail", "unverifiable")
        return
    for key in ("actions_min", "actions_max", "risks_exact", "advice_nonempty"):
        if bounds[key] != C.FORMAT_RULES[key]:
            report.add(
                "format.rules",
                target,
                f"{key} is {bounds[key]} in code; contract requires {C.FORMAT_RULES[key]}",
            )
    status = (
        "pass"
        if not any(v.check == "format.rules" for v in report.violations)
        else "fail"
    )
    report.record_check("format.rules", target, status, f"observed={bounds}")


def _string_constants(func: ast.FunctionDef) -> set[str]:
    return {
        n.value
        for n in ast.walk(func)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def check_summary_schema_drift(report: Report) -> None:
    tree = _parse(EVALS_SRC)

    # CSV columns: the DictWriter fieldnames list inside export_summary_csv.
    target = "orbit_agent/evals.py::export_summary_csv"
    csv_func = _find_func(tree, "export_summary_csv")
    if csv_func is None:
        report.add("summary.csv", target, "export_summary_csv not found")
    else:
        found = None
        for node in ast.walk(csv_func):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "fieldnames"
                and isinstance(node.value, ast.List)
            ):
                found = tuple(
                    e.value for e in node.value.elts if isinstance(e, ast.Constant)
                )
        if found is None:
            # Fail-closed: if the column set is built dynamically we cannot
            # confirm the contract, so we do not let it pass silently.
            report.add(
                "summary.csv",
                target,
                "CSV columns are not a static list; cannot verify against contract",
            )
        elif found != C.SUMMARY_CSV_COLUMNS:
            report.add(
                "summary.csv",
                target,
                f"CSV columns {found} != contract {C.SUMMARY_CSV_COLUMNS}",
            )
        else:
            report.record_check("summary.csv", target, "pass", "columns match")

    # MD header cells: the header pipe-row inside export_summary_md.
    target = "orbit_agent/evals.py::export_summary_md"
    md_func = _find_func(tree, "export_summary_md")
    if md_func is None:
        report.add("summary.md", target, "export_summary_md not found")
    else:
        header = _contract_md_header()
        strings = _string_constants(md_func)
        if any(_normalize_md_header(s) == header for s in strings):
            report.record_check("summary.md", target, "pass", "header matches")
        else:
            report.add(
                "summary.md",
                target,
                f"no header row matching contract cells {C.SUMMARY_MD_HEADER_CELLS} found",
            )

    # Run-summary keys: the dict literal returned by summarize_results.
    target = "orbit_agent/evals.py::summarize_results"
    sr_func = _find_func(tree, "summarize_results")
    if sr_func is None:
        report.add("summary.run_keys", target, "summarize_results not found")
    else:
        key_sets: list[set[str]] = []
        for node in ast.walk(sr_func):
            if isinstance(node, ast.Dict):
                ks = {
                    k.value
                    for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                if ks:
                    key_sets.append(ks)
        full = max(key_sets, key=len) if key_sets else set()
        if full != set(C.RUN_SUMMARY_KEYS):
            report.add(
                "summary.run_keys",
                target,
                f"summary keys {sorted(full)} != contract {sorted(C.RUN_SUMMARY_KEYS)}",
            )
        else:
            report.record_check("summary.run_keys", target, "pass", "keys match")


def _command_registration(fn: ast.FunctionDef) -> tuple[str | None, str | None]:
    """Return (typer_app_name, sub_name) from a ``@x.command(...)`` decorator."""
    for dec in fn.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        attr = call.func if call else dec
        if isinstance(attr, ast.Attribute) and attr.attr == "command":
            app = attr.value.id if isinstance(attr.value, ast.Name) else None
            sub = None
            if call and call.args and isinstance(call.args[0], ast.Constant):
                sub = call.args[0].value
            return app, sub
    return None, None


def _param_declarations(fn: ast.FunctionDef) -> dict[str, dict[str, object]]:
    """Map each parameter to its Typer kind and resolved CLI flag.

    kind is "option", "argument", or None (plain parameter / not Typer). flag is
    the resolved long CLI flag for options: an explicit "--x" override if passed
    to typer.Option, else derived from the parameter name.
    """
    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    n, d = len(positional), len(args.defaults)
    out: dict[str, dict[str, object]] = {}
    for i, a in enumerate(positional):
        default = args.defaults[i - (n - d)] if i >= n - d else None
        kind: str | None = None
        flag: str | None = None
        if isinstance(default, ast.Call) and isinstance(default.func, ast.Attribute):
            typer_fn = default.func.attr
            if typer_fn in ("Option", "Argument"):
                kind = "option" if typer_fn == "Option" else "argument"
                explicit = [
                    c.value
                    for c in default.args
                    if isinstance(c, ast.Constant)
                    and isinstance(c.value, str)
                    and c.value.startswith("--")
                ]
                if kind == "option":
                    flag = explicit[0] if explicit else "--" + a.arg.replace("_", "-")
        out[a.arg] = {"kind": kind, "flag": flag}
    return out


def check_cli_surface_drift(report: Report, src: str | None = None) -> None:
    target = "orbit_agent/cli.py"
    src = src if src is not None else CLI_SRC.read_text(encoding="utf-8")
    tree = _parse_src(src, target)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for entry in C.CLI_SURFACE:
        cmd = entry["command"]
        fname = entry["func"]
        where = f"{target}::{fname}"
        fn = funcs.get(fname)  # type: ignore[arg-type]
        if fn is None:
            report.add("cli.surface", where, f"command '{cmd}' handler missing")
            continue

        # 1. Registered on the expected Typer app under the expected sub-name.
        app, sub = _command_registration(fn)
        if app != entry["app"]:
            report.add(
                "cli.surface",
                where,
                f"command '{cmd}' registered on '{app}', contract expects '{entry['app']}'",
            )
        if sub != entry["sub"]:
            report.add(
                "cli.surface",
                where,
                f"command '{cmd}' sub-name is '{sub}', contract expects '{entry['sub']}'",
            )

        decls = _param_declarations(fn)

        # 2. Each documented option is an Option with the expected CLI flag.
        for opt in entry["options"]:  # type: ignore[union-attr]
            decl = decls.get(opt["param"])
            if decl is None:
                report.add(
                    "cli.surface",
                    where,
                    f"'{cmd}' missing option param '{opt['param']}'",
                )
            elif decl["kind"] != "option":
                report.add(
                    "cli.surface",
                    where,
                    f"'{cmd}' param '{opt['param']}' is {decl['kind']}, contract expects an option",
                )
            elif decl["flag"] != opt["flag"]:
                report.add(
                    "cli.surface",
                    where,
                    f"'{cmd}' option '{opt['param']}' exposes flag {decl['flag']}, contract expects {opt['flag']}",
                )

        # 3. Each documented argument is a positional Argument.
        for arg in entry["arguments"]:  # type: ignore[union-attr]
            decl = decls.get(arg["param"])
            if decl is None:
                report.add(
                    "cli.surface",
                    where,
                    f"'{cmd}' missing argument param '{arg['param']}'",
                )
            elif decl["kind"] != "argument":
                report.add(
                    "cli.surface",
                    where,
                    f"'{cmd}' param '{arg['param']}' is {decl['kind']}, contract expects a positional argument",
                )
    status = (
        "pass"
        if not any(v.check == "cli.surface" for v in report.violations)
        else "fail"
    )
    report.record_check(
        "cli.surface", target, status, f"{len(C.CLI_SURFACE)} commands checked"
    )


# --------------------------------------------------------------------------- #
# Artifact checks
# --------------------------------------------------------------------------- #
def _contract_md_header() -> str:
    return "| " + " | ".join(C.SUMMARY_MD_HEADER_CELLS) + " |"


def _normalize_md_header(line: str) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return "| " + " | ".join(cells) + " |"


def validate_jsonl_artifact(path: Path, report: Report) -> None:
    target = str(path)
    if not path.exists():
        report.add("artifact.jsonl", target, "file not found")
        return
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        report.add("artifact.jsonl", target, "no records to validate")
        return
    for i, line in enumerate(lines, 1):
        loc = f"{path.name}:line {i}"
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            report.add("record.parse", loc, f"invalid JSON: {e}")
            continue
        if not isinstance(rec, dict):
            report.add("record.schema", loc, "record is not a JSON object")
            continue
        _validate_record(rec, loc, report)
    status = (
        "pass"
        if not any(v.target.startswith(path.name) for v in report.violations)
        else "fail"
    )
    report.record_check("artifact.jsonl", target, status, f"{len(lines)} records")


def _validate_record(rec: dict[str, Any], loc: str, report: Report) -> None:
    # Required fields present and correctly typed.
    for name, jtype in C.EVAL_RECORD_REQUIRED_FIELDS.items():
        if name not in rec:
            report.add("record.schema", loc, f"missing required field '{name}'")
            continue
        if not _JSON_TYPE_CHECK[jtype](rec[name]):
            report.add(
                "record.schema",
                loc,
                f"field '{name}' has wrong type (want {jtype}, got {type(rec[name]).__name__})",
            )
    # Optional fields: null allowed, otherwise typed.
    for name, jtype in C.EVAL_RECORD_OPTIONAL_FIELDS.items():
        if (
            name in rec
            and rec[name] is not None
            and not _JSON_TYPE_CHECK[jtype](rec[name])
        ):
            report.add(
                "record.schema",
                loc,
                f"optional field '{name}' has wrong type (want {jtype} or null)",
            )
    # Unknown fields → drift a consumer can't rely on.
    known = set(C.EVAL_RECORD_REQUIRED_FIELDS) | set(C.EVAL_RECORD_OPTIONAL_FIELDS)
    for extra in sorted(set(rec) - known):
        report.add(
            "record.schema", loc, f"unknown field '{extra}' not covered by contract"
        )

    # Internal consistency: counts must match the arrays they summarize.
    if (
        isinstance(rec.get("actions"), list)
        and isinstance(rec.get("actions_count"), int)
        and rec["actions_count"] != len(rec["actions"])
    ):
        report.add(
            "record.consistency",
            loc,
            f"actions_count={rec['actions_count']} but len(actions)={len(rec['actions'])}",
        )
    if (
        isinstance(rec.get("risks"), list)
        and isinstance(rec.get("risks_count"), int)
        and rec["risks_count"] != len(rec["risks"])
    ):
        report.add(
            "record.consistency",
            loc,
            f"risks_count={rec['risks_count']} but len(risks)={len(rec['risks'])}",
        )

    # Degraded-mode observability: format_ok must agree with the format rule.
    if isinstance(rec.get("format_ok"), bool):
        expected = _format_ok_expected(rec)
        if expected is not None and expected != rec["format_ok"]:
            report.add(
                "record.format_ok",
                loc,
                f"format_ok={rec['format_ok']} contradicts the format rule (expected {expected})",
            )


def _format_ok_expected(rec: dict[str, Any]) -> bool | None:
    ac = rec.get("actions_count")
    rc = rec.get("risks_count")
    advice = rec.get("advice")
    if (
        not isinstance(ac, int)
        or not isinstance(rc, int)
        or not isinstance(advice, str)
    ):
        return None
    return (
        C.FORMAT_RULES["actions_min"] <= ac <= C.FORMAT_RULES["actions_max"]
        and rc == C.FORMAT_RULES["risks_exact"]
        and bool(advice.strip())
    )


def validate_csv_artifact(path: Path, report: Report) -> None:
    import csv

    target = str(path)
    if not path.exists():
        report.add("artifact.csv", target, "file not found")
        return
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
    if header is None:
        report.add("artifact.csv", target, "empty CSV (no header)")
        return
    if tuple(header) != C.SUMMARY_CSV_COLUMNS:
        report.add(
            "artifact.csv",
            target,
            f"header {tuple(header)} != contract {C.SUMMARY_CSV_COLUMNS}",
        )
    else:
        report.record_check("artifact.csv", target, "pass", "header matches")


def validate_md_artifact(path: Path, report: Report) -> None:
    target = str(path)
    if not path.exists():
        report.add("artifact.md", target, "file not found")
        return
    header = _contract_md_header()
    matched = any(
        line.strip().startswith("|") and _normalize_md_header(line) == header
        for line in path.read_text(encoding="utf-8").splitlines()
    )
    if matched:
        report.record_check("artifact.md", target, "pass", "header matches")
    else:
        report.add("artifact.md", target, f"no header row matching {header!r} found")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_code_drift_checks(report: Report) -> None:
    report.inputs.extend(_input_evidence([EVALS_SRC, CLI_SRC]))
    check_record_schema_drift(report)
    check_format_rule_drift(report)
    check_summary_schema_drift(report)
    check_cli_surface_drift(report)


def verify(
    jsonl: Path | None = None,
    csv_path: Path | None = None,
    md_path: Path | None = None,
    include_code_drift: bool = True,
) -> Report:
    report = Report(C.CONTRACT_ID, C.CONTRACT_VERSION)
    if include_code_drift:
        run_code_drift_checks(report)
    artifacts = [p for p in (jsonl, csv_path, md_path) if p is not None]
    report.inputs.extend(_input_evidence(artifacts))
    if jsonl is not None:
        validate_jsonl_artifact(jsonl, report)
    if csv_path is not None:
        validate_csv_artifact(csv_path, report)
    if md_path is not None:
        validate_md_artifact(md_path, report)
    return report


def _input_evidence(paths: Iterable[Path]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for p in paths:
        rp = p.resolve()
        entry = {"path": str(rp.relative_to(REPO_ROOT)) if _within_repo(p) else str(p)}
        if rp.exists():
            entry["sha256"] = hashlib.sha256(rp.read_bytes()).hexdigest()
        else:
            entry["sha256"] = "<missing>"
        out.append(entry)
    return out


def _within_repo(p: Path) -> bool:
    try:
        p.resolve().relative_to(REPO_ROOT)
        return True
    except ValueError:
        return False


def _emit_evidence(report: Report, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report.evidence(), indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m conformance.verify",
        description="Verify orbit-agent eval artifacts against the conformance contract.",
    )
    parser.add_argument("--jsonl", type=Path, help="EvalRecord JSONL to validate")
    parser.add_argument("--csv", type=Path, dest="csv", help="CSV summary to validate")
    parser.add_argument(
        "--md", type=Path, dest="md", help="Markdown summary to validate"
    )
    parser.add_argument(
        "--no-code-drift",
        action="store_true",
        help="skip static code-drift checks (artifacts only)",
    )
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=Path(".orbit/conformance/evidence.json"),
        help="where to write the evidence JSON (default: .orbit/conformance/evidence.json)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="only print the verdict line"
    )
    args = parser.parse_args(argv)

    report = verify(
        jsonl=args.jsonl,
        csv_path=args.csv,
        md_path=args.md,
        include_code_drift=not args.no_code_drift,
    )
    _emit_evidence(report, args.evidence_out)

    if not args.quiet:
        for chk in report.checks_run:
            print(
                f"[{chk['status']:>4}] {chk['id']:<18} {chk['target']} {('- ' + chk['detail']) if chk['detail'] else ''}"
            )
        for v in report.violations:
            print(f"[FAIL] {v.check:<18} {v.target}: {v.message}")

    verdict = "PASS" if report.ok else "FAIL"
    print(
        f"{verdict}: {C.CONTRACT_ID} v{C.CONTRACT_VERSION} "
        f"- {len(report.violations)} violation(s); evidence -> {args.evidence_out}"
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
