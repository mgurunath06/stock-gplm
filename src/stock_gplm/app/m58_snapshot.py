"""Point-in-time, content-addressable feature evidence for M5.8 predictions."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..m54.indicators import _atr, _ema, _macd, _rsi, _sma
from ..m54.timeframes import Bars
from .db import sha
from .ingest import SymbolData
from .lanes import Lane

CONTRACT = "stock-gplm.m58-feature-snapshot.v1"
IMPLEMENTATION = "stock_gplm.app.m58_snapshot.v1"


def _number(value: Any, reason: str = "insufficient warm-up bars") -> dict[str, Any]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float("nan")
    return {"value": value, "reason": None} if np.isfinite(value) else {"value": None, "reason": reason}


def _effective_index(bar: Bars, cutoff_ns: int, *, source: bool) -> int | None:
    f = bar.frame
    if f.empty:
        return None
    clock = f["end_ns"] if source and bar.timeframe != "1w" else f["avail_ns"]
    ok = (clock.to_numpy(dtype=np.int64) <= cutoff_ns) & ~f["incomplete"].to_numpy(dtype=bool)
    rows = np.flatnonzero(ok)
    return int(rows[-1]) if len(rows) else None


def _session_vwap(data: SymbolData, session: str, cutoff_ns: int) -> dict[str, Any]:
    m = data.minute
    if m.empty:
        return _number(None, "timeframe unavailable: accepted minute bars are required for session VWAP")
    rows = m[m["session"].astype(str) == session].copy()
    if rows.empty:
        return _number(None, "missing source bars")
    starts = pd.to_datetime(rows["start_ist"]).dt.tz_localize("Asia/Kolkata").astype("int64")
    rows = rows[starts.to_numpy(dtype=np.int64) + 60_000_000_000 <= cutoff_ns]
    volume = rows["volume"].where(rows["volume"] > 0)
    total = float(volume.sum(min_count=1))
    if not np.isfinite(total) or total <= 0:
        return _number(None, "missing source bars: positive volume unavailable")
    typical = (rows["high"] + rows["low"] + rows["close"]) / 3.0
    return _number(float((typical * volume).sum(min_count=1) / total))


def _configured_indicators(frame: pd.DataFrame, row: int, config: Mapping[str, Any],
                           data: SymbolData, cutoff_ns: int) -> dict[str, Any]:
    """Calculate report-only indicators from declarative configuration, never in the renderer."""
    df = frame[["open", "high", "low", "close", "volume"]].astype(float)
    out: dict[str, Any] = {}
    for name, spec in config.items():
        params = spec.get("params", {})
        if name in {"EMA", "SMA"}:
            fn = _ema if name == "EMA" else _sma
            for period in params.get("period", []):
                out[f"{name}(period={period})"] = {
                    "indicator": name, "parameters": {"period": period}, "output": "value",
                    **_number(fn(df["close"], int(period)).iloc[row])}
        elif name == "RSI":
            for period in params.get("period", []):
                out[f"RSI(period={period})"] = {"indicator": name, "parameters": {"period": period},
                    "output": "value", **_number(_rsi(df, int(period))["value"].iloc[row])}
        elif name == "MACD":
            for fast in params.get("fast", []):
                for slow in params.get("slow", []):
                    for signal in params.get("signal", []):
                        p = {"fast": fast, "slow": slow, "signal": signal}
                        values = _macd(df, int(fast), int(slow), int(signal))
                        line = values["line_norm"].iloc[row] * df["close"].iloc[row]
                        hist = values["hist_norm"].iloc[row] * df["close"].iloc[row]
                        signal_value = line - hist
                        for output, value in (("macd", line), ("signal", signal_value), ("histogram", hist)):
                            out[f"MACD(fast={fast},signal={signal},slow={slow}).{output}"] = {
                                "indicator": name, "parameters": p, "output": output, **_number(value)}
        elif name == "ROC":
            for period in params.get("period", []):
                value = df["close"].iloc[row] / df["close"].shift(int(period)).iloc[row] - 1
                out[f"ROC(period={period})"] = {"indicator": name, "parameters": {"period": period},
                    "output": "value", **_number(value)}
        elif name == "ATR":
            for period in params.get("period", []):
                out[f"ATR(period={period})"] = {"indicator": name, "parameters": {"period": period},
                    "output": "value", **_number(_atr(df, int(period)).iloc[row])}
        elif name == "BOLLINGER":
            for period in params.get("period", []):
                for width in params.get("width", []):
                    mid = _sma(df["close"], int(period))
                    sd = df["close"].rolling(int(period), min_periods=int(period)).std(ddof=0)
                    values = {"lower": mid - float(width) * sd, "middle": mid,
                              "upper": mid + float(width) * sd}
                    for output, series in values.items():
                        out[f"BOLLINGER(period={period},width={width}).{output}"] = {
                            "indicator": name, "parameters": {"period": period, "width": width},
                            "output": output, **_number(series.iloc[row])}
        elif name == "VOLUME":
            for period in params.get("period", []):
                avg = df["volume"].rolling(int(period), min_periods=int(period)).mean().iloc[row]
                ratio = df["volume"].iloc[row] / avg if np.isfinite(avg) and avg != 0 else np.nan
                for output, value in (("average", avg), ("ratio", ratio)):
                    out[f"VOLUME(period={period}).{output}"] = {"indicator": name,
                        "parameters": {"period": period}, "output": output, **_number(value)}
        elif name == "VWAP":
            out["VWAP(session=true)"] = {"indicator": name, "parameters": {"session": True},
                "output": "value", **_session_vwap(data, str(frame["session"].iloc[row]), cutoff_ns)}
        else:
            out[f"{name}()"] = {"indicator": name, "parameters": params, "output": "value",
                                "value": None, "reason": "indicator implementation unavailable"}
    return out


def build_snapshot(lane: Lane, bars: Mapping[str, Bars], data: SymbolData, decision_row: int,
                   snapshot_config: Mapping[str, Any], patterns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    cutoff_ns = int(lane.market.end_ns[decision_row])
    consumed = {str(p["feature"]) for p in patterns}
    values = {name: lane.X[decision_row, i] for i, name in enumerate(lane.names)}
    frames: dict[str, Any] = {}
    for timeframe in [lane.timeframe, *lane.htf]:
        bar = bars[timeframe]
        idx = _effective_index(bar, cutoff_ns, source=timeframe == lane.timeframe)
        if idx is None:
            frames[timeframe] = {"source_timeframe": timeframe, "effective_bar_timestamp": None,
                "calculation_status": "UNAVAILABLE", "reason": "timeframe unavailable",
                "bar": None, "prediction_features": {}, "calculated_features": {},
                "contextual_indicators": {}}
            continue
        f = bar.frame
        prefix = "" if timeframe == lane.timeframe else f"{timeframe}:"
        feature_rows: dict[str, Any] = {}
        for key, value in values.items():
            if prefix and not key.startswith(prefix):
                continue
            if not prefix and ":" in key:
                continue
            local_key = key[len(prefix):]
            definition = lane.defs[key]
            feature_rows[local_key] = {"indicator": definition.indicator,
                "parameters": dict(definition.params), "output": definition.output,
                "family": definition.family, "consumed_by_prediction": key in consumed,
                **_number(value)}
        effective = int(f["end_ns"].iloc[idx])
        frames[timeframe] = {
            "source_timeframe": timeframe,
            "effective_bar_timestamp": pd.Timestamp(effective, unit="ns", tz="UTC").isoformat(),
            "bar_end": pd.Timestamp(effective, unit="ns", tz="UTC").isoformat(),
            "calculation_status": "COMPLETED",
            "bar": {k: (_number(f[k].iloc[idx])["value"]) for k in ("open", "high", "low", "close", "volume")},
            "prediction_features": {k: v for k, v in feature_rows.items() if v["consumed_by_prediction"]},
            "calculated_features": {k: v for k, v in feature_rows.items() if not v["consumed_by_prediction"]},
            "contextual_indicators": _configured_indicators(f, idx, snapshot_config, data, cutoff_ns),
        }
    fired, not_fired = [], []
    for pattern in patterns:
        value = values.get(str(pattern["feature"]), np.nan)
        holds = bool(np.isfinite(value) and (value <= pattern["threshold"] if pattern["threshold_side"] == "LOW"
                                              else value >= pattern["threshold"]))
        row = {"pattern_id": pattern["pattern_id"], "condition": pattern["condition"],
               "feature": pattern["feature"], "value": float(value) if np.isfinite(value) else None}
        (fired if holds else not_fired).append(row)
    snapshot = {"contract": CONTRACT, "indicator_implementation_version": IMPLEMENTATION,
                "data_fingerprint": lane.data_fingerprint,
                "feature_configuration_hash": sha({"model": lane.feature_config_sha,
                                                    "snapshot_indicators": snapshot_config}),
                "model_feature_configuration_hash": lane.feature_config_sha,
                "prediction_source_timeframe": lane.timeframe,
                "prediction_effective_bar_timestamp": pd.Timestamp(cutoff_ns, unit="ns", tz="UTC").isoformat(),
                "calculation_status": "COMPLETED", "timeframes": frames,
                "patterns": {"fired": fired, "not_fired": not_fired}}
    return {**snapshot, "snapshot_sha256": sha(snapshot)}


__all__ = ["CONTRACT", "IMPLEMENTATION", "build_snapshot"]
