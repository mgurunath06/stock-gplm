"""M6.4 — the permanent 13 August 2026 next-candle benchmark (PRD §4).

The benchmark tests *process*, not profitability: chronological processing, prediction freezing,
outcome revelation, feedback, versioning, reproducibility and leakage prevention.

Eligible cases per timeframe (PRD §4):

* intraday and daily: every target bar closing on the benchmark session. The decision bar is the bar
  before it; the first intraday target uses the last completed bar of the prior session.
* weekly and monthly: the candle containing the benchmark date. Its forecast is frozen at the
  preceding completed bar and scored only when the target candle is final (registered calendar);
  otherwise the case stays ``PENDING``.

Event-time ordering (documented, reproducible): all events sort by ``(event_ns, phase, timeframe,
decision_ns)`` with phase ``REVEAL`` (0) before ``FREEZE`` (1). Outcomes that mature at the same
instant are revealed as **one batch**; the adaptive predictor updates once per batch and only then
are forecasts whose decision bar closed at that instant frozen. Every forecast is frozen strictly
before its own target closes, so a later outcome can never affect an earlier decision.

Two variants run on identical inputs: ``frozen`` (no updates) and ``adaptive`` (updates after each
matured batch). The adaptive run is not an untouched holdout and is labelled as such.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

import numpy as np
import pandas as pd

from .db import AuditDB, jdump, sha, utc_now
from .targets import ns_label

BENCHMARK_CONTRACT = "stock-gplm.benchmark-13aug.m64.v1"
BENCHMARK_DATE = "2026-08-13"
UP, DOWN, NEUTRAL = "UP", "DOWN", "NEUTRAL"
INTRADAY_TF = ("1m", "3m", "5m", "15m", "30m", "60m", "4h")
REVEAL_PHASE, FREEZE_PHASE = 0, 1


class Predictor(Protocol):
    version: str

    def predict(self, timeframe: str, history: pd.DataFrame) -> dict[str, Any]: ...

    def update(self, timeframe: str, outcomes: Sequence[Mapping[str, Any]]) -> None: ...


@dataclass
class BaseRatePredictor:
    """Research reference predictor: Beta-smoothed P(up) and mean close-to-close return of the lane's own
    history before the decision. ``update`` folds matured outcomes in (adaptive variant only)."""

    band: float
    prior: float = 1.0
    margin: float = 0.02
    window: int = 250
    version: str = "base-rate.v1"
    _extra: dict[str, list[float]] = field(default_factory=dict)

    def predict(self, timeframe: str, history: pd.DataFrame) -> dict[str, Any]:
        c = history["close"].to_numpy(float)
        r = (c[1:] / c[:-1] - 1)[-self.window:]
        r = np.concatenate([r, np.asarray(self._extra.get(timeframe, []), float)])
        r = r[np.isfinite(r)]
        ups = float((r > self.band).sum())
        downs = float((r < -self.band).sum())
        p = (ups + self.prior) / (ups + downs + 2 * self.prior)
        exp = float(r.mean()) if r.size else 0.0
        d = UP if p >= 0.5 + self.margin else DOWN if p <= 0.5 - self.margin else NEUTRAL
        return {"direction": d, "p_up": round(p, 6), "expected_return": round(exp, 8), "samples": int(r.size),
                "range": round(float(np.std(r)) if r.size else 0.0, 8)}

    def update(self, timeframe: str, outcomes: Sequence[Mapping[str, Any]]) -> None:
        self._extra.setdefault(timeframe, []).extend(float(o["realized_return"]) for o in outcomes
                                                     if o.get("realized_return") is not None)


def classify(ret: float | None, band: float) -> str | None:
    if ret is None or not math.isfinite(ret):
        return None
    return UP if ret > band else DOWN if ret < -band else NEUTRAL


def cases(frames: Mapping[str, pd.DataFrame], date: str) -> list[dict[str, Any]]:
    """Eligible (timeframe, decision index, target index) cases. Frames need columns session, close,
    end_ns, avail_ns, incomplete (as built by the resamplers)."""
    out: list[dict[str, Any]] = []
    for tf, f in sorted(frames.items()):
        if f is None or not len(f):
            continue
        f = f.reset_index(drop=True)
        sess = f["session"].astype(str).to_numpy()
        if tf in ("1w", "1mo"):
            key = (lambda s: pd.Timestamp(s).isocalendar()[:2]) if tf == "1w" else (lambda s: s[:7])
            want = key(date)
            idx = [i for i in range(len(f)) if key(sess[i]) == want]
            if not idx:
                out.append({"timeframe": tf, "decision": None, "target": None, "status": "NO_TARGET_BAR"})
                continue
            tgt = idx[0]
            if tgt == 0 or bool(f["incomplete"].iloc[tgt - 1]):
                out.append({"timeframe": tf, "decision": None, "target": tgt, "status": "NO_COMPLETED_DECISION_BAR"})
                continue
            out.append({"timeframe": tf, "decision": tgt - 1, "target": tgt, "status": "ELIGIBLE"})
            continue
        targets = [i for i in range(len(f)) if sess[i] == date]
        if not targets:
            out.append({"timeframe": tf, "decision": None, "target": None, "status": "SESSION_NOT_IN_DATA"})
            continue
        for t in targets:
            if t == 0:
                continue
            out.append({"timeframe": tf, "decision": t - 1, "target": t, "status": "ELIGIBLE"})
    return out


def run(frames: Mapping[str, pd.DataFrame], date: str, predictor: Predictor, *, adaptive: bool, band: float
        ) -> dict[str, Any]:
    """Run one variant. Pure and deterministic: identical inputs give identical rows and ``result_sha256``."""
    cs = cases(frames, date)
    events: list[tuple[int, int, str, int, dict[str, Any]]] = []
    for k, c in enumerate(cs):
        if c["status"] != "ELIGIBLE":
            continue
        f = frames[c["timeframe"]].reset_index(drop=True)
        d, t = c["decision"], c["target"]
        dec_ns = int(f["avail_ns"].iloc[d]) if c["timeframe"] in ("1w", "1mo") else int(f["end_ns"].iloc[d])
        tgt_final = not bool(f["incomplete"].iloc[t])
        tgt_ns = int(f["avail_ns"].iloc[t]) if c["timeframe"] in ("1w", "1mo") else int(f["end_ns"].iloc[t])
        if tgt_ns <= dec_ns:
            raise AssertionError(f"case {k}: target closes before its decision")
        c.update(decision_ns=dec_ns, target_ns=tgt_ns, target_final=tgt_final)
        events.append((dec_ns, FREEZE_PHASE, c["timeframe"], dec_ns, c))
        if tgt_final:
            events.append((tgt_ns, REVEAL_PHASE, c["timeframe"], dec_ns, c))
    events.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
    rows: list[dict[str, Any]] = []
    frozen: dict[tuple[str, int], dict[str, Any]] = {}
    i = 0
    while i < len(events):
        j = i
        while j < len(events) and events[j][0] == events[i][0] and events[j][1] == events[i][1]:
            j += 1
        batch = events[i:j]
        ns, phase = batch[0][0], batch[0][1]
        if phase == FREEZE_PHASE:
            for _, _, tf, _, c in batch:
                f = frames[tf].reset_index(drop=True)
                hist = f.iloc[:c["decision"] + 1]
                pred = predictor.predict(tf, hist)
                rec = {"kind": "FREEZE", "event_ns": ns, "event_label": ns_label(ns), "timeframe": tf,
                       "decision_bar": ns_label(c["decision_ns"]), "target_bar": ns_label(c["target_ns"]),
                       "input_sha": sha({"tf": tf, "rows": len(hist), "close": [round(float(x), 6) for x in
                                                                               hist["close"].tail(50)]})[:16],
                       "model_version": predictor.version, **pred}
                frozen[(tf, c["decision_ns"])] = rec
                rows.append(rec)
        else:
            matured = []
            for _, _, tf, dns, c in batch:
                p = frozen.get((tf, dns))
                if p is None:
                    raise AssertionError("reveal before freeze")
                f = frames[tf].reset_index(drop=True)
                prev_c, tgt_c = float(f["close"].iloc[c["decision"]]), float(f["close"].iloc[c["target"]])
                ret = tgt_c / prev_c - 1 if prev_c > 0 else None
                actual = classify(ret, band)
                up = 1.0 if actual == UP else 0.0
                o = {"kind": "REVEAL", "event_ns": ns, "event_label": ns_label(ns), "timeframe": tf,
                     "decision_bar": p["decision_bar"], "target_bar": p["target_bar"], "predicted": p["direction"],
                     "actual": actual, "realized_return": None if ret is None else round(ret, 8),
                     "correct": None if actual is None else int(p["direction"] == actual),
                     "brier": None if actual is None else round((p["p_up"] - up) ** 2, 8),
                     "forecast_error": None if ret is None else round(ret - p["expected_return"], 8)}
                rows.append(o)
                matured.append(o)
            if adaptive and matured:
                for tf in sorted({o["timeframe"] for o in matured}):
                    predictor.update(tf, [o for o in matured if o["timeframe"] == tf])
                rows.append({"kind": "UPDATE", "event_ns": ns, "event_label": ns_label(ns),
                             "batch": len(matured), "timeframes": sorted({o["timeframe"] for o in matured})})
        i = j
    for c in cs:
        if c["status"] == "ELIGIBLE" and not c.get("target_final"):
            rows.append({"kind": "PENDING", "timeframe": c["timeframe"], "target_bar": ns_label(c["target_ns"]),
                         "reason": "target candle not final (needs a registered calendar proving completion)"})
        elif c["status"] != "ELIGIBLE":
            rows.append({"kind": "SKIPPED", "timeframe": c["timeframe"], "reason": c["status"]})
    return {"rows": rows, "result_sha256": sha(rows), "summary": summarize(rows)}


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        tf = r.get("timeframe")
        if tf is None:
            continue
        b = by.setdefault(tf, {"frozen": 0, "revealed": 0, "correct": 0, "brier": [], "pending": 0, "skipped": []})
        if r["kind"] == "FREEZE":
            b["frozen"] += 1
        elif r["kind"] == "REVEAL":
            b["revealed"] += 1
            b["correct"] += int(r["correct"] or 0)
            if r["brier"] is not None:
                b["brier"].append(r["brier"])
        elif r["kind"] == "PENDING":
            b["pending"] += 1
        elif r["kind"] == "SKIPPED":
            b["skipped"].append(r["reason"])
    for b in by.values():
        br = b.pop("brier")
        b["accuracy"] = round(b["correct"] / b["revealed"], 4) if b["revealed"] else None
        b["brier"] = round(float(np.mean(br)), 6) if br else None
    return {"by_timeframe": by, "updates": sum(1 for r in rows if r["kind"] == "UPDATE"),
            "note": "process benchmark only: one session never supports a profitability claim"}


def record(db: AuditDB, symbol: str, date: str, variant: str, config: Mapping[str, Any], result: Mapping[str, Any]
           ) -> dict[str, Any]:
    """Store a run append-only. Same inputs -> same run_key; a rerun must reproduce the stored result hash."""
    run_key = sha({"contract": BENCHMARK_CONTRACT, "symbol": symbol, "date": date, "variant": variant,
                   "config": config})[:32]
    prev = db.one("SELECT result_sha256 FROM benchmark_runs WHERE run_key=?", (run_key,))
    if prev:
        same = prev["result_sha256"] == result["result_sha256"]
        db.event("BENCHMARK_REPRODUCED" if same else "BENCHMARK_NOT_REPRODUCED",
                 {"run_key": run_key, "stored": prev["result_sha256"], "now": result["result_sha256"]},
                 subject=symbol, level="INFO" if same else "ERROR")
        return {"run_key": run_key, "stored": False, "reproduced": same}
    with db.tx() as c:
        c.execute("INSERT INTO benchmark_runs VALUES (?,?,?,?,?,?,?,?)",
                  (run_key, date, variant, jdump({"symbol": symbol, **config}), result["result_sha256"],
                   jdump(result["summary"]), utc_now(), db.run_id))
        for k, r in enumerate(result["rows"]):
            c.execute("INSERT INTO benchmark_rows VALUES (?,?,?,?,?)",
                      (run_key, k, r["kind"], int(r.get("event_ns") or 0), jdump(r)))
    db.event("BENCHMARK_RECORDED", {"run_key": run_key, "variant": variant, "result_sha256": result["result_sha256"]},
             subject=symbol)
    return {"run_key": run_key, "stored": True, "reproduced": None}


def frames_from_bars(bars: Mapping[str, Any], timeframes: Sequence[str]) -> dict[str, pd.DataFrame]:
    return {tf: bars[tf].frame for tf in timeframes if tf in bars and len(bars[tf])}


__all__ = ["BENCHMARK_CONTRACT", "BENCHMARK_DATE", "BaseRatePredictor", "Predictor", "cases", "classify",
           "frames_from_bars", "record", "run", "summarize"]
