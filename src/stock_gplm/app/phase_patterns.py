"""Phase patterns for ONE stock (owner 2026-11-22): what worked the last 3 times the stock was in today's phase.

    $env:PYTHONPATH="src"; python -m stock_gplm.app.phase_patterns --tf 1d --symbol RELIANCE   [--side long|short]

* PHASE of every bar (own TF): BULLISH = EMA9 > EMA13 > EMA20 > EMA50, BEARISH = EMA9 < EMA13 < EMA20 < EMA50,
  SIDEWAYS = anything else (config/research/phase_patterns.json).
* PATTERN = 1-3 conditions on indicator values (MACD below 0, close above VWAP, RSI(3) 55-70, volume 2x previous bar,
  volume slowly falling, ... = config/research/move_features.json + extra_features), levels at the 20/40/60/80% points.
* A TIME = the first bar of a new run of signal bars while the stock is in that phase (enter next open).
* WORKED = the trade reached +1% after costs (brokerage/STT/charges of the lane) before the stop (-0.5% after costs)
  or the time exit (H bars); each pattern uses the hold H from the grid that suits it best.
* Counted BACKWARDS FROM TODAY: streak = consecutive worked times from the pattern's most recent time back to its
  first failure. Streak >= 3 -> Medium; 5 / 8 / 12 -> Medium-high / High / Very high (config ladder).
* Shown with the phase BASE RATE (how often any bar in the phase reached +1% first) and a METHOD CHECK: in each
  pattern's history before its current streak, how often did the next time work right after 3 wins in a row?
* Output <reports>/tuning/phase-patterns-<tf>-<SYMBOL>-<side>/: PATTERNS.md (today's phase, patterns active today, best
  patterns per phase), patterns.csv (every qualifying pattern). All history from 2010 is used; no date split.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import tune_eval as te
from .discover import Discoverer
from .tune import CONFIG

REPO = Path(__file__).resolve().parents[3]
PATTERNS_CFG = REPO / "config" / "research" / "phase_patterns.json"
PHASES = {1: "BULLISH", 0: "SIDEWAYS", -1: "BEARISH"}
Cond = tuple[int, str, float]


def _date(d: int) -> str:
    return str(np.datetime64(int(d), "D"))


def _one_at_a_time(s: np.ndarray, res: np.ndarray) -> np.ndarray:
    """Times taken one trade at a time: a time counts only after the previous trade has closed (res = closing
    candle), so each counted time's result was known before the next one - no open trades in a streak."""
    out = np.empty(len(s), np.int64)
    k, nxt = 0, -1
    for i in range(len(s)):
        if s[i] > nxt:
            out[k] = s[i]
            k += 1
            nxt = res[s[i]]
    return out[:k]


try:
    from numba import njit
    _one_at_a_time = njit(cache=True)(_one_at_a_time)
except ImportError:  # plain Python fallback (slower)
    pass


