"""Decision-oriented notifications for the live lab (PAPER only).

One event per *stock setup*, not per strategy alert:
  SETUP     a group of long strategies fired together on one completed bar (symbol x timeframe x bar)
  OUTCOME   that setup's median target / stop / time limit / end of day was reached (tracked on 1-minute bars)
  CONFLICT  bearish strategies started firing on the same stock+timeframe while the setup is open
  BEHAVIOUR a behaviour change became CONFIRMED
  SYSTEM    the feed or cloud link changed state (alerts paused / resumed)
Every text is built from fixed templates over real numbers (no generated wording) and always says
what happened, why it matters and what a user could do. Nothing here is advice; no order is placed.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

import numpy as np

_seq = itertools.count(1)


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or not b else round((a / b - 1) * 100, 2)


def _f(v: float | None) -> str:
    return "–" if v is None else f"{v:,.2f}".rstrip("0").rstrip(".")


def note(kind: str, symbol: str | None, title: str, body: str, action: str, severity: str, key: str,
         data: Mapping[str, Any] | None = None, ts: str | None = None) -> dict[str, Any]:
    return {"id": next(_seq), "kind": kind, "symbol": symbol, "title": title, "body": body, "action": action,
            "severity": severity, "key": key, "data": dict(data or {}), "ts": ts}


def bar_close(bar_label: str, tf_min: int) -> datetime:
    t = datetime.fromisoformat(bar_label.replace(" ", "T")) + timedelta(minutes=tf_min)
    close = t.replace(hour=15, minute=30)
    return min(t, close)


def setup(symbol: str, tf: str, tf_min: int, bar: str, alerts: Sequence[Mapping[str, Any]], applicable: int,
          last: float | None, context: Mapping[str, Any], catch_up: bool, ts: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the tracked setup (median levels across the agreeing strategies) and its SETUP notification."""
    entry = float(alerts[0]["entry"])
    stops = [a["stop"] for a in alerts if a.get("stop")]
    tgts = [a["target"] for a in alerts if a.get("target")]
    hold = int(np.median([a["max_hold_min"] for a in alerts]))
    stop = round(float(np.median(stops)), 2) if stops else None
    tgt = round(float(np.median(tgts)), 2) if tgts else None
    closed = bar_close(bar, tf_min)
    until = min(closed + timedelta(minutes=hold), closed.replace(hour=15, minute=29))
    risk = entry - stop if stop else None
    rr = round((tgt - entry) / risk, 1) if risk and tgt and risk > 0 else None
    chase = round(entry + 0.5 * (tgt - entry), 2) if tgt else None
    names = sorted({a["name"] for a in alerts})
    n = len(alerts)
    key = f"{symbol}|{tf}|{bar}"
    cf = dict(context.get("confidence") or {})
    s = {"key": key, "symbol": symbol, "tf": tf, "bar": bar, "n": n, "applicable": applicable, "entry": entry,
         "confidence": cf,
         "stop": stop, "target": tgt, "until": until.strftime("%Y-%m-%dT%H:%M"), "closed_at": closed.strftime("%H:%M"),
         "state": "OPEN", "catch_up": catch_up, "names": names[:6], "rr": rr, "chase_above": chase,
         "conflict_noted": False}
    ctx = []
    if context.get("week"):
        ctx.append(f"week: {context['week']}")
    if context.get("change"):
        ctx.append(context["change"])
    ctx.append(f"{context['bear']} bearish strategies firing" if context.get("bear") else "no bearish signals")
    moved = _pct(last, entry) if last else None
    lv = lambda name, v: f"{name} {_f(v)} ({_pct(v, entry):+.1f}%)" if v else f"{name} none (strategy uses a time exit)"  # noqa: E731
    body = (f"Entry ≈{_f(entry)} (bar closed {s['closed_at']}) · {lv('stop', stop)} · {lv('target', tgt)}"
            + (f" · R:R {rr}" if rr else "") +
            f" · valid until ~{until:%H:%M}\nWhy: {n} of {applicable} {tf} strategies agree — e.g. {', '.join(names[:3])}"
            + (f"\nConfidence: {cf['label']} — these strategies won {cf['p'] * 100:.0f}% of {cf['n']} past signals on "
               f"{symbol} {tf} (last {cf.get('sessions')} sessions, avg {cf['avg']:+.2f}%/trade)"
               if cf.get("n") else "\nConfidence: NO_EVIDENCE — no completed past signal of these strategies here yet")
            + f"\nContext: {'; '.join(ctx)}")
    if moved is not None and tgt and last >= chase:
        action = f"Price already at {_f(last)} ({moved:+.1f}%), more than half way to target — don't chase; wait for a pullback."
    else:
        action = (f"Consider a long near {_f(entry)} only while price stays above {_f(stop)}"
                  + (f"; don't chase above {_f(chase)}" if chase else "") + ".")
    sev = "strong" if n >= 20 else "normal"
    title = (f"{symbol} · BUY setup ({tf}) · {n}/{applicable} agree · confidence {cf.get('label', '?')}"
             + (" · catch-up" if catch_up else ""))
    return s, note("SETUP", symbol, title, body, action, sev, key,
                   {"n": n, "tf": tf, "entry": entry, "stop": stop, "target": tgt, "rr": rr, "catch_up": catch_up,
                    "confidence": cf.get("label")}, ts)


