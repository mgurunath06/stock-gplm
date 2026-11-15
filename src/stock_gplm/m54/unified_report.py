"""Ranking, evidence classification, the per-stock report (sections A-R) and the strategy library.

Evidence tiers (strongest first). A strategy reaches a tier only if it satisfies every
condition of that tier; failure reasons are always listed.

* ``ROBUST_CANDIDATE``        survives per-symbol Benjamini-Hochberg FDR over every discovery test of
                              every lane, passes validation, is consistent on the inner test,
                              walk-forward positive in at least half its informative folds, and is
                              not parameter-unstable (entry or exit)
* ``PROMISING_NOT_SIGNIFICANT`` passes validation and the inner test but not FDR (or a robustness check)
* ``VALIDATION_ONLY``         passed validation; not tested (budget) or test had too few events
* ``REJECTED_TEST``           passed validation, inconsistent on the inner test
* ``REJECTED_VALIDATION``     failed validation (reasons listed)
* ``EXPLORATORY``             fallback candidate that never passed the discovery screen

None of these is a trading signal: the classification of every output is ``DIAGNOSTIC_ONLY_NON_INVESTABLE``.
"""
from __future__ import annotations

import csv
import io
import os
import stat
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from . import CLASSIFICATION
from .backtest import ExitPolicy, baseline, bh_adjust, metrics, simulate
from .hypotheses import Hypothesis, signal_mask
from .store import canonical_hash, dumps, read_json, sha256_file, utc_now, write_json_atomic, write_text_atomic

if TYPE_CHECKING:
    from .unified import UnifiedRun

STRATEGY_CONTRACT = "stock-gplm.m54-strategy.v2"
OUTPUTS = ("report.md", "strategies.json", "results.csv", "trades.csv", "coverage.json")
TIERS = ("ROBUST_CANDIDATE", "PROMISING_NOT_SIGNIFICANT", "VALIDATION_ONLY", "REJECTED_TEST", "REJECTED_VALIDATION",
         "EXPLORATORY")
DISCOVERY_STAGES = ("screen", "refine", "interact", "cross_tf", "exits")


def _screen_reason(r: Mapping[str, Any], need: int, max_p: float) -> str:
    if (r["effective_n"] or 0) < need:
        return "too_few_independent_events"
    if r["mean_net"] is None or r["mean_net"] <= 0:
        return "non_positive_net_after_costs"
    if (r["excess_mean"] or 0) <= 0:
        return "does_not_beat_baseline"
    if r["p_value"] is None or r["p_value"] > max_p:
        return "not_significant_at_screen_p"
    return "passed"


def _condition_spec(c: Mapping[str, Any], defs: Mapping[str, Any], lane_tf: str) -> dict[str, Any]:
    d = defs.get(c["feature"], {})
    tf = c["feature"].split(":", 1)[0] if ":" in c["feature"] else lane_tf
    return {"feature": c["feature"], "timeframe": tf, "indicator": d.get("indicator", "").split(":")[-1],
            "family": d.get("family"), "params": d.get("params", {}), "output": d.get("output"), "op": c["op"],
            "threshold": c["threshold"],
            "threshold_basis": (f"{c['label']} quantile of the feature on the discovery segment" if str(c["label"]).startswith("q")
                                else c["label"] or "binary event"), "text": Hypothesis.from_dict({"conditions": [c]}).text()}


