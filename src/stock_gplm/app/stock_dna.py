"""M5.8 Stock DNA: causal, stock-specific behaviour discovery on existing lane features."""
from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import stats as sps

from ..m54.backtest import bh_adjust
from .db import sha, utc_now
from .ingest import Ingestor, window_data
from .lanes import Lane, all_bars, build_lane

CONTRACT = "stock-gplm.stock-dna.v1"
DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "30m", "60m", "4h", "1d", "1w")


def _independent(indices: np.ndarray, horizon: int) -> np.ndarray:
    keep: list[int] = []
    last = -10**9
    for i in indices.tolist():
        if i > last + horizon:
            keep.append(i)
            last = i
    return np.asarray(keep, dtype=int)


def _wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = successes / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return center - half, center + half


def _regimes(lane: Lane) -> np.ndarray:
    close = lane.market.close
    r = np.diff(np.log(np.where(close > 0, close, np.nan)), prepend=np.nan)
    vol = np.full(len(close), np.nan)
    for i in range(19, len(close)):
        vol[i] = np.nanstd(r[i - 19:i + 1])
    high = np.nanmedian(vol)
    names = np.full(len(close), "SIDEWAYS", dtype="U16")
    ema50 = next((lane.X[:, i] for i, n in enumerate(lane.names) if n == "EMA(period=50).value"), None)
    ema200 = next((lane.X[:, i] for i, n in enumerate(lane.names) if n == "EMA(period=200).value"), None)
    if ema50 is not None and ema200 is not None:
        names[ema50 > ema200] = "BULLISH"
        names[ema50 < ema200] = "BEARISH"
    names[vol > high] = "HIGH_VOL"
    return names


def _candidate(feature: str, side: str, threshold: float, disc_values: np.ndarray,
               validation_values: np.ndarray, disc_returns: np.ndarray, validation_returns: np.ndarray,
               validation_rows: np.ndarray, current_value: float, horizon: int, baseline_up: float,
               baseline_mean: float, regimes: np.ndarray, min_samples: int) -> dict[str, Any]:
    op = "<=" if side == "LOW" else ">="
    dm = disc_values <= threshold if side == "LOW" else disc_values >= threshold
    direction = "UP" if np.nanmean(disc_returns[dm]) >= 0 else "DOWN"
    vm = validation_values <= threshold if side == "LOW" else validation_values >= threshold
    rows = _independent(validation_rows[vm & np.isfinite(validation_returns)], horizon)
    if len(rows):
        lookup = {int(r): float(v) for r, v in zip(validation_rows, validation_returns)}
        rets = np.array([lookup[int(r)] for r in rows])
    else:
        rets = np.zeros(0)
    success = rets > 0 if direction == "UP" else rets < 0
    n, wins = len(rets), int(success.sum())
    rate = float(success.mean()) if n else None
    base_rate = baseline_up if direction == "UP" else 1 - baseline_up
    p = float(sps.binomtest(wins, n, base_rate, alternative="greater").pvalue) if n else None
    lo, hi = _wilson(wins, n)
    signed = rets if direction == "UP" else -rets
    halves = np.array_split(signed, 2) if n else [np.zeros(0), np.zeros(0)]
    regime: dict[str, Any] = {}
    for name in sorted(set(regimes[rows].tolist())) if n else []:
        rr = signed[regimes[rows] == name]
        regime[name] = {"observations": len(rr), "mean_directional_net_return": float(np.mean(rr))}
    return {"pattern_id": "dna-" + sha({"f": feature, "s": side, "t": threshold, "h": horizon})[:16],
            "feature": feature, "condition": f"{feature} {op} {threshold:.10g}", "threshold": threshold,
            "threshold_side": side, "direction": direction, "horizon_bars": horizon, "observations": n,
            "successes": wins, "success_rate": rate, "success_ci95_low": lo, "success_ci95_high": hi,
            "baseline_success_rate": base_rate, "improvement": None if rate is None else rate - base_rate,
            "mean_forward_net_return": float(np.mean(rets)) if n else None,
            "median_forward_net_return": float(np.median(rets)) if n else None,
            "baseline_mean_net_return": baseline_mean, "p_value": p, "q_value": None,
            "stable_halves": bool(all(len(x) and np.mean(x) > 0 for x in halves)), "regimes": regime,
            "last_occurrence_index": int(rows[-1]) if n else None,
            "currently_holds": bool(np.isfinite(current_value) and
                                    (current_value <= threshold if side == "LOW" else current_value >= threshold)),
            "current_value": current_value if np.isfinite(current_value) else None,
            "status": "INSUFFICIENT_EVIDENCE" if n < min_samples else "PENDING_FDR"}


