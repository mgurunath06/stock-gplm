"""Section 0: the verifier detects planted defects (mutation tests), so a PASS elsewhere means something."""
from __future__ import annotations

from typing import Any
from unittest import mock

import numpy as np

from ..app import strategy_dsl as dsl
from ..app import strategy_engine as se
from . import fixtures as fx
from . import reference as R
from .core import Check, Section
from .suite_catalogue import semantic
from .suite_engine import SCENARIOS, _scenario, causal_check
from .suite_indicators import INDICATORS, compare


def run(sec: Section, ev: dict[str, Any]) -> None:
    main = fx.ohlcv()

    def wrong_rsi(c: Check) -> None:
        name, expr, ref, rec, p = next(x for x in INDICATORS if x[0] == "RSI(14)")
        bad = lambda ctx, x, n: 100 - 100 / (1 + dsl._div(dsl.sma(np.fmax(np.diff(dsl._arr(ctx, x), prepend=np.nan), 0), int(n)),  # noqa: E731
                                                         dsl.sma(np.fmax(-np.diff(dsl._arr(ctx, x), prepend=np.nan), 0), int(n))))
        with mock.patch.dict(dsl._IMPL, {"rsi": bad}):
            row = compare(name, expr, ref, rec, p, main)
        c.evidence.update(planted="RSI with SMA instead of Wilder smoothing", detected_error=row["max_rel_err"])
        c.expect(row["max_rel_err"] > 1e-3, "an RSI with the wrong smoothing was not detected")
    sec.run("detects_wrong_indicator", "A planted RSI with SMA (not Wilder) smoothing is reported as wrong", wrong_rsi)

    def peeking(c: Check) -> None:
        dsl.FUNCTIONS["__peek"] = (1, 1, "planted future read")
        try:
            with mock.patch.dict(dsl._IMPL, {"__peek": lambda ctx, x: np.concatenate((dsl._arr(ctx, x)[1:], [np.nan]))}):
                r = causal_check("__peek(close) > close", main.iloc[:600], "1d")
        finally:
            dsl.FUNCTIONS.pop("__peek", None)
        c.evidence.update(planted="primitive returning tomorrow's close", result=r)
        c.expect(r["leaks"], "a primitive that reads the next bar was not detected")
    sec.run("detects_future_leak", "A planted primitive that reads the next bar is reported as leaking", peeking)

    def mistranslation(c: Check) -> None:
        r = semantic("SMA(50)[t] > SMA(200)[t] AND SMA(50)[t-1] <= SMA(200)[t-1]",
                     "cross_above(sma(close, 50), sma(close, 100))", {}, [main])
        c.evidence.update(planted="Golden cross translated with SMA(100) instead of SMA(200)", result=r)
        c.expect(r["semantic"] == "DIFFERENT", f"a wrong translation was not detected ({r['semantic']})")
    sec.run("detects_mistranslation", "A planted mistranslation (SMA 100 for SMA 200) is reported as a mismatch",
            mistranslation)

    def wrong_fill(c: Check) -> None:
        sc = SCENARIOS[0]
        real = se._path

        def same_bar(m: Any, t: int, p: Any, last: int, short: bool, xm: Any) -> Any:
            r = real(m, t, p, last, short, xm)
            return None if r is None else (r[0] - 1, float(m.open[r[0] - 1]), r[2])
        with mock.patch.object(se, "_path", same_bar):
            trades, _ = _scenario(sc["edit"], sc["exit"], sc["signals"])
        got = trades[0]["exit_index"] if trades else None
        c.evidence.update(planted="time exit one bar early", exit_index=got, expected=sc["expect"]["exit_index"])
        c.expect(got != sc["expect"]["exit_index"], "an early exit was not detected by the fill scenario")
    sec.run("detects_wrong_fill", "A planted one-bar-early exit changes the scenario result the fill checks compare",
            wrong_fill)

    def wrong_bh(c: Check) -> None:
        p = [0.01, 0.011, 0.04]
        naive = [min(1.0, x * 3 / (sorted(p).index(x) + 1)) for x in p]  # missing the step-up minimum
        ref = R.bh(p, 3)
        c.evidence.update(planted="BH without the step-up minimum", naive=naive, reference=ref)
        c.expect(any(abs(a - b) > 1e-12 for a, b in zip(naive, ref)), "a BH without step-up was not distinguishable")
    sec.run("detects_wrong_bh", "A BH implementation without the step-up minimum differs from the reference", wrong_bh)
