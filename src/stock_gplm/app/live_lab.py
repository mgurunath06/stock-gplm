"""Live lab (M8 prototype, PAPER/TEST only): Oracle live 1m feed -> library strategies -> alerts -> web page.

Pull-only: every few seconds the PC reads the append-only ``~/gplm-live/data/<date>/bars.jsonl`` on the
``sne-cloud`` alias from the last byte offset (``tail -c``), so nothing is re-downloaded. 1-minute bars are
aggregated into 5m/15m bars; when a bar *completes*, every applicable long library strategy (default params)
is evaluated on it. A rising entry edge creates a PAPER alert that is then tracked on 1-minute bars
(stop / target from the strategy's ATR exit, time exit, end of day).

Not a LIVE decision: no strategy here is approved; probabilities/evidence are not computed. Stale feed
(heartbeat older than ``--stale-after`` seconds) suppresses new alerts; bars that arrive late are used for
state but never alerted as current (``CATCH_UP``).

Run: ``python -m stock_gplm.app.live_lab --port 8765`` then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import itertools
import os
import json
import queue
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=DeprecationWarning)

from . import notify as NT
from . import strategy_dsl as dsl
from .strategy_library import StrategyLibrary

IST = ZoneInfo("Asia/Kolkata")
HOST = "sne-cloud"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ClearAllForwardings=yes", HOST]
REPO = Path(__file__).resolve().parents[3]
STATIC = Path(__file__).with_name("static") / "live.html"
INDICES = {"NIFTY", "BANKNIFTY", "INDIAVIX"}  # context only: never evaluated for signals
BANKS = {"HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK"}  # compared with BANK NIFTY
TFS = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}  # 1d/1w only after the close (end-of-day job)
CONTRACT = "stock-gplm.live-lab.v1"


def now_ist() -> datetime:
    return datetime.now(IST)


def ssh(cmd: str, timeout: int = 40) -> bytes:
    r = subprocess.run(SSH + [cmd], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[:200] or f"ssh exit {r.returncode}")
    return r.stdout


CTL = "~/stock-gplm-venv/bin/python ~/gplm-live/gplm_live_ctl.py"
LOGIN_URL = "https://api.shoonya.com/OAuthlogin/investor-entry-level/login?api_key=FA51711_U&route_to=FA51711"


def ctl(action: str, stdin: str | None = None) -> dict[str, Any]:
    """Run the Oracle control script; the OAuth code travels only on stdin."""
    r = subprocess.run(SSH + [f"{CTL} {action}"], input=(stdin + "\n").encode() if stdin else b"",
                       capture_output=True, timeout=60)
    try:
        return json.loads(r.stdout.decode().strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "error": (r.stderr.decode(errors="replace") or "no response")[:200]}


OLLAMA = "http://127.0.0.1:11434/api/chat"  # local only: never a *-cloud model
EXPLAIN_MODEL = "qwen3:14b"


def explain_setup(st: dict[str, Any], prof: dict[str, Any], changes: list[dict[str, Any]], intra: dict[str, Any] | None,
                  intra_ev: list[dict[str, Any]], last: float | None) -> str:
    """Plain-English explanation of one setup by the LOCAL Ollama model. Facts only from the given numbers."""
    import urllib.request
    facts = {
        "stock": st["symbol"], "timeframe": st["tf"], "signal_bar_closed": st["closed_at"], "entry": st["entry"],
        "stop": st["stop"], "target": st["target"], "reward_to_risk": st["rr"], "valid_until": st["until"][11:],
        "status": st["state"], "last_price": last, "strategies_agreeing": f"{st['n']} of {st['applicable']}",
        "strategy_examples": st["names"][:5],
        "confidence": (lambda c: {"rating": c.get("label"), "past_signals_n": c.get("n"),
                                  "past_win_rate_pct": round(c["p"] * 100, 1) if c.get("p") is not None else None,
                                  "conservative_win_rate_lower_bound_pct": round(c["p_lo"] * 100, 1)
                                  if c.get("p_lo") is not None else None,
                                  "average_return_per_past_trade_pct": c.get("avg"),
                                  "evidence_basis": c.get("basis"),
                                  "cautious_net_per_trade_pct": c.get("cautious_pct"),
                                  "break_even_win_rate_pct": round(c["break_even"] * 100, 1)
                                  if c.get("break_even") is not None else None,
                                  "rule": c.get("rule") or "no backtest evidence: live replay only, capped at LOW"}
                       )(st.get("confidence") or {}),
        "behaviour_profile": {k: v.get("label") for k, v in (prof or {}).items()},
        "behaviour_changes": [c["text"] for c in changes][:4], "intraday_now": intra,
        "intraday_events_today": [f"{e['at']} {e['headline']}" for e in intra_ev][:5],
    }
    sys_p = ("You explain a PAPER trading setup from a research tool to a retail trader in India. Use ONLY the facts "
             "given (JSON). Never invent numbers, news or reasons. Plain English, no jargon without a short gloss. "
             "Format exactly:\nWhat it is: <1-2 sentences>\nWhy it fired: <1-2 sentences>\nHow reliable: <1-2 sentences, "
             "explain confidence = past win rate over n signals on this stock>\nMain risks: <2-3 short bullets>\n"
             "What a careful trader would watch: <1-2 sentences, using the entry/stop/target levels>\n"
             "End with: 'Research only - not advice.'")
    body = json.dumps({"model": EXPLAIN_MODEL, "stream": False, "think": False, "keep_alive": "30m",
                       "options": {"temperature": 0.2},
                       "messages": [{"role": "system", "content": sys_p},
                                    {"role": "user", "content": json.dumps(facts, default=str)}]}).encode()
    req = urllib.request.Request(OLLAMA, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["message"]["content"].strip()


def past_trades(m: np.ndarray, o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, atr: np.ndarray,
                stop_atr: float | None, target_atr: float | None, hold: int) -> list[float]:
    """Replay a strategy's earlier rising-edge signals on this frame with its own exit (stop/target/time).
    Entry at the signal bar's close; excludes the current bar; returns % returns of completed trades."""
    out = []
    edges = np.flatnonzero(m[1:-1] & ~m[:-2]) + 1
    for i in edges:
        if not np.isfinite(atr[i]):
            continue
        e = c[i]
        sp = e - stop_atr * atr[i] if stop_atr else None
        tp = e + target_atr * atr[i] if target_atr else None
        end = i + hold
        if end >= len(c) - 1:
            continue  # trade would still be running: not evidence yet
        r = None
        for j in range(i + 1, end + 1):
            if sp is not None and lo[j] <= sp:
                r = sp / e - 1
                break
            if tp is not None and h[j] >= tp:
                r = tp / e - 1
                break
        out.append((r if r is not None else c[end] / e - 1) * 100)
    return out


def confidence(trades: Sequence[float], conflict: bool) -> dict[str, Any]:
    """PRD §7 rule (opportunity.py): HIGH n>=200 & Wilson90 lower>0.55; MEDIUM n>=60 & lower>0.50; else LOW;
    one level down when signals conflict."""
    from .opportunity import wilson_lower
    n = len(trades)
    if not n:
        return {"label": "NO_EVIDENCE", "n": 0, "p": None, "p_lo": None, "avg": None}
    p = sum(1 for t in trades if t > 0) / n
    p_lo = wilson_lower(p, n, 1.6449)
    lab = "HIGH" if n >= 200 and p_lo > 0.55 else "MEDIUM" if n >= 60 and p_lo > 0.5 else "LOW"
    if conflict and lab != "LOW":
        lab = "MEDIUM" if lab == "HIGH" else "LOW"
    return {"label": lab, "n": n, "p": round(p, 3), "p_lo": round(p_lo, 3), "avg": round(float(np.mean(trades)), 3),
            "conflict": conflict}


def atr14(h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> float:
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))
    return float(pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]) if len(tr) else float("nan")


