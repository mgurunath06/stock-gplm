"""Section 6: coverage of the owner's minimum primitive list on every timeframe (``config/verify/primitives.json``)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..app import strategy_dsl as dsl
from . import fixtures as fx
from .core import FAIL, PASS, WARN, Check, Section, worst

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "config" / "verify" / "primitives.json"
TIMEFRAMES = ("1m", "5m", "15m", "30m", "60m", "4h", "1d", "1w")


def frames() -> dict[str, pd.DataFrame]:
    w = fx.ohlcv(520, fx.SEED + 21, vol=0.035)
    w["session"] = pd.date_range("2016-01-08", periods=len(w), freq="W-FRI").strftime("%Y-%m-%d")
    w["end_ns"] = pd.to_datetime(w["session"]).astype("int64").to_numpy() + 10 * 3600 * 10**9
    return {"1d": fx.ohlcv(700), "1w": w,
            "1m": fx.intraday(24, 250, fx.SEED + 29, minutes=1),
            "5m": fx.intraday(24, 75, fx.SEED + 31, minutes=5),
            "15m": fx.intraday(40, 25), "30m": fx.intraday(54, 13, fx.SEED + 33, minutes=30),
            "60m": fx.intraday(100, 7, fx.SEED + 35, minutes=60),
            "4h": fx.intraday(350, 2, fx.SEED + 37, minutes=240)}


def _ctx(frame: pd.DataFrame, tf: str, bench: dict[str, pd.DataFrame]) -> dsl.Context:
    intr = tf not in {"1d", "1w"}

    def loader(sym: str, t: str) -> dsl.Context | None:
        b = bench[t].copy()
        return dsl.Context.from_frame(b, t, intr)
    return dsl.Context.from_frame(frame, tf, intr, benchmark_loader=loader, default_benchmark="SYNBENCH-EQ")


def _bench(fr: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out = {}
    for tf, f in fr.items():
        b = f.copy()
        rng = np.random.default_rng(len(f))
        k = np.exp(np.cumsum(rng.normal(0, 0.004, len(b))))
        for col in ("open", "high", "low", "close"):
            b[col] = b[col] * k
        b["high"] = b[["open", "high", "low", "close"]].max(axis=1)
        b["low"] = b[["open", "high", "low", "close"]].min(axis=1)
        out[tf] = b
    return out


def evaluate(expr: str, frame: pd.DataFrame, tf: str, bench: dict[str, pd.DataFrame]) -> dict[str, Any]:
    n = len(frame)
    try:
        full = np.asarray(dsl.evaluate(dsl.parse(expr), _ctx(frame, tf, bench)), dtype=float)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if full.shape != (n,):
        return {"ok": False, "error": f"shape {full.shape}"}
    finite = int(np.isfinite(full).sum())
    leak = None
    rng = np.random.default_rng(7)
    cut = (2 * n) // 3
    alt = frame.copy()
    mult = np.exp(rng.normal(0, 0.05, n - cut))
    for k in ("open", "high", "low", "close"):
        alt.loc[alt.index[cut:], k] = alt[k].to_numpy()[cut:] * mult
    alt.loc[alt.index[cut:], "volume"] = alt["volume"].to_numpy()[cut:] * rng.uniform(0.3, 3, n - cut)
    for variant in (frame.iloc[:cut].copy(), alt):
        got = np.asarray(dsl.evaluate(dsl.parse(expr), _ctx(variant, tf, bench)), dtype=float)[:cut]
        same = (np.isnan(got) & np.isnan(full[:cut])) | np.isclose(got, full[:cut], rtol=1e-9, atol=1e-12)
        if not same.all():
            leak = int(np.nonzero(~same)[0][0])
            break
    return {"ok": finite > 0 and leak is None, "finite_bars": finite, "bars": n, "leak_bar": leak,
            "inf": int(np.isinf(full).sum()), "last_value": float(full[-1]) if n else None}


def run(sec: Section, ev: dict[str, Any]) -> None:
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    fr = frames()
    bench = _bench(fr)
    verified = {r["indicator"] for r in ev.get("indicator_rows", []) if r["max_rel_err"] <= 1e-8
                and not r["infinite_values"]}
    rows: list[dict[str, Any]] = []
    ev["primitive_rows"] = rows

    def coverage(c: Check) -> None:
        for row in man["rows"]:
            res = _row(row, fr, bench, verified)
            rows.append(res)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if r["verdict"] == FAIL:
                c.fail(("TRACKED GAP " if r["status"] in ("NOT_IMPLEMENTED", "PARTIAL") else "")
                       + f"[{r['module']}] {r['name']}: {r['why']}")
        for r in rows:
            if r["verdict"] == WARN:
                c.warn(f"[{r['module']}] {r['name']}: {r['why']}")
        evals = sum(len(r["evaluations"]) for r in rows)
        c.evidence.update(rows=len(rows), counts=counts, evaluations=evals, timeframes=list(TIMEFRAMES),
                          manifest=str(MANIFEST))
        c.note(f"{len(rows)} listed calculations (the owner's list says 118; its tables hold {len(rows)} rows); "
               f"{evals} expression x timeframe evaluations; " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    sec.run("minimum_primitive_coverage", "Every calculation in the owner's minimum list is computed, finite and "
            "causal on all eight governed timeframes (intraday-only rows on the six intraday lanes), or its gap "
            "is named", coverage)


def _row(row: dict[str, Any], fr: dict[str, pd.DataFrame], bench: dict[str, pd.DataFrame], verified: set[str]
         ) -> dict[str, Any]:
    tfs = tuple(row.get("timeframes") or
                (tuple(tf for tf in TIMEFRAMES if tf not in {"1d", "1w"}) if row.get("intraday") else TIMEFRAMES))
    evals = []
    errors = []
    base_exprs = list(row.get("exprs", []))
    scoped = row.get("exprs_by_timeframe") or {}
    for tf in tfs:
        expressions = base_exprs + (scoped.get("intraday", []) if tf not in {"1d", "1w"} else scoped.get("daily", []))
        expressions += scoped.get(tf, [])
        for expr in dict.fromkeys(expressions):
            r = evaluate(expr, fr[tf], tf, bench)
            evals.append({"expr": expr, "timeframe": tf, **r})
            if not r["ok"]:
                errors.append(f"{expr} on {tf}: " + (r.get("error") or (f"future leak at bar {r['leak_bar']}"
                                                                        if r.get("leak_bar") is not None
                                                                        else "no finite value")))
    missing = row.get("missing") or []
    kinds = {m["kind"] for m in missing}
    vb = row.get("verified_by")
    all_exprs = base_exprs + [x for values in (row.get("exprs_by_timeframe") or {}).values() for x in values]
    if errors:
        status, verdict, why = "ERROR", FAIL, "; ".join(errors[:4])
    elif not all_exprs and "NOT_IMPLEMENTED" in kinds:
        status, verdict, why = "NOT_IMPLEMENTED", FAIL, "; ".join(f"{m['what']} ({m['reason']})" for m in missing)
    elif not all_exprs:
        status, verdict = ("NEEDS_DATA" if kinds & {"DATA", "DEFERRED"} else "METHOD"), WARN
        why = "; ".join(f"{m['kind']}: {m['what']} ({m['reason']})" for m in missing) or row.get("method", "")
    else:
        if vb and vb in verified:
            status = "VERIFIED_REFERENCE"
        elif vb:
            status, errors = "ERROR", [f"reference check {vb} did not pass"]
        elif row.get("composed"):
            status = "COMPOSED"
        else:
            status = "COMPUTED"
        verdict = FAIL if status == "ERROR" else PASS
        why = "; ".join(errors)
        if "NOT_IMPLEMENTED" in kinds:
            status, verdict = "PARTIAL", FAIL
            why = "missing: " + "; ".join(f"{m['what']} ({m['reason']})" for m in missing if m["kind"] == "NOT_IMPLEMENTED")
        elif kinds:
            verdict = worst(verdict, WARN)
            why = "; ".join(f"{m['kind']}: {m['what']}" for m in missing)
        if row.get("approximation"):
            verdict = worst(verdict, WARN)
            why = (why + "; " if why else "") + "approximation: " + row["approximation"]
    return {"module": row["module"], "name": row["name"], "status": status, "verdict": verdict, "why": why,
            "exprs": all_exprs, "timeframes": list(tfs), "verified_by": vb or "",
            "missing": missing, "evaluations": evals}
