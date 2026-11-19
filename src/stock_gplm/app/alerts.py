"""M7.1 — alert lifecycle, deduplication, stale-data suppression and notifications (PRD §7, §8).

States (append-only ``alert_events``; each state at most once per alert)::

    CANDIDATE -> ARMED -> TRIGGERED -> EXIT
                     \\-> CANCELLED (invalidation before entry)
                     \\-> EXPIRED   (entry window passed)
    CANDIDATE -> SUPPRESSED (stale/incomplete data, alerts disabled, risk blocker, duplicate)

* One alert per BUY opportunity; the opportunity snapshot is frozen in ``alerts.snapshot_json``.
* **Dedupe**: an opportunity whose symbol and source already have an ARMED or TRIGGERED alert is
  recorded as SUPPRESSED (``DUPLICATE_ACTIVE``), never as a second live alert.
* **Mode**: an alert is ``LIVE`` only when its opportunity status is LIVE; otherwise ``SHADOW``
  (research). Notifications go out for LIVE alerts only (``notify_shadow`` opts SHADOW in).
* **Catch-up**: an event is ``current`` only when its bar ended within ``max_event_age_minutes`` of the
  processing time. Events found while catching up after an outage are stored with ``current=0``
  and are never notified, so obsolete alerts are never presented as current.
* No order is ever placed. Fills shown in alerts are trigger levels, not executions.
"""
from __future__ import annotations

import json
import math
import os
import smtplib
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping, Sequence

from .db import AuditDB, jdump, sha, utc_now
from .targets import ns_label

ALERT_CONTRACT = "stock-gplm.alert-lifecycle.m71.v1"
STATES = ("CANDIDATE", "ARMED", "TRIGGERED", "CANCELLED", "EXPIRED", "EXIT", "SUPPRESSED")
TERMINAL = ("CANCELLED", "EXPIRED", "EXIT", "SUPPRESSED")
NOTIFY_STATES = ("ARMED", "TRIGGERED", "CANCELLED", "EXPIRED", "EXIT")
DEFAULTS = {"max_event_age_minutes": 30, "notify_shadow": False,
            "channels": {"file": {"enabled": True}, "webhook": {"enabled": False, "url_env": "GPLM_ALERT_WEBHOOK"},
                         "email": {"enabled": False, "host_env": "GPLM_SMTP_HOST", "user_env": "GPLM_SMTP_USER",
                                   "password_env": "GPLM_SMTP_PASSWORD", "to_env": "GPLM_ALERT_EMAIL_TO"}}}


def _state(db: AuditDB, alert_id: str) -> str | None:
    r = db.one("SELECT state FROM alert_events WHERE alert_id=? ORDER BY seq DESC LIMIT 1", (alert_id,))
    return r["state"] if r else None


def _event(db: AuditDB, alert_id: str, state: str, bar_ns: int | None, price: float | None, reason: str,
           current: bool, payload: Mapping[str, Any] | None = None) -> bool:
    with db.tx() as c:
        n = c.execute("INSERT OR IGNORE INTO alert_events (alert_id, state, bar_label, bar_ns, price, reason, current, "
                      "payload_json, ts_utc, run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (alert_id, state, ns_label(bar_ns) if bar_ns else None, bar_ns, price, reason, int(current),
                       jdump(payload or {}), utc_now(), db.run_id)).rowcount
    return bool(n)


def _is_current(bar_ns: int | None, now_ns: int, cfg: Mapping[str, Any]) -> bool:
    return bar_ns is not None and 0 <= now_ns - int(bar_ns) <= int(cfg["max_event_age_minutes"]) * 60_000_000_000


