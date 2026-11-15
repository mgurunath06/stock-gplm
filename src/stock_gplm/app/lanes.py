"""One research lane = one symbol on one timeframe: bars, causal features and simulation arrays.

Bars come from the canonical candles (:mod:`.ingest`) through the M5.4 resamplers
(:mod:`stock_gplm.m54.timeframes`): 1m, 5m, 15m, 30m and 60m are session-anchored at 09:15 and never
filled; weekly bars group sessions by ISO week and become readable only after their week closes.
Cross-timeframe features use the causal as-of rule *latest higher-timeframe bar whose
``avail_ns`` <= this bar's ``end_ns``*, so an unfinished higher-timeframe candle is never read.

Every feature at bar t depends only on bars <= t. :func:`assert_causal` re-computes the features on a
truncated history and checks they are identical, and the test suite runs it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..m54.backtest import Costs, LaneMarket
from ..m54.indicators import FeatureDef, _atr, build_features
from ..m54.timeframes import Bars, align, bars_per_year, build_all, session_ids, timeframe_minutes
from ..m54.unified import _blocked, _intraday_extras, _quarantine
from .db import sha
from .ingest import SymbolData
from .settings import REPO

FEATURE_SPACE = REPO / "config" / "app" / "feature-space.json"
INTRADAY = ("1m", "5m", "15m", "30m", "60m", "4h")


def load_space(path: Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or FEATURE_SPACE).read_text(encoding="utf-8"))


def all_bars(data: SymbolData) -> dict[str, Bars]:
    daily = data.daily[["date", "open", "high", "low", "close", "volume"]].copy()
    minute = data.minute[["start_ist", "session", "open", "high", "low", "close", "volume"]].copy() \
        if len(data.minute) else pd.DataFrame()
    return build_all(daily, minute)


def session_features(minute: pd.DataFrame) -> pd.DataFrame:
    """Per-session intraday summary features from 1-minute bars (known at the session close).

    first-hour and last-hour returns, close versus session VWAP (continuous-phase volume only),
    realised 1-minute volatility, close location in the day's range and the share of bars with a
    withheld or zero volume. Sessions without minute data are simply absent (NaN downstream).
    """
    if minute.empty:
        return pd.DataFrame(columns=["session"])
    m = minute.copy()
    t = m["start_ist"].dt.hour * 60 + m["start_ist"].dt.minute
    g = m.groupby("session", sort=True)
    first_open = g["open"].first()
    fh_close = m[t < 10 * 60 + 15].groupby("session")["close"].last()
    lh_start = m[t >= 14 * 60 + 30].groupby("session")["open"].first()
    last_close = g["close"].last()
    vol_ok = m["volume"].where(m["volume"] > 0)
    tp = (m["high"] + m["low"] + m["close"]) / 3
    pv = (tp * vol_ok).groupby(m["session"]).sum(min_count=1)
    vv = vol_ok.groupby(m["session"]).sum(min_count=1)
    vwap = pv / vv
    lr = np.log(m["close"]).groupby(m["session"]).diff()
    rv = lr.groupby(m["session"]).std() * np.sqrt(375)
    hi, lo = g["high"].max(), g["low"].min()
    out = pd.DataFrame({
        "INTRADAY().first_hour_return": fh_close / first_open - 1,
        "INTRADAY().last_hour_return": last_close / lh_start - 1,
        "INTRADAY().close_vs_vwap": last_close / vwap - 1,
        "INTRADAY().realized_vol": rv,
        "INTRADAY().close_location": (last_close - lo) / (hi - lo).where(hi > lo),
        "INTRADAY().volume_unknown_share": m["volume"].isna().groupby(m["session"]).mean(),
    })
    out.index.name = "session"
    return out.reset_index()


@dataclass
class Lane:
    symbol: str
    timeframe: str
    bars: Bars
    market: LaneMarket
    names: list[str]
    X: np.ndarray  # (T, F) float, NaN where undefined
    defs: dict[str, FeatureDef]
    htf: list[str]
    feature_config_sha: str
    data_fingerprint: str
    costs: Costs
    cost_profile: str
    source: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype="U16"))  # per bar: DAILY / MINUTE_DERIVED / 1m

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}"

    @property
    def intraday(self) -> bool:
        return self.timeframe in INTRADAY

    def label(self, i: int) -> str:
        return self.market.label(i)

    def feature_hash_at(self, i: int) -> str:
        row = self.X[i]
        return sha([None if not np.isfinite(v) else round(float(v), 10) for v in row])[:16]


def _features_for(bars: Bars, htf_bars: Mapping[str, Bars], space: Mapping[str, Any], session_feats: pd.DataFrame | None
                  ) -> tuple[dict[str, np.ndarray], dict[str, FeatureDef], np.ndarray]:
    f = bars.frame.reset_index(drop=True)
    q = space["quality"]
    blocked = _blocked(f, bars.intraday, q)
    df = f[["open", "high", "low", "close", "volume"]]
    arrays, defs, _ = build_features(df, space)
    if bars.intraday:
        ea, ed = _intraday_extras(f, timeframe_minutes(bars.timeframe))
        arrays.update(ea)
        defs.update(ed)
    _quarantine(arrays, blocked, int(q.get("quarantine_bars", 20)))
    end_ns = f["end_ns"].to_numpy(dtype=np.int64)
    for t, hb in htf_bars.items():
        hf = hb.frame.reset_index(drop=True)
        if hf.empty:
            continue
        ha, hd, _ = build_features(hf[["open", "high", "low", "close", "volume"]], space["htf"])
        _quarantine(ha, _blocked(hf, hb.intraday, q), int(q.get("quarantine_bars", 20)))
        avail = hf["avail_ns"].to_numpy(dtype=np.int64)
        for key, d in hd.items():
            k2 = f"{t}:{key}"
            arrays[k2] = align(end_ns, avail, ha[key])
            defs[k2] = FeatureDef(k2, f"htf_{t}", f"{t}:{d.indicator}", d.params, d.output, d.kind, d.levels)
    if session_feats is not None and len(session_feats) and bars.timeframe == "1d":
        sf = session_feats.set_index("session")
        idx = f["session"].map(lambda s: s in sf.index).to_numpy()
        for col in sf.columns:
            v = np.full(len(f), np.nan)
            v[idx] = sf.loc[f["session"][idx], col].to_numpy(dtype=float)
            arrays[col] = v
            defs[col] = FeatureDef(col, "intraday_summary", "INTRADAY", (), col.split(".")[-1], "signed")
    for k in arrays:
        arrays[k] = np.asarray(arrays[k], dtype=float)
        arrays[k][~np.isfinite(arrays[k])] = np.nan
    return arrays, defs, blocked


def build_lane(data: SymbolData, timeframe: str, costs_cfg: Mapping[str, Any], space: Mapping[str, Any] | None = None,
               bars: Mapping[str, Bars] | None = None, cache_dir: Path | None = None) -> Lane | None:
    space = dict(space or load_space())
    bars = bars or all_bars(data)
    b = bars[timeframe]
    if not len(b):
        return None
    htf_names = [t for t in space["lane_htf"].get(timeframe, []) if len(bars[t])]
    htf = {t: bars[t] for t in htf_names}
    # Report-only snapshot indicators must not change model features or frozen prediction identity.
    model_space = {k: v for k, v in space.items() if k != "snapshot_indicators"}
    feat_sha = sha({"space": model_space, "tf": timeframe, "htf": htf_names, "v": "lane.v1"})
    f = b.frame.reset_index(drop=True)
    frame_hash = sha({"bars": int(pd.util.hash_pandas_object(f[["session", "open", "high", "low", "close", "volume",
                                                                "end_ns"]], index=False).sum() % (1 << 62)),
                      "htf": {t: int(pd.util.hash_pandas_object(h.frame[["session", "close", "avail_ns"]], index=False)
                                     .sum() % (1 << 62)) for t, h in htf.items()},
                      "minute": data.summary.get("minute_bars")})
    cache = (cache_dir / f"{data.symbol}-{timeframe}-{feat_sha[:12]}-{frame_hash[:12]}.npz") if cache_dir else None
    if cache is not None and cache.is_file():
        with np.load(cache, allow_pickle=False) as z:
            names = [str(x) for x in z["names"]]
            X = z["X"]
            blocked = z["blocked"]
        defs = {}
        meta = json.loads(cache.with_suffix(".json").read_text(encoding="utf-8"))
        for k, d in meta.items():
            defs[k] = FeatureDef(k, d["family"], d["indicator"], tuple(sorted(d["params"].items())), d["output"],
                                 d["kind"], tuple(d["levels"]))
    else:
        sf = session_features(data.minute) if timeframe == "1d" else None
        arrays, defs, blocked = _features_for(b, htf, space, sf)
        names = sorted(arrays)
        X = np.column_stack([arrays[k] for k in names]) if names else np.zeros((len(f), 0))
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(f".{cache.stem}.{os.getpid()}.npz")
            np.savez_compressed(tmp, names=np.array(names), X=X, blocked=blocked)
            os.replace(tmp, cache)
            cache.with_suffix(".json").write_text(json.dumps({k: d.to_dict() for k, d in defs.items()}), encoding="utf-8")
    natr = (_atr(f, 14) / f["close"]).to_numpy(dtype=float)
    no_signal = f["incomplete"].to_numpy(dtype=bool).copy()
    k = int(space["quality"].get("quarantine_bars", 20))
    for i in np.nonzero(blocked)[0]:
        no_signal[i:i + k + 1] = True
    sid = session_ids(f["session"].to_numpy())
    market = LaneMarket(timeframe, f["session"].to_numpy(), sid, f["end_ns"].to_numpy(dtype=np.int64),
                        *(f[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "volume")),
                        natr, blocked.astype(bool), no_signal, b.intraday, bars_per_year(timeframe))
    profile = "intraday" if b.intraday else "delivery"
    if timeframe == "1d":
        src = data.daily.set_index("date")["source"].reindex(f["session"]).fillna("UNKNOWN").to_numpy(dtype="U16")
    else:
        src = np.full(len(f), "1m" if b.intraday else "DAILY", dtype="U16")
    return Lane(data.symbol, timeframe, b, market, names, X, defs, htf_names, feat_sha,
                sha({"fp": data.fingerprint, "tf": timeframe, "frame": frame_hash})[:24],
                Costs.from_bps(costs_cfg[profile]), profile, src)


def assert_causal(data: SymbolData, timeframe: str, costs_cfg: Mapping[str, Any], cut_session: str,
                  space: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Features up to ``cut_session`` must be identical whether or not later candles exist."""
    full = build_lane(data, timeframe, costs_cfg, space)
    d2 = SymbolData(data.symbol, data.daily[data.daily["date"] <= cut_session].reset_index(drop=True),
                    data.minute[data.minute["session"] <= cut_session].reset_index(drop=True) if len(data.minute)
                    else data.minute, data.fingerprint + "-cut", dict(data.summary))
    part = build_lane(d2, timeframe, costs_cfg, space)
    assert full is not None and part is not None
    n = len(part.market)
    common = [k for k in part.names if k in full.names]
    a = full.X[:n, [full.names.index(k) for k in common]]
    b2 = part.X[:, [part.names.index(k) for k in common]]
    # The final week's weekly bar may legitimately be "not yet readable" on the cut copy; compare exact equality.
    same = np.array_equal(np.nan_to_num(a, nan=1e300), np.nan_to_num(b2, nan=1e300))
    diff_cols = [] if same else [common[j] for j in range(len(common))
                                 if not np.array_equal(np.nan_to_num(a[:, j], nan=1e300), np.nan_to_num(b2[:, j], nan=1e300))]
    return {"bars_compared": n, "features_compared": len(common), "identical": same, "differing": diff_cols[:20]}


def feature_matrix(lane: Lane, rows: np.ndarray, columns: Sequence[int] | None = None) -> np.ndarray:
    X = lane.X[rows]
    return X if columns is None else X[:, list(columns)]
