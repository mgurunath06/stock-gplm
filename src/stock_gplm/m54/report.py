"""Per-stock intelligence outputs: report.md, findings.json, results.csv, events.csv, run-manifest.json."""
from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from . import CLASSIFICATION
from .hypotheses import Hypothesis
from .indicators import UNSUPPORTED
from .store import dumps, read_json, sha256_file, utc_now, write_json_atomic, write_text_atomic

if TYPE_CHECKING:
    from .engine import Run

OUTPUTS = ("report.md", "findings.json", "results.csv", "events.csv")
STRATEGY_CONTRACT = "stock-gplm.m54-pattern-strategy.v1"

EVIDENCE_TIERS = {
    "discovery": "EXPLORATORY_DISCOVERY (in-sample; thresholds and selection were fitted here)",
    "validation": "VALIDATION_EVIDENCE (out-of-sample once; selection frozen beforehand)",
    "test": "INNER_TEST_EVIDENCE (evaluated once after validation; NOT the M3.2 locked holdout)",
    "holdout": "FINAL_HOLDOUT: not accessed (M3.2 locked holdout is never opened by this engine)",
}


def _pct(v: Any, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v * 100:.{digits}f}%"


def _num(v: Any, digits: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{digits}g}"


def classify(screen: Mapping[str, Any], refine: Mapping[str, Any], interact: Mapping[str, Any],
             validate: Mapping[str, Any], test: Mapping[str, Any], budget: Mapping[str, Any]) -> dict[tuple[str, int], str]:
    status: dict[tuple[str, int], str] = {}
    for r in screen["rows"] + interact["rows"]:
        status[(r["hypothesis"]["hypothesis_id"], r["horizon"])] = "REJECTED_SCREEN"
    for i, s in enumerate(screen["survivors"]):
        status[(s["hypothesis_id"], s["horizon"])] = "SCREEN_PASS_NOT_REFINED_BUDGET" if i >= budget["refine_top_k"] else "SCREEN_PASS"
    for s in interact["survivors"]:
        status[(s["hypothesis_id"], s["horizon"])] = "SCREEN_PASS"
    for r in refine["results"]:
        if r["stability"] == "UNSTABLE":
            status[(r["hypothesis_id"], r["horizon"])] = "REJECTED_UNSTABLE_NEIGHBOURS"
    finalists = {tuple(f) for f in validate["finalists"]}
    for k, v in status.items():
        if v == "SCREEN_PASS" and k not in finalists:
            status[k] = "STABLE_NOT_VALIDATED_BUDGET"
    for r in validate["rows"]:
        k = (r["hypothesis"]["hypothesis_id"], r["horizon"])
        status[k] = "VALIDATION_PASS" if r["passed"] else "REJECTED_VALIDATION"
    for r in test["rows"]:
        status[(r["hypothesis"]["hypothesis_id"], r["horizon"])] = r["verdict"]
    return status


def _csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})
    return buf.getvalue()


RESULT_COLUMNS = ("stage", "segment", "hypothesis_id", "origin", "hypothesis", "horizon", "raw_events",
                  "independent_events", "mean_net", "median_net", "hit_rate", "baseline_mean_net", "excess_mean",
                  "t_stat", "p_value", "mean_mfe", "mean_mae", "random_entry_percentile", "status")
EVENT_COLUMNS = ("segment", "hypothesis_id", "horizon", "signal_date", "entry_date", "exit_date", "entry_price",
                 "exit_price", "gross_return", "net_return", "mfe", "mae", "max_drawdown", "time_to_target",
                 "time_to_failure")


def _flat(stage: str, seg: str, r: Mapping[str, Any], status: Mapping[tuple[str, int], str]) -> dict[str, Any]:
    h = r["hypothesis"]
    return {"stage": stage, "segment": seg, "hypothesis_id": h["hypothesis_id"], "origin": h["origin"],
            "hypothesis": h["text"],
            **{k: r.get(k) for k in RESULT_COLUMNS if k in r and k not in {"hypothesis", "stage", "segment", "status"}},
            "status": status.get((h["hypothesis_id"], r["horizon"]), "")}