def rank(run: "UnifiedRun", lanes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    c = run.s["criteria"]
    fdr = float(c["fdr_q"])
    # ---- per-symbol multiple-testing over every discovery evaluation of every lane
    pvals: list[float | None] = []
    keys: list[tuple[str, str, str, str]] = []
    for tf, L in lanes.items():
        for st in DISCOVERY_STAGES:
            for r in L[st]["rows"]:
                pvals.append(r["p_value"])
                keys.append((tf, st, r["hypothesis"]["hypothesis_id"], r["exit_key"]))
    total = len(pvals)
    qs = bh_adjust(pvals, total)
    qmap: dict[tuple[str, str, str], float | None] = {}
    for (tf, st, hid, ek), q in zip(keys, qs):
        if st == "exits":
            qmap.setdefault((tf, hid, ek), q)
    finite = [q for q in qs if q is not None]
    strategies, rejections, exploratory = [], {}, {}
    for tf, L in lanes.items():
        lane = L["lane"]
        intraday = lane.bars.intraday
        need = c["min_effective_n_discovery_intraday" if intraday else "min_effective_n_discovery"]
        tally = Counter()
        for st in ("screen", "interact", "cross_tf"):
            for r in L[st]["rows"]:
                tally[f"{st}:{_screen_reason(r, need, c['screen_max_p'])}"] += 1
        rejections[tf] = dict(sorted(tally.items()))
        top = sorted((r for r in L["screen"]["rows"] if r["t_stat"] is not None), key=lambda r: -r["t_stat"])[:10]
        exploratory[tf] = [{"hypothesis": r["hypothesis"]["text"], "horizon": r["horizon"], "trades": r["trades"],
                            "mean_net": r["mean_net"], "excess_mean": r["excess_mean"], "p_value": r["p_value"],
                            "status": _screen_reason(r, need, c["screen_max_p"])} for r in top]
        val = {r["key"]: r for r in L["validate"]["rows"]}
        test = {r["key"]: r for r in L["test"]["rows"]}
        wf = {r["key"]: r for r in L["walk_forward"]["rows"]}
        sel = L["validate"]["recency"]["selected"]
        scores = L["validate"]["recency"]["discovery_scores"]
        hyps = {r["hypothesis"]["hypothesis_id"]: r["hypothesis"] for r in L["exits"]["rows"]}
        defs = L["features"]["defs"]
        for ch in L["exits"]["chosen"]:
            if ch.get("exit") is None:
                continue
            k = ch["key"]
            h = Hypothesis.from_dict(hyps[ch["hypothesis_id"]])
            p = ExitPolicy.from_dict(ch["exit"])
            seg = lane.segs["discovery"]
            tr = simulate(lane.market, signal_mask(h, lane.features), seg, p, lane.costs)
            disc = metrics(tr, lane.market, seg, baseline(lane.market, seg, p, lane.costs, lane.base_cache))
            v, t, w = val.get(k), test.get(k), wf.get(k)
            q = qmap.get((tf, ch["hypothesis_id"], ch["exit_key"]))
            reasons: list[str] = []
            if ch["fallback"]:
                reasons.append("never passed the discovery screen (retained as exploratory)")
            if q is None or q > fdr:
                reasons.append(f"does not survive BH FDR {fdr} over {total} discovery tests (q={q if q is None else round(q, 3)})")
            if ch["entry_stability"] == "UNSTABLE":
                reasons.append("entry parameters unstable")
            ef = ch.get("exit_neighbour_positive_fraction")
            if ef is not None and ef < 0.5:
                reasons.append(f"exit parameters unstable ({ef:.0%} of neighbouring exit settings positive)")
            wpf = w.get("positive_fold_fraction") if w else None
            if wpf is None or wpf < 0.5:
                reasons.append("walk-forward positive in fewer than half the informative folds" if wpf is not None
                               else "walk-forward uninformative (no fold produced trades)")
            if v is not None:
                reasons += v["failure_reasons"]
            if t is not None and t["verdict"] != "TEST_CONSISTENT":
                reasons.append(t["verdict"].lower().replace("_", " "))
            if ch["fallback"]:
                tier = "EXPLORATORY"
            elif v is None or not v["passed"]:
                tier = "REJECTED_VALIDATION"
            elif t is None or t["verdict"] == "TEST_INSUFFICIENT_EVENTS":
                tier = "VALIDATION_ONLY"
                if t is None:
                    reasons.append("not evaluated on the inner test (budget)")
            elif t["verdict"] == "TEST_INCONSISTENT":
                tier = "REJECTED_TEST"
            else:
                robust = (q is not None and q <= fdr and ch["entry_stability"] != "UNSTABLE"
                          and (ef is None or ef >= 0.5) and wpf is not None and wpf >= 0.5)
                tier = "ROBUST_CANDIDATE" if robust else "PROMISING_NOT_SIGNIFICANT"
            conds = [_condition_spec(cc, defs, tf) for cc in h.to_dict()["conditions"]]
            sid = canonical_hash({"symbol": run.symbol, "tf": tf, "conditions": [cc for cc in h.to_dict()["conditions"]],
                                  "exit": ch["exit"]})[:16]
            strategies.append({
                "strategy_id": sid, "key": k, "timeframe": tf, "intraday": intraday, "hypothesis_id": ch["hypothesis_id"],
                "entry_text": h.text(), "conditions": conds, "source": ch["source"], "fallback": ch["fallback"],
                "cross_timeframe": any(":" in cc["feature"] for cc in h.to_dict()["conditions"]),
                "entry_stability": ch["entry_stability"], "exit": ch["exit"], "exit_text": p.text(),
                "exit_neighbour_positive_fraction": ef, "time_exit_only_excess": ch.get("time_exit_only_excess"),
                "discovery": disc, "validation": v["metrics"] if v else None, "validation_regimes": v["regimes"] if v else None,
                "test": t["metrics"] if t else None, "test_verdict": t["verdict"] if t else None,
                "walk_forward": w, "q_bh": q, "p_bonferroni": None if ch["discovery"]["p_value"] is None
                else min(1.0, ch["discovery"]["p_value"] * total),
                "recency_selected": sel, "score_uniform": scores.get("uniform", {}).get(k),
                "score_selected": scores.get(sel, {}).get(k), "tier": tier, "reasons": reasons,
                "costs": run.s["costs"][lane.cfg["cost_profile"]]})
    order = {t: i for i, t in enumerate(TIERS)}
    # Two horizons can converge on the same entry and exit; keep one copy of each distinct rule set.
    seen_ids: dict[str, dict[str, Any]] = {}
    for st in strategies:
        prior = seen_ids.get(st["strategy_id"])
        if prior is None or order[st["tier"]] < order[prior["tier"]]:
            seen_ids[st["strategy_id"]] = st
    strategies = list(seen_ids.values())
    strategies.sort(key=lambda s: (order[s["tier"]], -((s["validation"] or {}).get("excess_mean") or -1e9),
                                   -((s["test"] or {}).get("excess_mean") or -1e9), s["strategy_id"]))
    for i, s in enumerate(strategies, 1):
        s["rank"] = i
    tiers = Counter(s["tier"] for s in strategies)
    robust = tiers.get("ROBUST_CANDIDATE", 0)
    verdict = (f"{robust} strategy(ies) met every robustness criterion (still diagnostic, not investable)." if robust else
               "NO ROBUST STRATEGY FOUND: no candidate survived multiple-testing control together with validation, "
               "inner-test, walk-forward and stability checks. Exploratory and partially validated candidates are "
               "listed with their failure reasons.")
    return {"total_discovery_tests": total, "fdr_q": fdr, "min_q": min(finite) if finite else None,
            "strategies": strategies, "tier_counts": dict(tiers), "verdict": verdict,
            "screen_rejections": rejections, "exploratory_screen_leads": exploratory}


# ----------------------------------------------------------------------------- formatting

def _pct(v: Any, d: int = 2) -> str:
    return "n/a" if v is None else f"{v * 100:.{d}f}%"


def _num(v: Any, d: int = 3) -> str:
    return "n/a" if v is None else f"{v:.{d}g}"


def _csv(rows: Sequence[Mapping[str, Any]], cols: Sequence[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(cols), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return buf.getvalue()


def _holding(tf: str, bars: float | None) -> str:
    if bars is None:
        return "n/a"
    if tf == "1d":
        return f"{bars:.1f} sessions"
    if tf == "1w":
        return f"{bars:.1f} weeks"
    mins = bars * int(tf.rstrip("m"))
    return f"{bars:.1f} bars (~{mins:.0f} min)"


def strategy_definition(run: "UnifiedRun", s: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract": STRATEGY_CONTRACT, "strategy_id": s["strategy_id"], "symbol": run.symbol, "timeframe": s["timeframe"],
        "direction": "long", "classification": CLASSIFICATION, "editable": False,
        "entry": {"logic": "AND", "conditions": s["conditions"],
                  "evaluation": "conditions evaluated at the close of bar t on causal features",
                  "fill": "open of bar t+1 (next executable bar)" + ("; never across a session boundary" if s["intraday"] else ""),
                  "higher_timeframe_rule": "a higher-timeframe value is read only after its source candle closed "
                                           "(daily values reach intraday bars from 00:00 IST of the next day)"},
        "exit": {**s["exit"], "atr": "ATR(14) of this timeframe measured at the signal bar",
                 "order_of_checks": "stop (gap fills at open), then target (gap fills at open); both in one bar = stop",
                 "time_exit": "open of bar t+1+max_hold" + ("; flat at the session's last bar close" if s["intraday"] else ""),
                 "text": s["exit_text"]},
        "costs": s["costs"],
        "evidence": {"tier": s["tier"], "reasons": s["reasons"], "q_bh": s["q_bh"],
                     "discovery": {k: s["discovery"].get(k) for k in ("trades", "effective_n", "mean_net", "win_rate",
                                                                       "profit_factor", "max_drawdown", "p_value")},
                     "validation": None if not s["validation"] else {k: s["validation"].get(k) for k in (
                         "trades", "effective_n", "mean_net", "win_rate", "profit_factor", "max_drawdown")},
                     "test": None if not s["test"] else {k: s["test"].get(k) for k in (
                         "trades", "effective_n", "mean_net", "win_rate", "profit_factor", "max_drawdown")},
                     "test_verdict": s["test_verdict"]},
        "provenance": {"run_id": run.run_id, "code_sha256": run.code_sha, "policy": run.policy.to_dict(),
                       "budget": run.s["budget_name"], "generated_utc": utc_now()},
    }


def _write_library(run: "UnifiedRun", defs: Sequence[dict[str, Any]]) -> list[str]:
    """Immutable discovery defaults: written once, read-only. Editable copies come from `gplm library export`."""
    root = run.output_root / "library" / "defaults" / run.symbol
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for d in defs:
        path = root / f"{d['strategy_id']}--{run.run_id[:12]}.json"
        if not path.exists():
            write_text_atomic(path, dumps(d))
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        paths.append(str(path))
    return paths


def _seg_row(name: str, m: Mapping[str, Any] | None) -> str:
    if not m:
        return f"| {name} | not evaluated | | | | | | | | | |"
    return (f"| {name} | {m['trades']} | {m.get('effective_n')} | {_pct(m.get('mean_gross'))} | {_pct(m.get('mean_net'))} | "
            f"{_pct(m.get('total_net_compounded'))} | {_pct(m.get('win_rate'), 0)} | {_num(m.get('profit_factor'))} | "
            f"{_pct(m.get('max_drawdown'))} | {_num(m.get('sharpe_annualized'))} | {_pct(m.get('excess_mean'))} / p={_num(m.get('p_value'))} |")


def write_outputs(run: "UnifiedRun", data: Mapping[str, Any], lanes: Mapping[str, Mapping[str, Any]],
                  ranked: Mapping[str, Any]) -> dict[str, Any]:
    s = run.s
    strategies = ranked["strategies"]
    L: list[str] = []
    add = L.append
    add(f"# {run.symbol}: unified multi-timeframe strategy discovery")
    add("")
    add(f"Classification: **{CLASSIFICATION}**. Research diagnostics only. No orders, live signals or investment advice.")
    if run.policy.basis == "SYNTHETIC_FIXTURE":
        add("")
        add("> **SYNTHETIC FIXTURE DATA.** This run proves the pipeline end to end. Its numbers say nothing about any market.")
    add("")
    add(f"- Run `{run.run_id}` | budget `{s['budget_name']}` | data `{run.data_mode}` | timeframes `{run.timeframes}` | "
        f"device `{run.device}` | workers {run.workers}")
    add(f"- Code SHA-256 `{run.code_sha[:16]}…` | access policy `{run.policy.basis}`, cutoff **{run.policy.cutoff}** "
        f"(partitions ending on or after it are excluded whole and never opened)")
    add(f"- Generated {utc_now()}")
    add("")
    add("## Verdict")
    add("")
    add(f"**{ranked['verdict']}**")
    add("")
    add(f"Tier counts: {ranked['tier_counts'] or 'no strategy reached the exit stage'}. Total discovery tests across all "
        f"lanes: **{ranked['total_discovery_tests']}**. The smallest BH q-value is {_num(ranked['min_q'])}, against an FDR "
        f"threshold of {ranked['fdr_q']}.")
    add("")

    # A
    add("## A. Available data coverage by timeframe")
    add("")
    add("| Timeframe | Source | Partitions found | Permitted | Excluded (reason: count) | First…last found |")
    add("| --- | --- | ---: | ---: | --- | --- |")
    for tf, cv in data["coverage"].items():
        ex = ", ".join(f"{k}: {v}" for k, v in cv["excluded"].items()) or "none"
        add(f"| {tf} raw | {'daily files' if tf == '1d' else 'minute session files'} | {cv['partitions']} | {cv['permitted']} | {ex} | {cv['first']} … {cv['last']} |")
    if "1m" not in data["coverage"]:
        add("| 1m raw | minute session files | 0 | 0 | none found under the minute roots | n/a |")
    add("")
    add("| Lane | Bars (permitted) | Sessions | First | Last | Incomplete bars | Source | Lane decision |")
    add("| --- | ---: | ---: | --- | --- | ---: | --- | --- |")
    for tf, t in data["timeframes"].items():
        add(f"| {tf} | {t['bars']} | {t['sessions']} | {t['first']} | {t['last']} | {t['incomplete_bars']} | {t['source']} | "
            f"{data['lanes'][tf]['decision']} |")
    add("")
    add(f"Daily root: `{data['daily_root']}`. Minute roots: {', '.join(f'`{r}`' for r in data['minute_roots']) or 'not used'}.")
    add("")

    # B
    add("## B. Data used and excluded")
    add("")
    pol = data["policy"]
    add(f"- Access policy **{pol['basis']}**, cutoff {pol['cutoff']}"
        + (f", from M3.2 run `{pol['holdout_pin']['m32_run_id'][:16]}…` (split plan `{pol['holdout_pin']['split_plan_sha256'][:16]}…`)"
           if "holdout_pin" in pol else "") + ".")
    add("- An excluded partition's payload is never opened. Only its sidecar marker and meta (bytes, records, SHA-256, "
        "session type) are read, for coverage.")
    used = [p for p in data["partitions"] if p["permitted"]]
    add(f"- Partitions used: {len(used)}.")
    for p in (x for x in used if x["timeframe"] == "1d"):
        add(f"  - `{p['file']}` (1d, {'…'.join(p['window'])}, {p.get('records')} records, SHA-256 `{str(p.get('sha256'))[:16]}…`, {p['disposition']})")
    mins = [x for x in used if x["timeframe"] == "1m"]
    if mins:
        dec = Counter(x["disposition"] for x in mins)
        add(f"  - {len(mins)} minute sessions {mins[0]['session']} … {mins[-1]['session']} from "
            f"{sorted({x['root'] for x in mins})}; decisions {dict(dec)}; "
            f"{sum(x.get('missing_regular_minutes') or 0 for x in mins)} missing regular minutes in total (never filled). "
            "Per-session hashes are in coverage.json.")
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in data["partitions"]:
        if not p["permitted"]:
            groups.setdefault(f"{p['timeframe']} {p['disposition'].split(':')[0]}", []).append(p)
    for g, ps in sorted(groups.items()):
        firsts = sorted(p.get("session") or p["window"][0] for p in ps)
        add(f"- Excluded **{g}**: {len(ps)} partitions, {firsts[0]} … {firsts[-1]}. Example reason: {ps[0]['disposition']}")
    dups = data["minute_duplicates"]
    if dups:
        same = sum(1 for d in dups if d["identical_declared_sha256"])
        add(f"- Minute sessions present in more than one root: {len(dups)} ({same} with identical declared SHA-256 values, "
            f"{len(dups) - same} conflicting). Conflicting copies are never used.")
    if data["daily_issues"]:
        add(f"- Daily loader issues: {data['daily_issues']}")
    rec = data["reconciliation"]
    add(f"- Minute vs daily reconciliation: {rec.get('shared_sessions', 0)} shared sessions"
        + (f"; median absolute close difference {_pct(rec.get('close_median_abs_rel_diff'), 3)}; sessions over 0.5%: "
           f"close {rec.get('close_sessions_over_0.5pct')}, high {rec.get('high_sessions_over_0.5pct')}"
           if rec.get("shared_sessions") else "") + ". Reported only; never substituted.")
    gaps = data["daily_calendar_gaps"]["gaps_over_4_calendar_days"]
    add(f"- Daily gaps over 4 calendar days: {len(gaps)}. These are listed, not explained, because no registered NSE "
        "holiday calendar is used.")
    add(f"- Corporate actions: {data['corporate_actions']}.")
    add("")

    # C
    add("## C. Indicators and parameter ranges evaluated")
    add("")
    add(f"Budget `max_period` = {s['budget']['max_period']} bars. Thresholds come from discovery-segment quantiles "
        f"{s['threshold_quantiles']} or from the configured oscillator levels. No period or level is preferred in code.")
    add("")
    add("| Indicator | Parameter grid |")
    add("| --- | --- |")
    for name, spec in sorted(s["search_space"]["indicators"].items()):
        add(f"| {name} | {spec.get('params', {})} |")
    add("")
    add(f"Higher-timeframe confirmation set: {', '.join(f'{k}{v.get('params', {})}' for k, v in sorted(s['htf_search_space']['indicators'].items()))}.")
    add("Intraday-only features (minute data only): session VWAP distance and reclaim, opening-range position and "
        "breakout (15/30 min), minute of session, opening gap.")
    add("")
    add("| Lane | Features | Base | Higher-timeframe | Families | Cache | Top discovery rank-IC features |")
    add("| --- | ---: | ---: | --- | --- | --- | --- |")
    for tf, Ld in lanes.items():
        f = Ld["features"]
        top = "; ".join(f"{k} ({v:.2f})" for k, v in f["top_ic"][:3])
        add(f"| {tf} | {f['features']} | {f['base_features']} | {f['htf_features']} | {len(f['families'])} | "
            f"{'hit' if f.get('cache', {}).get('hit') else 'computed'} | {top} |")
    add("")

    # D
    add("## D. Candidate strategies discovered")
    add("")
    add("| Lane | Enumerated singles | Screened (IC-guided) | Screen tests | Survivors | Refine tests | Interaction tests | "
        "Cross-TF tests | Cross-TF survivors | Exit-grid tests | Strategies |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for tf, Ld in lanes.items():
        sc = Ld["screen"]
        add(f"| {tf} | {sc['enumerated']} | {sc['tested_hypotheses']} ({sc['ic_guided']}) | {sc['tests']} | {len(sc['survivors'])} | "
            f"{Ld['refine']['tests']} | {Ld['interact']['tests']} | {Ld['cross_tf']['tests']} | {len(Ld['cross_tf']['survivors'])} | "
            f"{Ld['exits']['tests']} | {sum(1 for c in Ld['exits']['chosen'] if c.get('exit'))} |")
    skipped = {tf: p["decision"] for tf, p in data["lanes"].items() if p["decision"] != "RUN"}
    add("")
    add(f"Lanes not run: {skipped or 'none'}.")
    add("")

    # P (ranked comparison first for readability)
    add("## P. Ranked strategy comparison")
    add("")
    if not strategies:
        add("No strategy reached the exit stage.")
    else:
        add("| # | Tier | TF | Entry | Exit | Disc. net/trade (n) | Val. net/trade (n) | Test net/trade (n) | q (BH) | WF+ |")
        add("| ---: | --- | --- | --- | --- | --- | --- | --- | ---: | ---: |")
        for st in strategies[:30]:
            v, t = st["validation"] or {}, st["test"] or {}
            wf = (st["walk_forward"] or {}).get("positive_fold_fraction")
            add(f"| {st['rank']} | {st['tier']} | {st['timeframe']} | {st['entry_text'][:90]} | {ExitPolicy.from_dict(st['exit']).key()} | "
                f"{_pct(st['discovery'].get('mean_net'))} ({st['discovery'].get('effective_n')}) | "
                f"{_pct(v.get('mean_net'))} ({v.get('effective_n', 'n/a')}) | {_pct(t.get('mean_net'))} ({t.get('effective_n', 'n/a')}) | "
                f"{_num(st['q_bh'])} | {_pct(wf, 0)} |")
    add("")

    # E-N
    add("## E–N. Strategy details")
    add("")
    for st in strategies[:12]:
        add(f"### #{st['rank']} `{st['strategy_id']}` [{st['tier']}] {st['timeframe']}{' (cross-timeframe)' if st['cross_timeframe'] else ''}")
        add("")
        add("**E. Entry rules** (all at the close of bar t; fill at the next bar's open):")
        for cdef in st["conditions"]:
            add(f"- [{cdef['timeframe']}] {cdef['text']} — {cdef['indicator']} {cdef['params']} `{cdef['output']}`; threshold basis: {cdef['threshold_basis']}")
        add(f"\n**F. Exit and risk:** {st['exit_text']}. Stop and target are checked in that order, a gap fills at the open, "
            f"and a bar that touches both counts as the stop. Neighbouring exit settings positive: "
            f"{_pct(st['exit_neighbour_positive_fraction'], 0)}. A time exit alone gave discovery excess {_pct(st['time_exit_only_excess'])}.")
        add(f"\n**G. Timeframe and holding:** {st['timeframe']}. Average holding is {_holding(st['timeframe'], st['discovery'].get('avg_bars_held'))} "
            f"(discovery). The maximum is {st['exit']['max_hold_bars']} bars.")
        add("\n**H–L. Performance** (net figures are after the configured costs and slippage; costs are an ASSUMPTION, not broker-verified):\n")
        add("| Segment | Trades | Effective n | Mean gross | Mean net | Compounded net | Win rate | Profit factor | Max DD | Sharpe (ann.) | Excess vs baseline |")
        add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        add(_seg_row("discovery (in-sample)", st["discovery"]))
        add(_seg_row("validation (once)", st["validation"]))
        add(_seg_row("inner test (once)", st["test"]))
        wf = st["walk_forward"] or {}
        add(f"\n**M. Validation evidence:** walk-forward exit re-selection gave {wf.get('oos_trades', 0)} out-of-fold trades, "
            f"mean excess {_pct(wf.get('oos_mean_excess'))}, positive folds {_pct(wf.get('positive_fold_fraction'), 0)}. "
            f"BH q = {_num(st['q_bh'])} and Bonferroni p = {_num(st['p_bonferroni'])}. The entry is {st['entry_stability']}. "
            f"Recency scheme `{st['recency_selected']}` gives a score of {_pct(st['score_selected'])}, against {_pct(st['score_uniform'])} uniform.")
        if st["validation_regimes"]:
            add(f"Validation regimes: {st['validation_regimes']}")
        add(f"\n**N. Evidence classification:** `{st['tier']}`." + (f" Reasons: {'; '.join(st['reasons'])}." if st["reasons"] else ""))
        add("")

    # Recency
    add("## Recency weighting (selected on validation)")
    add("")
    for tf, Ld in lanes.items():
        rec = Ld["validate"]["recency"]
        add(f"**{tf}**: selected `{rec['selected']}` ({rec['rule']}).")
        add("")
        add("| Scheme | Top-K | With validation trades | Mean validation net |")
        add("| --- | ---: | ---: | ---: |")
        for name, row in rec["table"].items():
            add(f"| {name} | {row['top_k']} | {row['with_validation_trades']} | {_pct(row['mean_validation_net'])} |")
        add("")
    add("Recency weights depend on age in sessions, not bars. They affect ranking only, never the p-values used for "
        "multiple-testing control.")
    add("")

    # O
    add("## O. Failure reasons for rejected hypotheses")
    add("")
    for tf, tally in ranked["screen_rejections"].items():
        add(f"- **{tf}**: " + ", ".join(f"{k} {v}" for k, v in tally.items()))
    rej = [st for st in strategies if st["tier"] not in ("ROBUST_CANDIDATE",)]
    if rej:
        add("")
        add("Strategies that reached validation but are not robust:")
        for st in rej[:25]:
            add(f"- `{st['strategy_id']}` ({st['timeframe']}, {st['tier']}): {'; '.join(st['reasons']) or 'n/a'}")
    add("")
    add("Exploratory screen leads (best discovery t-statistics, retained for transparency; not evidence):")
    for tf, rows in ranked["exploratory_screen_leads"].items():
        for r in rows[:5]:
            add(f"- {tf} h={r['horizon']}: {r['hypothesis'][:100]} | n={r['trades']} net {_pct(r['mean_net'])} p={_num(r['p_value'])} [{r['status']}]")
    add("")

    # Q, R
    defs = [strategy_definition(run, st) for st in strategies]
    lib = _write_library(run, defs)
    add("## Q. Machine-readable strategy definitions")
    add("")
    add(f"`strategies.json` holds {len(defs)} definitions (contract `{STRATEGY_CONTRACT}`), with entry conditions "
        "(indicator, parameters, output, operator, threshold and its basis), the exit policy, costs, evidence and provenance.")
    add("")
    add("## R. Editable strategy library")
    add("")
    add(f"The immutable discovery defaults are read-only files under `{run.output_root / 'library' / 'defaults' / run.symbol}` ({len(lib)} files).")
    add("To make an editable copy (the default is never modified):")
    add("")
    add(f"```\nbin/gplm library export --strategy-id <id> --name my-variant\nbin/gplm library list --stock {run.symbol.removesuffix('-EQ')}\n```")
    add("")
    add("## Method and limits")
    add("")
    add("- Segments are chronological session splits of the permitted data "
        f"({s['split']['discovery']:.0%}/{s['split']['validation']:.0%}/{s['split']['test']:.0%}), with an embargo between them. "
        "A trade must finish inside its segment. Thresholds and selections use discovery data only. Validation picks the "
        "recency scheme and gates the strategy. The inner test is scored once and changes nothing.")
    add("- On intraday lanes, statistics are clustered by session, so minute bars cannot pose as independent observations.")
    add("- Survivorship and point-in-time universe effects are not addressed for single-stock research. Corporate actions are "
        "UNKNOWN. Costs are assumptions. Past behaviour does not imply future behaviour.")
    add("")
    report = "\n".join(L) + "\n"

    seg_rows, trade_rows = [], []
    for st in strategies:
        for seg in ("discovery", "validation", "test"):
            m = st.get(seg)
            if m:
                seg_rows.append({"rank": st["rank"], "strategy_id": st["strategy_id"], "tier": st["tier"],
                                 "timeframe": st["timeframe"], "segment": seg, "entry": st["entry_text"],
                                 "exit": ExitPolicy.from_dict(st["exit"]).key(), **{k: m.get(k) for k in METRIC_COLS}})
    for tf, Ld in lanes.items():
        sids = {st["key"]: st["strategy_id"] for st in strategies if st["timeframe"] == tf}
        for r in Ld["test"]["rows"]:
            for t in r["trades"]:
                trade_rows.append({"strategy_id": sids.get(r["key"]), "timeframe": tf, "segment": "test", **t})
    files = {"report.md": report, "strategies.json": dumps({"contract": "stock-gplm.m54-strategy-set.v1", "symbol": run.symbol,
                                                             "run_id": run.run_id, "strategies": defs}),
             "results.csv": _csv(seg_rows, RESULT_COLS), "trades.csv": _csv(trade_rows, TRADE_COLS),
             "coverage.json": dumps({k: data[k] for k in ("policy", "coverage", "partitions", "minute_duplicates",
                                                           "timeframes", "lanes", "reconciliation", "daily_calendar_gaps")})}
    hashes = {}
    for name, text in files.items():
        write_text_atomic(run.dir / name, text)
        hashes[name] = sha256_file(run.dir / name)
    write_json_atomic(run.dir / "run-manifest.json", {"contract": "stock-gplm.m54-unified-manifest.v1", "run_id": run.run_id,
                                                      "symbol": run.symbol, "state": "COMPLETED", "outputs": hashes,
                                                      "library": lib, "classification": CLASSIFICATION,
                                                      "completed_utc": utc_now()})
    return {"outputs": hashes, "library_files": len(lib)}


METRIC_COLS = ("trades", "effective_n", "mean_gross", "mean_net", "total_net_compounded", "win_rate", "profit_factor", "max_drawdown",
               "sharpe_annualized", "sortino_annualized", "excess_mean", "p_value", "baseline_mean_net", "buy_hold_return",
               "avg_bars_held", "exposure_fraction")
RESULT_COLS = ("rank", "strategy_id", "tier", "timeframe", "segment", "entry", "exit", *METRIC_COLS)
TRADE_COLS = ("strategy_id", "timeframe", "segment", "signal", "entry", "exit", "entry_price", "exit_price", "gross_return",
              "net_return", "bars_held", "exit_reason", "mfe", "mae")


def verify_outputs(run_dir: Path) -> None:
    manifest = read_json(Path(run_dir) / "run-manifest.json")
    for name, digest in manifest["outputs"].items():
        if sha256_file(Path(run_dir) / name) != digest:
            raise ValueError(f"{name} in {run_dir} does not match its run manifest")
