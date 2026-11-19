"""Section 3: signal generation, next-bar execution, exits, costs and future-data leakage."""
from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from ..app import strategy_dsl as dsl
from ..app.strategy_engine import Exit, LaneInputs, evaluate_strategy, simulate
from ..app.strategy_library import StrategyLibrary
from ..m54 import backtest as m54bt
from . import fixtures as fx
from .core import Check, Section

REPO = Path(__file__).resolve().parents[3]
BUY, SELL = 0.0010, 0.0020


def _net_long(entry: float, exit_: float) -> float:
    return exit_ * (1 - SELL) / (entry * (1 + BUY)) - 1


def _net_short(entry: float, exit_: float) -> float:
    return (entry * (1 - SELL) - exit_ * (1 + BUY)) / entry


def _scenario(edit: dict[int, dict[str, float]], exit_: dict[str, Any], signals: list[int], short: bool = False,
              n: int = 60, xbars: list[int] | None = None, seg: tuple[int, int] | None = None,
              blocked: list[int] | None = None, no_signal: list[int] | None = None) -> tuple[list[dict[str, Any]], Any]:
    f = fx.flat_path(n)
    for i, cols in edit.items():
        for k, v in cols.items():
            f.loc[i, k] = v
    bl = np.zeros(n, bool)
    bl[blocked or []] = True
    ns = np.zeros(n, bool)
    ns[no_signal or []] = True
    m = fx.market(f, 0.02, bl, ns)
    mask = np.zeros(n, bool)
    mask[signals] = True
    xm = None
    if xbars is not None:
        xm = np.zeros(n, bool)
        xm[xbars] = True
    ex = Exit.of({"max_hold_bars": 5, "stop_atr": None, "target_atr": None, "trail_atr": None, **exit_})
    return simulate(m, mask, seg or (0, n), ex, BUY, SELL, short, xm), f


