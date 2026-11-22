"""The owner's own strategy, encoded exactly and checked trade by trade against his trades file (owner 2026-11-22).

    $env:PYTHONPATH="src"; python -m stock_gplm.app.owner_strategy --symbol RELIANCE
    (options: --tf 15m; --spec config/research/owner_strategy.json; --no-check)

Rules (config/research/owner_strategy.json, from config/research/owner_trades.csv):
* ENTRY long: EMA13 crosses above EMA20, or the milestone EMA13 above EMA200; buy at the next candle's open.
  Short = mirror. No previous-day-high rule (owner: that was only the reason to skip 16 Sep).
* STOP: below the lowest low of the pullback = last run of candles with EMA13 below EMA20, up to the signal candle
  (can be days back). Shorts: above the highest high of the last run of EMA13 above EMA20.
* EXIT: signal close below EMA200 -> target EMA200; above EMA200 -> hold until a close breaches EMA20.
* Longs held overnight; shorts same day (no entry after 14:45, square-off 15:15).
Every stock is judged on its own (label from its own trades, recent trades weigh more).

Output <reports>/tuning/owner-strategy-<tf>-<SYM>/: REPORT.md (check against the owner's trades for each exit reading,
signals the owner did not list, every trade, label, today's signal), trades.csv, check.csv.
Times = TradingView candle START (IST).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .tune import CONFIG

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "config" / "research" / "owner_strategy.json"
TF_MIN = {"5m": 5, "15m": 15, "60m": 60}


def owner_rows(path: Path, sym: str, tf: str) -> list[dict[str, Any]]:
    """owner_trades.csv: DD-MM-YYYY dates; times before 09:15 are afternoon (02:30 = 14:30); a row without
    symbol / side continues the previous row's; an empty decision with an exit = take."""
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out

    def when(x: str) -> str | None:
        x = (x or "").strip()
        if not x:
            return None
        if len(x) <= 10:  # daily candle: date only
            return datetime.strptime(x, "%d-%m-%Y").strftime("%Y-%m-%d")
        d = datetime.strptime(x, "%d-%m-%Y %H:%M")
        if d.hour < 9 or (d.hour == 9 and d.minute < 15):
            d = d.replace(hour=d.hour + 12)
        return d.strftime("%Y-%m-%d %H:%M")

    prev = {"symbol": "", "tf": "", "side": "long"}
    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        for i, r in enumerate(csv.DictReader(fh), start=2):
            for k in prev:
                if (r.get(k) or "").strip():
                    prev[k] = r[k].strip()
                else:
                    r[k] = prev[k]
            if r["symbol"].upper().removesuffix("-EQ") != sym or (r["tf"] or tf) != tf:
                continue
            dec = (r.get("decision") or "").strip().lower() or ("take" if (r.get("exit_candle") or "").strip() else "?")
            out.append({"row": i, "side": r["side"].strip().lower(), "signal": when(r["signal_candle"]), "decision": dec,
                        "stop": when(r.get("stop_candle", "")), "exit": when(r.get("exit_candle", "")),
                        "reason": (r.get("exit_reason") or "").strip(), "why": (r.get("why") or "").strip(),
                        "mode": (r.get("mode") or "hindsight").strip()})
    return out


def all_owner_rows(path: Path, sym: str, tf: str) -> list[dict[str, Any]]:
    """owner_trades.csv plus the chart exports owner_trades-<SYM>-<tf>.csv next to it (owner_chart.py Export)."""
    base = owner_rows(path, sym, tf)
    newer: list[dict[str, Any]] = []
    for p in sorted(path.parent.glob(f"{path.stem}-*.csv")):
        rows = owner_rows(p, sym, tf)
        for r in rows:
            r["row"] = f"{p.stem.split('-', 1)[1]}:{r['row']}"
        newer += rows
    # owner 2026-11-22: the chart marks are the latest - they replace the old file's rows of the same side and day
    seen = {((r["side"] or "long"), (r["signal"] or "")[:10]) for r in newer}
    return [r for r in base if ((r["side"] or "long"), (r["signal"] or "")[:10]) not in seen] + newer


