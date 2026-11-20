"""Stock behaviour profile per timeframe (PAPER/research): how each stock behaves and what it reacts to.

For every stock x timeframe (5m/15m/30m/60m from minute data, 1d from daily data):
  * character - trend vs mean reversion (lag-1 autocorrelation), share of bars above EMA50, bar range %,
                gap habits, best / worst 30-minute window of the session (intraday);
  * reactions - for ~40 simple events (strategy_llm._PROBES: EMA touches, prior-day levels, VWAP, opening range,
                RSI(2), NR7, volume surges, time of day ...) the mean forward return H bars later, net of costs,
                and the excess over an average bar, measured separately in DEVELOPMENT (<= --split) and TEST (after).
A reaction is HELD when it has >= MIN_N events in both periods and the same sign of excess in both.

Outputs <home>/reports/profiles/<name>/: reactions.csv, character.csv, profile.md (per stock: character +
reactions that held). Usage (PYTHONPATH=src): python -m stock_gplm.app.stock_profile --name latest
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import strategy_dsl as dsl
from .live_lab import INDICES, REPO, TFS, build_frame
from .setup_replay import COST_PCT as INTRADAY_COST_PCT, available_sessions, read_minute
from .strategy_llm import _PROBES

DELIVERY_COST_PCT = (12.41 + 10.91 + 3.0 * 2 + 5.0 * 2) / 100  # config costs.delivery round trip
HORIZON = {"5m": 12, "15m": 8, "30m": 6, "60m": 5, "1d": 5}     # bars ahead (~1h, 2h, 3h, 5h, 1 week)
MIN_N = 15


def read_daily(root: Path, sym: str) -> pd.DataFrame:
    rows: dict[str, dict[str, float]] = {}
    d = Path(root) / f"{sym}-EQ"
    for p in sorted(d.glob("*_*.json.gz")) if d.is_dir() else []:
        try:
            data = json.load(gzip.open(p))
        except (OSError, ValueError):
            continue
        for r in data:
            r = json.loads(r) if isinstance(r, str) else r
            if not isinstance(r, dict) or "intc" not in r:
                continue
            day = pd.to_datetime(r["time"], format="%d-%b-%Y").strftime("%Y-%m-%d")
            rows[day] = {"open": float(r["into"]), "high": float(r["inth"]), "low": float(r["intl"]),
                         "close": float(r["intc"]), "volume": float(r.get("intv") or 0)}
    f = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    if f.empty:
        return f
    f.index.name = "session"
    f = f.reset_index()
    f["end_ns"] = pd.to_datetime(f["session"]).astype("int64")
    f["label"] = f["session"]
    return f


def _fwd(f: pd.DataFrame, h: int, intraday: bool) -> np.ndarray:
    """% return from the next bar's open to the close h bars later (same session only for intraday)."""
    o, c = f["open"].to_numpy(float), f["close"].to_numpy(float)
    n = len(f)
    out = np.full(n, np.nan)
    sess = f["session"].to_numpy()
    for i in range(n - h - 1):
        j = i + h
        if intraday and sess[j] != sess[i + 1]:
            continue
        out[i] = (c[j] / o[i + 1] - 1) * 100
    return out