def load_strats(roots: list[Path], scratch: Path) -> list[dict[str, Any]]:
    """Enabled long/short library strategies with an intraday timeframe and no benchmark/htf/feature needs."""
    lib = StrategyLibrary(roots, scratch).load(record=False)
    strats = []
    for sid, s in sorted(lib.strategies.items()):
        if not (s.enabled and not s.blocked and s.direction in ("long", "short") and s.entry_node is not None):
            continue
        needs = dsl.requirements(s.entry_node)
        if needs["benchmark"] or needs["htf"] or needs["features"]:
            continue
        node = dsl.substitute(s.entry_node, s.defaults()) if s.defaults() else s.entry_node
        ex = s.doc.get("exit") or {}
        strats.append({"id": sid, "name": s.doc.get("name") or sid, "tfs": set(s.timeframes) & set(TFS),
                       "node": node, "stop_atr": ex.get("stop_atr"), "target_atr": ex.get("target_atr"),
                       "max_hold": int(ex.get("max_hold_bars") or 12), "apply": s.applies,
                       "dir": s.direction})
    return [s for s in strats if s["tfs"]]


def build_frame(bars: list[dict[str, Any]], k: int, day: str) -> pd.DataFrame:
    """1m bars {t,o,h,l,c,v} -> k-minute session buckets; bars of sessions before `day` count as complete."""
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    ts = pd.to_datetime(df["t"])
    df["session"] = ts.dt.strftime("%Y-%m-%d")
    mos = ts.dt.hour * 60 + ts.dt.minute - (9 * 60 + 15)
    df["bucket"] = mos // k
    g = df.groupby(["session", "bucket"], sort=True)
    f = g.agg(open=("o", "first"), high=("h", "max"), low=("l", "min"), close=("c", "last"),
              volume=("v", "sum"), n=("o", "size"), last_t=("t", "last")).reset_index()
    f["minute_of_session"] = (f["bucket"] + 1) * k
    end = pd.to_datetime(f["session"]) + pd.to_timedelta(9 * 60 + 15 + f["minute_of_session"], unit="m")
    f["end_ns"] = end.dt.tz_localize(IST).astype("int64")
    f["label"] = (end - pd.Timedelta(minutes=k)).dt.strftime("%Y-%m-%d %H:%M")
    # a bucket is complete when its last minute is present (or the session is finished)
    last_min = pd.to_datetime(f["last_t"])
    close_t = pd.to_datetime(f["session"]) + pd.Timedelta(minutes=15 * 60 + 30)
    bucket_last = end.where(end <= close_t, close_t) - pd.Timedelta(minutes=1)  # last bucket ends 15:29
    f["complete"] = (last_min >= bucket_last) | (f["session"] < day)
    return f



def group_rows(alerts: list[dict[str, Any]], setups: dict[str, dict[str, Any]],
           bear: dict[str, dict[str, int]] | None) -> list[dict[str, Any]]:
    """One row per symbol x timeframe x signal bar; bear=None for earlier days (no live bearish state)."""
    bear = bear if bear is not None else defaultdict(dict)
    self_setups = setups
    g: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in alerts:
        g[a["group"]].append(a)
    out = []
    for key, xs in g.items():
        a0 = xs[0]
        med = lambda k: round(float(np.median([x[k] for x in xs if x[k] is not None])), 2) \
            if any(x[k] is not None for x in xs) else None  # noqa: E731
        st = defaultdict(int)
        for x in xs:
            st[x["state"]] += 1
        out.append({"key": key, "symbol": a0["symbol"], "tf": a0["tf"], "bar": a0["bar"], "created": a0["created"],
                    "catch_up": a0.get("catch_up", False), "n": len(xs), "entry": a0["entry"],
                    "stop": med("stop"), "target": med("target"), "last": max(xs, key=lambda x: x["created"])["last"],
                    "pnl_pct": med("pnl_pct"), "mfe_pct": max(x["mfe_pct"] for x in xs),
                    "mae_pct": min(x["mae_pct"] for x in xs), "states": dict(st),
                    # bearish strategies firing NOW on this stock (any timeframe) while longs are open
                    "setup_state": (self_setups.get(key) or {}).get("state", "OPEN"),
                    "setup_exit": {k: (self_setups.get(key) or {}).get(k) for k in ("exit_at", "exit_px", "exit_pct",
                                                                                     "until")},
                    "confidence": (self_setups.get(key) or {}).get("confidence") or {},
                    "bear_now": bear[a0["symbol"]].get(a0["tf"], 0),
                    "conflict": bool(st.get("OPEN")) and bear[a0["symbol"]].get(a0["tf"], 0) > 0,
                    "items": [{k: x[k] for k in ("strategy", "name", "stop", "target", "state", "closed", "pnl_pct",
                                                 "exit")} for x in xs]})
    out.sort(key=lambda r: (r["bar"], r["n"]), reverse=True)
    per: dict[str, int] = defaultdict(int)
    keep = []
    for r in out:  # cap per timeframe so slower timeframes are never crowded out by 5m rows
        per[r["tf"]] += 1
        if per[r["tf"]] <= 150:
            keep.append(r)
    return keep


