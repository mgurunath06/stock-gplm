"""Section 2: indicators against independent reference implementations, edge cases and the M5.4 path."""
from __future__ import annotations

import math
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from ..app import strategy_dsl as dsl
from . import fixtures as fx
from . import reference as R
from .core import Check, Section

Ref = Callable[[Mapping[str, list[float]], str], list[float]]

# name, DSL expression, reference(cols, seed), recursive (seed-dependent), period for warm-up scale
INDICATORS: list[tuple[str, str, Ref, bool, int]] = [
    ("SMA(20)", "sma(close, 20)", lambda d, s: R.sma(d["c"], 20), False, 20),
    ("SMA(200)", "sma(close, 200)", lambda d, s: R.sma(d["c"], 200), False, 200),
    ("EMA(12)", "ema(close, 12)", lambda d, s: R.ema(d["c"], 12, s), True, 12),
    ("EMA(200)", "ema(close, 200)", lambda d, s: R.ema(d["c"], 200, s), True, 200),
    ("WMA(10)", "wma(close, 10)", lambda d, s: R.wma(d["c"], 10), False, 10),
    ("HMA(21)", "hma(close, 21)", lambda d, s: R.hma(d["c"], 21), False, 21),
    ("RMA(14)", "rma(close, 14)", lambda d, s: R.rma(d["c"], 14, s), True, 14),
    ("RSI(14)", "rsi(close, 14)", lambda d, s: R.rsi(d["c"], 14, s), True, 14),
    ("RSI(2)", "rsi(close, 2)", lambda d, s: R.rsi(d["c"], 2, s), True, 2),
    ("TrueRange", "tr", lambda d, s: R.true_range(d["h"], d["l"], d["c"]), False, 1),
    ("ATR(14)", "atr(14)", lambda d, s: R.atr(d["h"], d["l"], d["c"], 14, s), True, 14),
    ("MACD line(12,26)", "macd(close, 12, 26)", lambda d, s: R.macd(d["c"], 12, 26, 9, s)[0], True, 26),
    ("MACD signal(12,26,9)", "macd_signal(close, 12, 26, 9)", lambda d, s: R.macd(d["c"], 12, 26, 9, s)[1], True, 35),
    ("MACD hist(12,26,9)", "macd_hist(close, 12, 26, 9)", lambda d, s: R.macd(d["c"], 12, 26, 9, s)[2], True, 35),
    ("ADX(14)", "adx(14)", lambda d, s: R.dmi(d["h"], d["l"], d["c"], 14, s)[0], True, 28),
    ("+DI(14)", "plus_di(14)", lambda d, s: R.dmi(d["h"], d["l"], d["c"], 14, s)[1], True, 14),
    ("-DI(14)", "minus_di(14)", lambda d, s: R.dmi(d["h"], d["l"], d["c"], 14, s)[2], True, 14),
    ("Stoch %K(14,3)", "stoch_k(14, 3)", lambda d, s: R.stoch_k(d["h"], d["l"], d["c"], 14, 3), False, 16),
    ("Williams %R(14)", "willr(14)", lambda d, s: R.willr(d["h"], d["l"], d["c"], 14), False, 14),
    ("CCI(20)", "cci(20)", lambda d, s: R.cci(d["h"], d["l"], d["c"], 20), False, 20),
    ("MFI(14)", "mfi(14)", lambda d, s: R.mfi(d["h"], d["l"], d["c"], d["v"], 14), False, 14),
    ("OBV", "obv", lambda d, s: R.obv(d["c"], d["v"]), False, 1),
    ("CMF(20)", "cmf(20)", lambda d, s: R.cmf(d["h"], d["l"], d["c"], d["v"], 20), False, 20),
    ("VWMA(20)", "vwma(close, 20)", lambda d, s: R.vwma(d["c"], d["v"], 20), False, 20),
    ("Bollinger upper(20,2)", "bb_upper(close, 20, 2)", lambda d, s: R.bollinger(d["c"], 20, 2)[0], False, 20),
    ("Bollinger lower(20,2)", "bb_lower(close, 20, 2)", lambda d, s: R.bollinger(d["c"], 20, 2)[2], False, 20),
    ("Bollinger %B(20,2)", "pctb(close, 20, 2)", lambda d, s: R.pctb(d["c"], 20, 2), False, 20),
    ("StdDev(20)", "stdev(close, 20)", lambda d, s: R.stdev(d["c"], 20), False, 20),
    ("ZScore(20)", "zscore(close, 20)", lambda d, s: R.zscore(d["c"], 20), False, 20),
    ("Highest(high,20)", "highest(high, 20)", lambda d, s: R.highest(d["h"], 20), False, 20),
    ("Lowest(low,20)", "lowest(low, 20)", lambda d, s: R.lowest(d["l"], 20), False, 20),
    ("ROC(10)", "roc(close, 10)", lambda d, s: R.roc(d["c"], 10), False, 10),
    ("Sum(volume,5)", "sum(volume, 5)", lambda d, s: R.rsum(d["v"], 5), False, 5),
    ("LinReg slope(20)", "slope(close, 20)", lambda d, s: R.ols_slope(d["c"], 20), False, 20),
    ("PercentRank(20)", "percentrank(close, 20)", lambda d, s: R.percentrank(d["c"], 20), False, 20),
    ("Supertrend(10,3)", "supertrend(10, 3)", lambda d, s: R.supertrend(d["h"], d["l"], d["c"], 10, 3, s)[0], True, 10),
    ("Supertrend dir(10,3)", "supertrend_dir(10, 3)", lambda d, s: R.supertrend(d["h"], d["l"], d["c"], 10, 3, s)[1],
     True, 10),
    ("EMA(9)xEMA(21) cross", "cross_above(ema(close, 9), ema(close, 21))",
     lambda d, s: [float(b) for b in R.cross_above(R.ema(d["c"], 9, s), R.ema(d["c"], 21, s))], True, 21),
]