def create(db: AuditDB, opp: Mapping[str, Any], now_ns: int, cfg: Mapping[str, Any] | None = None,
           blockers: Sequence[str] = ()) -> dict[str, Any] | None:
    """Create the alert of one BUY opportunity (idempotent). Returns the alert summary or None (not a BUY)."""
    c = {**DEFAULTS, **(cfg or {})}
    if opp.get("action") != "BUY":
        return None
    src = opp["source"]
    dedupe = opp["natural_key"]
    aid = "al-" + sha(dedupe)[:20]
    mode = "LIVE" if opp.get("status") == "LIVE" else "SHADOW"
    with db.tx() as cx:
        new = cx.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?,?)",
                         (aid, dedupe, opp["opportunity_id"], opp["symbol"],
                          jdump({**opp, "alert_mode": mode, "contract": ALERT_CONTRACT}), utc_now(), db.run_id)).rowcount
    dec = int(opp["decision_ns"])
    cur = _is_current(dec, now_ns, c)
    if not new:
        return status(db, aid)
    _event(db, aid, "CANDIDATE", dec, opp.get("trigger"), "opportunity created", cur, {"mode": mode})
    active = db.q("SELECT a.alert_id FROM alerts a WHERE a.symbol=? AND a.alert_id<>? AND "
                  "json_extract(a.snapshot_json, '$.source.id')=? AND "
                  "(SELECT state FROM alert_events e WHERE e.alert_id=a.alert_id ORDER BY seq DESC LIMIT 1) "
                  "IN ('ARMED','TRIGGERED')", (opp["symbol"], aid, src["id"]))
    why: list[str] = list(blockers)
    fresh = (opp.get("freshness") or {}).get("status")
    if fresh is not None and fresh != "FRESH":
        why.append(f"DATA_NOT_FRESH:{fresh}")
    if active:
        why.append(f"DUPLICATE_ACTIVE:{active[0]['alert_id']}")
    if why:
        _event(db, aid, "SUPPRESSED", dec, None, "; ".join(why), cur, {"mode": mode})
    else:
        _event(db, aid, "ARMED", dec, opp.get("trigger"), "waiting for the entry zone", cur, {"mode": mode})
    db.event("ALERT_CREATED", {"alert_id": aid, "mode": mode, "suppressed": why}, subject=opp["symbol"])
    return status(db, aid)


def advance(db: AuditDB, alert_id: str, bars: Sequence[Mapping[str, Any]], now_ns: int,
            cfg: Mapping[str, Any] | None = None) -> list[str]:
    """Advance one alert over completed bars after its decision bar (each: end_ns, open, high, low, close,
    session). Returns the new states recorded."""
    c = {**DEFAULTS, **(cfg or {})}
    a = db.one("SELECT * FROM alerts WHERE alert_id=?", (alert_id,))
    if a is None:
        raise KeyError(alert_id)
    opp = json.loads(a["snapshot_json"])
    st = _state(db, alert_id)
    if st in TERMINAL:
        return []
    dec = int(opp["decision_ns"])
    after = sorted((b for b in bars if int(b["end_ns"]) > dec), key=lambda b: int(b["end_ns"]))
    zone = opp.get("entry_zone") or [None, None]
    stop = opp.get("stop")
    targets = opp.get("targets") or []
    hold = int(opp.get("expected_holding_bars") or opp.get("horizon_bars") or 1)
    valid = int(opp.get("entry_valid_bars") or 1)
    out: list[str] = []
    entry_i = None
    trig = db.one("SELECT bar_ns, price FROM alert_events WHERE alert_id=? AND state='TRIGGERED'", (alert_id,))
    if trig:
        entry_i = next((i for i, b in enumerate(after) if int(b["end_ns"]) == int(trig["bar_ns"])), None)
    for i, b in enumerate(after):
        ns = int(b["end_ns"])
        cur = _is_current(ns, now_ns, c)
        if st == "ARMED":
            if i >= valid:
                if _event(db, alert_id, "EXPIRED", ns, None, f"entry zone not reached within {valid} bar(s)", cur):
                    out.append("EXPIRED")
                st = "EXPIRED"
                break
            if stop is not None and float(b["low"]) <= float(stop):
                if _event(db, alert_id, "CANCELLED", ns, float(stop), "invalidated before entry", cur):
                    out.append("CANCELLED")
                st = "CANCELLED"
                break
            lo, hi = float(zone[0]), float(zone[1])
            if float(b["low"]) <= hi and float(b["high"]) >= lo:
                px = min(max(float(b["open"]), lo), hi)
                if _event(db, alert_id, "TRIGGERED", ns, px, "price traded inside the entry zone (simulated level, "
                                                              "no order placed)", cur):
                    out.append("TRIGGERED")
                st, entry_i = "TRIGGERED", i
        if st == "TRIGGERED" and entry_i is not None and i >= entry_i:
            # conservative: on the trigger bar itself only the stop is checked (target not credited)
            why = px = None
            if stop is not None and float(b["low"]) <= float(stop):
                why, px = "STOP", float(stop)
            elif i == entry_i:
                continue
            elif targets and float(b["high"]) >= float(targets[0]):
                why, px = "TARGET_1", float(targets[0])
            elif i - entry_i >= hold:
                why, px = "TIME", float(b["close"])
            if why:
                if _event(db, alert_id, "EXIT", ns, px, why, cur):
                    out.append("EXIT")
                st = "EXIT"
                break
    return out


def status(db: AuditDB, alert_id: str) -> dict[str, Any]:
    a = db.one("SELECT alert_id, symbol, opportunity_id, created_utc, json_extract(snapshot_json, '$.alert_mode') mode "
               "FROM alerts WHERE alert_id=?", (alert_id,))
    ev = db.q("SELECT state, bar_label, price, reason, current FROM alert_events WHERE alert_id=? ORDER BY seq",
              (alert_id,))
    return {**(a or {}), "state": ev[-1]["state"] if ev else None, "history": ev}