SCENARIOS: list[dict[str, Any]] = [
    {"name": "next_bar_open_entry_and_time_exit", "signals": [10], "edit": {11: {"open": 101}, 16: {"open": 103}},
     "exit": {}, "expect": {"t": 10, "entry_price": 101, "exit_index": 16, "exit_price": 103, "exit_reason": "TIME",
                            "net_return": _net_long(101, 103)},
     "why": "signal at the close of bar 10 fills at the open of bar 11; a 5-bar hold exits at the open of bar 16"},
    {"name": "stop_loss_intrabar", "signals": [10], "edit": {13: {"low": 96.5}}, "exit": {"stop_atr": 1.5},
     "expect": {"exit_index": 13, "exit_price": 97.0, "exit_reason": "STOP", "net_return": _net_long(100, 97)},
     "why": "ATR = 2% x 100 = 2; stop 1.5 ATR = 97, touched by bar 13's low 96.5, filled at the stop"},
    {"name": "stop_loss_gap_fills_at_worse_open", "signals": [10], "edit": {13: {"open": 95, "low": 94, "high": 95.5}},
     "exit": {"stop_atr": 1.5}, "expect": {"exit_index": 13, "exit_price": 95.0, "exit_reason": "STOP_GAP"},
     "why": "bar 13 opens at 95, below the 97 stop, so the fill is the (worse) open, not the stop price"},
    {"name": "profit_target", "signals": [10], "edit": {12: {"high": 104.5}}, "exit": {"target_atr": 2.0},
     "expect": {"exit_index": 12, "exit_price": 104.0, "exit_reason": "TARGET", "net_return": _net_long(100, 104)},
     "why": "target 2 ATR = 104, reached by bar 12's high"},
    {"name": "stop_before_target_same_bar", "signals": [10], "edit": {12: {"high": 105, "low": 96}},
     "exit": {"stop_atr": 1.5, "target_atr": 2.0}, "expect": {"exit_index": 12, "exit_price": 97.0, "exit_reason": "STOP"},
     "why": "bar 12 touches both levels; intrabar order is unknown, so the pessimistic stop is assumed"},
    {"name": "trailing_stop_ratchets", "signals": [10], "edit": {12: {"high": 106}, 13: {"open": 105, "high": 105.5,
                                                                                         "low": 103.5}},
     "exit": {"trail_atr": 1.0}, "expect": {"exit_index": 13, "exit_price": 104.0, "exit_reason": "TRAIL"},
     "why": "high-water 106 after bar 12 moves the 1-ATR trail to 104; bar 13 trades down through it"},
    {"name": "signal_exit_next_open", "signals": [10], "edit": {14: {"open": 102}}, "exit": {"max_hold_bars": 10},
     "xbars": [13], "expect": {"exit_index": 14, "exit_price": 102.0, "exit_reason": "SIGNAL_EXIT"},
     "why": "exit condition true at the close of bar 13 fills at the open of bar 14"},
    {"name": "short_mirror", "signals": [10], "short": True, "edit": {11: {"open": 101}, 16: {"open": 99}}, "exit": {},
     "expect": {"entry_price": 101, "exit_price": 99, "exit_reason": "TIME", "net_return": _net_short(101, 99)},
     "why": "short: sells at 101 less costs, buys back at 99 plus costs"},
    {"name": "short_stop_above_entry", "signals": [10], "short": True, "edit": {12: {"high": 103.5}},
     "exit": {"stop_atr": 1.5}, "expect": {"exit_index": 12, "exit_price": 103.0, "exit_reason": "STOP"},
     "why": "short stop sits 1.5 ATR above the 100 entry"},
    {"name": "no_overlapping_trades", "signals": [10, 11, 12, 15, 16], "edit": {}, "exit": {},
     "expect_trades": [10, 16], "why": "a trade from bar 10 exits at bar 16; signals at 11, 12, 15 are ignored"},
    {"name": "trade_must_finish_inside_segment", "signals": [10, 52], "edit": {}, "exit": {}, "seg": (0, 55),
     "expect_trades": [10], "why": "signal 52 would exit at bar 58, beyond the segment end 54: purged"},
    {"name": "last_bar_signal_cannot_trade", "signals": [59], "edit": {}, "exit": {}, "expect_trades": [],
     "why": "no next bar to fill"},
    {"name": "discontinuity_bars_block_paths", "signals": [10, 20], "edit": {}, "exit": {}, "blocked": [13],
     "expect_trades": [20], "why": "the path of the bar-10 trade touches flagged bar 13 (suspected corporate action)"},
    {"name": "suppressed_signals_ignored", "signals": [10], "edit": {}, "exit": {}, "no_signal": [10],
     "expect_trades": [], "why": "bars flagged no_signal (incomplete/warm-up after a discontinuity) never fire"},
]