class Finder:
    def __init__(self, a: argparse.Namespace):
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        self.pc = json.loads(Path(a.patterns).read_text(encoding="utf-8"))
        self.sym = a.symbol.upper().removesuffix("-EQ")
        tmp = Path(tempfile.gettempdir()) / f"phase_patterns_{self.sym}.txt"
        tmp.write_text(self.sym + "\n", encoding="utf-8")
        self.du = Discoverer(a.tf, f"phase-patterns-{a.tf}-{self.sym}-{a.side}", cfg, "@" + str(tmp), 1)
        self.tf, self.short, self.side, self.log = a.tf, a.side == "short", a.side, self.du.log
        ex = self.pc["exits"].get(a.tf) or self.pc["exits"]["1d"]
        self.exits = [int(h) for h in ex["holds"]]
        self.R = int(self.pc["recent_wins_needed"])
        wk = self.pc["worked"].get(a.tf) or self.pc["worked"]["default"]
        self.atr_mode = "target_atr" in wk
        self.overnight = bool(wk.get("overnight", False))
        if self.atr_mode:
            self.gain, self.loss = float(wk["target_atr"]), float(wk["stop_atr"])
            self.wtext = f"+{self.gain:g} ATR before -{self.loss:g} ATR"
        else:
            self.gain, self.loss = float(wk["target_pct"]) / 100.0, float(wk["stop_pct"]) / 100.0
            self.wtext = f"+{100 * self.gain:g}% before -{100 * self.loss:g}% (after costs)"

    # ------------------------------------------------------------------ data
    def load(self) -> bool:
        from . import strategy_dsl as dsl
        self.log(f"{self.sym} {self.tf}: loading candles and computing indicators")
        L = te._load(self.du.stocks[0], self.du.config, [self.tf], bool(self.du.t["use_minute"])).get(self.tf)
        if L is None:
            self.log(f"no {self.tf} data for {self.sym}")
            return False
        m, ctx = L["m"], L["ctx"]
        n = len(m)
        self.n, self.days = n, L["days"]
        a0 = int(np.searchsorted(L["ses"], self.du.t["tune_start"]))
        self.L, self.a0 = L, a0  # kept for stock_tf (real-engine check on the same data)
        floor = np.asarray(dsl.signal(self.du.floor, ctx), bool)
        exprs = {**te.feature_exprs(m.intraday), **(self.pc.get("extra_features") or {})}
        names, cols = [], []
        for nm, ex in exprs.items():
            try:
                x = np.broadcast_to(np.asarray(dsl.evaluate(dsl.parse(ex), ctx), dtype=float), (n,))
            except Exception as exc:  # noqa: BLE001 - a feature that cannot be computed is skipped
                self.log(f"  feature {nm} skipped ({type(exc).__name__})")
                continue
            names.append(nm)
            cols.append(np.array(x, dtype=float))
        self.base_n = len(names)
        for lb in self.pc.get("change_lookbacks") or []:  # how each indicator changed over the last lb candles
            for j in range(self.base_n):
                d = np.full(n, np.nan)
                d[lb:] = cols[j][lb:] - cols[j][:-lb]
                names.append(f"{names[j]}_chg{lb}")
                cols.append(d)
        self.o, self.c = np.asarray(m.open, dtype=float), np.asarray(m.close, dtype=float)
        self.intraday = bool(m.intraday)
        self.end_ns = np.asarray(m.end_ns, dtype=np.int64)
        self.names, self.exprs = names, exprs
        self.F = np.stack(cols, axis=1)
        bull = np.asarray(dsl.signal(self.pc["phase"]["bullish"], ctx), bool)
        bear = np.asarray(dsl.signal(self.pc["phase"]["bearish"], ctx), bool)
        self.phase = np.where(bull, 1, np.where(bear, -1, 0))
        self.ok = floor & (np.arange(n) >= a0)
        E = len(self.exits)
        self.win = np.zeros((E, n), bool)
        self.net = np.full((E, n), np.nan)
        self.done = np.zeros((E, n), bool)
        self.peak = np.full((E, n), np.nan)
        self.res = np.full((E, n), n + 1, np.int64)  # candle where the trade closed
        for k, h in enumerate(self.exits):
            t, net, win, pk, rs = self.label(m, n, h, L["buy"], L["sell"])
            self.net[k, t], self.win[k, t], self.done[k, t], self.peak[k, t] = net, win, True, pk
            self.res[k, t] = rs
        # owner rule (2026-11-21, MOST IMPORTANT): recent trades weigh more - age counted in sessions
        self.sid = (np.asarray(m.session_id, dtype=np.int64) if m.intraday else np.arange(n, dtype=np.int64))
        self.full_ok, self.full_done = self.ok.copy(), self.done.copy()
        self.cut = n
        self.log(f"{n:,} bars {_date(self.days[0])} .. {_date(self.days[-1])}; {self.base_n} indicators + their "
                 f"changes over {self.pc.get('change_lookbacks')} candles = {len(names)} values per bar; phase today: "
                 f"{PHASES[int(self.phase[-1])]}")
        return True

    def set_cut(self, cut: int) -> None:
        """Stand at candle `cut` (exclusive): only candles before it, and only trades that had CLOSED before it,
        are known - used by the causal replay in stock_tf. cut = n means today."""
        self.cut = int(cut)
        idx = np.arange(self.n)
        self.ok = self.full_ok & (idx < self.cut)
        self.done = self.full_done & (self.res < self.cut)
        self._legs = None

    def wt(self, idx: np.ndarray) -> np.ndarray:
        """Recency weight of candles `idx` seen from the cut: (1 + age_sessions / decay) ** -power (owner rule:
        recent trades weigh more; same decay as confidence.py - weight halves after ~40 sessions)."""
        rc = self.pc.get("recency") or {}
        age = self.sid[self.cut - 1] - self.sid[idx]
        return (1.0 + np.maximum(age, 0) / float(rc.get("decay_sessions", 100))) ** -float(rc.get("power", 2))

    def analyse(self, cut: int) -> dict[int, list]:
        """Conditions, base rates and the pattern search for all 3 phases, standing at `cut`."""
        self.set_cut(cut)
        self.build_conditions()
        self.base = {}
        for q in (1, 0, -1):
            sel = self.ok & (self.phase == q)
            self.base[q] = [float(self.win[e, sel & self.done[e]].mean()) if (sel & self.done[e]).any() else 0.0
                            for e in range(len(self.exits))]
        P = int(self.phase[self.cut - 1])
        self.res = {p: self.search(p) for p in (P, *[q for q in (1, 0, -1) if q != P])}
        self.P = P
        return self.res

    def label(self, m: Any, n: int, h: int, buy: float, sell: float):
        """Every bar: enter next open; worked = +gain net (after costs) reached before -loss net / time exit."""
        natr = np.asarray(m.natr, dtype=float)
        t = np.arange(n - 1)
        t = t[np.isfinite(natr[t])]
        if m.intraday and not self.overnight:
            end = np.minimum(t + h, np.asarray(m.exit_last)[t + 1].astype(np.int64))
            keep = end >= t + 1
        else:
            end = t + h
            keep = end <= n - 1
        t, end = t[keep], end[keep]
        o, hi, lo, cl = (np.asarray(x, dtype=float) for x in (m.open, m.high, m.low, m.close))
        ent = o[t + 1]
        g, lz = self.gain, self.loss
        if self.atr_mode:
            atr = natr[t] * ent
            sg = -1.0 if self.short else 1.0
            tgt, stp = ent + sg * g * atr, ent - sg * lz * atr
        elif self.short:
            tgt, stp = ent * (1 - sell - g) / (1 + buy), ent * (1 - sell + lz) / (1 + buy)
        else:
            tgt, stp = ent * (1 + g) * (1 + buy) / (1 - sell), ent * (1 - lz) * (1 + buy) / (1 - sell)
        never = h + 10
        ft, fs = np.full(len(t), never), np.full(len(t), never)
        best = ent.copy()  # best price reached before the stop (for the size of the move)
        for k in range(1, h + 1):
            j = t + k
            live = j <= end
            if not live.any():
                break
            jj = np.minimum(j, n - 1)
            th = live & ((lo[jj] <= tgt) if self.short else (hi[jj] >= tgt))
            sh = live & ((hi[jj] >= stp) if self.short else (lo[jj] <= stp))
            ft = np.where(th & (ft == never), k, ft)
            fs = np.where(sh & (fs == never), k, fs)
            run = live & (fs == never)
            best = np.where(run, np.minimum(best, lo[jj]) if self.short else np.maximum(best, hi[jj]), best)
        win = ft < fs  # same bar -> stop first (conservative)
        px = np.where(win, tgt, np.where(fs < never, stp, cl[end]))
        net = (ent * (1 - sell) - px * (1 + buy)) / ent if self.short else px * (1 - sell) / (ent * (1 + buy)) - 1
        pk = (ent / best - 1) if self.short else (best / ent - 1)
        rs = t + np.minimum(np.minimum(ft, fs), end - t)
        return t, net, win, pk, rs

    def build_conditions(self) -> None:
        conds: list[Cond] = []
        F = self.F[self.ok]
        for j in range(F.shape[1]):
            x = F[:, j]
            x = x[np.isfinite(x)]
            if len(x) < 100:
                continue
            u = np.unique(x)
            if len(u) < 2:
                continue
            if np.isin(u, (0.0, 1.0)).all():
                conds += [(j, "yes", 1.0), (j, "no", 0.0)]
                continue
            for e in np.unique([float(f"{q:.4g}") for q in np.quantile(x, self.pc["levels"])]):
                if e > u[0]:
                    conds += [(j, "ge", float(e)), (j, "lt", float(e))]
        self.conds = conds
        M = np.zeros((len(conds), self.n), bool)
        for c, (j, kind, lv) in enumerate(conds):
            x = self.F[:, j]
            fin = np.isfinite(x)
            M[c] = fin & ((x >= lv) if kind == "ge" else (x < lv) if kind == "lt" else (x > 0.5) if kind == "yes"
                          else (x <= 0.5))
        self.M = M
        self.log(f"{len(conds)} conditions")

    # ------------------------------------------------------------------ judging
    def confidence(self, streak: int) -> tuple[str, int]:
        lad = self.pc["confidence_ladder"]
        for i, r in enumerate(lad):
            if streak >= r["streak_min"]:
                return r["label"], len(lad) - i
        return self.pc["base_confidence"], 0

    def judge(self, sig: np.ndarray) -> dict[str, Any] | None:
        """Best hold for which the streak (worked times counted back from the latest) is >= R; None otherwise.
        Also the method check: in the history BEFORE the streak (and before the failure that ends it), each time
        that came right after R wins in a row -> did it work?"""
        st = np.flatnonzero(sig & ~np.r_[False, sig[:-1]])
        if len(st) < self.R:
            return None
        best = None
        R = self.R
        for e in range(len(self.exits)):
            s = st[self.done[e, st]]
            if len(s) < R:
                continue
            s = _one_at_a_time(s, self.res[e])
            if len(s) < R:
                continue
            w = self.win[e, s]
            if not w[-R:].all():
                continue
            fails = np.flatnonzero(~w)
            streak = len(w) - (int(fails[-1]) + 1 if len(fails) else 0)
            before = w[:max(len(w) - streak - 1, 0)]
            ck_n = ck_w = 0
            if len(before) > R:
                c = np.convolve(before.astype(int), np.ones(R, int), "valid")[:-1]  # wins in each R-window
                nxt = before[R:]
                hit = c == R
                ck_n, ck_w = int(hit.sum()), int(nxt[hit].sum())
            label, rank = self.confidence(streak)
            ww = self.wt(s)
            nt = self.net[e, s]
            rate_w = float((ww * w).sum() / ww.sum())
            avg_w = float((ww * nt).sum() / ww.sum())
            n_eff = float(ww.sum() ** 2 / (ww ** 2).sum())
            key = (avg_w > 0, rank, avg_w)  # recent results first (owner's most important rule)
            if best is None or key > best["key"]:
                best = {"key": key, "e": e, "times": s, "streak": streak, "all_won": int(w.sum()),
                        "conf": label, "rank": rank, "avg": float(np.mean(nt)), "rate_w": rate_w, "avg_w": avg_w,
                        "n_eff": n_eff, "ck_n": ck_n, "ck_w": ck_w}
        return best

    def search(self, P: int) -> list[tuple[tuple[int, ...], dict]]:
        base = self.ok & (self.phase == P)
        found: list[tuple[tuple[int, ...], dict]] = []
        seen: set[bytes] = set()

        def add(rule: tuple[int, ...], sig: np.ndarray) -> dict | None:
            r = self.judge(sig)
            if r is None:
                return None
            k = r["times"].tobytes() + bytes([r["e"]])
            if k in seen:  # same signals as a simpler rule already found
                return None
            seen.add(k)
            found.append((rule, r))
            return r

        live = [c for c in range(len(self.conds)) if (self.M[c] & base).sum() >= self.R]
        for c in live:
            add((c,), self.M[c] & base)
        t0 = time.monotonic()
        pairs: list[tuple[tuple[int, int], dict]] = []
        for i, c1 in enumerate(live):
            s1 = self.M[c1] & base
            for c2 in live[i + 1:]:
                if self.conds[c1][0] == self.conds[c2][0]:
                    continue
                r = add((c1, c2), s1 & self.M[c2])
                if r:
                    pairs.append(((c1, c2), r))
            if i % 100 == 99:
                self.log(f"  {PHASES[P]}: pairs {i + 1}/{len(live)} conditions, {len(found):,} qualifying "
                         f"({time.monotonic() - t0:.0f}s)")
        pairs.sort(key=lambda x: x[1]["key"], reverse=True)
        tried: set[tuple[int, ...]] = set()
        for (c1, c2), _ in pairs[:int(self.pc["triples_from_best_pairs"])]:
            s12 = self.M[c1] & self.M[c2] & base
            used = {self.conds[c1][0], self.conds[c2][0]}
            for c in live:
                rule = tuple(sorted((c1, c2, c)))
                if self.conds[c][0] in used or rule in tried:
                    continue
                tried.add(rule)
                add(rule, s12 & self.M[c])
        found.sort(key=lambda x: x[1]["key"], reverse=True)
        self.log(f"{PHASES[P]}: {len(found):,} qualifying patterns ({time.monotonic() - t0:.0f}s)")
        return found

    # ------------------------------------------------------------------ text
    def ctext(self, c: Cond) -> str:
        j, kind, lv = c
        nm = self.names[j]
        return {"ge": f"{nm} >= {lv:g}", "lt": f"{nm} < {lv:g}", "yes": nm, "no": f"not {nm}"}[kind]

    def rtext(self, rule: tuple[int, ...]) -> str:
        return " AND ".join(self.ctext(self.conds[c]) for c in rule)

    def when(self, i: int) -> str:
        """Candle time for reports: date (1d) or IST date + candle end time (intraday)."""
        if not self.intraday:
            return _date(self.days[i])
        t = np.datetime64(int(self.end_ns[i]), "ns") + np.timedelta64(330, "m")
        return str(t.astype("datetime64[m]")).replace("T", " ")

    def legs(self) -> np.ndarray:
        """Start candle of every move: consecutive candles that all lead into a move (longest hold) form one move;
        its start is the candle with the best entry (lowest next open for long)."""
        if getattr(self, "_legs", None) is not None:
            return self._legs
        e = len(self.exits) - 1
        w = np.flatnonzero(self.ok & self.done[e] & self.win[e])
        out: list[int] = []
        if len(w):
            groups = np.split(w, np.flatnonzero(np.diff(w) > 1) + 1)
            for g in groups:
                px = self.o[np.minimum(g + 1, self.n - 1)]
                out.append(int(g[np.argmax(px) if self.short else np.argmin(px)]))
        self._legs = np.asarray(out, np.int64)
        return self._legs

    def moves_table(self) -> list[str]:
        e = len(self.exits) - 1
        legs = self.legs()
        out = [f"## Moves found ({len(legs)}: {self.wtext} within {self.exits[e]} candles; newest first) - "
               "check these against your chart", "",
               "| Start candle | Phase at start | Entry (next open) | Biggest move before the stop |", "|---|---|---|---|"]
        for i in legs[::-1][:80]:
            out.append(f"| {self.when(i)} | {PHASES[int(self.phase[i])]} | {self.o[i + 1]:.2f} | "
                       f"{100 * self.peak[e, i]:+.1f}% |")
        if len(legs) > 80:
            out.append(f"| ... {len(legs) - 80} older | | | |")
        return out + [""]

    def after(self, times: np.ndarray) -> list[float]:
        """Average move from the entry (next open) to the close k candles after the signal, for each after offset."""
        sg = -1.0 if self.short else 1.0
        out = []
        for k in self.pc["after_offsets"]:
            t = times[times + max(k, 1) < self.n]
            out.append(float(np.mean(sg * (self.c[t + k] / self.o[t + 1] - 1))) if len(t) else float("nan"))
        return out

    def row(self, rule: tuple[int, ...], r: dict, P: int) -> str:
        h = self.exits[r["e"]]
        last = ", ".join(self.when(t) for t in r["times"][-self.R:][::-1])
        n = len(r["times"])
        rate = r["all_won"] / n
        base = self.base[P][r["e"]]
        aft = " / ".join(f"{100 * x:+.1f}" for x in self.after(r["times"]))
        won = (f"{r['all_won']} of {n} ({rate:.0%}" + (f", x{rate / base:.1f} base)" if base > 0 else ")")
               + f"; recent-weighted {r['rate_w']:.0%}")
        return (f"| {self.rtext(rule)} | max {h} bars | **{r['conf']}** | {r['streak']} | {last} | {won} | "
                f"{100 * r['avg']:+.2f}% (recent-weighted {100 * r['avg_w']:+.2f}%) | {aft} |")

    def profile(self, P: int) -> list[str]:
        """-10..0 candles: how each indicator behaved before the right moves vs other bars; +1..+10: price after."""
        import warnings
        e = len(self.exits) - 1
        sel = self.ok & (self.phase == P) & self.done[e]
        legs = self.legs()
        mv = legs[sel[legs]]
        near = np.zeros(self.n, bool)
        for k in range(-10, 11):
            near[np.clip(legs + k, 0, self.n - 1)] = True
        ot = np.flatnonzero(sel & ~self.win[e] & ~near)
        h = self.exits[e]
        out = [f"### Move starts in this phase: the candles around them ({len(mv):,} moves started in this phase "
               f"({self.wtext} within {h} candles, start = best entry of each move); vs {len(ot):,} ordinary candles)",
               ""]
        if len(mv) < 20:
            return out + ["Too few right moves to profile.", ""]
        offs = self.pc["profile_offsets"]
        rows = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for j in range(self.base_n):
                x = self.F[:, j]
                xs = x[sel][np.isfinite(x[sel])]
                if len(xs) < 50:
                    continue
                binary = bool(np.isin(np.unique(xs), (0.0, 1.0)).all())
                stat = np.nanmean if binary else np.nanmedian
                scale = 0.5 if binary else float(np.percentile(xs, 75) - np.percentile(xs, 25))
                if not scale > 0:
                    continue
                mm = [float(stat(x[mv[mv + k >= 0] + k])) for k in offs]
                oo = [float(stat(x[ot[ot + k >= 0] + k])) for k in offs]
                d = max(abs(p - q) for p, q in zip(mm, oo)) / scale
                if np.isfinite(d):
                    rows.append((d, j, mm, oo, binary))
        rows.sort(key=lambda r: -r[0])
        cols = " / ".join(str(k) for k in offs)
        out += [f"Indicators whose path before the move differs most (median; yes/no indicators: share true). Candles "
                f"{cols} (0 = signal candle).", "",
                f"| Indicator | Move starts ({cols}) | Ordinary candles ({cols}) | Difference (spreads) |",
                "|---|---|---|---|"]
        for d, j, mm, oo, binary in rows[:int(self.pc.get("profile_top", 20))]:
            f = (lambda v: f"{v:.0%}") if binary else (lambda v: f"{v:.3g}")
            out.append(f"| {self.names[j]} | {' / '.join(f(v) for v in mm)} | {' / '.join(f(v) for v in oo)} | "
                       f"{d:.2f} |")
        am, ao = self.after(mv), self.after(ot)
        ks = " / ".join(f"+{k}" for k in self.pc["after_offsets"])
        out += ["", f"Price after the signal (avg move from entry to close at candle {ks}): right moves "
                f"{' / '.join(f'{100 * x:+.2f}%' for x in am)}; other bars {' / '.join(f'{100 * x:+.2f}%' for x in ao)}.",
                ""]
        return out

    def distinct(self, lst: list[tuple[tuple[int, ...], dict]], top: int) -> list[tuple[tuple[int, ...], dict]]:
        """Skip patterns whose times overlap an already shown pattern by more than dedupe_overlap (same idea)."""
        lim = float(self.pc.get("dedupe_overlap", 0.6))
        shown: list[tuple[tuple[int, ...], dict, set]] = []
        for r, x in lst:
            ts = set(x["times"].tolist())
            if any(len(ts & s) / max(min(len(ts), len(s)), 1) > lim for _, _, s in shown):
                continue
            shown.append((r, x, ts))
            if len(shown) >= top:
                break
        return [(r, x) for r, x, _ in shown]

    def method_check(self, lst: list[tuple[tuple[int, ...], dict]], P: int) -> str:
        n = sum(x["ck_n"] for _, x in lst)
        w = sum(x["ck_w"] for _, x in lst)
        if not n:
            return "Method check: no earlier 3-in-a-row cases to test."
        base = float(np.mean(self.base[P]))
        verdict = ("better than the base rate - '3 in a row' does carry information" if w / n > base + 0.05 else
                   "about the base rate - '3 in a row' alone predicts little here; prefer long streaks"
                   if w / n > base - 0.05 else "below the base rate - streaks tend to break here")
        return (f"Method check (history before each pattern's current streak): right after 3 wins in a row, the next "
                f"time worked **{w / n:.0%}** of {n:,} cases vs base {base:.0%} -> {verdict}.")

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        if not self.load():
            return 1
        res = self.analyse(self.n)
        now = self.n - 1
        P = self.P
        k = now
        while k > 0 and self.phase[k - 1] == P:
            k -= 1
        top = int(self.pc["report_top"])
        ks = "/".join(f"+{k}" for k in self.pc["after_offsets"])
        head = ["| Pattern | Exit | Confidence | Streak (back from today) | Last 3 times, newest first | "
                f"All times in phase that worked | Avg net, all times | Avg move % after {ks} candles |",
                "|---|---|---|---|---|---|---|---|"]
        md = [f"# Phase patterns - {self.sym} {self.tf} {self.side}", "",
              f"Last bar {self.when(now)}: phase **{PHASES[P]}** since {self.when(k)} "
              f"({now - k + 1} bars). Phase: bullish = EMA9 > EMA13 > EMA20 > EMA50, bearish = the reverse, "
              "sideways = anything else.",
              f"Trade: enter next open; worked = {self.wtext}, else stop / time exit (same candle: stop first); "
              + ("held overnight (max hold " + " / ".join(str(h) for h in self.exits) + " candles). "
                 if self.intraday and self.overnight else "") +
              f"Patterns use {self.base_n} indicators and how each changed over the last "
              f"{self.pc.get('change_lookbacks')} candles (names ending _chg3 / _chg10). Streak = worked times in a row counted back from the pattern's most recent "
              f"time; >= {self.R} = Medium, longer = higher (" + ", ".join(
                  f"{r['label']} >= {r['streak_min']}" for r in self.pc["confidence_ladder"]) + "). "
              "Near-duplicate patterns (same signals) are shown once.", ""]
        md += self.moves_table()
        for q in (1, 0, -1):
            lst = res[q]
            n_conf: dict[str, int] = {}
            for _, x in lst:
                n_conf[x["conf"]] = n_conf.get(x["conf"], 0) + 1
            md += [f"## {PHASES[q]}" + (" - CURRENT PHASE" if q == P else ""), "",
                   f"Base rate (any bar in this phase: {self.wtext}): " + ", ".join(
                       f"{b:.0%} within {h} candles" for h, b in zip(self.exits, self.base[q])) + ".",
                   f"{len(lst):,} qualifying patterns: " + (", ".join(f"{v} {k}" for k, v in sorted(
                       n_conf.items(), key=lambda kv: -kv[1])) or "none") + ".", "", self.method_check(lst, q), ""]
            md += self.profile(q)
            if q == P:
                active = [(r, x) for r, x in lst if np.logical_and.reduce(self.M[list(r), now]) and self.ok[now]]
                md += ["### Active today (pattern true on the last bar)", ""]
                md += (head + [self.row(r, x, q) for r, x in self.distinct(active, top)]) if active else [
                    "None of the qualifying patterns is true on the last bar."]
                md += ["", "### Best patterns in this phase (watch list)", ""]
            md += (head + [self.row(r, x, q) for r, x in self.distinct(lst, top)]) if lst else ["No pattern qualified."]
            md.append("")
        root = self.du.root
        (root / "PATTERNS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        with (root / "patterns.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["phase", "pattern", "hold_bars", "confidence", "streak", "times", "times_won",
                        "avg_net_pct", "last_time", "active_today", "check_cases", "check_won"])
            for q, lst in res.items():
                for r, x in lst:
                    act = q == P and bool(np.logical_and.reduce(self.M[list(r), now]))
                    w.writerow([PHASES[q], self.rtext(r), self.exits[x["e"]], x["conf"], x["streak"],
                                len(x["times"]), x["all_won"], round(100 * x["avg"], 3),
                                self.when(x["times"][-1]), act, x["ck_n"], x["ck_w"]])
        self.log(f"report: {root / 'PATTERNS.md'}")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tf", default="all", help="1d, 60m, 15m, 5m, a comma list, or all (default)")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--patterns", default=str(PATTERNS_CFG))
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    tfs = ["1d", "60m", "15m", "5m"] if a.tf == "all" else [t.strip() for t in a.tf.split(",") if t.strip()]
    rc = 0
    for i, tf in enumerate(tfs):
        print(f"=== {a.symbol.upper()} {tf} ({i + 1}/{len(tfs)}) ===", flush=True)
        try:
            f = Finder(argparse.Namespace(**{**vars(a), "tf": tf}))
        except Exception as exc:  # noqa: BLE001 - one timeframe failing must not stop the others
            print(f"{tf}: cannot start ({type(exc).__name__}: {exc})", flush=True)
            rc = 1
            continue
        try:
            rc = f.run() or rc
        except Exception as exc:  # noqa: BLE001
            f.log(f"{tf} failed: {type(exc).__name__}: {exc}")
            rc = 1
        finally:
            f.du.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