def write_outputs(run: "Run", data: Mapping[str, Any], screen: Mapping[str, Any], refine: Mapping[str, Any],
                  interact: Mapping[str, Any], validate: Mapping[str, Any], test: Mapping[str, Any]) -> dict[str, Any]:
    s = run.s
    status = classify(screen, refine, interact, validate, test, s["budget"])
    counts = Counter(status.values())
    disc_idx = run._discovery_index(screen, interact)
    mt = {(m["hypothesis_id"], m["horizon"]): m for m in validate["multiple_testing"]}
    val_idx = {(r["hypothesis"]["hypothesis_id"], r["horizon"]): r for r in validate["rows"]}
    test_idx = {(r["hypothesis"]["hypothesis_id"], r["horizon"]): r for r in test["rows"]}
    stab = {(r["hypothesis_id"], r["horizon"]): r for r in refine["results"]}
    fdr = float(s["criteria"]["fdr_q"])

    findings = []
    events: list[dict[str, Any]] = []
    for hid, hz in (tuple(f) for f in validate["finalists"]):
        d = disc_idx[(hid, hz)]
        h = Hypothesis.from_dict(d["hypothesis"], d["hypothesis"]["origin"])
        disc_full = run._eval(h, "discovery", hz, regimes=run.regimes, folds=int(s["budget"]["walk_forward_folds"]),
                              keep_events=True)
        v, t = val_idx.get((hid, hz)), test_idx.get((hid, hz))
        if v is not None:
            v_full = run._eval(h, "validation", hz, keep_events=True)
            events += [{"segment": "validation", "hypothesis_id": hid, "horizon": hz, **e} for e in v_full["events"]]
        events += [{"segment": "discovery", "hypothesis_id": hid, "horizon": hz, **e} for e in disc_full.pop("events")]
        if t is not None:
            events += [{"segment": "test", "hypothesis_id": hid, "horizon": hz, **e} for e in t["events"]]
        m = mt.get((hid, hz), {})
        q = m.get("discovery_q_bh")
        notes = []
        if q is None or q > fdr:
            notes.append(f"does not survive Benjamini-Hochberg FDR {fdr} over {validate['total_discovery_tests']} discovery tests")
        if stab.get((hid, hz), {}).get("stability") == "NO_NEIGHBOURS":
            notes.append("parameter stability could not be assessed (fewer than two neighbours)")
        findings.append({
            "hypothesis": h.to_dict(), "horizon": hz, "status": status[(hid, hz)],
            "stability": stab.get((hid, hz)), "multiple_testing": m, "discovery": disc_full,
            "validation": {k: val for k, val in (v or {}).items() if k != "hypothesis"} or None,
            "test": {k: val for k, val in (t or {}).items() if k not in {"hypothesis", "events"}} or None,
            "caveats": notes, "investable": False,
        })
    # Immutable discovered defaults (inside the run directory) for validation passers.
    discovered = []
    for f in findings:
        if f["status"] in {"VALIDATION_PASS", "TEST_CONSISTENT", "TEST_INCONSISTENT", "TEST_INSUFFICIENT_EVENTS"}:
            name = f"{f['hypothesis']['hypothesis_id']}-h{f['horizon']}.json"
            write_json_atomic(run.dir / "discovered" / name, strategy_document(run, f))
            discovered.append(name)

    results = []
    for stage, st in (("screen", screen), ("refine", refine), ("interact", interact)):
        results += [_flat(stage, "discovery", r, status) for r in st["rows"]]
    results += [_flat("validate", "validation", r, status) for r in validate["rows"]]
    results += [_flat("test", "test", r, status) for r in test["rows"]]

    summary = {
        "contract": "stock-gplm.m54-findings.v1", "run_id": run.run_id, "symbol": run.symbol,
        "classification": CLASSIFICATION, "evidence_tiers": EVIDENCE_TIERS, "budget_name": s["budget_name"],
        "budget": s["budget"], "segments": data["segments"], "holdout_pin": data["holdout_pin"],
        "status_counts": dict(sorted(counts.items())), "total_discovery_tests": validate["total_discovery_tests"],
        "enumerated_single_conditions": screen["enumerated_single_conditions"],
        "tested_hypotheses": screen["tested_hypotheses"], "budget_limited": screen["budget_limited"],
        "findings": findings, "discovered_defaults": discovered,
        "baselines": _baselines(run), "data_quality": _data_quality(data),
        "governance": _governance(), "unsupported_indicators": UNSUPPORTED,
    }
    files = {"findings.json": dumps(summary), "results.csv": _csv(results, RESULT_COLUMNS),
             "events.csv": _csv(events, EVENT_COLUMNS), "report.md": render_markdown(run, data, screen, summary)}
    for name, text in files.items():
        write_text_atomic(run.dir / name, text)
    manifest = {"contract": "stock-gplm.m54-run-manifest.v1", "run_id": run.run_id, "symbol": run.symbol,
                "state": "COMPLETED", "completed_utc": utc_now(), "classification": CLASSIFICATION,
                "identity_sha256": sha256_file(run.dir / "identity.json"),
                "outputs": {n: sha256_file(run.dir / n) for n in OUTPUTS},
                "discovered": {n: sha256_file(run.dir / "discovered" / n) for n in discovered},
                "governance": _governance(), "device": run.device}
    write_json_atomic(run.dir / "run-manifest.json", manifest)
    return {"outputs": manifest["outputs"]}