def run(sec: Section, ev: dict[str, Any]) -> None:
    ledger: list[dict[str, Any]] = []
    ev["engine_trades"] = ledger

    for sc in SCENARIOS:
        def one(c: Check, sc: dict[str, Any] = sc) -> None:
            trades, _ = _scenario(sc["edit"], sc["exit"], sc["signals"], sc.get("short", False),
                                  xbars=sc.get("xbars"), seg=sc.get("seg"), blocked=sc.get("blocked"),
                                  no_signal=sc.get("no_signal"))
            c.evidence.update(why=sc["why"], trades=trades)
            for t in trades:
                ledger.append({"scenario": sc["name"], **{k: t[k] for k in ("t", "exit_index", "entry_price",
                                                                            "exit_price", "exit_reason",
                                                                            "gross_return", "net_return")}})
            if "expect_trades" in sc:
                got = [t["t"] for t in trades]
                c.expect(got == sc["expect_trades"], f"trade signal bars {got}, expected {sc['expect_trades']}")
                return
            if not c.expect(len(trades) == 1, f"{len(trades)} trades, expected 1"):
                return
            t = trades[0]
            for k, want in sc["expect"].items():
                got = t[k]
                ok = got == want if isinstance(want, str) else math.isclose(float(got), float(want), rel_tol=1e-12,
                                                                            abs_tol=1e-12)
                c.expect(ok, f"{k}: got {got}, expected {want}")
        sec.run(f"fill:{sc['name']}", sc["why"], one)

    def costs(c: Check) -> None:
        cfg = json.loads((REPO / "config" / "app" / "gplm.json").read_text(encoding="utf-8"))["costs"]
        out = {}
        sched = cfg.get("schedule")
        for prof, d in cfg.items():
            if prof == "schedule":
                continue
            got = m54bt.Costs.from_bps(d)
            want_b = (d["commission_bps"] + d["slippage_bps"] + d["buy_statutory_bps"]) / 1e4
            want_s = (d["commission_bps"] + d["slippage_bps"] + d["sell_statutory_bps"]) / 1e4
            out[prof] = {"buy": got.buy, "sell": got.sell, "round_trip_bps": (got.buy + got.sell) * 1e4,
                         "basis": d.get("basis")}
            c.expect(math.isclose(got.buy, want_b) and math.isclose(got.sell, want_s), f"{prof}: cost mismatch")
        if sched and sched.get("enabled", True):
            # M6.0: the compiled per-side rates must equal the itemised contract-note legs at the assumed order value
            from stock_gplm.app.costs import lane_costs, leg

            v = float(sched["assumed_order_value_inr"])
            for prof in ("delivery", "intraday"):
                lc, det = lane_costs(cfg, prof)
                slip = det["slippage_bps"]
                want_b = leg(sched, prof, "BUY", v, slip).total / v
                want_s = leg(sched, prof, "SELL", v, slip).total / v
                c.expect(math.isclose(lc.buy, want_b, rel_tol=1e-12) and math.isclose(lc.sell, want_s, rel_tol=1e-12),
                         f"schedule/{prof}: compiled rates differ from the itemised legs")
                out[f"schedule/{prof}"] = {"buy": lc.buy, "sell": lc.sell, "round_trip_bps": (lc.buy + lc.sell) * 1e4,
                                           "basis": det.get("liquidity_bucket")}
        trades, _ = _scenario({}, {}, [10])
        c.expect(math.isclose(trades[0]["net_return"], (1 - SELL) / (1 + BUY) - 1),
                 "a flat round trip must lose exactly the two cost legs")
        c.expect(trades[0]["gross_return"] == 0.0, "a flat round trip has zero gross return")
        c.evidence.update(profiles=out, flat_round_trip_net=trades[0]["net_return"])
        c.note(f"delivery round trip {out['delivery']['round_trip_bps']:.2f} bps, intraday "
               f"{out['intraday']['round_trip_bps']:.2f} bps (configured ASSUMPTION, not broker-verified)")
    sec.run("costs_applied_both_legs", "Configured costs (commission + slippage + statutory) are charged on both legs; "
            "a flat round trip loses exactly the costs", costs)

    def parity(c: Check) -> None:
        f = fx.ohlcv(800)
        m = fx.market(f, fx.natr_of(f))
        rng = np.random.default_rng(5)
        mask = rng.random(len(f)) < 0.08
        diffs = 0
        n_tr = 0
        for hold, stop, tgt, trail in itertools.product((3, 10), (None, 1.5), (None, 2.5), (None, 2.0)):
            a = simulate(m, mask, (0, len(f)), Exit(hold, stop, tgt, trail), BUY, SELL)
            b = m54bt.simulate(m, mask, (0, len(f)), m54bt.ExitPolicy(hold, stop, tgt, trail), m54bt.Costs(BUY, SELL))
            n_tr += len(a)
            ka = [(t["t"], t["exit_index"], round(t["net_return"], 12), t["exit_reason"]) for t in a]
            kb = [(t["t"], t["exit_index"], round(t["net_return"], 12), t["exit_reason"]) for t in b]
            if ka != kb:
                diffs += 1
                c.fail(f"hold={hold} stop={stop} target={tgt} trail={trail}: M5.7 and M5.4 simulators disagree")
        c.evidence.update(exit_policies=16, trades_compared=n_tr, disagreements=diffs)
    sec.run("simulator_parity_m57_vs_m54", "The M5.7 engine and the independent M5.4 backtester produce identical "
            "trades for 16 exit policies on 800 random bars", parity)

    def primitives(c: Check) -> None:
        daily, intra = fx.ohlcv(700), fx.intraday()
        out, untested = [], []
        for name in sorted(dsl.FUNCTIONS):
            if name in dsl.LAZY or name in dsl.FEATURE_FUNCS:
                untested.append({"function": name, "reason": "joins another series/timeframe as-of; covered by the "
                                                             "as-of alignment check"})
                continue
            frame, tf = (intra, "15m") if name in dsl.INTRADAY_ONLY else (daily, "1d")
            expr = _call_for(name, frame, tf)
            if expr is None:
                untested.append({"function": name, "reason": "no argument template evaluated"})
                continue
            res = causal_check(expr, frame, tf)
            res["function"] = name
            out.append(res)
            if res["leaks"]:
                c.fail(f"{name}: {expr} changes past values when future bars change (first bar {res['first_leak']})")
        c.evidence.update(tested=out, untested=untested)
        c.note(f"{len(out)} primitives tested for causality by truncation and future perturbation; "
               f"{len(untested)} not directly ({', '.join(u['function'] for u in untested)})")
        if any(u["reason"].startswith("no argument") for u in untested):
            c.warn("some primitives could not be exercised: " + ", ".join(u["function"] for u in untested
                                                                          if u["reason"].startswith("no argument")))
    sec.run("every_primitive_is_causal", "No DSL primitive reads the future: values up to bar t are identical when "
            "later bars are removed or randomly altered", primitives)

    def asof(c: Check) -> None:
        from ..m54.timeframes import align

        end = np.array([10, 20, 30, 40, 50], dtype=np.int64)
        avail = np.array([15, 35, 55], dtype=np.int64)
        vals = np.array([1.0, 2.0, 3.0])
        got = align(end, avail, vals)
        want = [math.nan, 1.0, 1.0, 2.0, 2.0]
        ok = all(fx.isclose_or_nan(float(a), b) for a, b in zip(got, want))
        c.evidence.update(lane_bar_end=end, other_series_available=avail, values=vals, aligned=got, expected=want)
        c.expect(ok, f"as-of join returned {got.tolist()}, expected {want}: a value may be used only after it is "
                     "available")
    sec.run("asof_alignment_no_lookahead", "Higher-timeframe and benchmark values join a lane only after their "
            "availability time", asof)

    def library_causal(c: Check) -> None:
        lib = StrategyLibrary([REPO / "config" / "strategies" / "library"], Path(ev["work"]) / "lib-causal").load(
            record=False)
        f = fx.ohlcv(900, fx.SEED + 11)
        tested = leaks = 0
        bad = []
        for s in lib.strategies.values():
            if s.blocked or "1d" not in s.timeframes or s.needs.get("benchmark") or s.needs.get("features") \
                    or s.needs.get("htf"):
                continue
            for text in [s.doc["entry"]] + ([s.doc["exit"]["when"]] if s.doc["exit"].get("when") else []):
                r = causal_check(text, f, "1d", s.defaults(), cuts=(500, 700))
                tested += 1
                if r["leaks"]:
                    leaks += 1
                    bad.append({"id": s.id, "expr": text, "first_leak": r["first_leak"]})
        c.evidence.update(rules_tested=tested, leaking=bad)
        c.expect(not bad, f"{leaks} library rules leak future data: {[b['id'] for b in bad[:10]]}")
        c.note(f"{tested} entry/exit expressions of shipped strategies tested (daily lane)")
    sec.run("every_library_rule_is_causal", "Every executable shipped strategy's entry and exit signal is "
            "unchanged when future bars are removed or altered", library_causal)

    def parser_safety(c: Check) -> None:
        ctx = fx.ctx(fx.ohlcv(300))
        bad = ["__import__('os').system('x')", "close[-1] > close", "open.__class__", "eval('1')",
               "close > lowest(low, -5)", "exec"]
        out = []
        for b in bad:
            try:
                dsl.signal(b, ctx)
                out.append({"expression": b, "rejected": False})
                c.fail(f"accepted unsafe or future-reading expression {b!r}")
            except dsl.DSLError as exc:
                out.append({"expression": b, "rejected": True, "error": str(exc)})
        c.evidence["attempts"] = out
    sec.run("parser_rejects_code_and_negative_lags", "Expressions are parsed, never evaluated as Python; negative "
            "lags (future bars) are rejected", parser_safety)

    def engine_future(c: Check) -> None:
        f = fx.ohlcv(1000)
        g = f.copy()
        cut = 700
        rng = np.random.default_rng(1)
        for k in ("open", "high", "low", "close"):
            g.loc[cut:, k] = g.loc[cut:, k] * np.exp(rng.normal(0, 0.05, len(g) - cut))
        g["high"] = g[["open", "high", "low", "close"]].max(axis=1)
        g["low"] = g[["open", "high", "low", "close"]].min(axis=1)
        out = []
        for expr, ex in (("cross_above(ema(close, 9), ema(close, 21))", {"max_hold_bars": 10, "stop_atr": 2.0}),
                         ("rsi(close, 2) < 10 and close > sma(close, 200)", {"max_hold_bars": 5, "trail_atr": 2.0})):
            res = []
            for frame in (f, g):
                m = fx.market(frame, fx.natr_of(frame))
                mask = dsl.signal(expr, fx.ctx(frame))
                tr = simulate(m, mask, (0, len(frame)), Exit.of({"max_hold_bars": 5, **ex}), BUY, SELL)
                res.append([(t["t"], t["exit_index"], round(t["net_return"], 12)) for t in tr if t["exit_index"] < cut])
            out.append({"rule": expr, "trades_closed_before_cut": len(res[0]), "identical": res[0] == res[1]})
            c.expect(res[0] == res[1], f"{expr}: trades closed before bar {cut} change when later bars change")
        c.evidence["rules"] = out
    sec.run("backtest_future_invariance", "Trades that close before bar t are identical when every bar after t is "
            "randomly altered", engine_future)