def profile_lane(f: pd.DataFrame, sym: str, tf: str, split: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    intraday = tf != "1d"
    ctx = dsl.Context.from_frame(f, tf, intraday)
    cost = INTRADAY_COST_PCT if intraday else DELIVERY_COST_PCT
    fwd = _fwd(f, HORIZON[tf], intraday)
    dev = (f["session"] <= split).to_numpy()
    base = {seg: float(np.nanmean(fwd[m])) for seg, m in (("dev", dev), ("test", ~dev))}
    rows = []
    for e in _PROBES:
        try:
            m = np.asarray(dsl._bool(ctx, dsl.evaluate(dsl.parse(e), ctx)), bool)
        except (dsl.DSLError, dsl.MissingData, Exception):
            continue
        if len(m) != len(f):
            continue
        ev = m & ~np.concatenate([[False], m[:-1]]) & np.isfinite(fwd)
        r: dict[str, Any] = {"symbol": sym, "tf": tf, "event": e, "horizon_bars": HORIZON[tf]}
        for seg, sm in (("dev", dev), ("test", ~dev)):
            x = fwd[ev & sm]
            r[f"{seg}_n"] = int(x.size)
            r[f"{seg}_net_pct"] = round(float(x.mean() - cost), 3) if x.size else None
            r[f"{seg}_excess_pct"] = round(float(x.mean() - base[seg]), 3) if x.size else None
            r[f"{seg}_win_pct"] = round(float((x - cost > 0).mean() * 100), 1) if x.size else None
        ok = r["dev_n"] >= MIN_N and r["test_n"] >= MIN_N
        r["held"] = bool(ok and np.sign(r["dev_excess_pct"]) == np.sign(r["test_excess_pct"]) != 0)
        rows.append(r)
    c = f["close"].to_numpy(float)
    ret = np.diff(np.log(c))
    char: dict[str, Any] = {
        "symbol": sym, "tf": tf, "bars": len(f), "from": f["session"].iloc[0], "to": f["session"].iloc[-1],
        "lag1_autocorr": round(float(np.corrcoef(ret[1:], ret[:-1])[0, 1]), 3) if ret.size > 20 else None,
        "pct_above_ema50": round(float(np.nanmean(np.asarray(dsl.evaluate(dsl.parse("close > ema(close, 50)"), ctx),
                                                              float))) * 100, 1),
        "avg_range_pct": round(float(((f["high"] - f["low"]) / f["close"]).mean() * 100), 3),
    }
    ac = char["lag1_autocorr"] or 0
    char["character"] = "TRENDING (moves continue)" if ac > 0.05 else "MEAN-REVERTING (moves fade)" if ac < -0.05 \
        else "NEUTRAL"
    if intraday:
        slot = ((f["minute_of_session"] - 1) // 30).clip(upper=12)
        r1 = (f["close"] / f["open"] - 1) * 100
        by = r1.groupby(slot).mean()
        lab = lambda s: f"{(555 + s * 30) // 60:02d}:{(555 + s * 30) % 60:02d}"  # noqa: E731
        char["best_30min"] = f"{lab(int(by.idxmax()))} ({by.max():+.3f}%/bar)"
        char["worst_30min"] = f"{lab(int(by.idxmin()))} ({by.min():+.3f}%/bar)"
    else:
        g = (f["open"] / f["close"].shift(1) - 1) * 100
        char["gap_up_gt1pct_share"] = round(float((g > 1).mean() * 100), 1)
        char["gap_fill_same_day_pct"] = round(float(((g > 1) & (f["low"] <= f["close"].shift(1))).sum()
                                                    / max(1, (g > 1).sum()) * 100), 1)
    return rows, char


def profile_symbol(sym: str, minute_roots: list[Path], daily_root: Path, split: str) -> tuple[str, list, list]:
    reactions, chars = [], []
    sess = available_sessions(minute_roots, sym)
    if sess:
        bars = read_minute(minute_roots, sym, set(sess))
        for tf, k in TFS.items():
            f = build_frame(bars, k, "9999-12-31")
            if len(f) > 200:
                r, c = profile_lane(f, sym, tf, split)
                reactions += r
                chars.append(c)
    d = read_daily(daily_root, sym)
    if len(d) > 200:
        r, c = profile_lane(d, sym, "1d", split)
        reactions += r
        chars.append(c)
    return sym, reactions, chars


def report(rx: pd.DataFrame, ch: pd.DataFrame) -> str:
    lines = ["# Stock behaviour profiles", "Reactions: mean move H bars after an event, net of costs; excess = vs an "
             "average bar. HELD = same direction in development (<= split) and test (after), >= "
             f"{MIN_N} events each. PAPER/research only - not advice.", ""]
    for sym in sorted(ch["symbol"].unique()):
        lines.append(f"## {sym}")
        for _, c in ch[ch["symbol"] == sym].iterrows():
            extra = (f"best {c.get('best_30min')}, worst {c.get('worst_30min')}" if c["tf"] != "1d"
                     else f"gap-ups >1%: {c.get('gap_up_gt1pct_share')}% of days, filled same day "
                          f"{c.get('gap_fill_same_day_pct')}%")
            lines.append(f"- **{c['tf']}**: {c['character']} (autocorr {c['lag1_autocorr']}), above EMA50 "
                         f"{c['pct_above_ema50']}% of bars, range {c['avg_range_pct']}%/bar; {extra}")
            h = rx[(rx["symbol"] == sym) & (rx["tf"] == c["tf"]) & rx["held"]].copy()
            if len(h):
                h["score"] = h[["dev_excess_pct", "test_excess_pct"]].min(axis=1).where(
                    h["dev_excess_pct"] > 0, h[["dev_excess_pct", "test_excess_pct"]].max(axis=1))
                good = h[h["dev_excess_pct"] > 0].sort_values("score", ascending=False).head(3)
                bad = h[h["dev_excess_pct"] < 0].sort_values("score").head(2)
                for _, r in good.iterrows():
                    tag = ("✅ profitable after costs" if r["dev_net_pct"] > 0 and r["test_net_pct"] > 0
                           else "↑ stronger than average, still loses after costs")
                    lines.append(f"  - {tag} after `{r['event']}`: net {r['dev_net_pct']:+.2f}% / "
                                 f"{r['test_net_pct']:+.2f}% (dev/test, n {r['dev_n']}/{r['test_n']}, "
                                 f"{r['horizon_bars']} bars)")
                for _, r in bad.iterrows():
                    lines.append(f"  - ⛔ avoid after `{r['event']}`: net {r['dev_net_pct']:+.2f}% / "
                                 f"{r['test_net_pct']:+.2f}%")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="latest")
    ap.add_argument("--stocks", default=None)
    ap.add_argument("--split", default="2026-08-13", help="last development session (test = after)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 4))
    a = ap.parse_args()
    from .settings import load as load_settings
    st = load_settings(REPO / "config" / "app" / "gplm.windows.json")
    if a.stocks:
        syms = [s.strip().upper() for s in a.stocks.split(",") if s.strip()]
    else:
        wl = (REPO / "deploy" / "oracle" / "live" / "watchlist.txt").read_text().splitlines()
        syms = [s for ln in wl for s in ln.split("#", 1)[0].split() if s not in INDICES]
    out = Path(st.reports_dir) / "profiles" / a.name
    out.mkdir(parents=True, exist_ok=True)
    rx, ch = [], []
    with ProcessPoolExecutor(a.workers) as pool:
        futs = [pool.submit(profile_symbol, s, [Path(r) for r in st.minute_roots], Path(st.daily_root), a.split)
                for s in syms]
        for n, fu in enumerate(as_completed(futs), 1):
            try:
                s, r, c = fu.result()
                rx += r
                ch += c
                print(f"  [{n}/{len(syms)}] {s}: {len(c)} timeframes", flush=True)
            except Exception as exc:
                print(f"  [{n}/{len(syms)}] FAILED: {exc}", flush=True)
    rxd, chd = pd.DataFrame(rx), pd.DataFrame(ch)
    rxd.to_csv(out / "reactions.csv", index=False)
    chd.to_csv(out / "character.csv", index=False)
    txt = report(rxd, chd)
    (out / "profile.md").write_text(txt, encoding="utf-8")
    print(f"held reactions: {int(rxd['held'].sum())} of {len(rxd)}; -> {out / 'profile.md'}")


if __name__ == "__main__":
    main()