class Lab:
    def __init__(self, day: str, out: Path, stale_after: int, roots: list[Path]):
        self.day, self.out, self.stale_after = day, out, stale_after
        self.remote = f"~/gplm-live/data/{day}"
        self.lock = threading.RLock()
        self.min1: dict[str, list[dict[str, Any]]] = defaultdict(list)  # symbol -> 1m bars (history + today)
        self.prev_close: dict[str, float] = {}
        self.offset = 0
        self.heartbeat: dict[str, Any] = {}
        self.feed = {"state": "CONNECTING", "error": None, "last_pull": None, "lag_s": None}
        self.alerts: list[dict[str, Any]] = []
        self.past_groups: list[dict[str, Any]] = []  # earlier days, read-only (load_past)
        self.signals: dict[str, dict[str, int]] = defaultdict(dict)  # symbol -> tf -> strategies firing now
        self.last_eval: dict[str, str] = {}
        self.evaluated_until: dict[tuple[str, str], str] = {}
        self.subs: list[queue.Queue] = []
        self.oracle: dict[str, Any] = {}
        self.history_loaded = False
        self.behaviour: dict[str, Any] = {}
        self.inbox: queue.Queue = queue.Queue()
        self.notes: list[dict[str, Any]] = []        # decision-oriented notifications (notify.py)
        self.setups: dict[str, dict[str, Any]] = {}  # symbol|tf|bar -> tracked setup
        self.intraday: dict[str, Any] = {}
        self.intraday_events: list[dict[str, Any]] = []
        self.intraday_mkt30: float | None = None
        self.explained: dict[str, str] = {}  # setup key -> local-LLM explanation (cached)
        self.resume_t: dict[str, str] = {}   # warm restart: last bar per symbol already processed
        self.done_t: dict[str, str] = {}
        self._saved = 0.0
        self.recompute = threading.Event()
        self.evidence = None  # evidence.Evidence (backtest rule C), set by main()
        self.link: dict[str, Any] = {}
        self.out.mkdir(parents=True, exist_ok=True)
        self.events_path = out / "events.jsonl"
        if self.events_path.exists():  # a restart rebuilds alerts by replay; keep the old log, never append twice
            self.events_path.rename(out / f"events-{now_ist():%H%M%S}.jsonl")
        self.strats = load_strats(roots, out / "_unused-writable")
        # bearish (short) strategies are WARNINGS only in PRD v1 (cash equity, long-only): no paper trade
        self.bear: dict[str, dict[str, int]] = defaultdict(dict)          # symbol -> tf -> firing now
        self.bear_events: list[dict[str, Any]] = []                        # rising edges today

    # ------------------------------------------------------------------ feed
    def load_history(self) -> None:
        raw = ssh(f"cat {self.remote}/history.jsonl {self.remote}/history-extra.jsonl 2>/dev/null || true", timeout=120)
        for line in raw.decode().splitlines():
            r = json.loads(line)
            self.min1[r["s"]].append(r)
        for s, bars in self.min1.items():
            bars.sort(key=lambda b: b["t"])
            if bars:
                self.prev_close[s] = bars[-1]["c"]

    def pull(self) -> list[dict[str, Any]]:
        cmd = (f"cat {self.remote}/heartbeat.json 2>/dev/null; echo; echo '@@BARS@@'; "
               f"tail -c +{self.offset + 1} {self.remote}/bars.jsonl 2>/dev/null || true")
        raw = ssh(cmd).decode()
        hb_txt, _, bars_txt = raw.partition("@@BARS@@\n")
        try:
            self.heartbeat = json.loads(hb_txt.strip() or "{}")
        except json.JSONDecodeError:
            pass
        complete = bars_txt.rpartition("\n")[0] + "\n" if "\n" in bars_txt else ""
        self.offset += len(complete.encode())
        return [json.loads(x) for x in complete.splitlines() if x.strip()]

    def feed_state(self) -> str:
        hb = self.heartbeat
        n = now_ist()
        if not hb:
            return "NO_HEARTBEAT"
        st = hb.get("state", "?")
        if st != "RUNNING":
            return st
        upd = datetime.fromisoformat(hb["updated"])
        self.feed["lag_s"] = round((n - upd).total_seconds())
        return "STALE" if self.feed["lag_s"] > self.stale_after else "LIVE"

    # ------------------------------------------------------------------ bars
    def frame(self, sym: str, k: int) -> pd.DataFrame:
        return build_frame(self.min1[sym], k, self.day)

    def evaluate(self, sym: str, catch_up: bool, t: str | None = None) -> None:
        if sym in INDICES:
            return
        mos = None
        if t:  # only rebuild a timeframe when this minute closes one of its bars
            hh, mm = int(t[11:13]), int(t[14:16])
            mos = hh * 60 + mm - (9 * 60 + 15) + 1
        for tf, k in TFS.items():
            if mos is not None and (sym, tf) in self.evaluated_until and mos % k and mos != 375:
                continue
            f = self.frame(sym, k)
            if f.empty:
                continue
            f = f[f["complete"]].reset_index(drop=True)
            if len(f) < 30:
                continue
            last = f["label"].iloc[-1]
            if self.evaluated_until.get((sym, tf)) == last:
                continue
            first_time = (sym, tf) not in self.evaluated_until
            self.evaluated_until[(sym, tf)] = last
            ctx = dsl.Context.from_frame(f, tf, True)
            a = atr14(f["high"].to_numpy(float), f["low"].to_numpy(float), f["close"].to_numpy(float))
            close = float(f["close"].iloc[-1])
            firing = bearish = applicable = 0
            added: list[dict[str, Any]] = []
            masks: dict[str, tuple[np.ndarray, dict[str, Any]]] = {}
            bear_rising = 0
            for s in self.strats:
                if tf not in s["tfs"] or not s["apply"](sym, tf):
                    continue
                applicable += s["dir"] == "long"
                try:
                    m = dsl.evaluate(s["node"], ctx)
                    m = dsl._bool(ctx, m)
                except Exception:
                    continue
                if not (len(m) >= 2 and m[-1]):
                    continue
                rising = not (m[-2] or first_time or f["session"].iloc[-1] != self.day)
                if s["dir"] == "short":
                    bearish += 1
                    if rising:
                        bear_rising += 1
                        self.bear_events.append({"symbol": sym, "tf": tf, "bar": last, "strategy": s["id"],
                                                 "name": s["name"], "close": round(close, 2), "catch_up": catch_up,
                                                 "created": now_ist().strftime("%H:%M:%S")})
                        self.log("BEARISH", self.bear_events[-1])
                    continue
                firing += 1
                if not rising:
                    continue  # only rising edges on today's bars create alerts
                masks[s["id"]] = (m, s)
                if any(x["strategy"] == s["id"] and x["symbol"] == sym and x["tf"] == tf and x["state"] == "OPEN"
                       for x in self.alerts):
                    continue
                stop = close - s["stop_atr"] * a if s["stop_atr"] and np.isfinite(a) else None
                tgt = close + s["target_atr"] * a if s["target_atr"] and np.isfinite(a) else None
                self.add_alert({"symbol": sym, "tf": tf, "strategy": s["id"], "name": s["name"], "bar": last,
                                "entry": round(close, 2), "stop": round(stop, 2) if stop else None,
                                "target": round(tgt, 2) if tgt else None, "max_hold_min": s["max_hold"] * k,
                                "atr": round(a, 2) if np.isfinite(a) else None,
                                "state": "OPEN", "catch_up": catch_up, "mode": "PAPER/TEST"})
                added.append(self.alerts[-1])
            self.signals[sym][tf] = firing
            self.bear[sym][tf] = bearish
            ts = now_ist().strftime("%H:%M:%S")
            if added:
                prof = ((self.behaviour.get("profiles") or {}).get(sym) or {}).get("W") or {}
                chg = next((c for c in self.behaviour.get("changes", []) if c["symbol"] == sym
                            and c["status"] == "CONFIRMED"), None)
                ctx = {"week": prof.get("label"), "change": f"{chg['phrase']} since {chg['since']}" if chg else None,
                       "bear": bearish}
                H, L_, C_ = (f[x].to_numpy(float) for x in ("high", "low", "close"))
                pc = np.concatenate([[C_[0]], C_[:-1]])
                tr = np.maximum(H - L_, np.maximum(abs(H - pc), abs(L_ - pc)))
                atr_s = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
                trades: list[float] = []
                for a_ in added:
                    mm, sd = masks.get(a_["strategy"], (None, None))
                    if mm is not None:
                        trades += past_trades(mm, f["open"].to_numpy(float), H, L_, C_, atr_s, sd["stop_atr"],
                                              sd["target_atr"], sd["max_hold"])
                live = confidence(trades, conflict=bearish > 0)
                live["sessions"] = int(f["session"].nunique())
                rrs = [sd_["target_atr"] / sd_["stop_atr"] for a_ in added
                       if (sd_ := (masks.get(a_["strategy"]) or (None, None))[1]) and sd_.get("stop_atr")
                       and sd_.get("target_atr")]
                ev = self.evidence.lookup([a_["strategy"] for a_ in added], sym, tf,
                                          min(rrs) if rrs else None, bearish > 0) if self.evidence else None
                if ev:
                    conf = ev | {"live": {k: live.get(k) for k in ("label", "n", "p", "avg", "sessions")}}
                else:  # no backtest for these strategies x tf: live replay is informational, capped at LOW
                    conf = live | {"basis": "LIVE_REPLAY_ONLY",
                                   "label": "NO_EVIDENCE" if live["label"] == "NO_EVIDENCE" else "LOW"}
                ctx["confidence"] = conf
                st, n = NT.setup(sym, tf, k, last, added, applicable, close, ctx, catch_up, ts)
                self.setups[st["key"]] = st
                self.add_note(n)
            if bear_rising:
                for st in self.setups.values():
                    if st["symbol"] == sym and st["tf"] == tf and st["state"] == "OPEN" and not st["conflict_noted"]:
                        st["conflict_noted"] = True
                        self.add_note(NT.conflict(st, bearish, close, ts) | {"data": {
                            "setup": st["key"], "tf": tf, "n": st["n"], "bear": bearish, "catch_up": catch_up}})
            self.last_eval[sym] = now_ist().strftime("%H:%M:%S")

    def add_alert(self, a: dict[str, Any]) -> None:
        a["id"] = f"{a['symbol']}|{a['tf']}|{a['strategy']}|{a['bar']}"
        a["group"] = f"{a['symbol']}|{a['tf']}|{a['bar']}"
        # tracking starts with the first minute AFTER the signal bar closes (entry = that bar's close)
        a["after"] = (datetime.fromisoformat(a["bar"].replace(" ", "T")) + timedelta(minutes=TFS[a["tf"]])
                      ).strftime("%Y-%m-%dT%H:%M")
        a["created"] = now_ist().strftime("%H:%M:%S")
        a.update(last=a["entry"], pnl_pct=0.0, mfe_pct=0.0, mae_pct=0.0, closed=None, exit=None)
        self.alerts.append(a)
        self.log("ALERT", a)

    def advance(self, bar: dict[str, Any]) -> None:
        sym = bar["s"]
        for a in self.alerts:
            if a["symbol"] != sym or a["state"] != "OPEN":
                continue
            if bar["t"] < a["after"]:
                continue
            e = a["entry"]
            a["last"] = bar["c"]
            a["mfe_pct"] = round(max(a["mfe_pct"], (bar["h"] / e - 1) * 100), 2)
            a["mae_pct"] = round(min(a["mae_pct"], (bar["l"] / e - 1) * 100), 2)
            exit_px, why = None, None
            if a["stop"] and bar["l"] <= a["stop"]:
                exit_px, why = a["stop"], "STOP_HIT"
            elif a["target"] and bar["h"] >= a["target"]:
                exit_px, why = a["target"], "TARGET_HIT"
            elif (datetime.fromisoformat(bar["t"]) - datetime.fromisoformat(a["bar"].replace(" ", "T"))) \
                    >= timedelta(minutes=a["max_hold_min"] + TFS[a["tf"]]):
                exit_px, why = bar["c"], "TIME_EXIT"
            elif bar["t"].endswith("15:29"):
                exit_px, why = bar["c"], "EOD_EXIT"
            a["pnl_pct"] = round(((exit_px or bar["c"]) / e - 1) * 100, 2)
            if why:
                a.update(state=why, exit=exit_px, closed=bar["t"][11:16])
                self.log("EXIT", a)
        # setup-level outcome on the setup's own (median) levels: one notification per setup
        for st in self.setups.values():
            if st["symbol"] != sym or st["state"] != "OPEN":
                continue
            after = (datetime.fromisoformat(st["bar"].replace(" ", "T")) + timedelta(minutes=TFS[st["tf"]])
                     ).strftime("%Y-%m-%dT%H:%M")
            if bar["t"] < after:
                continue
            why, px = None, None
            if st["stop"] and bar["l"] <= st["stop"]:
                why, px = "STOP", st["stop"]
            elif st["target"] and bar["h"] >= st["target"]:
                why, px = "TARGET", st["target"]
            elif bar["t"] >= st["until"]:
                why, px = ("EOD" if bar["t"].endswith("15:29") else "EXPIRED"), bar["c"]
            if why:
                st.update(state=why, exit_at=bar["t"][11:16], exit_px=px,
                          exit_pct=round((px / st["entry"] - 1) * 100, 2))
                old = bar["t"] < (now_ist() - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M")
                n = NT.outcome(st, why, px, bar["t"][11:16], now_ist().strftime("%H:%M:%S"))
                n["data"]["catch_up"] = old or st["catch_up"]
                self.add_note(n)

    def add_note(self, n: dict[str, Any]) -> None:
        n["ts"] = n.get("ts") or now_ist().strftime("%H:%M:%S")
        self.notes.append(n)
        del self.notes[:-400]
        self.log("NOTE", {k: n[k] for k in ("kind", "symbol", "title", "body", "action", "key")},
                 publish=not (n.get("data") or {}).get("owner_only"))  # owner-only notes never stream to viewers

    def log(self, kind: str, a: dict[str, Any], publish: bool = True) -> None:
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_ist().isoformat(timespec="seconds"), "kind": kind, **a}) + "\n")
        if publish:
            self.publish({"type": kind, "alert": a})

    # ------------------------------------------------------------------ state + push
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            mk = self.market()
            syms, shown = [], None
            for s, bars in sorted(self.min1.items()):
                today, pc, sess = self._shown(s)  # before the open: the last session's bars
                shown = shown or sess
                last = today[-1] if today else None
                syms.append({"symbol": s, "last": last["c"] if last else None, "t": last["t"][11:16] if last else None,
                             "chg_pct": round((last["c"] / pc - 1) * 100, 2) if last and pc else None,
                             "bars_today": len(today), "sig": dict(self.signals[s]), "bear": dict(self.bear[s]),
                             "sig5": self.signals[s].get("5m", 0), "sig15": self.signals[s].get("15m", 0), "index": s in INDICES,
                             **self.rs(s, round((last["c"] / pc - 1) * 100, 2) if last and pc else None, mk)})
            return {"contract": CONTRACT, "day": self.day, "now": now_ist().strftime("%H:%M:%S"),
                    "feed": self.feed, "oracle": self.oracle, "login_url": LOGIN_URL,
                    "link": {k: getattr(v, "state", v) for k, v in self.link.items()}, "learning": self.learning(), "heartbeat": self.heartbeat, "strategies": len(self.strats),
                    "shown_session": shown, "symbols": syms, "groups": self.groups() + self.past_groups, "bear_events": self.bear_events[-300:][::-1],
                    "timeframes": list(TFS), "counts": self.counts(), "notes": self.notes[-150:],
                    "intraday": self.intraday, "intraday_events": self.intraday_events[-150:][::-1],
                    "intraday_mkt30": self.intraday_mkt30, "market": mk}

    # ------------------------------------------------------------------ market context (header) + charts
    def _today(self, sym: str) -> list[dict[str, Any]]:
        return [b for b in self.min1.get(sym, []) if b["t"].startswith(self.day)]

    def market(self) -> dict[str, Any]:
        idx = {}
        for s in ("NIFTY", "BANKNIFTY", "INDIAVIX"):
            tb, pc, _ = self._shown(s)
            if not tb:
                continue
            closes = [b["c"] for b in tb]
            spark = closes[4::5] + ([closes[-1]] if len(closes) % 5 != 0 else [])
            idx[s] = {"last": tb[-1]["c"], "t": tb[-1]["t"][11:16], "prev_close": pc,
                      "chg": round(tb[-1]["c"] - pc, 2) if pc else None,
                      "chg_pct": round((tb[-1]["c"] / pc - 1) * 100, 2) if pc else None,
                      "high": max(b["h"] for b in tb), "low": min(b["l"] for b in tb), "spark": spark}
        up = down = above = n_vw = 0
        for s in self.min1:
            if s in INDICES:
                continue
            tb, pc, _ = self._shown(s)
            if tb and pc:
                up += tb[-1]["c"] > pc
                down += tb[-1]["c"] < pc
            st = self.intraday.get(s)
            if st:
                n_vw += 1
                above += bool(st.get("above_vwap"))
        regime, why = "RANGE", []
        nf = idx.get("NIFTY")
        if nf and nf.get("chg_pct") is not None:
            tb = self._shown("NIFTY")[0]
            c5 = [b["c"] for b in tb][4::5]
            o = tb[0]["o"]
            share_up = sum(c > o for c in c5) / len(c5) if c5 else 0.5
            if nf["chg_pct"] >= 0.5 and share_up >= 0.7:
                regime = "TREND UP"
            elif nf["chg_pct"] <= -0.5 and share_up <= 0.3:
                regime = "TREND DOWN"
            why.append(f"NIFTY {nf['chg_pct']:+.2f}%, {share_up * 100:.0f}% of 5-min closes above the open")
        vix = idx.get("INDIAVIX")
        risk = None
        if vix and vix.get("chg_pct") is not None:
            risk = "RISK-OFF" if vix["chg_pct"] >= 5 else "CALM" if vix["chg_pct"] <= -5 else None
            why.append(f"India VIX {vix['chg_pct']:+.1f}%")
        if up + down:
            why.append(f"breadth {up} up / {down} down")
        return {"indices": idx, "breadth": {"up": up, "down": down, "above_vwap": above, "with_vwap": n_vw},
                "regime": regime, "risk": risk, "why": why}

    def rs(self, sym: str, chg_pct: float | None, mk: dict[str, Any]) -> dict[str, Any]:
        ref = "BANKNIFTY" if sym in BANKS else "NIFTY"
        r = (mk.get("indices") or {}).get(ref) or {}
        beta = (((self.behaviour.get("profiles") or {}).get(sym) or {}).get("M") or {}).get("beta")
        return {"ref": ref, "rs": round(chg_pct - r["chg_pct"], 2) if chg_pct is not None and r.get("chg_pct")
                is not None else None, "beta_m": beta}

    def bars(self, sym: str, tf: str) -> dict[str, Any]:
        """Candles for the chart: intraday tfs from the in-memory feed (last 3 sessions), 1D from the local store."""
        IST_OFF = 19800  # lightweight-charts shows UTC; shift so the axis reads IST
        with self.lock:
            if tf == "1D":
                d = getattr(self, "local", {}).get(sym, (pd.DataFrame(), None))[0]
                rows = [] if d is None or d.empty else [
                    {"time": str(r.date), "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                     "value": r.volume} for r in d.tail(250).itertuples()]
                tb = self._today(sym)
                if tb and (not rows or rows[-1]["time"] < self.day):
                    rows.append({"time": self.day, "open": tb[0]["o"], "high": max(b["h"] for b in tb),
                                 "low": min(b["l"] for b in tb), "close": tb[-1]["c"], "value": sum(b["v"] for b in tb)})
                return {"symbol": sym, "tf": tf, "candles": rows, "prev_close": self.prev_close.get(sym)}
            k = 1 if tf == "1m" else TFS.get(tf, 5)
            sessions = sorted({b["t"][:10] for b in self.min1.get(sym, [])})[-3:]
            src = [b for b in self.min1.get(sym, []) if b["t"][:10] in sessions]
        if not src:
            return {"symbol": sym, "tf": tf, "candles": []}
        df = pd.DataFrame(src)
        ts = pd.to_datetime(df["t"])
        df["session"] = df["t"].str[:10]
        df["bucket"] = ((ts.dt.hour * 60 + ts.dt.minute) - (9 * 60 + 15)) // k
        g = df.groupby(["session", "bucket"], sort=True).agg(t=("t", "first"), o=("o", "first"), h=("h", "max"),
                                                             l=("l", "min"), c=("c", "last"), v=("v", "sum")).reset_index()
        start = (pd.to_datetime(g["session"]) + pd.to_timedelta(9 * 60 + 15 + g["bucket"] * k, unit="m"))
        secs = (start.astype("int64") // 10**9 + 0).tolist()  # naive IST times read as UTC == IST on the axis
        candles = [{"time": int(t), "open": o, "high": h, "low": lo, "close": c, "value": v}
                   for t, o, h, lo, c, v in zip(secs, g["o"], g["h"], g["l"], g["c"], g["v"])]
        today = g[g["session"] == self.day]
        vw = []
        if len(today) and today["v"].sum() > 0:
            tp = (today["h"] + today["l"] + today["c"]) / 3
            cv = today["v"].clip(lower=0).cumsum().replace(0, np.nan)
            vwap = (tp * today["v"].clip(lower=0)).cumsum() / cv
            vw = [{"time": int(t), "value": round(float(v), 2)} for t, v in
                  zip([secs[i] for i in today.index], vwap) if np.isfinite(v)]
        sets = [{"key": st["key"], "tf": st["tf"], "bar": st["bar"], "closed_at": st["closed_at"], "entry": st["entry"],
                 "stop": st["stop"], "target": st["target"], "state": st["state"], "n": st["n"],
                 "conf": (st.get("confidence") or {}).get("label"),
                 "time": int(pd.Timestamp(st["bar"]).value // 10**9 + TFS[st["tf"]] * 60)}
                for st in self.setups.values() if st["symbol"] == sym]
        bears = [{"time": int(pd.Timestamp(e["bar"]).value // 10**9 + TFS[e["tf"]] * 60), "tf": e["tf"],
                  "strategy": e["strategy"]} for e in self.bear_events if e["symbol"] == sym]
        _ = IST_OFF
        return {"symbol": sym, "tf": tf, "candles": candles, "vwap": vw, "prev_close": self.prev_close.get(sym),
                "setups": sets, "bearish": bears, "sessions": sessions}

    def counts(self) -> dict[str, Any]:
        st: dict[str, int] = defaultdict(int)
        pnl = []
        for a in self.alerts:
            st[a["state"]] += 1
            if a["state"] != "OPEN":
                pnl.append(a["pnl_pct"])
        closed = len(pnl)
        wins = sum(1 for p in pnl if p > 0)
        return {"open": st["OPEN"], "total": len(self.alerts), "target": st["TARGET_HIT"], "stop": st["STOP_HIT"],
                "time": st["TIME_EXIT"], "eod": st["EOD_EXIT"], "closed": closed,
                "win_rate": round(wins / closed * 100, 1) if closed else None,
                "avg_closed_pnl": round(float(np.mean(pnl)), 2) if closed else None,
                "avg_open_pnl": round(float(np.mean([a["pnl_pct"] for a in self.alerts if a["state"] == "OPEN"])), 2)
                if st["OPEN"] else None}

    def groups(self) -> list[dict[str, Any]]:
        """One row per symbol x timeframe x signal bar (many strategies often fire on the same bar)."""
        return group_rows(self.alerts, self.setups, self.bear)

    def load_past(self, days: int = 31) -> None:
        """Signal rows of earlier live-lab days (their saved state.pkl), read-only, for the week/month windows
        and for the empty page before today's first candle."""
        import pickle
        import re
        lo = (datetime.fromisoformat(self.day) - timedelta(days=days)).date().isoformat()
        rows: list[dict[str, Any]] = []
        for d in sorted(self.out.parent.iterdir()):
            if not (d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and lo <= d.name < self.day):
                continue
            try:
                st = pickle.loads((d / "state.pkl").read_bytes())
            except Exception:  # noqa: BLE001 - missing / old state: skip that day
                continue
            for r in group_rows(st.get("alerts") or [], st.get("setups") or {}, None):
                rows.append(r | {"past": True, "day": d.name})
        self.past_groups = rows
        print(f"earlier sessions: {len(rows)} signal rows from {len({r['day'] for r in rows})} day(s)")

    def _shown(self, sym: str) -> tuple[list[dict[str, Any]], float | None, str | None]:
        """Today's 1m bars + previous close; before today's first candle the LAST session's bars + its previous
        close (third item = that session's date), so the page is never empty before the open."""
        bars = self.min1.get(sym, [])
        tb = [b for b in bars if b["t"].startswith(self.day)]
        if tb or not bars:
            return tb, self.prev_close.get(sym), None
        last_day = bars[-1]["t"][:10]
        tb = [b for b in bars if b["t"].startswith(last_day)]
        before = [b for b in bars if b["t"][:10] < last_day]
        return tb, (before[-1]["c"] if before else None), last_day

    def publish(self, msg: dict[str, Any]) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    # ------------------------------------------------------------------ behaviour (multi-scale profiles)
    def load_local(self) -> None:
        from .context import App
        from .ingest import Ingestor
        app = App(REPO / "config" / "app" / "gplm.windows.json", quiet=True)
        ing = Ingestor(app.s, app.db)
        local: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
        for s in sorted(self.min1):
            if s in INDICES:
                continue
            try:
                d = ing.ingest(s)
            except Exception:  # noqa: BLE001
                continue
            m = d.minute
            if len(m):
                m = m.assign(ts=pd.to_datetime(m["start_ns"], utc=True).dt.tz_convert(IST))[
                    ["session", "ts", "open", "high", "low", "close", "volume"]]
            local[s] = (d.daily[["date", "open", "high", "low", "close", "volume"]].copy(), m)
        app.close()
        self.local = local  # swap in one step

    def compute_behaviour(self) -> None:
        from . import behaviour as BH
        if not hasattr(self, "local") or getattr(self, "local_stale", False):
            self.local_stale = False
            self.load_local()
        data, today_min = {}, {}
        with self.lock:
            live = {s: pd.DataFrame(b) for s, b in self.min1.items() if s not in INDICES and b}
        for s, lv in live.items():
            d, m = self.local.get(s, (pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]),
                                      pd.DataFrame()))
            lv = lv.assign(ts=pd.to_datetime(lv["t"]).dt.tz_localize(IST), session=lv["t"].str[:10])
            lv = lv.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
            past = lv[lv["session"] < self.day]
            last_local = str(d["date"].max()) if len(d) else ""
            add_d = past[past["session"] > last_local].groupby("session").agg(
                open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                volume=("volume", "sum")).reset_index().rename(columns={"session": "date"})
            daily = pd.concat([d, add_d], ignore_index=True)
            have = set(m["session"].astype(str)) if len(m) else set()
            add_m = past[~past["session"].isin(have)][["session", "ts", "open", "high", "low", "close", "volume"]]
            minute = pd.concat([m, add_m], ignore_index=True) if len(m) else add_m
            data[s] = (daily, minute)
            today_min[s] = lv[lv["session"] == self.day][["ts", "open", "high", "low", "close", "volume"]]
        res = BH.build(data, self.day, today_min)
        res["computed"] = now_ist().strftime("%H:%M:%S")
        before = {f"{c['symbol']}|{c['feature']}" for c in (self.behaviour.get("changes") or [])
                  if c.get("status") == "CONFIRMED"} if self.behaviour.get("changes") is not None else None
        self.behaviour = res
        if before is not None:  # first computation is the baseline; notify only newly confirmed changes
            for c in res["changes"]:
                if c["status"] == "CONFIRMED" and f"{c['symbol']}|{c['feature']}" not in before:
                    self.add_note(NT.behaviour(c, res["computed"]))
        (self.out / "behaviour.json").write_text(json.dumps(res, default=str), encoding="utf-8")
        self.publish({"type": "behaviour"})

    def behaviour_loop(self) -> None:
        while not self.history_loaded:
            time.sleep(5)
        time.sleep(30)  # let the first pull bring today's bars in, so "D (today)" is populated
        while True:
            try:
                self.compute_behaviour()
            except Exception as exc:  # noqa: BLE001
                self.behaviour = {"error": f"{type(exc).__name__}: {exc}", "computed": now_ist().strftime("%H:%M:%S")}
            self.recompute.wait(900)  # every 15 min, or sooner when the sync brings new sessions
            self.recompute.clear()

    def intraday_loop(self) -> None:
        """Every minute: today's intraday behaviour per stock vs its own recent sessions at the same time of day."""
        from . import behaviour as BH
        while not self.history_loaded or self.feed.get("catching_up") or not self.inbox.empty() \
                or not any(b and b[-1]["t"] >= (now_ist() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
                           for b in list(self.min1.values())):
            time.sleep(5)  # wait until the replay has brought today's bars up to date
        last_fired: dict[str, datetime] = {}
        while True:
            try:
                t0 = now_ist()
                slot = ((t0.hour * 60 + t0.minute) - (9 * 60 + 15)) // 5  # bars before the current slot are complete
                with self.lock:
                    data = {s: list(b) for s, b in self.min1.items() if s not in INDICES}
                first = {s: BH.intraday(b, self.day, None, slot) for s, b in data.items()}
                r30 = [v["state"]["ret30"] for v in first.values() if v]
                mkt30 = float(np.median(r30)) if r30 else None
                res = {s: (BH.intraday(data[s], self.day, mkt30, slot) if v else v) for s, v in first.items()}
                self.intraday = {s: v.get("state") for s, v in res.items() if v}
                self.intraday_mkt30 = mkt30
                for s, v in res.items():
                    for ev in (v or {}).get("events", []):
                        k = f"{s}|{ev['type']}|{ev['side']}"
                        if k in last_fired and t0 - last_fired[k] < timedelta(minutes=30):
                            continue  # same event re-arms only after 30 minutes
                        last_fired[k] = t0
                        ev = {"symbol": s, **ev, "ts": t0.strftime("%H:%M:%S")}
                        self.intraday_events.append(ev)
                        del self.intraday_events[:-300]
                        nt = NT.intraday(ev, ev["ts"])
                        nt["data"]["catch_up"] = ev["at"] < (t0 - timedelta(minutes=10)).strftime("%H:%M")
                        self.add_note(nt)
                self.publish({"type": "tick"})
            except Exception as exc:  # noqa: BLE001
                self.intraday_error = f"{type(exc).__name__}: {exc}"
            time.sleep(60)

    def on_synced(self, files: list[str]) -> None:
        self.local_stale = True  # reload the local store (new sessions) on the next behaviour pass
        self.recompute.set()

    def learning(self) -> dict[str, Any]:
        """Automatic research progress (var/runs/research_feed.py) + report freshness, for the Data link tab."""
        runs = REPO / "var" / "runs"
        out: dict[str, Any] = {}
        try:
            done = (runs / "research_feed_done.txt").read_text().split() if (runs / "research_feed_done.txt").exists() else []
            out["researched"] = len(done)
            out["last_done"] = done[-3:]
            lines = (runs / "research-feed.log").read_text(errors="ignore").splitlines()[-3:]                 if (runs / "research-feed.log").exists() else []
            out["last"] = [ln[11:] for ln in lines]
            for k, f in (("scorecard", "scorecard/latest/scorecard.md"), ("profiles", "profiles/latest/profile.md")):
                p = REPO.parent / "state" / "reports" / f
                out[k] = datetime.fromtimestamp(p.stat().st_mtime, IST).strftime("%d %b %H:%M") if p.exists() else None
        except OSError as exc:
            out["error"] = str(exc)[:120]
        return out

    def refresh_oracle(self) -> None:
        try:
            self.oracle = ctl("status") | {"checked": now_ist().strftime("%H:%M:%S")}
        except Exception as exc:  # noqa: BLE001
            self.oracle = {"ok": False, "error": str(exc)[:200]}
        self.check_login()

    def check_login(self) -> None:
        """Remind the owner (once per reason per day) when the Shoonya token won't cover today's session:
        from 08:45 IST on weekdays if it expires before 15:35 (token life ~23 h), and whenever it has expired."""
        tok, age = self.oracle.get("token"), self.oracle.get("age_hours")
        if tok is None:
            return  # status unknown (Oracle unreachable): the feed state already reports that
        t = now_ist()
        sent = getattr(self, "_login_sent", set())
        self._login_sent = sent
        ts = t.strftime("%H:%M:%S")
        if tok != "CACHED":
            key = f"{t:%Y-%m-%d}|expired"
            if key not in sent:
                sent.add(key)
                self.add_note(NT.login_reminder("The Shoonya token has expired or is missing: the live feed and "
                                                "the cloud download are paused until you reconnect.", ts))
            return
        mins = t.hour * 60 + t.minute
        if t.weekday() < 5 and 8 * 60 + 45 <= mins <= 15 * 60 and age is not None:
            left_h = 23.0 - float(age)
            need_h = (15 * 60 + 35 - mins) / 60
            key = f"{t:%Y-%m-%d}|morning"
            if left_h < need_h and key not in sent:
                sent.add(key)
                self.add_note(NT.login_reminder(f"Token is {age} h old (about {max(left_h, 0):.1f} h left) - it "
                                                "would expire during today's session.", ts))

    # ------------------------------------------------------------------ warm restart (no full-day replay)
    STATE_KEYS = ("alerts", "setups", "notes", "bear_events", "intraday_events", "evaluated_until", "signals", "bear",
                  "explained", "done_t")

    def save_state(self) -> None:
        import pickle
        with self.lock:
            blob = pickle.dumps({"day": self.day, "v": 1, **{k: getattr(self, k) for k in self.STATE_KEYS}})
        tmp = self.out / ".state.tmp"
        tmp.write_bytes(blob)
        os.replace(tmp, self.out / "state.pkl")
        self._saved = time.time()

    def load_state(self) -> None:
        import pickle
        p = self.out / "state.pkl"
        if not p.exists():
            return
        try:
            d = pickle.loads(p.read_bytes())
        except Exception:  # noqa: BLE001
            return
        if d.get("day") != self.day or d.get("v") != 1:
            return
        with self.lock:
            for k in self.STATE_KEYS:
                if k in d:
                    setattr(self, k, d[k])
            self.resume_t = dict(self.done_t)
            if self.notes:  # keep notification ids increasing so pages don't re-announce old ones
                NT._seq = itertools.count(max(n["id"] for n in self.notes) + 1)
        print(f"warm restart: state reloaded ({len(self.alerts)} alerts, {len(self.setups)} setups)")

    def on_line(self, line: str) -> None:  # called by the push stream thread
        try:
            self.inbox.put(json.loads(line))
        except json.JSONDecodeError:
            pass

    def loop(self, every: float) -> None:
        """Process pushed bars as they arrive; heartbeat every ~15 s, Oracle status every ~60 s."""
        n = 0
        while True:
            try:
                if n % 60 == 0:
                    self.refresh_oracle()
                    if self.evidence:
                        try:
                            self.evidence.refresh()
                        except Exception as exc:  # evidence is optional; never stop the feed
                            print(f"evidence refresh failed: {exc}")
                if n % 15 == 0:
                    try:
                        self.heartbeat = json.loads(ssh(f"cat {self.remote}/heartbeat.json 2>/dev/null || echo '{{}}'"))
                    except (json.JSONDecodeError, RuntimeError) as exc:
                        self.feed.update(state="CLOUD_UNREACHABLE", error=str(exc)[:200])
                n += 1
                if not self.history_loaded:
                    if self.heartbeat.get("state") in (None, "STARTING", "WARMING_UP", "AUTH_FAILED"):
                        self.feed.update(state=self.heartbeat.get("state") or "NO_COLLECTOR",
                                         last_pull=now_ist().strftime("%H:%M:%S"))
                        if n % 15 == 1:
                            self.publish({"type": "tick"})
                        time.sleep(1)
                        continue
                    self.load_history()
                    self.history_loaded = True
                    self.load_state()
                bars = []
                while not self.inbox.empty():
                    bars.append(self.inbox.get_nowait())
                if not bars and n % 15 != 1:
                    time.sleep(1)
                    continue
                st = self.feed_state()
                prev = self.feed.get("state")
                if prev in ("LIVE", "STALE", "CLOUD_UNREACHABLE", "PULL_ERROR") and st != prev and \
                        (st == "LIVE" or prev == "LIVE"):
                    self.add_note(NT.system(prev, st, f"last candle {self.heartbeat.get('last_complete_minute')}",
                                            now_ist().strftime("%H:%M:%S")))
                self.feed.update(state=st, error=None, last_pull=now_ist().strftime("%H:%M:%S"))
                cutoff = (now_ist() - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M")
                # replay in time order so every completed 5m/15m bar is evaluated exactly once;
                # the lock is taken per bar so the page stays responsive during a long catch-up
                for i, b in enumerate(sorted(bars, key=lambda x: (x["t"], x["s"]))):
                    with self.lock:
                        self.min1[b["s"]].append(b)
                        if b["t"] <= self.resume_t.get(b["s"], ""):
                            continue  # warm restart: already processed before the restart (state reloaded)
                        self.advance(b)
                        self.evaluate(b["s"], catch_up=(b["t"] < cutoff or st != "LIVE"), t=b["t"])
                        self.done_t[b["s"]] = b["t"]
                    if i % 200 == 199:
                        self.feed["catching_up"] = f"{i + 1}/{len(bars)}"
                        self.publish({"type": "tick"})
                self.feed.pop("catching_up", None)
                if time.time() - self._saved > 60:
                    self.save_state()
                self.publish({"type": "tick"})
            except Exception as exc:  # noqa: BLE001
                self.feed.update(state="PULL_ERROR", error=str(exc)[:200])
                self.publish({"type": "tick"})
            time.sleep(1)


VIEWER_FILE = REPO / "var" / "live-lab" / "viewer_password.txt"  # family password (local file, never in git)
_fails: dict[str, list[float]] = defaultdict(list)


def viewer_password() -> str:
    import secrets
    VIEWER_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not VIEWER_FILE.exists():
        words = ["amber", "river", "lotus", "cedar", "maple", "coral", "delta", "ember", "harbor", "indigo", "jasmine",
                 "kestrel", "lagoon", "monsoon", "nectar", "orchid", "pepper", "quartz", "saffron", "tamarind"]
        VIEWER_FILE.write_text("-".join(secrets.choice(words) for _ in range(3)) + f"-{secrets.randbelow(90) + 10}\n",
                               encoding="utf-8")
    return VIEWER_FILE.read_text(encoding="utf-8").strip()


ACCESS_LOG = REPO / "var" / "live-lab" / "access.jsonl"  # who opened what (viewers only; owner is not logged)
_last_view: dict[str, str] = {}


def _device(ua: str) -> str:
    u = ua.lower()
    os_ = ("iPhone" if "iphone" in u else "iPad" if "ipad" in u else "Android" if "android" in u else
           "Windows" if "windows" in u else "Mac" if "mac os" in u else "Linux" if "linux" in u else "other")
    br = ("Edge" if "edg/" in u else "Chrome" if "chrome" in u or "crios" in u else "Safari" if "safari" in u else
          "Firefox" if "firefox" in u else "other")
    return f"{os_} · {br}"


def log_access(h: Any, event: str, detail: str = "", user: str | None = None) -> None:
    ip = (h.headers.get("X-Forwarded-For") or h.client_address[0]).split(",")[0].strip()
    ua = h.headers.get("User-Agent") or ""
    rec = {"ts": now_ist().isoformat(timespec="seconds"), "event": event, "detail": detail[:120], "ip": ip,
           "user": user, "device": _device(ua), "visitor": f"{ip}|{_device(ua)}"}
    ACCESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ACCESS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def visitors_summary() -> dict[str, Any]:
    rows = []
    if ACCESS_LOG.exists():
        rows = [json.loads(x) for x in ACCESS_LOG.read_text(encoding="utf-8").splitlines() if x.strip()]
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        v = by.setdefault(r["visitor"], {"visitor": r["visitor"], "ip": r["ip"], "device": r["device"], "user": None,
                                         "first": r["ts"], "last": r["ts"], "visits": 0, "symbols": {}, "tabs": {},
                                         "explains": 0, "failed": 0})
        v["last"] = r["ts"]
        v["user"] = r.get("user") or v["user"]
        e = r["event"]
        if e == "OPEN_PAGE":
            v["visits"] += 1
        elif e == "VIEW_SYMBOL":
            s = r["detail"].split(" ")[0]
            v["symbols"][s] = v["symbols"].get(s, 0) + 1
        elif e == "TAB":
            v["tabs"][r["detail"]] = v["tabs"].get(r["detail"], 0) + 1
        elif e == "EXPLAIN":
            v["explains"] += 1
        elif e == "LOGIN_FAILED":
            v["failed"] += 1
    out = sorted(by.values(), key=lambda x: x["last"], reverse=True)
    today = now_ist().date().isoformat()
    return {"visitors": out, "recent": rows[-200:][::-1],
            "totals": {"visitors": sum(1 for v in out if v["visits"]), "visits": sum(v["visits"] for v in out),
                       "today": sum(1 for v in out if v["last"].startswith(today) and v["visits"]),
                       "failed": sum(v["failed"] for v in out)}}


def handler_for(lab: Lab):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        # owner = this PC directly, or anyone arriving through Tailscale Serve with a tailnet identity;
        # viewer = everyone else (e.g. Tailscale Funnel): family password + read-only
        def role(self) -> str:
            direct = self.client_address[0] in ("127.0.0.1", "::1") and not self.headers.get("X-Forwarded-For")
            return "owner" if direct or self.headers.get("Tailscale-User-Login") else "viewer"

        def allowed(self) -> bool:
            if self.role() == "owner":
                return True
            import base64
            import hmac
            ip = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
            now = time.time()
            _fails[ip] = [t for t in _fails[ip] if now - t < 900]
            if len(_fails[ip]) >= 10:
                self.send_error(429, "Too many attempts; try again in 15 minutes")
                return False
            auth = self.headers.get("Authorization") or ""
            ok = False
            if auth.startswith("Basic "):
                user = ""
                try:
                    user, pw = base64.b64decode(auth[6:]).decode().split(":", 1)
                    want = viewer_password()  # "password" or "username:password" (one line, file is re-read each time)
                    want_user, want_pw = want.split(":", 1) if ":" in want else (None, want)
                    ok = hmac.compare_digest(pw.strip(), want_pw.strip()) and (
                        want_user is None or hmac.compare_digest(user.strip().lower(), want_user.strip().lower()))
                except Exception:  # noqa: BLE001
                    ok = False
            if ok:
                self.user = user.strip()
            if not ok:
                if auth:
                    _fails[ip].append(now)
                    try:
                        log_access(self, "LOGIN_FAILED", user=user.strip())
                    except Exception:  # noqa: BLE001
                        pass
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="GPLM family view", charset="UTF-8"')
                self.end_headers()
                self.wfile.write(b"Password required")
            return ok

        def do_GET(self) -> None:  # noqa: N802
            if not self.allowed():
                return
            viewer = self.role() != "owner"
            if viewer and self.path in ("/", "/index.html"):
                log_access(self, "OPEN_PAGE", user=getattr(self, "user", None))
            if viewer and self.path.startswith("/api/bars?"):
                from urllib.parse import parse_qs, urlparse
                q_ = parse_qs(urlparse(self.path).query)
                key_ = f"{q_.get('s', [''])[0]} {q_.get('tf', [''])[0]}"
                vid = (self.headers.get("X-Forwarded-For") or "") + (self.headers.get("User-Agent") or "")
                if _last_view.get(vid) != key_:  # chart auto-refresh is not a new view
                    _last_view[vid] = key_
                    log_access(self, "VIEW_SYMBOL", key_, user=getattr(self, "user", None))
            if self.path == "/api/visitors":
                if viewer:
                    return self.send_error(403)
                body = json.dumps(visitors_summary()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/role":
                body = json.dumps({"role": self.role()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in ("/", "/index.html"):
                body = STATIC.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/bars?"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                body = json.dumps(lab.bars(q.get("s", [""])[0].upper(), q.get("tf", ["5m"])[0]), default=float).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/behaviour":
                body = json.dumps(lab.behaviour, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/state":
                snap = lab.snapshot()
                if self.role() != "owner":  # owner-only notes (Shoonya login) never reach family viewers
                    snap["notes"] = [n for n in snap.get("notes", []) if not (n.get("data") or {}).get("owner_only")]
                body = json.dumps(snap).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                q: queue.Queue = queue.Queue(maxsize=200)
                lab.subs.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        try:
                            msg = q.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    lab.subs.remove(q)
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            host = self.headers.get("Host")
            if self.headers.get("Origin") not in (None, f"http://{host}", f"https://{host}"):
                return self.send_error(403)  # only this page may drive Oracle
            if not self.allowed():
                return
            if self.role() != "owner" and self.path not in ("/api/explain", "/api/track"):
                return self.send_error(403, "Read-only view")  # family can read and explain, never control
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/track":
                if self.role() != "owner":
                    log_access(self, str(body.get("event", "TAB"))[:20].upper(), str(body.get("value", "")),
                               user=getattr(self, "user", None))
                self.send_response(204)
                self.end_headers()
                return
            if self.role() != "owner" and self.path == "/api/explain":
                log_access(self, "EXPLAIN", str(body.get("key", "")), user=getattr(self, "user", None))
            if self.path == "/api/login":
                res = ctl("login", stdin=str(body.get("code", "")))
                if res.get("ok"):
                    res["collector"] = ctl("start")
            elif self.path in ("/api/collector/start", "/api/collector/stop"):
                res = ctl(self.path.rsplit("/", 1)[1])
            elif self.path == "/api/explain" and body.get("key"):
                key = str(body["key"])
                st = lab.setups.get(key)
                if not st:
                    res = {"ok": False, "error": "setup not found (it may be from before a restart)"}
                elif key in lab.explained:
                    res = {"ok": True, "text": lab.explained[key], "cached": True}
                else:
                    sym = st["symbol"]
                    try:
                        bars = lab.min1.get(sym) or []
                        txt = explain_setup(st, ((lab.behaviour.get("profiles") or {}).get(sym)) or {},
                                            [c for c in lab.behaviour.get("changes", []) if c["symbol"] == sym],
                                            lab.intraday.get(sym), [e for e in lab.intraday_events if e["symbol"] == sym],
                                            bars[-1]["c"] if bars else None)
                        lab.explained[key] = txt
                        res = {"ok": True, "text": txt, "model": EXPLAIN_MODEL}
                    except Exception as exc:  # noqa: BLE001
                        res = {"ok": False, "error": f"local model unavailable: {type(exc).__name__}: {str(exc)[:120]}"}
            elif self.path == "/api/sync/reset" and body.get("symbol"):
                lab.link["sync"].reset(str(body["symbol"]))
                res = {"ok": True, "reset": body["symbol"]}
            else:
                return self.send_error(404)
            lab.refresh_oracle()
            lab.publish({"type": "tick"})
            data = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
    return H


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", default=now_ist().date().isoformat())
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--every", type=float, default=10.0, help="seconds between pulls")
    ap.add_argument("--stale-after", type=int, default=150, help="heartbeat age (s) that marks the feed STALE")
    ap.add_argument("--out", default=str(REPO / "var" / "live-lab"))
    ap.add_argument("--library", action="append", default=[str(REPO / "config" / "strategies" / "library")])
    ap.add_argument("--evidence-run", action="append", default=None,
                    help="research-matrix run name(s) used for confidence, first wins per lane "
                         "(default: evidence-intraday, evidence-daily)")
    a = ap.parse_args()
    lab = Lab(a.date, Path(a.out) / a.date, a.stale_after, [Path(p) for p in a.library])
    print(f"live lab {a.date}: {len(lab.strats)} strategies ({'/'.join(TFS)}; long = paper alerts, short = bearish warnings)")
    from .cloud_link import CloudSync, Stream
    from .settings import load as load_settings
    st = load_settings(REPO / "config" / "app" / "gplm.windows.json")
    from .evidence import Evidence
    lab.evidence = Evidence(Path(st.reports_dir) / "research", a.evidence_run or ["evidence-intraday", "evidence-daily"])
    lab.evidence.refresh()
    lab.load_past()
    print(f"evidence: {len(lab.evidence.trades)} validation trades from {sorted(lab.evidence.trades['run'].unique())}")
    wl = (REPO / "deploy" / "oracle" / "live" / "watchlist.txt").read_text().splitlines()
    syms = [s for ln in wl for s in ln.split("#", 1)[0].split()]
    stream = Stream(f"{lab.remote}/bars.jsonl", lab.on_line)
    sync = CloudSync(syms, st.daily_root, st.minute_roots, Path(a.out) / "_staging", on_new=lab.on_synced,
                     index_root=st.index_root)
    lab.link = {"stream": stream, "sync": sync}
    threading.Thread(target=stream.run, daemon=True).start()
    threading.Thread(target=sync.loop, args=(30,), daemon=True).start()
    threading.Thread(target=lab.loop, args=(a.every,), daemon=True).start()
    threading.Thread(target=lab.behaviour_loop, daemon=True).start()
    threading.Thread(target=lab.intraday_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), handler_for(lab))
    print(f"open http://127.0.0.1:{a.port}  (Ctrl+C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
