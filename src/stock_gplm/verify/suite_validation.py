"""Section 4: walk-forward segments, discovery-only optimisation, statistics, gates and holdout protection."""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

from ..app import strategy_engine as se
from ..app.m56_status import GatePolicy, derive, final_gates, validation_gates
from ..app.strategy_library import StrategyDef, normalize, validate
from ..m54 import backtest as m54bt
from . import fixtures as fx
from . import pipeline as pl
from . import reference as R
from .core import Check, Section


def _sdef(doc: dict[str, Any]) -> StrategyDef:
    d = normalize(doc)
    en, xn, needs = validate(d)
    return StrategyDef(d, "(verify)", "", en, xn, 1, "x", True, needs)


def _lane(frame: Any) -> se.LaneInputs:
    return se.LaneInputs(fx.ctx(frame), fx.market(frame, fx.natr_of(frame)), 0.001, 0.002, False, "1d", 4)


GRID_DOC = {"id": "VER-GRID", "origin": "USER_DEFINED", "name": "grid probe", "entry": "rsi(close, $n) < $lvl",
            "params": {"n": {"default": 2, "grid": [2, 3, 5]}, "lvl": {"default": 10, "grid": [5, 10, 20, 30]}},
            "exit": {"max_hold_bars": 5, "stop_atr": 2.0}, "applicability": {"timeframes": ["1d"]}}