def discover_lane(lane: Lane, cfg: Mapping[str, Any], horizons: Sequence[int] = (1,)) -> dict[str, Any]:
    m, X = lane.market, lane.X
    n = len(m)
    split = max(1, min(n - 3, int(n * float(cfg.get("discovery_fraction", 0.55)))))
    validation_end = max(split + 1, min(n - 2, split + int(n * float(cfg.get("validation_fraction", 0.25)))))
    quantiles = [float(x) for x in cfg.get("quantiles", [0.2, 0.8])]
    max_features = int(cfg.get("max_features", 160))
    min_samples = int(cfg.get("min_validation_observations", 20))
    fdr = float(cfg.get("fdr_q", 0.10))
    regimes = _regimes(lane)
    rows: list[dict[str, Any]] = []
    for horizon in horizons:
        valid_end = n - horizon
        if valid_end <= split + 2:
            continue
        all_rows = np.arange(valid_end)
        returns = m.close[all_rows + horizon] / m.open[all_rows + 1] - 1 - lane.costs.buy - lane.costs.sell
        drows = all_rows[all_rows < split]
        vrows = all_rows[(all_rows >= split) & (all_rows < validation_end)]
        dret, vret = returns[drows], returns[vrows]
        baseline_up = float(np.mean(vret > 0))
        baseline_mean = float(np.mean(vret))
        usable = sorted(range(X.shape[1]), key=lambda j: -int(np.isfinite(X[:split, j]).sum()))[:max_features]
        for j in usable:
            dv, vv = X[drows, j], X[vrows, j]
            finite = dv[np.isfinite(dv)]
            if len(finite) < max(20, min_samples):
                continue
            for q in quantiles:
                side = "LOW" if q < 0.5 else "HIGH"
                threshold = float(np.quantile(finite, q))
                rows.append(_candidate(lane.names[j], side, threshold, dv, vv, dret, vret, vrows,
                                       float(X[-1, j]), horizon, baseline_up, baseline_mean, regimes, min_samples))
    tested = [r for r in rows if r["p_value"] is not None and r["observations"] >= min_samples]
    adjusted = bh_adjust([r["p_value"] for r in tested], len(tested)) if tested else []
    for row, q in zip(tested, adjusted):
        row["q_value"] = q
        directional = (row["mean_forward_net_return"] or 0) * (1 if row["direction"] == "UP" else -1)
        row["status"] = ("VALIDATED" if q <= fdr and directional > 0 and row["stable_halves"] else
                         "EXPERIMENTAL" if row["p_value"] < 0.10 and directional > 0 else "REJECTED")
    rows.sort(key=lambda r: (r["status"] != "VALIDATED", r["status"] != "EXPERIMENTAL",
                             r["q_value"] if r["q_value"] is not None else 1.0,
                             -(r["improvement"] or -1)))
    return {"contract": CONTRACT, "symbol": lane.symbol, "timeframe": lane.timeframe,
            "data_fingerprint": lane.data_fingerprint, "feature_config_sha": lane.feature_config_sha,
            "bars": n, "data_start": lane.label(0), "data_end": lane.label(n - 1), "split_index": split,
            "validation_end_index": validation_end, "calibration_start_index": validation_end,
            "patterns": rows, "counts": {s: sum(r["status"] == s for r in rows) for s in
                                           ("VALIDATED", "EXPERIMENTAL", "REJECTED", "INSUFFICIENT_EVIDENCE")}}


def run(app: Any, symbol: str, timeframes: Sequence[str], as_of: str | None, out: Path,
        horizons: Sequence[int] = (1,)) -> dict[str, Any]:
    data = Ingestor(app.s, app.db).ingest(symbol, use_minute=True)
    data = window_data(data, end=as_of)
    bars = all_bars(data)
    cfg = app.s.section("m58").get("stock_dna", {})
    reports = []
    for timeframe in timeframes:
        if timeframe not in bars or not len(bars[timeframe]):
            reports.append({"symbol": data.symbol, "timeframe": timeframe, "status": "BLOCKED_MISSING_DATA",
                            "reason": f"no accepted {timeframe} candles at or before {as_of or data.last_session}"})
            continue
        lane = build_lane(data, timeframe, app.s.section("costs"), bars=bars,
                          cache_dir=app.s.cache_dir / "features")
        if lane is None:
            continue
        reports.append({"status": "COMPLETED", **discover_lane(lane, cfg, horizons)})
    out.mkdir(parents=True, exist_ok=True)
    doc = {"contract": CONTRACT, "as_of": as_of or data.last_session, "created_utc": utc_now(),
           "symbol": data.symbol, "timeframes": list(timeframes), "lanes": reports}
    (out / "stock-dna.json").write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
    flat = [{"symbol": r["symbol"], "timeframe": r["timeframe"], **p} for r in reports
            if r.get("status") == "COMPLETED" for p in r["patterns"]]
    fields = list(flat[0]) if flat else ["symbol", "timeframe", "status"]
    with (out / "stock-dna.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows({k: json.dumps(v) if isinstance(v, dict) else v for k, v in row.items()} for row in flat)
    return {"status": "COMPLETED", "symbol": data.symbol, "as_of": doc["as_of"], "lanes": len(reports),
            "patterns": len(flat), "validated": sum(p["status"] == "VALIDATED" for p in flat),
            "experimental": sum(p["status"] == "EXPERIMENTAL" for p in flat), "report": str(out / "stock-dna.json")}


__all__ = ["CONTRACT", "DEFAULT_TIMEFRAMES", "discover_lane", "run"]
