"""Trigger + structure-exit check for one stock (owner 2026-11-22: "13 Oct 2 pm EMA13 x EMA20, target EMA 200, SL the
last swing low - why can't this simple strategy be picked?").

    $env:PYTHONPATH="src"; python -m stock_gplm.app.trigger_check --symbol RELIANCE --tf 15m --around "2026-10-13 14:00"

Times are CANDLE START times (as TradingView shows them), IST. For every trigger in config/research/triggers.json and
every exit (stop: last swing low / %; target: EMA 200 at the signal / N x risk / %): every 2026 signal of the stock,
entered at the next open, held until stop or target (overnight on intraday, max_hold_days), one position at a time,
costs included. Stats plain and recency-weighted (recent trades weigh more - owner's most important rule).
--around: the candles before/after that time with EMA 13/20/200, swing lows and which triggers fired, plus the trade
each exit would have made from it.
Output <reports>/tuning/trigger-check-<tf>-<SYM>/REPORT.md and trades.csv (every trade and every skipped signal).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from . import tune_eval as te
from .discover import FLOOR_DAILY, FLOOR_INTRADAY

from .tune import CONFIG

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "config" / "research" / "triggers.json"
TF_MIN = {"5m": 5, "15m": 15, "60m": 60}


class Check:
    def __init__(self, a: argparse.Namespace):
        from . import strategy_dsl as dsl
        from .settings import load
        self.a = a
        self.spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        self.tf = a.tf
        t = cfg["timeframes"][a.tf]
        config = str(REPO / t["config"])
        self.sym = a.symbol.upper().removesuffix("-EQ")
        self.root = load(Path(config)).reports_dir / "tuning" / f"trigger-check-{a.tf}-{self.sym}"
        self.root.mkdir(parents=True, exist_ok=True)
        print(f"{self.sym} {a.tf}: loading candles", flush=True)
        L = te._load(self.sym, config, [a.tf], bool(t["use_minute"]))[a.tf]
        self.m, self.ctx, self.buy, self.sell = L["m"], L["ctx"], L["buy"], L["sell"]
        m = self.m
        self.n = len(m)
        self.a0 = int(np.searchsorted(L["ses"], t["tune_start"]))
        self.o, self.h, self.lo, self.c = (np.asarray(x, dtype=float) for x in (m.open, m.high, m.low, m.close))
        self.end_ns = np.asarray(m.end_ns, dtype=np.int64)
        self.days = L["days"]
        self.sid = np.asarray(m.session_id, dtype=np.int64) if m.intraday else np.arange(self.n)
        ev = lambda e: np.asarray(dsl.evaluate(dsl.parse(e), self.ctx), dtype=float)  # noqa: E731
        self.ema13, self.ema20, self.ema200 = ev("ema(close, 13)"), ev("ema(close, 20)"), ev("ema(close, 200)")
        self.floor = FLOOR_DAILY if a.tf == "1d" else FLOOR_INTRADAY
        self.masks: dict[str, np.ndarray] = {}
        for name, expr in self.spec["triggers"].items():
            try:
                self.masks[name] = np.asarray(dsl.signal(f"{self.floor} and ({expr})", self.ctx), bool)
            except Exception as exc:  # noqa: BLE001 - a bad trigger is reported, the rest run
                print(f"  trigger {name}: {type(exc).__name__}: {exc}", flush=True)
        per_day = max(1, int(np.median(np.bincount(self.sid - self.sid.min())))) if m.intraday else 1
        self.max_bars = int(self.spec.get("max_hold_days", 10)) * per_day
        sw = self.spec.get("swing") or {}
        self.sw_buf, self.sw_lb = float(sw.get("buffer", 0.002)), int(sw.get("lookback", 20))
        self.swings = {"swing_low": (int(sw.get("strength", 2)), self.sw_lb, self.sw_buf, True)}
        mj = self.spec.get("swing_major") or {}
        if mj:
            self.swings["swing_major"] = (int(mj.get("strength", 5)), int(mj.get("lookback", 150)),
                                          float(mj.get("buffer", 0.002)), False)

    # ------------------------------------------------------------------ helpers
    def when(self, i: int) -> str:
        """Candle START time (TradingView), IST; date on 1d."""
        if self.tf == "1d":
            return str(np.datetime64(int(self.days[i]), "D"))
        ts = np.datetime64(int(self.end_ns[i]), "ns") + np.timedelta64(330 - TF_MIN.get(self.tf, 0), "m")
        return str(ts.astype("datetime64[m]")).replace("T", " ")

    def find(self, text: str) -> int | None:
        for i in range(self.n):
            if self.when(i).startswith(text):
                return i
        return None

    def swing(self, t: int, entry: float, kind: str) -> int | None:
        """Candle of the last swing low before t, below the entry: its low is the lowest of `strength` candles on
        each side (confirmed by candle t). The minor swing (k=2) uses the engine's rule incl. its fallback (lowest of
        the last 5 candles)."""
        k, lb, _, fallback = self.swings[kind]
        for i in range(t - k, max(k, t - lb) - 1, -1):
            v = self.lo[i]
            if v < entry and v <= self.lo[i - k:i + k + 1].min():
                return i
        if fallback:
            w0 = max(0, t - 4)
            i = w0 + int(np.argmin(self.lo[w0:t + 1]))
            return i if self.lo[i] < entry else None
        return None

    def levels(self, t: int, ex: dict[str, str]) -> tuple[float, float, float, str]:
        """(entry, stop, target, why-skipped) for a signal at candle t."""
        entry = self.o[t + 1]
        s = ex["stop"]
        self.last_swing = None
        if s in self.swings:
            i = self.swing(t, entry, s)
            if i is None:
                return entry, np.nan, np.nan, "no swing low below the entry"
            self.last_swing = i
            stop = self.lo[i] * (1 - self.swings[s][2])
        else:
            stop = entry * (1 - float(s.removeprefix("pct")) / 100)
        risk = entry - stop
        g = ex["target"]
        if g == "ema200":
            target = self.ema200[t]
            if not target > entry:
                return entry, stop, target, "price already above EMA 200"
        elif g.startswith("rr"):
            target = entry + float(g[2:]) * risk
        else:
            target = entry * (1 + float(g.removeprefix("pct")) / 100)
        if (target - entry) / risk < float(self.spec.get("min_rr", 0)):
            return entry, stop, target, f"reward/risk {(target - entry) / risk:.2f} < {self.spec.get('min_rr')}"
        return entry, stop, target, ""

    def trade(self, t: int, entry: float, stop: float, target: float) -> tuple[int, float, str]:
        last = min(self.n - 1, t + self.max_bars)
        for j in range(t + 1, last + 1):
            o, h, lo = self.o[j], self.h[j], self.lo[j]
            if j > t + 1 and o <= stop:
                return j, o, "stop (gap)"
            if lo <= stop:
                return j, stop, "stop"
            if j > t + 1 and o >= target:
                return j, o, "target (gap)"
            if h >= target:
                return j, target, "target"
        if last == self.n - 1 and t + self.max_bars > self.n - 1:
            return last, self.c[last], "still open"
        return last, self.c[last], "time"

    def run_trades(self, name: str, ex: dict[str, str]) -> list[dict[str, Any]]:
        rows, free = [], -1
        for t in np.flatnonzero(self.masks[name]):
            if t < self.a0 or t + 1 >= self.n:
                continue
            entry, stop, target, skip = self.levels(int(t), ex)
            base = {"trigger": name, "signal": self.when(int(t)), "entry": round(entry, 2),
                    "stop": round(stop, 2) if np.isfinite(stop) else "", "target": round(target, 2)
                    if np.isfinite(target) else ""}
            if t < free:
                continue  # a position from this trigger x exit is still open
            if skip:
                rows.append({**base, "result": "skipped: " + skip})
                continue
            j, px, how = self.trade(int(t), entry, stop, target)
            net = px * (1 - self.sell) / (entry * (1 + self.buy)) - 1
            rows.append({**base, "closed": self.when(j), "exit_price": round(px, 2), "how": how,
                         "net_pct": round(100 * net, 2), "R": round((px - entry) / (entry - stop), 2), "t": int(t)})
            free = j
        return rows

    def stats(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        done = [r for r in rows if "net_pct" in r and r["how"] != "still open"]
        if not done:
            return {"trades": 0}
        net = np.array([r["net_pct"] for r in done]) / 100
        R = np.array([r["R"] for r in done])
        ts = np.array([r["t"] for r in done])
        w = (1 + (self.sid[-1] - self.sid[ts]) / 100.0) ** -2.0
        gl = -net[net <= 0].sum()
        return {"trades": len(done), "win": float((net > 0).mean()), "avg": float(net.mean()), "avgR": float(R.mean()),
                "pf": float(net[net > 0].sum() / gl) if gl > 0 else 99.0, "total": float(net.sum()),
                "win_w": float(w[net > 0].sum() / w.sum()), "avg_w": float((w * net).sum() / w.sum()),
                "skipped": sum(1 for r in rows if str(r.get("result", "")).startswith("skipped")),
                "open": sum(1 for r in rows if r.get("how") == "still open")}

    # ------------------------------------------------------------------ report
    def around(self, text: str) -> list[str]:
        i = self.find(text)
        if i is None:
            return [f"## Around {text}", "", f"No {self.tf} candle starts at {text}.", ""]
        lo_i, hi_i = max(0, i - self.a.before), min(self.n - 1, i + self.a.after)
        piv = np.zeros(self.n, bool)
        for k in range(2, self.n - 2):
            piv[k] = self.lo[k] <= self.lo[k - 2:k + 3].min()
        out = [f"## Around {text} ({self.sym} {self.tf}; candle start times, IST)", "",
               "| Candle | Open | High | Low | Close | EMA13 | EMA20 | EMA200 | Swing low | Triggers firing |",
               "|---|---|---|---|---|---|---|---|---|---|"]
        for k in range(lo_i, hi_i + 1):
            fired = [nm for nm, mk in self.masks.items() if mk[k]]
            mark = "**>>**" if k == i else ""
            out.append(f"| {mark}{self.when(k)} | {self.o[k]:.2f} | {self.h[k]:.2f} | {self.lo[k]:.2f} | "
                       f"{self.c[k]:.2f} | {self.ema13[k]:.2f} | {self.ema20[k]:.2f} | {self.ema200[k]:.2f} | "
                       f"{'pivot low' if piv[k] else ''} | {', '.join(fired)} |")
        out += ["", f"Trade from the {text} candle (enter next open), per exit:", ""]
        for en, ex in self.spec["exits"].items():
            entry, stop, target, skip = self.levels(i, ex)
            sw = (f", swing low used: {self.when(self.last_swing)} low {self.lo[self.last_swing]:.2f}"
                  if self.last_swing is not None else "")
            if skip:
                out.append(f"- {en}: skipped - {skip} (entry {entry:.2f}, stop {stop:.2f}, target {target:.2f}{sw})")
                continue
            j, px, how = self.trade(i, entry, stop, target)
            net = px * (1 - self.sell) / (entry * (1 + self.buy)) - 1
            out.append(f"- {en}: entry {entry:.2f}, stop {stop:.2f}{sw}, target {target:.2f} (reward/risk "
                       f"{(target - entry) / (entry - stop):.2f}) -> {how} at {px:.2f} on {self.when(j)}, net "
                       f"{100 * net:+.2f}%")
        return out + [""]

    def run(self) -> int:
        md = [f"# Trigger check - {self.sym} {self.tf}", "",
              f"Signals from {self.when(self.a0)}; enter next open; hold until stop or target (max "
              f"{self.spec.get('max_hold_days', 10)} days, overnight); one position at a time per trigger x exit; "
              f"costs in. Swing low = last confirmed 2-2 pivot low within {self.sw_lb} candles, minus "
              f"{self.sw_buf:.1%}. Recent-weighted = (1 + age_sessions/100)^-2.", ""]
        for x in self.a.around or []:
            md += self.around(x)
        allrows: list[dict[str, Any]] = []
        res = []
        for name in self.masks:
            for en, ex in self.spec["exits"].items():
                rows = self.run_trades(name, ex)
                for r in rows:
                    r["exit_rule"] = en
                allrows += rows
                s = self.stats(rows)
                res.append((name, en, s))
                if s["trades"]:
                    print(f"  {name:22s} {en:20s} {s['trades']:3d} trades, win {s['win']:.0%}, avg "
                          f"{100 * s['avg']:+.2f}% ({s['avgR']:+.2f}R), total {100 * s['total']:+.1f}%; recent-weighted "
                          f"avg {100 * s['avg_w']:+.2f}%", flush=True)
        res.sort(key=lambda r: -(r[2].get("avg_w", -9) if r[2]["trades"] >= 5 else -9))
        md += ["## Every trigger x exit (2026, ranked by recent-weighted average; >= 5 trades first)", "",
               "| Trigger | Exit | Trades | Win | Avg | Avg R | PF | Total | Recent-weighted win / avg | Skipped | Open |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, en, s in res:
            if not s["trades"]:
                md.append(f"| {name} | {en} | 0 | | | | | | | | |")
                continue
            md.append(f"| {name} | {en} | {s['trades']} | {s['win']:.0%} | {100 * s['avg']:+.2f}% | {s['avgR']:+.2f} | "
                      f"{min(s['pf'], 99):.2f} | {100 * s['total']:+.1f}% | {s['win_w']:.0%} / {100 * s['avg_w']:+.2f}% | "
                      f"{s['skipped']} | {s['open']} |")
        (self.root / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        keys = ["trigger", "exit_rule", "signal", "entry", "stop", "target", "closed", "exit_price", "how", "net_pct",
                "R", "result"]
        with (self.root / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(allrows)
        print(f"report: {self.root / 'REPORT.md'}", flush=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--around", action="append",
                    help='candle start time as on TradingView, e.g. "2026-10-13 14:00" (repeat for more)')
    ap.add_argument("--before", type=int, default=12)
    ap.add_argument("--after", type=int, default=60)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--config", default=str(CONFIG))
    return Check(ap.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