def open_alerts(db: AuditDB) -> list[str]:
    rows = db.q("SELECT a.alert_id FROM alerts a WHERE (SELECT state FROM alert_events e WHERE e.alert_id=a.alert_id "
                "ORDER BY seq DESC LIMIT 1) IN ('ARMED','TRIGGERED','CANDIDATE') ORDER BY a.created_utc")
    return [r["alert_id"] for r in rows]


# ----------------------------------------------------------------------------- notifications

def _message(snap: Mapping[str, Any], ev: Mapping[str, Any]) -> str:
    return (f"[{snap.get('alert_mode')}] {snap.get('symbol')} {ev['state']} @ {ev.get('price')} "
            f"({ev.get('bar_label')}) | action {snap.get('action')} | entry {snap.get('entry_zone')} | stop "
            f"{snap.get('stop')} | targets {snap.get('targets')} | p={snap.get('probability')} conf "
            f"{snap.get('confidence')} | horizon {snap.get('horizon')} | {ev.get('reason')} | research decision "
            f"support, no order placed")


def _send(channel: str, ch: Mapping[str, Any], text: str, payload: Mapping[str, Any], outbox: Path) -> str:
    if channel == "file":
        outbox.parent.mkdir(parents=True, exist_ok=True)
        with outbox.open("a", encoding="utf-8") as f:
            f.write(jdump({"text": text, **payload}) + "\n")
        return "DELIVERED"
    if channel == "webhook":
        url = os.environ.get(ch.get("url_env", ""), "")
        if not url:
            return "NOT_CONFIGURED"
        req = urllib.request.Request(url, data=json.dumps({"text": text, "alert": payload}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (owner-configured URL)
            return "DELIVERED" if 200 <= r.status < 300 else f"HTTP_{r.status}"
    if channel == "email":
        host, to = os.environ.get(ch.get("host_env", ""), ""), os.environ.get(ch.get("to_env", ""), "")
        if not host or not to:
            return "NOT_CONFIGURED"
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = text[:120], os.environ.get(ch.get("user_env", ""), "gplm"), to
        msg.set_content(text)
        with smtplib.SMTP_SSL(host, 465, timeout=15) as s:
            user = os.environ.get(ch.get("user_env", ""), "")
            if user:
                s.login(user, os.environ.get(ch.get("password_env", ""), ""))
            s.send_message(msg)
        return "DELIVERED"
    return "UNKNOWN_CHANNEL"


def notify(db: AuditDB, outbox: Path, cfg: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Deliver every not-yet-notified *current* event of LIVE alerts (SHADOW too with ``notify_shadow``)."""
    c = {**DEFAULTS, **(cfg or {})}
    chans = {k: v for k, v in (c.get("channels") or {}).items() if v.get("enabled")}
    rows = db.q("SELECT e.*, a.snapshot_json FROM alert_events e JOIN alerts a ON a.alert_id=e.alert_id "
                "WHERE e.current=1 AND e.state IN ({}) ORDER BY e.seq".format(",".join("?" * len(NOTIFY_STATES))),
                NOTIFY_STATES)
    counts: dict[str, int] = {}
    for r in rows:
        snap = json.loads(r["snapshot_json"])
        if snap.get("alert_mode") != "LIVE" and not c.get("notify_shadow"):
            continue
        for name, ch in chans.items():
            if db.one("SELECT 1 FROM notifications WHERE alert_id=? AND state=? AND channel=?",
                      (r["alert_id"], r["state"], name)):
                continue
            try:
                res = _send(name, ch, _message(snap, r), {"alert_id": r["alert_id"], "state": r["state"],
                                                          "bar": r["bar_label"], "symbol": snap.get("symbol")}, outbox)
            except Exception as exc:  # delivery failure is logged, never fatal
                res = f"FAILED:{type(exc).__name__}"
            with db.tx() as cx:
                cx.execute("INSERT OR IGNORE INTO notifications (alert_id, state, channel, status, detail, ts_utc, run_id) "
                           "VALUES (?,?,?,?,?,?,?)", (r["alert_id"], r["state"], name, res.split(":")[0], res,
                                                      utc_now(), db.run_id))
            counts[res.split(":")[0]] = counts.get(res.split(":")[0], 0) + 1
    return counts


def history(db: AuditDB, limit: int = 100) -> list[dict[str, Any]]:
    rows = db.q("SELECT alert_id FROM alerts ORDER BY created_utc DESC LIMIT ?", (int(limit),))
    return [status(db, r["alert_id"]) for r in rows]


__all__ = ["ALERT_CONTRACT", "DEFAULTS", "NOTIFY_STATES", "STATES", "TERMINAL", "advance", "create", "history", "notify",
           "open_alerts", "status"]