def outcome(s: dict[str, Any], why: str, price: float, at: str, ts: str) -> dict[str, Any]:
    p = _pct(price, s["entry"]) or 0.0
    head = {"TARGET": f"🎯 target reached {p:+.1f}%", "STOP": f"✖ stop hit {p:+.1f}%",
            "EXPIRED": f"⏱ time limit reached ({p:+.1f}%)", "EOD": f"🔔 market close ({p:+.1f}%)"}[why]
    act = {"TARGET": "If you took it: book profit, or trail the stop to entry to protect the gain.",
           "STOP": "Setup failed — if you took it, exit; don't average down.",
           "EXPIRED": "The setup's time window has passed without target or stop — if still in, exit or re-evaluate.",
           "EOD": "Session over — intraday setups close here; review in the History tab."}[why]
    return note("OUTCOME", s["symbol"], f"{s['symbol']} {s['tf']} setup ({s['closed_at']}): {head}",
                f"Entry {_f(s['entry'])} → {_f(price)} at {at} · stop {_f(s['stop'])} · target {_f(s['target'])} · "
                f"{s['n']} strategies had agreed", act, "good" if why == "TARGET" else "bad" if why == "STOP" else "info",
                s["key"], {"setup": s["key"], "result": why, "pnl_pct": p, "tf": s["tf"], "n": s["n"]}, ts)


def conflict(s: dict[str, Any], bear_n: int, last: float | None, ts: str) -> dict[str, Any]:
    p = _pct(last, s["entry"])
    return note("CONFLICT", s["symbol"], f"{s['symbol']}: ⚠ {bear_n} bearish strategies now firing ({s['tf']})",
                f"Against the open BUY setup from {s['closed_at']} (entry {_f(s['entry'])}, now {_f(last)}"
                f"{f' {p:+.1f}%' if p is not None else ''}).",
                f"Signals disagree — if you took the long, tighten the stop to entry ({_f(s['entry'])}) or exit.",
                "warn", s["key"], {"setup": s["key"], "tf": s["tf"], "n": s["n"], "bear": bear_n}, ts)


def behaviour(change: Mapping[str, Any], ts: str) -> dict[str, Any]:
    up = change.get("effect", 0) > 0
    hint = {"ret": "Favour setups in the new direction; treat opposite signals with caution.",
            "range": "Volatility changed — size positions and stops to the new range.",
            "relvol": "Unusual volume — moves may be news- or flow-driven; check for news.",
            "follow_d": "Day-to-day character changed — breakout setups behave differently now.",
            "ac5": "Intraday character changed — adjust between breakout and mean-reversion setups.",
            "open_follow": "Opening behaviour changed — be careful with opening-range setups.",
            "mkt_agree": "Link to the market changed — index direction is a weaker guide for this stock now.",
            }.get(change.get("feature"), "Behaviour shifted — review this stock's setups with the new context.")
    return note("BEHAVIOUR", change["symbol"], f"{change['symbol']} behaviour changed: {change['phrase']}",
                f"Since {change.get('since')}: recent {change['recent']} vs usual {change['usual']} {change['unit']} · "
                f"{change['persist']}/5 recent sessions · {change.get('scope', '').lower().replace('_', ' ')}",
                hint, "info", f"beh|{change['symbol']}|{change['feature']}|{'up' if up else 'down'}",
                {"feature": change.get("feature")}, ts)


def intraday(ev: Mapping[str, Any], ts: str) -> dict[str, Any]:
    act = {"VWAP_LOST": "Buyers lost control intraday — be careful with new longs; tighten stops on open ones.",
           "VWAP_RECLAIM": "Buyers regained VWAP — long setups have better odds while price holds above it.",
           "VOLUME_SURGE": "Unusual participation — moves can extend; check news before acting.",
           "REVERSAL": "Intraday direction flipped — re-check any open setup on this stock.",
           "RANGE_EXPANSION": "Volatility expanded — widen stops or reduce size; avoid chasing.",
           "AGAINST_MARKET": "Stock-specific flow — index direction is not a guide right now."}.get(ev["type"], "")
    return note("INTRADAY", ev["symbol"], f"{ev['symbol']} {ev['at']}: {ev['headline']}", ev["detail"], act,
                ev.get("severity", "info"), f"intra|{ev['symbol']}|{ev['type']}|{ev['side']}",
                {"type": ev["type"], "side": ev["side"]}, ts)


def login_reminder(reason: str, ts: str) -> dict[str, Any]:
    """Owner reminder: the Shoonya token will not cover today's session (or has expired)."""
    return note("SYSTEM", None, "Shoonya login needed", reason,
                "Open the page on this PC or your own device and use 'Connect Shoonya' (owner only).",
                "bad", f"login|{ts[:5]}", {"owner_only": True}, ts)


def system(old: str, new: str, detail: str, ts: str) -> dict[str, Any]:
    bad = new not in ("LIVE",)
    return note("SYSTEM", None, f"Feed {new}" + (" — alerts paused" if bad else " — alerts resumed"),
                f"Was {old}. {detail}".strip(),
                "Check the Data link tab (token, collector, cloud link)." if bad else "No action needed.",
                "bad" if bad else "good", f"sys|{new}", {}, ts)