def verify_outputs(run_dir: Path) -> None:
    manifest = read_json(run_dir / "run-manifest.json")
    for name, digest in {**manifest["outputs"], **{f"discovered/{k}": v for k, v in manifest.get("discovered", {}).items()}}.items():
        if sha256_file(run_dir / name) != digest:
            raise ValueError(f"{run_dir / name} does not match run-manifest.json; completed evidence was modified")


def _governance() -> dict[str, Any]:
    return {"minute_payloads_opened": 0, "m32_locked_holdout_opened": False, "models_fitted": 0,
            "trial_ledger_touched": False, "external_api_calls": 0, "orders_or_live_signals": 0,
            "raw_files_written": 0}


def _baselines(run: "Run") -> dict[str, Any]:
    out = {}
    for seg, (a, b) in run.segs.items():
        if b - a < 2:
            continue
        m = run.market
        buy = (run.costs.commission_bps + run.costs.slippage_bps + run.costs.buy_statutory_bps) / 1e4
        sell = (run.costs.commission_bps + run.costs.slippage_bps + run.costs.sell_statutory_bps) / 1e4
        bh = m.open[b - 1] * (1 - sell) / (m.open[a] * (1 + buy)) - 1
        out[seg] = {"buy_and_hold_net": float(bh), "from": run.dates[a], "to": run.dates[b - 1],
                    "note": "enter at first open, exit at last open of the segment, round-trip costs applied"}
    return out


def _data_quality(data: Mapping[str, Any]) -> list[str]:
    notes = [
        "Provider price basis (adjusted vs unadjusted) is unverified; sessions with large gaps are excluded from event paths.",
        "Calendar completeness, corporate-action coverage and point-in-time membership are UNKNOWN (M2 rules).",
        "Costs are explicit assumptions, not broker-verified schedules.",
    ]
    if data.get("blocked_sessions"):
        notes.append(f"Suspected discontinuities excluded from event paths: {', '.join(data['blocked_sessions'])}.")
    if data.get("segments_withheld"):
        notes.append("Withheld (never opened) segments at/after the M3.2 cutoff: "
                     + ", ".join(w["file"] for w in data["segments_withheld"]) + ".")
    return notes


def strategy_document(run: "Run", finding: Mapping[str, Any]) -> dict[str, Any]:
    return {"contract": STRATEGY_CONTRACT, "name": f"{run.symbol.lower()}-{finding['hypothesis']['hypothesis_id']}-h{finding['horizon']}",
            "version": 1, "status": "DISCOVERED_DEFAULT_IMMUTABLE", "activation": "RESEARCH_ONLY_NOT_FOR_TRADING",
            "scope": {"symbols": [run.symbol], "timeframe": "1d"},
            "entry": {"conditions": finding["hypothesis"]["conditions"], "fill": "next_session_open"},
            "exit": {"horizon_sessions": finding["horizon"], "fill": "open_after_horizon"},
            "costs": run.s["costs"],
            "provenance": {"run_id": run.run_id, "hypothesis_id": finding["hypothesis"]["hypothesis_id"],
                           "status_at_discovery": finding["status"], "origin": finding["hypothesis"]["origin"],
                           "feature_definitions": {c["feature"]: run.defs[c["feature"]].to_dict()
                                                   for c in finding["hypothesis"]["conditions"]}}}