def run(sec: Section, ev: dict[str, Any]) -> None:
    def segs(c: Check) -> None:
        out = []
        for n, purge in ((727, 10), (727, 30), (1500, 40), (200, 5)):
            s = se.segments(n, purge)
            if s is None:
                out.append({"bars": n, "purge": purge, "segments": None})
                continue
            d, v, t = s["discovery"], s["validation"], s["test"]
            out.append({"bars": n, "purge": purge, **{k: list(x) for k, x in s.items()}})
            c.expect(d[0] == 0 and d[1] == int(n * 0.6), f"{n}: discovery {d}")
            c.expect(v[0] - d[1] == purge and t[0] - v[1] == purge,
                     f"{n}/{purge}: purge gaps {v[0] - d[1]}, {t[0] - v[1]} must equal the max holding period")
            c.expect(d[1] <= v[0] < v[1] <= t[0] < t[1] == n, f"{n}: segments overlap or are out of order")
        c.expect(se.segments(80, 5) is None, "fewer than 90 bars must refuse to split")
        c.evidence["splits"] = out
    sec.run("chronological_purged_split", "Development data splits 60/20/20 in time order with a purge gap equal to "
            "the maximum holding period; too-short history is refused", segs)

    def discovery_only(c: Check) -> None:
        frame = fx.ohlcv(1200)
        li = _lane(frame)
        s = _sdef(GRID_DOC)
        calls: list[tuple[int, int]] = []
        real = se.simulate

        def spy(m: Any, mask: Any, seg: tuple[int, int], *a: Any, **k: Any) -> Any:
            calls.append(tuple(seg))
            return real(m, mask, seg, *a, **k)
        with mock.patch.object(se, "simulate", spy):
            res = se.evaluate_strategy(s, li, {}, None)
        sg = res["segs"]
        by = defaultdict(int)
        for seg in calls:
            name = next((k for k, v in sg.items() if tuple(v) == seg), "other")
            by[name] += 1
        c.evidence.update(grid_size=res["grid_size"], chosen=res["params"], simulate_calls_by_segment=dict(by),
                          segments=sg)
        c.expect(by.get("test", 0) == 0, "the final (test) segment was simulated during optimisation")
        c.expect(by.get("validation", 0) == 2, f"validation segment simulated {by.get('validation', 0)} times; "
                                               "expected 2 (strategy once + its unconditional baseline once)")
        c.expect(by.get("discovery", 0) >= res["grid_size"], "every grid point must be scored on discovery data")
        # selection must not depend on validation/test data
        g = frame.copy()
        cut = sg["discovery"][1]
        rng = np.random.default_rng(3)
        for k in ("open", "high", "low", "close"):
            g.loc[cut:, k] = g.loc[cut:, k] * np.exp(rng.normal(0, 0.08, len(g) - cut))
        g["high"] = g[["open", "high", "low", "close"]].max(axis=1)
        g["low"] = g[["open", "high", "low", "close"]].min(axis=1)
        res2 = se.evaluate_strategy(s, _lane(g), {}, None)
        c.evidence.update(chosen_after_scrambling_later_data=res2["params"])
        c.expect(res2["params"] == res["params"], "parameter choice changed when only validation/test bars changed")
    sec.run("optimisation_uses_discovery_only", "Parameter grids are fitted on the discovery segment only; the "
            "validation segment is read once for the chosen configuration; the final segment is never read", discovery_only)

    def baseline_exit(c: Check) -> None:
        """The excess-return test compares a rule with 'the same exit entered on every bar'. Is it the same exit?"""
        from ..app import strategy_dsl as dsl

        out = []
        for name, frame in (("uptrend", fx.ohlcv(1200, fx.SEED + 5, drift=0.0015, regime_flip=False)),
                            ("main", fx.ohlcv(1200))):
            li = _lane(frame)
            seg = se.segments(len(frame), 10)["validation"]
            for short, when in ((False, "close > sma(close, 5)"), (True, "close < sma(close, 5)")):
                ex = se.Exit(10, 2.5, None, None)
                xm = dsl.signal(when, li.ctx)
                engine = se.baseline(li.market, seg, ex, li.buy, li.sell, short, {})
                a, b = seg
                every = np.arange(a, b)
                same = se.simulate(li.market, np.ones(len(frame), bool), seg, ex, li.buy, li.sell, short, xm,
                                   independent=False, fired=every)
                ref = float(np.mean([t["net_return"] for t in same]))
                out.append({"fixture": name, "direction": "short" if short else "long", "exit_when": when,
                            "engine_baseline_mean_net": engine, "baseline_with_same_signal_exit": ref,
                            "bias_in_excess_return": ref - engine})
        c.evidence["cases"] = out
        worst = max(abs(r["bias_in_excess_return"]) for r in out)
        if worst > 1e-6:
            c.fail(f"the baseline ignores the strategy's signal exit (exit.when): it holds every baseline trade to "
                   f"the time/stop exit, so a rule with an early signal exit is compared with a different exit policy. "
                   f"Excess return is mis-stated by up to {100 * worst:.2f}% per trade on these fixtures "
                   f"(sign depends on trend), which moves t-statistics, p-values, BH q-values, GP fitness and "
                   f"gate decisions for every strategy with exit.when. Fix: pass the exit mask to baseline() "
                   f"(strategy_engine.baseline -> simulate(..., xmask)).")
    sec.run("baseline_uses_same_exit_policy", "The unconditional baseline in the excess-return test uses exactly the "
            "strategy's exit policy (time, stop, target, trail and signal exit)", baseline_exit)

    def folds(c: Check) -> None:
        trades = [{"t": t, "net_return": r} for t, r in ((100, .01), (130, -.02), (160, .03), (190, .005), (199, .01))]
        rows = m54bt.fold_metrics(trades, (100, 200), 4, 0.001)
        c.evidence["folds"] = rows
        # edges 100,125,150,175,200: signal bars 100 | 130 | 160 | 190,199
        c.expect([r["n"] for r in rows] == [1, 1, 1, 2], f"fold counts {[r['n'] for r in rows]} expected [1,1,1,2]")
        c.expect(math.isclose(rows[3]["mean_excess"], ((.005 - .001) + (.01 - .001)) / 2), "fold 4 mean excess")
    sec.run("walk_forward_folds", "Validation trades are assigned to contiguous, non-overlapping time folds by "
            "signal bar; fold means are excess over the baseline", folds)

    def bh(c: Check) -> None:
        rng = np.random.default_rng(11)
        worst = 0.0
        for k in (1, 5, 20, 113, 400):
            p = list(rng.uniform(0, 1, k) ** 3)
            for extra in (0, 7):
                got = m54bt.bh_adjust(p, k + extra)
                want = R.bh(p, k + extra)
                worst = max(worst, max(abs(a - b) for a, b in zip(got, want)))
        textbook = [0.01, 0.04, 0.03, 0.005]
        got = m54bt.bh_adjust(textbook, 4)
        want = [0.02, 0.04, 0.04, 0.02]  # p*m/rank with step-up minimum
        c.expect(all(math.isclose(a, b) for a, b in zip(got, want)), f"textbook BH: got {got} expected {want}")
        withnone = m54bt.bh_adjust([0.01, None, 0.02], 3)
        c.expect(withnone[1] is None and math.isclose(withnone[0], 0.03) and math.isclose(withnone[2], 0.03),
                 f"untestable entries must count as tests (p=1) but stay None: {withnone}")
        c.expect(worst < 1e-12, f"BH differs from the reference by {worst}")
        c.evidence.update(max_abs_diff=worst, textbook_case={"p": textbook, "q": got}, none_case=withnone)
    sec.run("benjamini_hochberg", "BH q-values equal an independent step-up implementation; untestable "
            "configurations still count in the family", bh)

    def pvalue(c: Check) -> None:
        from scipy import stats as sps

        f = fx.flat_path(200)
        m = fx.market(f)
        rng = np.random.default_rng(4)
        out = []
        for n in (5, 30, 120):
            x = rng.normal(0.002, 0.02, n)
            trades = [{"t": i, "net_return": float(v), "gross_return": float(v), "session_id": i, "exit_reason": "TIME",
                       "bars_held": 1, "mfe": 0.0, "mae": 0.0} for i, v in enumerate(x)]
            met = m54bt.metrics(trades, m, (0, 200), 0.0005)
            t_ref, p_ref = R.t_one_sided_p(list(x - 0.0005))
            p_sp = sps.ttest_1samp(x - 0.0005, 0.0, alternative="greater").pvalue
            out.append({"n": n, "t": met["t_stat"], "t_ref": t_ref, "p": met["p_value"], "p_ref": p_ref,
                        "p_scipy_ttest": float(p_sp)})
            c.expect(math.isclose(met["t_stat"], t_ref, rel_tol=1e-9), f"n={n}: t {met['t_stat']} vs {t_ref}")
            c.expect(math.isclose(met["p_value"], p_ref, rel_tol=1e-6, abs_tol=1e-12), f"n={n}: p {met['p_value']} vs {p_ref}")
            eq = np.cumprod(1 + x)
            dd = float((eq / np.maximum.accumulate(np.concatenate(([1.0], eq)))[1:] - 1).min())
            c.expect(math.isclose(met["max_drawdown"], dd, rel_tol=1e-12), "max drawdown")
        c.evidence["cases"] = out
    sec.run("excess_return_t_test", "The one-sided p-value of mean excess return equals an independent Student-t "
            "(incomplete-beta) computation; drawdown is from compounded trade returns", pvalue)

    def gates(c: Check) -> None:
        pol = GatePolicy()
        good = {"effective_n": 20, "q_value": 0.05, "mean_net": 1e-6, "max_drawdown": -0.30,
                "folds": [{"mean_excess": 0.01}] * 3 + [{"mean_excess": -0.01}]}
        cases = [("all gates exactly at the boundary", good, True)]
        for k, v in (("effective_n", 19), ("q_value", 0.0501), ("mean_net", 0.0), ("max_drawdown", -0.3001),
                     ("folds", [{"mean_excess": 0.01}] * 2 + [{"mean_excess": -0.01}] * 2)):
            cases.append((f"{k}={v if k != 'folds' else '2 of 4 positive'}", {**good, k: v}, False))
        for k in good:
            cases.append((f"missing {k} fails closed", {kk: vv for kk, vv in good.items() if kk != k}, False))
        out = []
        for name, met, want in cases:
            got = validation_gates(met, pol)["passed"]
            out.append({"case": name, "passed": got, "expected": want})
            c.expect(got == want, f"{name}: gate result {got}, expected {want}")
        c.expect(not final_gates({"effective_n": 25, "mean_net": -0.001, "max_drawdown": -0.1}, pol)["passed"],
                 "final segment with a negative mean must fail")
        val_ok = derive({"evidence_stage": "VALIDATION", "metrics": good}, pol)["classification"]
        fin_ok = derive({"evidence_stage": "TEST_ONCE", "metrics": {"effective_n": 25, "mean_net": 0.01,
                                                                    "max_drawdown": -0.1, "validation_evidence": good}},
                        pol)["classification"]
        fin_noev = derive({"evidence_stage": "TEST_ONCE", "metrics": {"effective_n": 25, "mean_net": 0.01,
                                                                      "max_drawdown": -0.1}}, pol)
        c.expect(val_ok == "EXPERIMENTAL", f"validation pass alone must be EXPERIMENTAL, got {val_ok}")
        c.expect(fin_ok == "VALIDATED", f"validation + final pass must be VALIDATED, got {fin_ok}")
        c.expect(fin_noev["classification"] != "VALIDATED", "a final score without validation evidence must not "
                                                            "become VALIDATED")
        c.evidence.update(policy=pol.to_dict(), cases=out, validation_only=val_ok, validation_and_final=fin_ok,
                          final_without_validation=fin_noev["classification"])
    sec.run("promotion_gates", "Gates (>=20 trades, BH q<=0.05, positive net, drawdown >= -30%, >=3 of 4 positive "
            "folds) are applied at their exact boundaries, fail closed on missing inputs, and VALIDATED needs both "
            "validation and the once-only final segment", gates)

    def holdout(c: Check) -> None:
        from ..app import research
        from ..app.ingest import SymbolData, window_data
        import pandas as pd

        class S:
            def __init__(self, m56: dict[str, Any]):
                self.m56 = m56

            def section(self, name: str) -> dict[str, Any]:
                return self.m56 if name == "m56" else {}

        class A:
            def __init__(self, m56: dict[str, Any]):
                self.s = S(m56)
        refusals = []
        for m56, end in (({"development_end": "2025-10-20"}, "2025-10-21"), ({}, None)):
            try:
                research.run(A(m56), ["X"], ["1d"], None, catalogue=Path("."), methods=[], out=Path("."), end=end)
                refusals.append({"config": m56, "end": end, "refused": False})
                c.fail(f"research-matrix accepted end={end} with config {m56}")
            except ValueError as exc:
                refusals.append({"config": m56, "end": end, "refused": True, "message": str(exc)})
        daily = pd.DataFrame({"date": ["2025-10-19", "2025-10-20", "2025-10-21", "2026-01-02"], "open": 1.0,
                              "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "source": "x"})
        w = window_data(SymbolData("X-EQ", daily, pd.DataFrame(), "fp"), None, "2025-10-20")
        c.expect(w.daily["date"].max() == "2025-10-20" and w.fingerprint != "fp",
                 "window_data must drop post-boundary rows and re-fingerprint the windowed copy")
        c.evidence.update(refusals=refusals, windowed_last_session=w.daily["date"].max())
    sec.run("holdout_refusal", "research-matrix refuses to run without a holdout boundary or past it, and windowing "
            "removes every post-boundary candle before any computation", holdout)

    def end_to_end(c: Check) -> None:
        p = ev.get("pipeline")
        if p is None:
            c.fail("pipeline run unavailable")
            return
        rows = pl.rows(p["out"])
        c.expect(p["exit_codes"] == [0, 0], f"exit codes {p['exit_codes']}")
        c.expect(all((r["data_end"] or "") <= p["dev_end"] for r in rows), "a matrix row uses data after the boundary")
        c.expect(p["rows_first"] == p["rows_second"], "resume duplicated rows")
        c.expect(p["finals_first"] == p["finals_second"], "resume re-read a final segment")
        lanes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            if r["bh_family_size"] not in (None, "") and r["method"] not in ("tree", "screen"):
                lanes[r["lane_key"]].append(r)
        worst, checked = 0.0, 0
        for fam in lanes.values():
            size = int(fam[0]["bh_family_size"])
            c.expect(size == len(fam), f"BH family size {size} but {len(fam)} rows carry it")
            pv = [float(r["val_p_value"]) if r["val_p_value"] not in (None, "") else None for r in fam]
            qq = m54bt.bh_adjust(pv, size)
            idx = [i for i, v in enumerate(pv) if v is not None]
            ref = R.bh([pv[i] for i in idx] + [1.0] * (len(pv) - len(idx)), size)
            for j, i in enumerate(idx):
                stored = float(fam[i]["val_q_value"])
                worst = max(worst, abs(stored - ref[j]), abs(stored - qq[i]))
                checked += 1
        c.expect(worst < 1e-9, f"stored q-values differ from independent BH by {worst}")
        bad_cls = []
        for r in rows:
            if r["classification"] in ("EXPERIMENTAL", "VALIDATED"):
                ok = (float(r["val_trades"] or 0) >= 20 and float(r["val_q_value"] or 1) <= 0.05
                      and float(r["val_mean_net"] or 0) > 0 and float(r["val_max_drawdown"] or -1) >= -0.3)
                if not ok:
                    bad_cls.append(r["strategy_id"])
            if r["status"] != "TESTED" and r["classification"] != "NOT_TESTED":
                bad_cls.append(r["strategy_id"])
        c.expect(not bad_cls, f"classification inconsistent with recomputed gates: {bad_cls[:10]}")
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            counts[f"{r['source']}/{r['method']}/{r['classification']}"] += 1
        c.evidence.update(command="gplm " + " ".join(p["args"][:13]) + " ...", rows=len(rows),
                          q_values_rechecked=checked, max_q_diff=worst, final_reads=p["finals_first"],
                          classifications=dict(counts), development_end=p["dev_end"])
        c.note(f"{len(rows)} matrix rows; {checked} stored q-values recomputed independently; final-segment reads "
               f"{len(p['finals_first'])} (unchanged on resume)")
    sec.run("end_to_end_research_matrix", "A real research-matrix run on synthetic raw files respects the boundary, "
            "stores q-values that an independent BH reproduces, classifies consistently with the gates, and on "
            "resume neither duplicates rows nor re-reads the final segment", end_to_end)