TEMPLATES = [(), ("close",), ("14",), ("close", "14"), ("close > open", "14"), ("close > open", "close"),
             ("close", "close", "20"), ("close", "14", "2"), ("14", "3"), ("10", "3"), ("close", "12", "26"),
             ("close", "12", "26", "9"), ("30", "120"), ("15",), ("5",), ("10", "2", "30"), ("close", "close")]


def _call_for(name: str, frame: Any, tf: str) -> str | None:
    lo, hi, _ = dsl.FUNCTIONS[name]
    ctx = dsl.Context.from_frame(frame, tf, tf != "1d")
    for args in TEMPLATES:
        if not lo <= len(args) <= hi:
            continue
        expr = f"{name}({', '.join(args)})"
        try:
            v = dsl.evaluate(dsl.parse(expr), ctx)
        except Exception:
            continue
        v = np.asarray(v, dtype=float)
        if v.shape == (len(frame),) and np.isfinite(v).any():
            return expr
    return None


def causal_check(expr: str, frame: Any, tf: str, params: dict[str, Any] | None = None,
                 cuts: tuple[int, ...] | None = None) -> dict[str, Any]:
    """Truncate after each cut and also randomise every later bar; values up to the cut must not move."""
    n = len(frame)
    intr = tf != "1d"
    cuts = cuts or (n // 2, (3 * n) // 4)
    node = dsl.compile_text(expr, params or {})
    full = np.asarray(dsl.evaluate(node, dsl.Context.from_frame(frame, tf, intr)), dtype=float)
    rng = np.random.default_rng(99)
    first: int | None = None
    for cut in cuts:
        trunc = frame.iloc[:cut].copy()
        alt = frame.copy()
        mult = np.exp(rng.normal(0, 0.05, n - cut))
        for k in ("open", "high", "low", "close"):
            alt.loc[alt.index[cut:], k] = alt[k].to_numpy()[cut:] * mult
        alt.loc[alt.index[cut:], "volume"] = alt["volume"].to_numpy()[cut:] * rng.uniform(0.2, 5, n - cut)
        for variant in (trunc, alt):
            got = np.asarray(dsl.evaluate(node, dsl.Context.from_frame(variant, tf, intr)), dtype=float)[:cut]
            ref = full[:cut]
            same = (np.isnan(got) & np.isnan(ref)) | np.isclose(got, ref, rtol=1e-9, atol=1e-12)
            if not same.all():
                k = int(np.nonzero(~same)[0][0])
                first = k if first is None else min(first, k)
    return {"expression": expr, "cuts": list(cuts), "leaks": first is not None, "first_leak": first}