def cols(frame: pd.DataFrame) -> dict[str, list[float]]:
    return {"o": frame["open"].tolist(), "h": frame["high"].tolist(), "l": frame["low"].tolist(),
            "c": frame["close"].tolist(), "v": frame["volume"].tolist()}


def dsl_values(expr: str, frame: pd.DataFrame) -> np.ndarray:
    out = dsl.evaluate(dsl.parse(expr), fx.ctx(frame))
    return np.asarray(out, dtype=float)


def _sample(frame: pd.DataFrame, a: np.ndarray, b: np.ndarray, bars: list[int]) -> list[dict[str, Any]]:
    return [{"bar": i, "session": frame["session"].iloc[i], "close": round(float(frame["close"].iloc[i]), 6),
             "gplm": float(a[i]), "reference": float(b[i])} for i in bars if 0 <= i < len(a)]


def compare(name: str, expr: str, ref: Ref, recursive: bool, period: int, frame: pd.DataFrame
            ) -> dict[str, Any]:
    d = cols(frame)
    got = dsl_values(expr, frame)
    want = np.asarray(ref(d, "sma"), dtype=float)
    err = fx.rel_err(got, want)
    nan_mis = int((np.isfinite(got) != np.isfinite(want)).sum())
    mis_bars = np.nonzero(np.isfinite(got) != np.isfinite(want))[0]
    inf = int(np.isinf(got).sum())
    n = len(frame)
    row: dict[str, Any] = {"indicator": name, "expression": expr, "bars": n, "max_rel_err": err,
                           "nan_pattern_mismatches": nan_mis,
                           "nan_mismatch_bars": mis_bars[:10].tolist(), "infinite_values": inf,
                           "finite_bars": int(np.isfinite(got).sum())}
    fin = np.nonzero(np.isfinite(want))[0]
    probe = sorted({int(fin[0]) if fin.size else 0, n // 2, n - 1})
    row["sample"] = _sample(frame, got, want, probe)
    if recursive:
        v1 = np.asarray(ref(d, "first"), dtype=float)  # DSL v1 (first-bar seed), to quantify the change
        row.update(textbook_max_rel_dev=fx.rel_err(got, want), textbook_converged_1e3=fx.first_converged(got, want, 1e-3),
                   textbook_converged_1e6=fx.first_converged(got, want, 1e-6), v1_max_rel_dev=fx.rel_err(v1, want),
                   v1_converged_1e3=fx.first_converged(v1, want, 1e-3))
    return row


def verdict_for(row: Mapping[str, Any], c: Check) -> None:
    name = row["indicator"]
    if row["infinite_values"]:
        c.fail(f"{name}: {row['infinite_values']} infinite values (must be NaN)")
    late = [b for b in row["nan_mismatch_bars"] if b >= 3 * max(2, _period(name))]
    if row["nan_pattern_mismatches"] and late:
        c.fail(f"{name}: defined/undefined bars differ from the reference after warm-up at bars {late}")
    elif row["nan_pattern_mismatches"]:
        c.warn(f"{name}: warm-up length differs from the textbook reference on {row['nan_pattern_mismatches']} "
               f"bar(s) {row['nan_mismatch_bars']}")
    if row["max_rel_err"] > 1e-8:
        c.fail(f"{name}: max relative error {row['max_rel_err']:.3g} vs reference (tolerance 1e-8)")


def _period(name: str) -> int:
    for n_, _, _, _, p in INDICATORS:
        if n_ == name:
            return p
    return 20


def run(sec: Section, ev: dict[str, Any]) -> None:
    main = fx.ohlcv()
    rows: list[dict[str, Any]] = []
    ev["indicator_rows"] = rows

    def hand(c: Check) -> None:
        f = fx.frame_from([10, 11, 12, 13, 14], [10.5, 11.5, 12.5, 13.5, 14.5], [9.5, 10.5, 11.5, 12.5, 13.5],
                          [10, 11, 12, 13, 14])
        cases = [
            ("sma(close, 3)", [math.nan, math.nan, 11.0, 12.0, 13.0]),
            ("ema(close, 3)", [math.nan, math.nan, 11.0, 12.0, 13.0]),  # TA-Lib: seed SMA(10,11,12)=11, alpha 0.5
            ("highest(high, 2)", [math.nan, 11.5, 12.5, 13.5, 14.5]),
            ("tr", [math.nan, 1.5, 1.5, 1.5, 1.5]),  # TA-Lib: undefined without a previous close
            ("roc(close, 2)", [math.nan, math.nan, 0.2, 13 / 11 - 1, 14 / 12 - 1]),
        ]
        f2 = fx.frame_from([10, 11, 10, 12], [10, 11, 10, 12], [10, 11, 10, 12], [10, 11, 10, 12])
        out = []
        for expr, want in cases:
            got = dsl_values(expr, f)
            ok = all(fx.isclose_or_nan(float(a), b) for a, b in zip(got, want))
            out.append({"expression": expr, "gplm": got.tolist(), "hand_computed": want, "ok": ok})
            c.expect(ok, f"{expr}: got {got.tolist()} expected {want}")
        got = dsl_values("rsi(close, 2)", f2)
        want = [math.nan, math.nan, 50.0, 100 - 100 / 6]  # gains 1,0,2 / losses 0,1,0, Wilder alpha 1/2
        ok = all(fx.isclose_or_nan(float(a), b) for a, b in zip(got, want))
        out.append({"expression": "rsi(close, 2) on 10,11,10,12", "gplm": got.tolist(), "hand_computed": want,
                    "ok": ok})
        c.expect(ok, f"rsi(close,2): got {got.tolist()} expected {want}")
        c.evidence["cases"] = out
    sec.run("hand_computed_values", "Small hand-computed examples reproduce exactly (SMA, EMA, rolling max, "
            "true range, ROC, RSI)", hand)

    for name, expr, ref, rec, period in INDICATORS:
        def one(c: Check, name: str = name, expr: str = expr, ref: Ref = ref, rec: bool = rec, period: int = period
                ) -> None:
            row = compare(name, expr, ref, rec, period, main)
            rows.append(row)
            c.evidence.update(row)
            verdict_for(row, c)
            if rec:
                c.note(f"TA-Lib/Wilder SMA seeding from the first bar (the reference uses it); the DSL v1 first-bar "
                       f"seed differed by up to {100 * row['v1_max_rel_dev']:.2f}% until bar {row['v1_converged_1e3']}")
        sec.run(f"indicator:{name}", f"{expr} equals an independent pure-Python reference on a 1,500-bar fixture",
                one)

    def edges(c: Check) -> None:
        out = []
        warm: dict[str, list[str]] = {}
        for case, frame in fx.edge_cases().items():
            for name, expr, ref, rec, period in INDICATORS:
                try:
                    row = compare(name, expr, ref, rec, period, frame)
                except Exception as exc:
                    c.fail(f"{case}/{name}: raised {type(exc).__name__}: {exc}")
                    continue
                row["case"] = case
                out.append({k: row[k] for k in ("case", "indicator", "max_rel_err", "nan_pattern_mismatches",
                                                "infinite_values", "finite_bars")})
                if row["infinite_values"]:
                    c.fail(f"{case}/{name}: infinite values")
                if row["max_rel_err"] > 1e-8:
                    c.fail(f"{case}/{name}: max relative error {row['max_rel_err']:.3g}")
                if row["nan_pattern_mismatches"]:
                    warm.setdefault(name, []).append(f"{case}{row['nan_mismatch_bars']}")
        for name, where in warm.items():
            c.warn(f"{name}: defined on a bar where the textbook is still warming up, in {len(where)} case(s): "
                   + ", ".join(where))
        cases = fx.edge_cases()
        expect = [("constant", "rsi(close, 14)", 50.0, "flat prices: RSI 50 (no gains, no losses)"),
                  ("monotonic_up", "rsi(close, 14)", 100.0, "only gains: RSI 100"),
                  ("monotonic_down", "rsi(close, 14)", 0.0, "only losses: RSI 0"),
                  ("constant", "stoch_k(14, 3)", math.nan, "zero range: %K undefined (NaN), never a fake 0/100"),
                  ("constant", "zscore(close, 20)", math.nan, "zero dispersion: z-score undefined"),
                  ("zero_volume", "vwma(close, 20)", math.nan, "zero volume: VWMA undefined"),
                  ("zero_volume", "cmf(20)", math.nan, "zero volume: CMF undefined"),
                  ("short_series", "sma(close, 14)", math.nan, "fewer bars than the period: undefined"),
                  ("short_series", "rsi(close, 14)", math.nan, "fewer bars than the period: undefined")]
        sem = []
        for case, expr, want, why in expect:
            got = float(dsl_values(expr, cases[case])[-1])
            ok = fx.isclose_or_nan(got, want)
            sem.append({"case": case, "expression": expr, "last_value": got, "expected": want, "why": why, "ok": ok})
            c.expect(ok, f"{case}: {expr} last value {got} expected {want} ({why})")
        mz = float(dsl_values("mfi(14)", cases["zero_volume"])[-1])
        sem.append({"case": "zero_volume", "expression": "mfi(14)", "last_value": mz, "expected": math.nan,
                    "why": "no money flow at all: undefined, never 100", "ok": math.isnan(mz)})
        c.expect(math.isnan(mz), f"zero_volume: mfi(14) = {mz}; a window without money flow must be NaN")
        sp = dsl_values("rsi(close, 14)", cases["single_spike"])
        c.expect(bool(np.all(np.isfinite(sp[15:]))), "single spike: RSI must stay finite")
        c.evidence.update(cases=out, semantic_expectations=sem)
    sec.run("edge_cases", "Constant, monotonic, alternating, single-spike, zero-volume and too-short series: no "
            "infinities, reference agreement, documented undefined values", edges)

    def m54(c: Check) -> None:
        """The M5.4 feature path (used by tree discovery and the stop/target ATR) is a second implementation."""
        from ..m54 import indicators as m54i

        f = main.reset_index(drop=True)
        pairs = [("RSI(14)", m54i._rsi(f, 14)["value"], "rsi(close, 14)", 14),
                 ("ATR(14)/close (stop & target distance)", m54i._atr(f, 14) / f["close"], "atr(14) / close", 14),
                 ("MACD line/close", m54i._macd(f, 12, 26, 9)["line_norm"], "macd(close, 12, 26) / close", 26),
                 ("ADX(14)", m54i._adx(f, 14)["adx"], "adx(14)", 28),
                 ("EMA(50) slope source", m54i._ema(f["close"], 50), "ema(close, 50)", 50)]
        out = []
        for name, a, expr, p in pairs:
            g = dsl_values(expr, main)
            a = np.asarray(a, float)
            k = fx.first_converged(g, a, 1e-6)
            dev = fx.rel_err(g, a)
            late = fx.rel_err(g[20 * p:], a[20 * p:])
            out.append({"indicator": name, "max_rel_dev": dev, "converged_1e6_from_bar": k,
                        "max_rel_dev_after_20x_period": late})
            c.expect(late < 1e-6, f"{name}: DSL and M5.4 disagree after warm-up (20 x period: {late:.3g})")
            if dev > 1e-3:
                c.warn(f"{name}: DSL and M5.4 feature path differ during warm-up (max deviation {100 * dev:.2f}%, "
                       f"identical to 1e-6 from bar {k}). Both seed with an SMA; M5.4 counts the first bar's "
                       f"directional movement as 0 where the DSL (and TA-Lib) leave it undefined. M5.4 is left "
                       f"unchanged so cached M5.4 features and tree results stay reproducible.")
        c.evidence["pairs"] = out
    sec.run("cross_check_m54_feature_path", "The DSL agrees with the independent M5.4 feature implementation after "
            "warm-up", m54)