def render_markdown(run: "Run", data: Mapping[str, Any], screen: Mapping[str, Any], summary: Mapping[str, Any]) -> str:
    s = run.s
    seg = data["segments"]
    fams = Counter(d.family for d in run.defs.values())
    L = [f"# {run.symbol} behaviour discovery report", "",
         f"Run `{run.run_id[:16]}` · budget **{s['budget_name']}** · classification **{CLASSIFICATION}**", "",
         "> Research diagnostics only. Nothing here is an investment recommendation, a trading signal or proof of edge.",
         "> Discovery thresholds were fitted in-sample; only validation and inner-test rows are out-of-sample, and "
         "the M3.2 locked holdout was never opened.", "",
         "## 1. Data and evaluation boundaries", "",
         f"- Bars: {data['bars']} sessions, {data['first']} to {data['last']} (bars SHA-256 `{data['bars_sha256'][:16]}…`).",
         f"- Holdout pin: M3.2 run `{data['holdout_pin']['m32_run_id'][:16]}…`, cutoff **{data['holdout_pin']['cutoff']}**. "
         "Only segments ending before the cutoff were opened.", ""]
    L += ["| Segment | Span | Sessions | Evidence tier |", "| --- | --- | ---: | --- |"]
    for k in ("discovery", "validation", "test"):
        L.append(f"| {k} | {seg[k]['first']} → {seg[k]['last']} | {seg[k]['sessions']} | {EVIDENCE_TIERS[k]} |")
    L += ["", f"- Final holdout: {EVIDENCE_TIERS['holdout']}.",
          "- Timing: signal at close t, entry at open t+1, exit at open t+1+h; events must exit inside their segment.",
          f"- Costs per side: commission {s['costs']['commission_bps']} bps, slippage {s['costs']['slippage_bps']} bps, "
          f"statutory buy {s['costs']['buy_statutory_bps']} / sell {s['costs']['sell_statutory_bps']} bps ({s['costs']['basis']}).", "",
          "## 2. What was investigated", "",
          f"- Indicator families: {', '.join(f'{k} ({v} features)' for k, v in sorted(fams.items()))}.",
          f"- Features computed: {len(run.defs)}. Threshold rule: discovery-segment quantiles {s['threshold_quantiles']} "
          "plus configured levels for bounded oscillators, and zero crossings for signed features.",
          f"- Single-condition hypotheses enumerated: {screen['enumerated_single_conditions']}; tested: "
          f"{screen['tested_hypotheses']} ({'budget-limited stratified sample' if screen['budget_limited'] else 'all'}).",
          f"- Horizons (sessions): {run.horizons}. Total discovery tests (hypothesis × horizon): "
          f"**{summary['total_discovery_tests']}**.",
          "- This is a budget-limited search. It did not test every possible relationship.", "",
          "Parameter grids searched:", ""]
    for name, spec in sorted(s["search_space"]["indicators"].items()):
        if spec.get("enabled", True):
            L.append(f"- `{name}`: " + ", ".join(f"{k}={v}" for k, v in sorted(spec.get("params", {}).items())))
    L += ["", "## 3. Outcome counts", "", "| Status | Count |", "| --- | ---: |"]
    L += [f"| {k} | {v} |" for k, v in summary["status_counts"].items()]
    L += ["", "## 4. Baselines (same segments, same costs)", "", "| Segment | Buy-and-hold net |", "| --- | ---: |"]
    L += [f"| {k} | {_pct(v['buy_and_hold_net'])} |" for k, v in summary["baselines"].items()]
    L += ["", "Each hypothesis is also compared with unconditional entry on every eligible session (same horizon) and "
          "with random entry sets of the same size (`random_entry_percentile`).", "",
          "## 5. Finalists (selected on discovery, frozen before validation)", ""]
    if not summary["findings"]:
        L.append("No hypothesis survived screening and stability checks. This is a valid negative result for this budget.")
    for f in summary["findings"]:
        d, v, t = f["discovery"], f["validation"] or {}, f["test"] or {}
        mtst = f["multiple_testing"]
        L += [f"### `{f['hypothesis']['hypothesis_id']}` · h={f['horizon']} · **{f['status']}**", "",
              f"`{f['hypothesis']['text']}` (origin: {f['hypothesis']['origin']})", "",
              "| Metric | Discovery | Validation | Inner test |", "| --- | ---: | ---: | ---: |"]
        for label, key, fmt in (("Raw events", "raw_events", str), ("Independent events", "independent_events", str),
                                ("Mean net return", "mean_net", _pct), ("Median net return", "median_net", _pct),
                                ("Hit rate (net > 0)", "hit_rate", _pct), ("Unconditional mean", "baseline_mean_net", _pct),
                                ("Excess vs unconditional", "excess_mean", _pct), ("t-stat", "t_stat", _num),
                                ("p (one-sided)", "p_value", _num), ("Mean MFE", "mean_mfe", _pct), ("Mean MAE", "mean_mae", _pct),
                                ("Random-entry percentile", "random_entry_percentile", _num),
                                ("Compounded (non-overlapping)", "compounded_return", _pct),
                                ("Equity max drawdown", "equity_max_drawdown", _pct)):
            def g(x: Mapping[str, Any]) -> str:
                val = x.get(key)
                return "n/a" if val is None else (fmt(val) if fmt is not str else str(val))
            L.append(f"| {label} | {g(d)} | {g(v)} | {g(t)} |")
        ci = d.get("ci95_mean_net") or [None, None]
        L += ["", f"- Discovery 95% bootstrap CI of mean net: {_pct(ci[0])} to {_pct(ci[1])}.",
              f"- Multiple testing: BH q = {_num(mtst.get('discovery_q_bh'))}, Bonferroni p = "
              f"{_num(mtst.get('discovery_p_bonferroni'))} over {summary['total_discovery_tests']} tests.",
              f"- Parameter stability: {(f['stability'] or {}).get('stability', 'interaction (not assessed)')}"
              f" (neighbour positive fraction {_num((f['stability'] or {}).get('neighbour_positive_fraction'))}).",
              f"- Time to target / failure (±{s['target_atr']}×ATR%): hit {_pct(d.get('target_hit_rate'))} in "
              f"{_num(d.get('mean_time_to_target'))} sessions / {_pct(d.get('failure_hit_rate'))} in "
              f"{_num(d.get('mean_time_to_failure'))} sessions."]
        wf = d.get("walk_forward") or {}
        if wf:
            L.append(f"- Walk-forward (discovery folds): positive fraction {_num(wf.get('positive_fraction'))} over "
                     f"{wf.get('informative_folds')} informative folds.")
        for name, groups in (d.get("regimes") or {}).items():
            L.append(f"- Regime `{name}`: " + "; ".join(f"{k} n={g['n']} mean {_pct(g['mean_net'])}" for k, g in groups.items()))
        for c in f["caveats"]:
            L.append(f"- Caveat: {c}.")
        L.append("")
    L += ["## 6. Interpretation rules", "",
          "- `TEST_CONSISTENT` means the pattern kept a positive net and excess mean on one short inner-test window. "
          "It is a candidate for further research, not a proven edge.",
          "- Findings that fail the FDR threshold are promising but unproven at best.",
          "- Patterns rejected at screen, stability or validation are listed in `results.csv` with their status.", "",
          "## 7. Data-quality concerns and limitations", ""]
    L += [f"- {n}" for n in summary["data_quality"]]
    L += ["- Survivorship: this run studies a single currently listed symbol; point-in-time membership is unknown.",
          "- The short pre-cutoff history (about two years) limits long horizons and regime coverage.", "",
          "## 8. Unsupported indicators (input data not available)", ""]
    L += [f"- `{k}`: needs {v}" for k, v in UNSUPPORTED.items()]
    L += ["", "## 9. Governance", "", "No minute payload, no M3.2 locked-holdout file, no trial ledger, no model fit, "
          "no external API and no order route was used. Raw files were opened read-only.", ""]
    return "\n".join(L)
