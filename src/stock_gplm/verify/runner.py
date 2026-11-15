"""Run the whole verification suite and write an auditable PASS/FAIL report.

``gplm verify [--out DIR]`` or ``python -m stock_gplm.verify [--out DIR]``. Exit code 0 when no check
FAILs (warnings allowed), 1 otherwise. Every input is synthetic and seeded; no real candle is read.

Outputs (in ``DIR``)::

    VERIFY_REPORT.md      human report: verdict per question, failures, strategy & indicator evidence
    verify-report.json    every check with its claim, verdict, details and evidence
    strategies.csv        one row per catalogue interpretation: original rule, DSL, audit result
    indicators.csv        one row per indicator: error vs reference, warm-up, textbook deviation
    primitives.csv        the owner's minimum calculation list: status per row and timeframe
    engine-trades.csv     trade ledger of the hand-built fill scenarios
    ml-evolve-log.jsonl   every genome the genetic programme evaluated (entry, exit, fitness, stats)
    audit-manifest.json   git commit, versions, seeds and SHA-256 of every input and suite file
    work/                 the synthetic end-to-end research-matrix run (matrix.sqlite, library)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import VERIFY_CONTRACT
from . import fixtures as fx
from . import pipeline as pl
from . import (suite_catalogue, suite_engine, suite_indicators, suite_ml, suite_primitives, suite_selftest,
               suite_validation)
from .core import FAIL, PASS, WARN, Section, clean, worst

REPO = Path(__file__).resolve().parents[3]
TRACKED_GAPS = "PASS_WITH_TRACKED_GAPS"


def exit_code(verdict: str) -> int:
    return 1 if verdict == FAIL else (2 if verdict == TRACKED_GAPS else 0)


QUESTIONS = [
    ("0. Verifier self-test", "Does the verifier itself catch planted defects?"),
    ("1. Strategy interpretation", "Did GPLM understand what each catalogue strategy says?"),
    ("2. Indicators", "Are RSI, MACD, EMA, SMA, ATR and the other indicators calculated correctly?"),
    ("3. Signals, fills, costs, leakage", "Are signals, next-bar execution, exits and costs right, with no future data?"),
    ("4. Validation & statistics", "Are the walk-forward split, statistics, gates and holdout sound?"),
    ("5. ML discovery", "Does ML discover new strategies, backtest them and save reusable configurations?"),
    ("6. Minimum calculation coverage", "Is every calculation in the owner's minimum list available on every timeframe?"),
]


def run(out: Path, quiet: bool = False) -> dict[str, Any]:
    out = Path(out).resolve()
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    ev: dict[str, Any] = {"work": str(work)}
    secs = {name: Section(name) for name, _ in QUESTIONS}

    def say(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    S = {k[:1]: secs[k] for k, _ in QUESTIONS}
    steps = [("synthetic end-to-end research-matrix run", None),
             ("verifier self-test (planted defects)", lambda: suite_selftest.run(S["0"], ev)),
             ("indicators vs independent references", lambda: suite_indicators.run(S["2"], ev)),
             ("catalogue interpretations vs original pseudo-code", lambda: suite_catalogue.run(S["1"], ev, work)),
             ("signals, fills, costs, leakage", lambda: suite_engine.run(S["3"], ev)),
             ("validation and statistics", lambda: suite_validation.run(S["4"], ev)),
             ("ML discovery", lambda: suite_ml.run(S["5"], ev, work)),
             ("minimum calculation coverage on every timeframe", lambda: suite_primitives.run(S["6"], ev))]
    for i, (label, fn) in enumerate(steps, 1):
        say(f"[{i}/{len(steps)}] {label} ...")
        if fn is not None:
            fn()
            continue
        try:
            ev["pipeline"] = pl.run(work / "pipeline")
        except Exception as exc:  # the sections that need it report a FAIL
            ev["pipeline_error"] = f"{type(exc).__name__}: {exc}"
    correctness = worst(*(s.verdict for k, s in secs.items() if not k.startswith("6.")))
    coverage = secs[QUESTIONS[-1][0]].verdict
    # Correctness failures fail the run. Missing calculations of the owner's list are tracked gaps
    # (config/verify/primitives.json): reported explicitly and with their own exit code, never hidden.
    verdict = FAIL if correctness == FAIL else (TRACKED_GAPS if coverage == FAIL else worst(correctness, coverage))
    report = {"contract": VERIFY_CONTRACT, "verdict": verdict, "correctness_verdict": correctness,
              "coverage_verdict": coverage, "exit_code": exit_code(verdict), "generated_utc": _now(),
              "seconds": round(time.perf_counter() - t0, 2), "out": str(out),
              "sections": [{"section": s.name, "verdict": s.verdict, "checks": [c.to_dict() for c in s.checks]}
                           for s in secs.values()],
              "manifest": manifest()}
    _write(out, report, ev, secs)
    say(f"\nGPLM verification: {verdict}  ({report['seconds']} s)  report: {out / 'VERIFY_REPORT.md'}")
    for name, q in QUESTIONS:
        s = secs[name]
        n = {v: sum(1 for c in s.checks if c.verdict == v) for v in (PASS, WARN, FAIL)}
        say(f"  {s.verdict:<4}  {name:<34} pass {n[PASS]:>3}  warn {n[WARN]:>3}  fail {n[FAIL]:>3}")
    return report


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest() -> dict[str, Any]:
    import numpy
    import pandas
    import scipy

    def git(*a: str) -> str:
        try:
            return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return ""
    inputs = [REPO / "config" / "strategies" / "reference-300-strategies.txt", REPO / "config" / "app" / "gplm.json",
              REPO / "config" / "verify" / "primitives.json"]
    inputs += sorted((REPO / "config" / "strategies" / "library").rglob("*.json"))
    code = sorted((REPO / "src" / "stock_gplm" / "verify").glob("*.py"))
    code += [REPO / "src" / "stock_gplm" / "app" / f for f in ("strategy_dsl.py", "strategy_engine.py",
                                                              "strategy_evolve.py", "strategy_library.py", "research.py",
                                                              "m56_status.py", "m56_catalogue.py")]
    code += [REPO / "src" / "stock_gplm" / "m54" / f for f in ("backtest.py", "indicators.py")]
    return {"git_commit": git("rev-parse", "HEAD"), "git_dirty_files": len(git("status", "--porcelain").splitlines()),
            "python": sys.version.split()[0], "numpy": numpy.__version__, "pandas": pandas.__version__,
            "scipy": scipy.__version__, "platform": platform.platform(), "fixture_seed": fx.SEED,
            "real_candles_read": False,
            "inputs": {str(p.relative_to(REPO)): _sha(p) for p in inputs if p.is_file()},
            "code_under_test_and_suite": {str(p.relative_to(REPO)): _sha(p) for p in code if p.is_file()}}


def _csv(path: Path, rows: Sequence[dict[str, Any]], cols: Sequence[str] | None = None) -> None:
    cols = list(cols or (list(rows[0]) if rows else []))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(clean(v)) if isinstance(v, (dict, list)) else clean(v)) for k, v in r.items()})


def _write(out: Path, report: dict[str, Any], ev: dict[str, Any], secs: dict[str, Section]) -> None:
    (out / "verify-report.json").write_text(json.dumps(clean(report), indent=2) + "\n", encoding="utf-8")
    (out / "audit-manifest.json").write_text(json.dumps(clean(report["manifest"]), indent=2) + "\n", encoding="utf-8")
    srows = ev.get("strategy_rows", [])
    _csv(out / "strategies.csv", srows, ["reference_id", "strategy_id", "interpretation", "name", "verdict", "reasons",
                                         "original_rule", "dsl_entry", "dsl_exit", "fidelity", "assumptions",
                                         "semantic", "compared_bars", "spec_signals", "dsl_signals", "mismatched_bars",
                                         "signal_jaccard", "mismatch_examples", "spec_families", "missing_families",
                                         "spec_numbers", "missing_numbers", "direction_lib", "direction_note",
                                         "timeframes_spec", "timeframes_lib", "block_status", "block_reason",
                                         "semantic_reason", "definition_sha256"])
    irows = ev.get("indicator_rows", [])
    _csv(out / "indicators.csv", irows, ["indicator", "expression", "bars", "max_rel_err", "nan_pattern_mismatches",
                                         "nan_mismatch_bars", "infinite_values", "finite_bars", "textbook_max_rel_dev",
                                         "textbook_converged_1e3", "textbook_converged_1e6", "sample"])
    prow = ev.get("primitive_rows", [])
    _csv(out / "primitives.csv", prow, ["module", "name", "status", "verdict", "why", "exprs", "timeframes",
                                        "verified_by", "missing", "evaluations"])
    _csv(out / "engine-trades.csv", ev.get("engine_trades", []))
    with (out / "ml-evolve-log.jsonl").open("w", encoding="utf-8") as fh:
        for r in ev.get("evolve_log", []):
            fh.write(json.dumps(clean(r)) + "\n")
    (out / "VERIFY_REPORT.md").write_text(markdown(report, ev, secs), encoding="utf-8")


def _md(v: Any) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ")


def markdown(report: dict[str, Any], ev: dict[str, Any], secs: dict[str, Section]) -> str:
    m = report["manifest"]
    L = [f"# GPLM correctness verification: **{report['verdict']}**", "",
         f"Correctness (sections 0-5): **{report['correctness_verdict']}**. Minimum calculation coverage (section 6): "
         f"**{report['coverage_verdict']}**{' (tracked gaps listed below)' if report['coverage_verdict'] == FAIL else ''}."
         f" Exit code {report['exit_code']} (0 pass, 1 correctness failure, 2 correct but tracked calculation gaps).", "",
         f"Generated {report['generated_utc']} in {report['seconds']} s. Git commit `{m['git_commit'][:12]}` "
         f"({m['git_dirty_files']} uncommitted file(s)). Python {m['python']}, NumPy {m['numpy']}, pandas "
         f"{m['pandas']}.", "",
         "All evidence comes from deterministic, seeded synthetic fixtures and small targeted runs. No real candle "
         "was read, and no research-scale workload was run. PASS = claim verified. WARN = verified with a "
         "documented deviation or a limitation to review. FAIL = the claim is false or not met.", "",
         "## Answers", "", "| Question | Verdict | Checks (pass / warn / fail) |", "|---|---|---|"]
    for name, q in QUESTIONS:
        s = secs[name]
        n = [sum(1 for c in s.checks if c.verdict == v) for v in (PASS, WARN, FAIL)]
        L.append(f"| **{name}**: {q} | **{s.verdict}** | {n[0]} / {n[1]} / {n[2]} |")
    fails = [(s.name, c) for s in secs.values() for c in s.checks if c.verdict == FAIL]
    L += ["", "## Failures", ""]
    if not fails:
        L.append("None.")
    for sname, c in fails:
        L.append(f"- **{sname} / {c.name}**: {c.claim}")
        for d in c.details:
            if d.startswith("FAIL"):
                L.append(f"  - {_md(d)[:600]}")
        extra = sum(1 for d in c.details if d.startswith("FAIL")) - 25
        if extra > 0:
            L.append(f"  - ... and {extra} more (see verify-report.json)")
    L += ["", "## Checks by section", ""]
    for s in secs.values():
        L += [f"### {s.name}: {s.verdict}", "", "| Verdict | Check | Claim | Notes |", "|---|---|---|---|"]
        for c in s.checks:
            notes = [d for d in c.details if not d.startswith("FAIL")]
            note = "<br>".join(_md(d)[:400] for d in notes[:4]) + (f"<br>(+{len(notes) - 4} more)" if len(notes) > 4 else "")
            L.append(f"| {c.verdict} | `{c.name}` | {_md(c.claim)} | {note} |")
        L.append("")
    L += _strategy_md(ev) + _indicator_md(ev) + _primitive_md(ev) + _ml_md(ev)
    L += ["## Files", "",
          "| File | Contents |", "|---|---|",
          "| `strategies.csv` | Every catalogue interpretation: original rule, DSL rule, verdict, reasons, bar-level agreement |",
          "| `indicators.csv` | Every indicator: max error vs reference, warm-up, textbook deviation, sample values |",
          "| `primitives.csv` | The owner's minimum calculation list: status, timeframes, per-expression results |",
          "| `engine-trades.csv` | Trade ledger of the fill scenarios (entry/exit bar, price, reason, net) |",
          "| `ml-evolve-log.jsonl` | Every genome the genetic programme evaluated, in order |",
          "| `verify-report.json` | All checks with evidence |",
          "| `audit-manifest.json` | Git commit, library versions and SHA-256 of every input and code file |",
          "| `work/pipeline/research-out/` | The end-to-end synthetic research-matrix run (matrix.sqlite, summary.md) |", "",
          "Reproduce: `.\\windows\\verify.cmd` (or `gplm verify --out <dir>`). Results are deterministic for a given "
          "commit."]
    return "\n".join(L) + "\n"


def _strategy_md(ev: dict[str, Any]) -> list[str]:
    rows = ev.get("strategy_rows", [])
    if not rows:
        return []
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    L = ["## Strategy-level evidence", "",
         "Each interpretation was checked against its original pseudo-code in two ways. **Semantic:** an independent "
         "evaluator (its own parser and pure-Python indicators) ran the original rule on three 1,500-bar fixtures and "
         "compared it bar by bar with the DSL entry. **Structural:** indicator families, constants and thresholds "
         "were matched. Verdicts:", "",
         "| Verdict | Meaning | Count |", "|---|---|---|"]
    meaning = {"VERIFIED_SEMANTIC": "original rule reproduced bar for bar",
               "VERIFIED_STRUCTURAL": "same indicators and constants; original not machine-readable",
               "APPROXIMATION": "declared interpretation; listed with its assumptions (and measured agreement)",
               "REVIEW": "declared exact, but a constant or indicator in the original was not found",
               "MISMATCH": "declared exact, but does not reproduce the literal original rule",
               "BLOCKED": "needs data or a primitive that is not available (reason recorded)",
               "DEFERRED": "owner-deferred scope (index-relative)"}
    for k in ("VERIFIED_SEMANTIC", "VERIFIED_STRUCTURAL", "APPROXIMATION", "REVIEW", "MISMATCH", "BLOCKED", "DEFERRED"):
        L.append(f"| {k} | {meaning[k]} | {counts.get(k, 0)} |")
    L.append("")
    for k in ("MISMATCH", "REVIEW"):
        sel = [r for r in rows if r["verdict"] == k]
        if sel:
            L += [f"### {k}", "", "| Id | Name | Original | Interpretation | Why |", "|---|---|---|---|---|"]
            for r in sel:
                L.append(f"| {r['strategy_id']} | {_md(r['name'])} | `{_md(r['original_rule'])[:160]}` | "
                         f"`{_md(r['dsl_entry'])[:160]}` | {_md(r['reasons'])[:300]} |")
            L.append("")
    appr = [r for r in rows if r["verdict"] == "APPROXIMATION"]
    if appr:
        L += [f"### APPROXIMATION ({len(appr)})", "", "| Id | Name | Assumptions | Measured agreement |", "|---|---|---|---|"]
        for r in appr:
            agree = f"Jaccard {r['signal_jaccard']:.3f}" if isinstance(r.get("signal_jaccard"), float) and \
                r.get("semantic") == "DIFFERENT" else ""
            L.append(f"| {r['strategy_id']} | {_md(r['name'])} | {_md(r['assumptions'])[:300]} | {agree} |")
        L.append("")
    return L


def _indicator_md(ev: dict[str, Any]) -> list[str]:
    rows = ev.get("indicator_rows", [])
    if not rows:
        return []
    L = ["## Indicator-level evidence", "",
         "GPLM vs an independent pure-Python reference with the same seeding (arithmetic check), and vs the "
         "TA-Lib/Wilder textbook seeding (convention check). 1,500-bar fixture.", "",
         "| Indicator | Max rel. error | Warm-up bars differing | Textbook max deviation | Within 0.1% of textbook from bar |",
         "|---|---|---|---|---|"]
    for r in rows:
        tb = r.get("textbook_max_rel_dev")
        L.append(f"| {r['indicator']} | {r['max_rel_err']:.1e} | {r['nan_pattern_mismatches']} | "
                 f"{'' if tb is None else f'{100 * tb:.2f}%'} | {r.get('textbook_converged_1e3') or ''} |")
    return L + [""]


def _primitive_md(ev: dict[str, Any]) -> list[str]:
    rows = ev.get("primitive_rows", [])
    if not rows:
        return []
    L = ["## Minimum calculation coverage (owner's list)", "",
         "Each listed calculation evaluated on 1d, 1w, 5m and 15m synthetic lanes (intraday-only on 5m/15m): "
         "it must return finite values and be unchanged when future bars change.", "",
         "| Module | Calculation | Status | Verdict | Notes |", "|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['module']} | {_md(r['name'])} | {r['status']} | {r['verdict']} | {_md(r['why'])[:260]} |")
    return L + [""]


def _ml_md(ev: dict[str, Any]) -> list[str]:
    log = ev.get("evolve_log", [])
    if not log:
        return []
    L = ["## ML discovery audit", "",
         f"The genetic programme evaluated {len(log)} distinct genomes on the discovery segment of a fixture with a "
         "planted edge; every one is in `ml-evolve-log.jsonl`. Top 10 by fitness:", "",
         "| Fitness | Direction | Entry | Exit | Trades | Mean net | Excess mean |", "|---|---|---|---|---|---|---|"]
    top = sorted([r for r in log if r.get("fitness") is not None], key=lambda r: -r["fitness"])[:10]
    for r in top:
        L.append(f"| {r['fitness']:.2f} | {r['direction']} | `{_md(r['entry'])[:120]}` | `{_md(json.dumps(r['exit']))[:90]}` | "
                 f"{r.get('trades')} | {r.get('mean_net', 0):.4f} | {r.get('excess_mean', 0):.4f} |")
    return L + [""]


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gplm verify", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", help="report directory (default reports/verify/<UTC timestamp> under the state home)")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)
    out = Path(a.out) if a.out else REPO.parent / "state" / "reports" / "verify" / \
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rep = run(out, a.quiet)
    return int(rep["exit_code"])


__all__ = ["main", "run"]