class OwnerStrategy:
    def __init__(self, sym: str, a: argparse.Namespace):
        from .discover import Discoverer
        self.sym, self.a = sym.upper().removesuffix("-EQ"), a
        self.spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
        self.tf = a.tf or self.spec.get("timeframe", "15m")
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        tmp = Path(tempfile.gettempdir()) / f"owner_strategy_{self.sym}.txt"
        tmp.write_text(self.sym + "\n", encoding="utf-8")
        self.du = Discoverer(self.tf, f"owner-strategy-{self.tf}-{self.sym}", cfg, "@" + str(tmp), 1)
        self.log = self.du.log

    # ------------------------------------------------------------------ helpers
    def tv(self, i: int) -> str:
        if not self.m.intraday:
            return str(np.datetime64(int(self.days[i]), "D"))
        ts = np.datetime64(int(self.end_ns[i]), "ns") + np.timedelta64(330 - TF_MIN.get(self.tf, 0), "m")
        return str(ts.astype("datetime64[m]")).replace("T", " ")

    def find(self, text: str | None) -> int | None:
        if not text:
            return None
        i = int(np.searchsorted(self.times, text))
        return i if i < len(self.times) and self.times[i] == text else None

    def exit_doc(self, side: str, variant: dict[str, Any] | None = None) -> dict[str, Any]:
        d = json.loads(json.dumps(self.spec["exit"][side]))
        d.pop("_doc", None)
        if variant:  # a version is complete on its own: start from the neutral rule, not from the default exit
            d["pullback_stop"] = {k: d["pullback_stop"][k] for k in ("fast", "slow", "buffer", "lookback")
                                  if k in d["pullback_stop"]}
            d["ema_regime"] = {k: d["ema_regime"][k] for k in ("anchor", "trail") if k in d["ema_regime"]}
        for k, v in (variant or {}).items():  # a version overrides parts of pullback_stop / ema_regime
            if isinstance(v, dict) and isinstance(d.get(k), dict):
                d[k] = {**d[k], **v}
        if side == "short" and (variant or {}).get("short_overnight"):
            d["overnight"] = True  # research only for cash (options can be held overnight)
        rg = d.get("ema_regime") or {}
        if rg.get("after_target") == "prev_close_far_or_touch" and "far" not in rg:
            far = getattr(self, "far", {"unit": "pct", "value": 0.003})  # calibrated from the owner's examples
            rg.update(far=far["value"], far_unit=far["unit"], far_ema=int(self.entry_rule(None).get("ema", 13)))
        return d

    def dist(self, t: int, ema: int = 13) -> tuple[float, float]:
        """Signed distance of the close from EMA(ema) at candle t: (fraction of price, ATR multiples)."""
        from .strategy_engine import _ema_of
        e = float(_ema_of(self.m, ema)[t])
        c = float(self.m.close[t])
        atr = float(self.m.natr[t]) * c
        return (c - e) / e, ((c - e) / atr if np.isfinite(atr) and atr > 0 else float("nan"))

    def is_far(self, t: int) -> bool:
        p, a = self.dist(t, int(self.entry_rule(None).get("ema", 13)))
        return (abs(a) > self.far["value"]) if self.far["unit"] == "atr" else (abs(p) > self.far["value"])

    def calibrate(self) -> list[str]:
        """'Far from EMA 13' from the owner's examples (2026-11-22): the line between the farthest 'fine' cross and the
        nearest 'far' one; ATR multiples preferred (scales to every stock / timeframe), % of price if only that
        separates them. entry_rule.max_dist_pct is used when there are no examples."""
        rule = self.entry_rule(None)
        md_ = rule.get("max_dist_pct", 0.003)
        self.far = {"unit": "pct", "value": 0.003 if md_ == "auto" else float(md_), "how": "fixed (entry_rule.max_dist_pct)"}
        ex = [(x, "far") for x in rule.get("far_examples") or []] + [(x, "fine") for x in rule.get("ok_examples") or []]
        if not ex:
            return []
        md = ["", "## 0. 'Far from EMA 13' - from your examples", "",
              "| Candle | You said | Close | EMA 13 | Distance % | Distance in ATR |", "|---|---|---|---|---|---|"]
        vals: dict[str, list[tuple[float, float]]] = {"far": [], "fine": []}
        from .strategy_engine import _ema_of
        e13 = _ema_of(self.m, int(rule.get("ema", 13)))
        for x, kind in ex:
            i = self.find(x)
            if i is None:
                md.append(f"| {x} | {kind} | no {self.tf} candle | | | |")
                continue
            p, a = self.dist(i, int(rule.get("ema", 13)))
            vals[kind].append((abs(p), abs(a)))
            md.append(f"| {x} | {kind} | {self.m.close[i]:.2f} | {e13[i]:.2f} | {100 * p:+.2f}% | {a:+.2f} |")
        if vals["far"] and vals["fine"]:
            for k, unit in ((1, "atr"), (0, "pct")):
                lo, hi = max(v[k] for v in vals["fine"]), min(v[k] for v in vals["far"])
                if lo < hi:
                    self.far = {"unit": unit, "value": (lo + hi) / 2, "how": f"midway between your farthest fine "
                                f"({lo:.2f}{' ATR' if unit == 'atr' else ''}) and nearest far"
                                f" ({hi:.2f}{' ATR' if unit == 'atr' else ''})" if unit == "atr" else
                                f"midway between your farthest fine ({100 * lo:.2f}%) and nearest far ({100 * hi:.2f}%)"}
                    break
            else:
                self.far["how"] += " - your examples overlap, kept the fixed value"
        v = self.far
        md += ["", f"**Far = more than {v['value']:.2f} ATR from EMA 13**" if v["unit"] == "atr" else
               f"**Far = more than {100 * v['value']:.2f}% from EMA 13**", f" ({v['how']}). Used for the pullback entry "
               "and for the 'close below the previous close' exit after EMA 200."]
        self.log("far from EMA 13 = " + (f"{v['value']:.2f} ATR" if v["unit"] == "atr" else f"{100 * v['value']:.2f}%")
                 + f" ({v['how']})")
        return md

    def entry_rule(self, variant: dict[str, Any] | None) -> dict[str, Any]:
        """Default run: the spec's entry_rule. A check version: plain entry unless the version sets "entry"."""
        base = dict(self.spec.get("entry_rule") or {})
        base.pop("_doc", None)
        if variant is None:
            return base
        return {**base, "pullback": False, **(variant.get("entry") or {})}

    def fills(self, side: str, arr: np.ndarray, rule: dict[str, Any]) -> dict[int, tuple[int, float] | None]:
        """Owner pullback entry (2026-11-22): cross candle closing more than max_dist_pct from EMA(ema) -> no entry at
        the next open; buy (short: sell) at EMA(ema) on the first later candle that touches it (limit at the previous
        candle's EMA, the open if it gaps through), while EMA(ema) is still above (short: below) EMA(cancel_ema);
        no trade if that ends first."""
        from .strategy_engine import _ema_of
        m, n = self.m, len(self.m)
        e, ec = _ema_of(m, int(rule.get("ema", 13))), _ema_of(m, int(rule.get("cancel_ema", 20)))
        md_ = rule.get("max_dist_pct", 0.003)
        mx, wait, S = (0.003 if md_ == "auto" else float(md_)), int(rule.get("max_wait", 400)), side == "short"
        out: dict[int, tuple[int, float] | None] = {}
        auto = rule.get("max_dist_pct") == "auto" or rule.get("far_examples")
        for t in np.asarray(arr).tolist():
            if not np.isfinite(e[t]) or not (self.is_far(t) if auto else abs(m.close[t] - e[t]) / e[t] > mx):
                continue  # close enough to EMA 13: normal entry at the next open
            out[t] = None
            for j in range(t + 1, min(n, t + 1 + wait)):
                if (e[j] >= ec[j]) if S else (e[j] <= ec[j]):
                    break  # the cross undid itself before price came back
                lv = float(e[j - 1])
                if (m.high[j] >= lv) if S else (m.low[j] <= lv):
                    o = float(m.open[j])
                    out[t] = (j, max(o, lv) if S else min(o, lv))
                    break
        return out

    def sim(self, side: str, fired: np.ndarray, variant: dict[str, Any] | None = None, independent: bool = True
            ) -> list[dict[str, Any]]:
        from .strategy_engine import Exit, simulate
        L = self.L
        rule = self.entry_rule(variant)
        fl = self.fills(side, fired, rule) if rule.get("pullback") else None
        return simulate(self.m, np.zeros(len(self.m), bool), (self.a0, len(self.m)), Exit.of(self.exit_doc(side, variant)),
                        L["buy"], L["sell"], side == "short", independent=independent, fired=np.asarray(fired, np.int64),
                        fills=fl)

    def owner_result(self, r: dict[str, Any], side: str) -> float | None:
        """Your trade's result as the chart shows it: entry at the next open, exit at the square-off candle's close
        (at the SL candle's low / high when the reason is a stop), before costs."""
        i, je, js = self.find(r["signal"]), self.find(r["exit"]), self.find(r["stop"])
        if i is None or je is None or i + 1 >= len(self.m):
            return None
        ent = float(self.m.open[i + 1])
        px = float(self.m.close[je])
        if r["reason"].lower().startswith("stop") and js is not None:
            px = float(self.m.high[js] if side == "short" else self.m.low[js])
        return (ent - px) / ent if side == "short" else (px - ent) / ent

    def diag(self, i: int, je: int | None, side: str) -> dict[str, Any]:
        """What the chart looked like at your entry / exit candle (owner 2026-11-22: to learn your 'far' and exits)."""
        from .strategy_engine import _ema_of
        S = side == "short"
        e13, e20 = _ema_of(self.m, 13), _ema_of(self.m, 20)
        p, a = self.dist(i)
        out = {"entry_dist_pct": round(100 * p, 2), "entry_dist_atr": round(a, 2),
               "entry_touched_ema13": bool(self.m.high[i] >= e13[i] if S else self.m.low[i] <= e13[i]),
               "entry_far": self.is_far(i)}
        if je is not None:
            p2, a2 = self.dist(je)
            out.update(exit_dist_pct=round(100 * p2, 2), exit_dist_atr=round(a2, 2),
                       exit_close_beyond_prev=bool(self.m.close[je] > self.m.close[je - 1] if S
                                                   else self.m.close[je] < self.m.close[je - 1]),
                       exit_touched_ema20=bool(self.m.high[je] >= e20[je] if S else self.m.low[je] <= e20[je]))
        return out

    def compare(self, rows: list[dict[str, Any]], variant: dict[str, Any] | None, W: int
                ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Your trades vs the program's whole trade list (one position per side): which of yours we also take
        (entry within W candles), how close our exit is, which we miss."""
        res = {"yours": 0, "matched": 0, "exit_same": 0, "exit_near": 0, "our_pct": 0.0, "your_pct": 0.0,
               "missed": 0}
        detail: list[dict[str, Any]] = []
        for side in self.sig:
            ours = self.sim(side, self.sig[side], variant)
            used: set[int] = set()
            for r in rows:
                if (r["side"] or "long") != side or r["decision"] != "take" or not r["exit"]:
                    continue
                i = self.find(r["signal"])
                if i is None:
                    continue
                res["yours"] += 1
                yr = self.owner_result(r, side)
                best, bd = None, W + 1
                for k, x in enumerate(ours):
                    d = min(abs(int(x["t"]) - i), abs(int(x["t"]) + 1 - i))
                    if k not in used and d < bd:
                        best, bd = k, d
                je = self.find(r["exit"])
                row = {"side": side, "your_entry": r["signal"], "your_exit": r["exit"], "your_reason": r["reason"],
                       "your_pct": round(100 * yr, 2) if yr is not None else "", "why": r["why"], **self.diag(i, je, side)}
                if best is None:
                    res["missed"] += 1
                    row.update(our_entry="", our_exit="", our_how="missed", our_pct="")
                else:
                    used.add(best)
                    x = ours[best]
                    e = int(x["exit_index"])
                    res["matched"] += 1
                    if je is not None:
                        res["exit_same"] += e == je
                        res["exit_near"] += abs(e - je) <= 4
                    res["our_pct"] += 100 * x["net_return"]
                    res["your_pct"] += 100 * (yr or 0.0)
                    row.update(our_entry=self.tv(int(x["t"]) + 1) + f" @ {x['entry_price']:.2f}", our_exit=self.tv(e),
                               our_how=x["exit_reason"], our_pct=round(100 * x["net_return"], 2),
                               exit_diff=(e - je) if je is not None else "")
                detail.append(row)
        return res, detail

    def stop_of(self, side: str, t: int, mode: str | None = None, strength: int | None = None
                ) -> tuple[float, int] | None:
        from .strategy_engine import _ema_of, pullback_level
        pb = self.spec["exit"][side]["pullback_stop"]
        entry = float(self.m.open[t + 1]) if t + 1 < len(self.m) else None
        return pullback_level(self.m.low, self.m.high, _ema_of(self.m, int(pb.get("fast", 13))),
                              _ema_of(self.m, int(pb.get("slow", 20))), t, side == "short", int(pb.get("lookback", 400)),
                              mode or pb.get("mode", "lowest"), int(strength or pb.get("strength", 1)), entry)

    def wt(self, idx: np.ndarray) -> np.ndarray:
        rc = self.spec.get("recency") or {}
        age = self.sid[-1] - self.sid[idx]
        return (1.0 + np.maximum(age, 0) / float(rc.get("decay_sessions", 100))) ** -float(rc.get("power", 2))

    def stats(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        done = [t for t in trades if t["exit_reason"] != "OPEN_END"]
        if not done:
            return {"trades": 0, "label": "LC", "text": "no closed trades"}
        net = np.array([t["net_return"] for t in done])
        w = self.wt(np.array([t["t"] for t in done], dtype=np.int64))
        s = {"trades": len(done), "losses": int((net <= 0).sum()), "win": float((net > 0).mean()),
             "avg": float(net.mean()), "total": float(net.sum()), "win_w": float(w[net > 0].sum() / w.sum()),
             "avg_w": float((w * net).sum() / w.sum())}
        L = self.spec["labels"]
        hc, hl, mc = L["HC"], L["HCLT"], L["MC"]
        lab = "LC"
        if s["avg_w"] > 0 and s["total"] > 0:
            if s["trades"] >= hc["min_trades"] and s["win_w"] >= hc["min_win_w"]:
                lab = "HC"
            elif hl["min_trades"] <= s["trades"] <= hl["max_trades"] and s["losses"] <= (
                    0 if s["trades"] < hl.get("small_below", 5) else hl.get("max_losses", 1)):
                lab = "HCLT"
            elif s["win_w"] >= mc["min_win_w"]:
                lab = "MC"
        s["label"] = lab
        s["text"] = (f"{s['trades']} closed trades, {s['trades'] - s['losses']} won ({s['win']:.0%}), avg "
                     f"{100 * s['avg']:+.2f}%, total {100 * s['total']:+.1f}% | recent-weighted win {s['win_w']:.0%}, "
                     f"avg {100 * s['avg_w']:+.2f}%")
        return s

    # ------------------------------------------------------------------ data
    def load(self) -> bool:
        from . import strategy_dsl as dsl
        from . import tune_eval as te
        self.log(f"{self.sym} {self.tf}: loading candles")
        L = te._load(self.du.stocks[0], self.du.config, [self.tf], bool(self.du.t["use_minute"])).get(self.tf)
        if L is None:
            self.log(f"no {self.tf} data for {self.sym}")
            return False
        self.L, self.m, ctx = L, L["m"], L["ctx"]
        n = len(self.m)
        self.days = L["days"]
        self.end_ns = np.asarray(self.m.end_ns, dtype=np.int64)
        self.sid = (np.asarray(self.m.session_id, dtype=np.int64) if self.m.intraday else np.arange(n, dtype=np.int64))
        self.times = np.array([self.tv(i) for i in range(n)])
        anchor = int(self.spec["exit"]["long"]["ema_regime"].get("anchor", 200))
        self.a0 = max(int(np.searchsorted(L["ses"], self.du.t["tune_start"])), anchor)  # EMA 200 warm-up
        floor = np.asarray(dsl.signal(self.du.floor, ctx), bool)
        self.sig: dict[str, np.ndarray] = {}
        self.kind: dict[str, dict[int, str]] = {}
        for side, ent in self.spec["entries"].items():
            mask = np.zeros(n, bool)
            kinds: dict[int, list[str]] = {}
            for name, expr in ent.items():
                mk = np.asarray(dsl.signal(expr, ctx), bool) & floor
                mk[:self.a0] = False
                for t in np.flatnonzero(mk):
                    kinds.setdefault(int(t), []).append(name)
                mask |= mk
            self.sig[side] = np.flatnonzero(mask)
            self.kind[side] = {t: "+".join(v) for t, v in kinds.items()}
        self.log(f"{n:,} candles {self.times[0]} .. {self.times[-1]}; signals from {self.times[self.a0]}: "
                 + ", ".join(f"{s} {len(v)}" for s, v in self.sig.items()))
        return True

    # ------------------------------------------------------------------ check against the owner's trades
    def check(self, root: Path) -> list[str]:
        ck = self.spec["check"]
        rows = all_owner_rows(REPO / ck["owner_trades"], self.sym, self.tf)
        if not rows:
            return ["", "## Check against your trades", "", f"No rows for {self.sym} {self.tf} in {ck['owner_trades']}."]
        W = int(ck.get("match_candles", 2))
        variants = ck["variants"]
        # --- your whole trade list vs ours, per version (owner 2026-11-22: 67 marked trades)
        yours = [(r, r["side"] or "long") for r in rows if r["decision"] == "take" and r["exit"]]
        yr = [self.owner_result(r, s) for r, s in yours]
        yr = [x for x in yr if x is not None]
        head = (f"Your {len(yr)} trades (entry next open, exit at the square-off close, before costs): "
                f"{sum(x > 0 for x in yr)} won ({sum(x > 0 for x in yr) / max(1, len(yr)):.0%}), total "
                f"{100 * sum(yr):+.1f}%." if yr else "")
        md0 = ["", "## 1. Your trades vs the program's trades", "", head, "",
               f"Matched = the program also took a trade of that side within {W} candles of your entry. Exit same = same "
               "candle; near = within 4 candles. Totals over the matched trades (program after costs, yours before).", "",
               "| Version | Matched of yours | Missed | Exit same / near | Program total | Your total | "
               "RECENT: matched | RECENT: exit same / near |",
               "|---|---|---|---|---|---|---|---|"]
        # owner 2026-11-22: the latest trades follow the rules best (earlier ones may break them) -> judged on recent
        dates = sorted(r["signal"][:10] for r in rows if r["signal"])
        cut = str(np.datetime64(dates[-1]) - np.timedelta64(int(ck.get("recent_days", 92)), "D")) if dates else ""
        recent = [r for r in rows if r["signal"] and r["signal"][:10] >= cut]
        md0[3] += f" Recent = your trades from {cut} on ({sum(1 for r in recent if r['decision'] == 'take' and r['exit'])})."
        best_v, best_key, detail_best = None, None, []
        for v in [None] + variants:
            res, det = self.compare(rows, v, W)
            rr, _ = self.compare(recent, v, W)
            nm = "DEFAULT (config)" if v is None else v["name"]
            md0.append(f"| {nm} | {res['matched']} of {res['yours']} | {res['missed']} | {res['exit_same']} / "
                       f"{res['exit_near']} | {res['our_pct']:+.1f}% | {res['your_pct']:+.1f}% | "
                       f"{rr['matched']} of {rr['yours']} | {rr['exit_same']} / {rr['exit_near']} |")
            key = (rr["matched"], rr["exit_near"], rr["exit_same"], res["matched"], res["exit_near"])
            if best_key is None or key > best_key:
                best_v, best_key, detail_best = nm, key, det
            if v is None:
                detail_default = det
            self.log(f"vs your trades - {nm}: matched {res['matched']}/{res['yours']}, exit same {res['exit_same']}, "
                     f"near {res['exit_near']}, program {res['our_pct']:+.1f}% vs yours {res['your_pct']:+.1f}%")
        md0 += ["", f"Most like you: **{best_v}**.", "", "Trade by trade (DEFAULT):", "",
                "Your entry: distance of the close from EMA 13 (% / ATR), did the candle touch EMA 13. Your exit: distance "
                "from EMA 13, closed beyond the previous close, touched EMA 20.", "",
                "| Side | Your entry | Entry dist % / ATR, touched 13 | Your exit | Exit dist % / ATR, close<prev, touched 20 "
                "| Your reason | Yours | Program entry | Program exit | How | Program | Exit diff | Your note |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        yn = lambda b: "y" if b is True else ("n" if b is False else "")  # noqa: E731
        for d in sorted(detail_default, key=lambda d: d["your_entry"], reverse=True):  # latest first (owner: they count most)
            md0.append(f"| {d['side']} | {d['your_entry']} | {d['entry_dist_pct']:+.2f}% / {d['entry_dist_atr']:+.2f}, "
                       f"{yn(d['entry_touched_ema13'])}{' FAR' if d['entry_far'] else ''} | {d['your_exit']} | "
                       + (f"{d['exit_dist_pct']:+.2f}% / {d['exit_dist_atr']:+.2f}, {yn(d['exit_close_beyond_prev'])}, "
                          f"{yn(d['exit_touched_ema20'])}" if "exit_dist_pct" in d else "")
                       + f" | {d['your_reason']} | {d['your_pct']} | "
                       f"{d['our_entry'] or '**missed**'} | {d['our_exit']} | {d['our_how']} | {d['our_pct']} | "
                       f"{d.get('exit_diff', '')} | {d['why']} |")
        with (root / "vs_yours.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["side", "your_entry", "your_exit", "your_reason", "your_pct", "our_entry",
                                               "our_exit", "our_how", "our_pct", "exit_diff", "why", "entry_dist_pct",
                                               "entry_dist_atr", "entry_touched_ema13", "entry_far", "exit_dist_pct",
                                               "exit_dist_atr", "exit_close_beyond_prev", "exit_touched_ema20"])
            w.writeheader()
            w.writerows(detail_default)
        score = {v["name"]: [0, 0] for v in variants}  # exit candle exact, within 1 candle
        readings = (("lowest", 1, "lowest"), ("last_swing", 1, "swing-1"), ("last_swing", 2, "swing-2"),
                    ("last_swing", 3, "swing-3"))
        stop_hits = {lab: 0 for _, _, lab in readings}
        out_rows: list[dict[str, Any]] = []
        md = md0 + ["", "## 1d. Row by row from YOUR candle (one trade each, ignores open positions)", "",
              f"Our signal = nearest {'/'.join(self.spec['entries'])} signal of the same side within {W} candles of "
              "yours (shift = our candle minus yours). Exit and stop are computed from YOUR signal candle when we have "
              "no signal there. Exit columns: our exit candle per reading of your exit rule; ✓ = same candle, "
              "~ = one candle off.", "",
              "| Row | Side | Your signal | Decision | Our signal (shift) | Your stop candle | Our stop candle (low/high) "
              "| Regime | Your exit | " + " | ".join(v["name"] for v in variants) + " |",
              "|---|---|---|---|---|---|---|---|---|" + "---|" * len(variants)]
        listed: set[tuple[str, int]] = set()
        for r in rows:
            side = r["side"] if r["side"] in self.sig else "long"
            i = self.find(r["signal"])
            if i is None:
                md.append(f"| {r['row']} | {side} | {r['signal']} | {r['decision']} | no {self.tf} candle at that time "
                          "|  |  |  |  |" + " |" * len(variants))
                continue
            near = [int(t) for t in self.sig[side] if abs(int(t) - i) <= W]
            t = min(near, key=lambda x: abs(x - i)) if near else i
            if near:
                listed.add((side, t))
            ours = f"{self.tv(t)} {self.kind[side].get(t, '')} ({t - i:+d})" if near else "**none**"
            js = self.find(r["stop"])
            your_stop = (f"{r['stop']} ({self.m.high[js] if side == 'short' else self.m.low[js]:.2f})"
                         if js is not None else (r["stop"] or "-"))
            stops = []
            for mode, k, lab in readings:
                sx = self.stop_of(side, t, mode, k)
                if sx is None:
                    stops.append(f"{lab}: none")
                    continue
                if js is not None:
                    stop_hits[lab] += sx[1] == js
                stops.append(f"{lab}: {self.tv(sx[1])[5:]} ({sx[0]:.2f})"
                             + (f" {'✓' if sx[1] == js else f'{sx[1] - js:+d}'}" if js is not None else ""))
            st = self.stop_of(side, t)
            our_stop = "<br>".join(stops)
            from .strategy_engine import _ema_of
            rg = self.spec["exit"][side]["ema_regime"]
            na, nt = int(rg.get("anchor", 200)), int(rg.get("trail", 20))
            anc = float(_ema_of(self.m, na)[t])
            c0 = float(self.m.close[t])
            regime = (f"target EMA{na}" if (c0 > anc if side == "short" else c0 < anc) else f"trail EMA{nt}") + \
                     f" (close {c0:.1f} / EMA{na} {anc:.1f})"
            je = self.find(r["exit"])
            cells = []
            rec = {"row": r["row"], "side": side, "your_signal": r["signal"], "decision": r["decision"],
                   "our_signal": self.tv(t) if near else "", "shift": (t - i) if near else "",
                   "your_stop": r["stop"] or "", "our_stop": self.tv(st[1]) if st else "",
                   "our_stop_level": round(st[0], 2) if st else "", "your_exit": r["exit"] or "",
                   "your_reason": r["reason"]}
            for v in variants:
                tr = self.sim(side, np.array([t]), v)
                if not tr:
                    cells.append("no trade (no stop below entry)")
                    rec[v["name"]] = "no trade"
                    continue
                x = tr[0]
                e = int(x["exit_index"])
                mark = ""
                if je is not None:
                    d = e - je
                    mark = " ✓" if d == 0 else (" ~" if abs(d) == 1 else f" ({d:+d})")
                    score[v["name"]][0] += d == 0
                    score[v["name"]][1] += abs(d) <= 1
                cells.append(f"{self.tv(e)} {x['exit_reason']} {100 * x['net_return']:+.2f}%{mark}")
                rec[v["name"]] = f"{self.tv(e)} {x['exit_reason']} {100 * x['net_return']:+.2f}%"
            md.append(f"| {r['row']} | {side} | {r['signal']} | {r['decision']} | {ours} | {your_stop} | {our_stop} | "
                      f"{regime} | {(r['exit'] or '-') + (' ' + r['reason'] if r['reason'] else '')} | "
                      + " | ".join(cells) + " |")
            out_rows.append(rec)
        taken = sum(1 for r in rows if r["decision"] == "take" and r["exit"])
        md += ["", f"Exit readings ({taken} rows with your exit): "
               + "; ".join(f"**{k}**: {a} same candle, {b} within 1" for k, (a, b) in score.items()), ""]
        best = max(score.items(), key=lambda kv: (kv[1][0], kv[1][1]))[0]
        md.append(f"Closest to your exits: **{best}**.")
        n_stop = sum(1 for r in rows if self.find(r["stop"]) is not None)
        md.append(f"Stop candle same as yours ({n_stop} rows with a stop; swing-k = last swing low with k candles "
                  "each side): " + ", ".join(f"{k} {v}" for k, v in stop_hits.items()) + ".")
        # our signals in your period that you did not list
        ts = [self.find(r["signal"]) for r in rows]
        ts = [x for x in ts if x is not None]
        if ts:
            lo_t, hi_t = min(ts), max(ts)
            extra = [(s, int(t)) for s, arr in self.sig.items() for t in arr
                     if lo_t - 30 <= t <= hi_t + 30 and (s, int(t)) not in listed]
            md += ["", f"## 2. Our signals {self.tv(lo_t)[:10]} .. {self.tv(hi_t)[:10]} that are NOT in your file", "",
                   "Tell me take or skip for these - they become extra labels for the success / failure learning.", "",
                   "| Side | Signal candle | Trigger | Result (default exit) |", "|---|---|---|---|"]
            for s, t in sorted(extra, key=lambda x: x[1]):
                tr = self.sim(s, np.array([t]))
                res = (f"{self.tv(tr[0]['exit_index'])} {tr[0]['exit_reason']} {100 * tr[0]['net_return']:+.2f}%"
                       if tr else "no trade (entry beyond the pullback stop or after 14:45)")
                md.append(f"| {s} | {self.tv(t)} | {self.kind[s].get(t, '')} | {res} |")
        with (root / "check.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(out_rows[0]) if out_rows else ["row"])
            w.writeheader()
            w.writerows(out_rows)
        self.log("check: " + "; ".join(f"{k}: {a} same / {b} within 1 of {taken}" for k, (a, b) in score.items()))
        self.log("stop candle same as yours: " + ", ".join(f"{k} {v}" for k, v in stop_hits.items()) + f" of {n_stop}")
        self.score, self.taken = score, taken
        return md

    def versions(self) -> list[str]:
        """Every exit version over every signal of this timeframe (owner 2026-11-22: win rate on all TFs)."""
        score, taken = getattr(self, "score", {}), getattr(self, "taken", 0)
        short_note = " (research only: overnight cash shorts are not possible)" if not self.m.intraday else ""
        md = ["", f"## 1b. Each exit version over every signal {self.tv(self.a0)[:10]} .. {self.tv(len(self.m) - 1)[:10]}"
              " (one position per side at a time)", "",
              f"| Version | Exit candles like yours (same / within 1) | Long | Short{short_note} |", "|---|---|---|---|"]
        for v in self.spec["check"]["variants"]:
            cells = []
            for side, arr in self.sig.items():
                s = self.stats(self.sim(side, arr, v))
                cells.append(f"{s['label']}: {s['text']}")
            a, b = score.get(v["name"], (0, 0))
            md.append(f"| {v['name']} | " + (f"{a} / {b} of {taken}" if taken else "-") + " | " + " | ".join(cells) + " |")
            self.log(f"{v['name']}: " + " | ".join(f"{side} {c.split(' | ')[0]}" for side, c in zip(self.sig, cells)))
        return md

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        t0 = time.monotonic()
        if not self.load():
            return 1
        root = self.du.root
        cal = self.calibrate()  # before any simulation: 'far' feeds the entry and the after-EMA200 exit
        md = [f"# Owner strategy {self.spec.get('name', '')} - {self.sym} {self.tf}", "",
              f"Data {self.tv(self.a0)} .. {self.tv(len(self.m) - 1)} (candle START times, IST). "
              "Every stock is judged on its own trades; recent trades weigh more.", "", "Exit rules:"]
        from .strategy_engine import Exit
        for side in self.sig:
            md.append(f"- **{side}**: {Exit.of(self.exit_doc(side)).text(None, side == 'short')}")
        md += cal
        if not self.a.no_check:
            md += self.check(root)
        md += self.versions()
        # every trade, one position per side at a time
        allrows: list[dict[str, Any]] = []
        md += ["", "## 3. Every trade (default exit reading, one position per side at a time)", ""]
        for side, arr in self.sig.items():
            tr = self.sim(side, arr)
            s = self.stats(tr)
            md.append(f"- **{side}: {s['label']}** - {s['text']}")
            for x in tr:
                st = self.stop_of(side, int(x["t"]))
                still = x["exit_reason"] == "OPEN_END"
                allrows.append({"side": side, "signal": self.tv(x["t"]), "trigger": self.kind[side].get(int(x["t"]), ""),
                                "entry": round(x["entry_price"], 2),
                                "stop_candle": self.tv(st[1]) if st else "", "stop": round(st[0], 2) if st else "",
                                "closed": "-" if still else self.tv(x["exit_index"]),
                                "exit_price": round(x["exit_price"], 2), "how": "still running" if still else
                                x["exit_reason"], "result_pct": round(100 * x["net_return"], 2),
                                "mfe_pct": round(100 * x["mfe"], 2), "mae_pct": round(100 * x["mae"], 2)})
        allrows.sort(key=lambda r: r["signal"])
        md += ["", "| Side | Signal candle | Trigger | Buy/sell at | Stop (candle) | Closed | Exit price | How | Result |",
               "|---|---|---|---|---|---|---|---|---|"]
        md += [f"| {r['side']} | {r['signal']} | {r['trigger']} | {r['entry']} | {r['stop']} ({r['stop_candle']}) | "
               f"{r['closed']} | {r['exit_price']} | {r['how']} | {r['result_pct']:+.2f}% |" for r in allrows]
        # today
        now = len(self.m) - 1
        today = [(s, self.kind[s][now]) for s in self.sig if now in self.kind[s]]
        md += ["", "## 4. Signal on the last candle", ""]
        md += [f"- {s}: {k} at {self.tv(now)} - enter at the next open" for s, k in today] or \
              [f"None at {self.tv(now)}."]
        with (root / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(allrows[0]) if allrows else ["side"])
            w.writeheader()
            w.writerows(allrows)
        (root / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        self.log(f"{self.sym} {self.tf}: {len(allrows)} trades ({time.monotonic() - t0:.0f}s); report {root / 'REPORT.md'}")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--tf", default=None)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--no-check", action="store_true")
    a = ap.parse_args(argv)
    os_ = OwnerStrategy(a.symbol, a)
    try:
        return os_.run()
    finally:
        os_.du.close()


if __name__ == "__main__":
    sys.exit(main())
