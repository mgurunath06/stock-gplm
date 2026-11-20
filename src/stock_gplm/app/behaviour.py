"""Behaviour profiles across time scales + change detection ("of late it behaves X instead of Y").

Causal by construction: every number uses completed sessions only (today's partial session is reported
separately as "today so far" and never enters change tests).

Per session we compute a small, fixed set of features:
  daily-derived (long history):  ret%, range%, gap%, close-location (0=low..1=high), relative volume,
                                 follow_d (today continued yesterday's direction: 1/0),
                                 mkt_agree (moved in the market's direction: 1/0)
  minute-derived (where minutes exist): ac5 (lag-1 autocorrelation of 5-min returns: >0 moves follow
                                 through, <0 snap back), open_follow (first-30-min move carried through the
                                 rest of the day: 1/0), vwap_above (share of 5-min closes above session VWAP)
Windows: D (today so far), W (5 sessions), M (21), Q (63), Y (252) -> plain labels.
Change detection: last 10 completed sessions vs the up-to-250 before them, per feature: Mann-Whitney U,
Benjamini-Hochberg over every (symbol, feature) test, effect size >= 0.5 sd, and persistence (>= 4 of the
last 5 sessions on the new side). A change seen in >= 50 % of symbols is reported as market-wide.
The market is the equal-weight average of the watchlist (the index is not stored locally).
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats as sps

CONTRACT = "stock-gplm.behaviour.v1"
WINDOWS = {"D": 1, "W": 5, "M": 21, "Q": 63, "Y": 252}
SHORT, LONG, PERSIST = 10, 250, 5
FEATURES = {  # name: (phrase if higher, phrase if lower, unit, minute_based)
    "ret": ("drifting up", "drifting down", "% / day", False),
    "range": ("more volatile", "calmer", "% day range", False),
    "gap": ("bigger opening gaps", "smaller opening gaps", "% |gap|", False),
    "clv": ("closing near the day's high (buying into the close)", "closing near the day's low (selling into the close)",
            "close location 0-1", False),
    "relvol": ("heavier volume", "lighter volume", "x usual volume", False),
    "follow_d": ("more trend-following day to day", "more reverting day to day", "share of days continuing", False),
    "mkt_agree": ("moving more with the market", "moving more on its own", "share of days with market", False),
    "ac5": ("intraday moves follow through", "intraday moves snap back", "5-min autocorr", True),
    "open_follow": ("opening move carries through the day", "opening move fades", "share of days", True),
    "vwap_above": ("holding above VWAP more", "holding below VWAP more", "share of time", True),
}


# ----------------------------------------------------------------------------- per-session features
def session_minutes(minute: pd.DataFrame, min_bars: int = 30) -> dict[str, pd.DataFrame]:
    """minute frame (session, ts, open, high, low, close, volume) -> per-session 5-minute frames."""
    out = {}
    if minute is None or minute.empty:
        return out
    for sess, g in minute.groupby("session", sort=True):
        g = g.sort_values("ts")
        if len(g) < min_bars:
            continue
        b = (g["ts"] - g["ts"].iloc[0]).dt.total_seconds().floordiv(300).astype(int)
        f = g.groupby(b).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                             volume=("volume", "sum"))
        f["tp_v"] = ((g["high"] + g["low"] + g["close"]) / 3 * g["volume"].clip(lower=0)).groupby(b).sum()
        out[str(sess)] = f.reset_index(drop=True)
    return out


def minute_features(f5: pd.DataFrame) -> dict[str, float]:
    c = f5["close"].to_numpy(float)
    r = np.diff(np.log(c))
    ac = float(np.corrcoef(r[:-1], r[1:])[0, 1]) if len(r) > 10 and np.std(r[:-1]) > 0 and np.std(r[1:]) > 0 else np.nan
    o = float(f5["open"].iloc[0])
    k = min(6, len(c) - 1)  # first 30 minutes
    first, rest = c[k - 1] / o - 1, c[-1] / c[k - 1] - 1
    of = float(np.sign(first) == np.sign(rest)) if first != 0 and rest != 0 and len(c) > 12 else np.nan
    vol = f5["volume"].clip(lower=0).cumsum().to_numpy(float)
    vwap = np.divide(f5["tp_v"].cumsum().to_numpy(float), vol, out=np.full(len(c), np.nan), where=vol > 0)
    va = float(np.nanmean(c > vwap)) if np.isfinite(vwap).any() else np.nan
    return {"ac5": ac, "open_follow": of, "vwap_above": va}


def daily_features(d: pd.DataFrame, mkt_ret: pd.Series) -> pd.DataFrame:
    d = d.sort_values("date").reset_index(drop=True)
    pc = d["close"].shift(1)
    out = pd.DataFrame({"date": d["date"].astype(str)})
    out["ret"] = (d["close"] / pc - 1) * 100
    out["range"] = (d["high"] - d["low"]) / pc * 100
    out["gap"] = (d["open"] / pc - 1).abs() * 100
    rng = (d["high"] - d["low"]).replace(0, np.nan)
    out["clv"] = (d["close"] - d["low"]) / rng
    vol = d["volume"].where(d["volume"] > 0)
    out["relvol"] = vol / vol.rolling(60, min_periods=20).median().shift(1)
    s = np.sign(out["ret"])
    out["follow_d"] = np.where((s != 0) & (s.shift(1) != 0) & s.notna() & s.shift(1).notna(),
                               (s == s.shift(1)).astype(float), np.nan)
    m = mkt_ret.reindex(out["date"]).to_numpy()
    out["mkt_agree"] = np.where(np.isfinite(m) & (m != 0) & (s != 0) & s.notna(), (np.sign(m) == s).astype(float), np.nan)
    out["mkt"] = m
    bad = out["ret"].abs() > 25  # split / corporate action, not behaviour
    out.loc[bad, ["ret", "range", "gap", "follow_d", "mkt_agree"]] = np.nan
    return out.iloc[1:].reset_index(drop=True)


# ----------------------------------------------------------------------------- window labels
def _f(x: Any, nd: int = 2) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def window_profile(feat: pd.DataFrame, n: int, ref_range: float) -> dict[str, Any]:
    w = feat.tail(n)
    if len(w) < max(1, min(n, 3)):
        return {"n": len(w), "label": "INSUFFICIENT"}
    r = w["ret"].dropna()
    cum = float(np.prod(1 + r / 100) - 1) * 100 if len(r) else np.nan
    if n == 1 or len(r) < 3:
        z = (cum / ref_range) if ref_range else np.nan
        direction = "UP" if z > 0.4 else "DOWN" if z < -0.4 else "FLAT"
    else:
        z = r.mean() / (r.std(ddof=1) / math.sqrt(len(r))) if r.std(ddof=1) > 0 else 0.0
        direction = "UP" if z > 1.0 else "DOWN" if z < -1.0 else "SIDEWAYS"
    if n >= 21 and len(r) >= 15:
        ac = float(r.autocorr(1))
        char = "TRENDING" if ac > 0.08 else "REVERTING" if ac < -0.08 else "MIXED"
        char_basis = f"day-to-day autocorr {ac:+.2f}"
    else:
        a5 = w["ac5"].dropna() if "ac5" in w else pd.Series(dtype=float)
        if len(a5):
            ac = float(a5.mean())
            char = "FOLLOW-THROUGH" if ac > 0.05 else "CHOPPY" if ac < -0.05 else "MIXED"
            char_basis = f"5-min autocorr {ac:+.2f}"
        else:
            ac, char, char_basis = np.nan, "?", "no minute data"
    vr = float(w["range"].mean() / ref_range) if ref_range else np.nan
    vol = "HIGH-VOL" if vr > 1.3 else "LOW-VOL" if vr < 0.75 else "NORMAL-VOL"
    mk = w[["ret", "mkt"]].dropna()
    corr = float(mk["ret"].corr(mk["mkt"])) if len(mk) >= 8 else np.nan
    beta = float(np.polyfit(mk["mkt"], mk["ret"], 1)[0]) if len(mk) >= 8 and mk["mkt"].std() > 0 else np.nan
    link = "WITH-MARKET" if corr > 0.6 else "OWN-WAY" if corr < 0.3 else "" if np.isfinite(corr) else ""
    rv = float(w["relvol"].mean()) if w["relvol"].notna().any() else np.nan
    vlab = "HEAVY-VOL" if rv > 1.5 else "LIGHT-VOL" if rv < 0.7 else ""
    tags = [t for t in (direction, char if char != "?" else "", vol, link, vlab) if t]
    return {"n": len(w), "label": " · ".join(tags), "direction": direction, "character": char,
            "character_basis": char_basis, "volatility": vol, "market": link or "PARTLY-WITH-MARKET",
            "ret_pct": _f(cum), "z": _f(z), "range_pct": _f(w["range"].mean()), "range_vs_year": _f(vr),
            "ac": _f(ac, 3), "corr_mkt": _f(corr), "beta": _f(beta), "relvol": _f(rv),
            "clv": _f(w["clv"].mean()), "open_follow": _f(w["open_follow"].mean()) if "open_follow" in w else None,
            "vwap_above": _f(w["vwap_above"].mean()) if "vwap_above" in w else None}


# ----------------------------------------------------------------------------- change detection
def _bh(p: Sequence[float]) -> np.ndarray:
    p = np.asarray(p, float)
    n = len(p)
    if not n:
        return p
    o = np.argsort(p)
    q = p[o] * n / (np.arange(n) + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(n)
    out[o] = np.minimum(q, 1)
    return out


def change_tests(feat: pd.DataFrame) -> list[dict[str, Any]]:
    tests = []
    for name, (hi, lo, unit, minute_based) in FEATURES.items():
        if name not in feat:
            continue
        x = feat[["date", name]].dropna()
        if len(x) < SHORT + (8 if minute_based else 30):
            continue
        short, long = x.tail(SHORT), x.iloc[:-SHORT].tail(LONG)
        a, b = short[name].to_numpy(float), long[name].to_numpy(float)
        sd = float(np.std(b, ddof=1))
        if not sd > 0:
            continue
        try:
            p = float(sps.mannwhitneyu(a, b, alternative="two-sided").pvalue)
        except ValueError:
            continue
        eff = (float(a.mean()) - float(b.mean())) / sd
        side = 1 if eff > 0 else -1
        base = float(b.mean())
        recent = x.tail(PERSIST)
        on_new = [(v - base) * side > 0 for v in recent[name].to_numpy(float)]
        # start of the run: walk back while sessions stay on the new side (allow one miss)
        seq = [(v - base) * side > 0 for v in x[name].to_numpy(float)][::-1]
        k, miss = 0, 0
        for ok in seq:
            if ok:
                k += 1
            elif miss == 0 and k > 0:
                miss, k = 1, k + 1
            else:
                break
        start = x["date"].iloc[-k] if k else None
        tests.append({"feature": name, "phrase": hi if side > 0 else lo, "unit": unit, "p": p, "effect": round(eff, 2),
                      "recent": round(float(a.mean()), 3), "usual": round(base, 3), "persist": int(sum(on_new)),
                      "since": start, "n_short": len(a), "n_long": len(b), "minute_based": minute_based})
    return tests


# ----------------------------------------------------------------------------- intraday (today, every minute)
def _five(df: pd.DataFrame) -> pd.DataFrame:
    """1m rows (t 'YYYY-MM-DDTHH:MM', o h l c v) of ONE session -> 5-min bars with slot index and running VWAP."""
    ts = pd.to_datetime(df["t"])
    slot = ((ts.dt.hour * 60 + ts.dt.minute) - (9 * 60 + 15)) // 5
    g = df.assign(slot=slot.to_numpy(), pv=((df["h"] + df["l"] + df["c"]) / 3 * df["v"].clip(lower=0)).to_numpy())
    f = g.groupby("slot").agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"),
                              pv=("pv", "sum"), n=("o", "size")).reset_index()
    cv = f["v"].clip(lower=0).cumsum().replace(0, np.nan)
    f["vwap"] = f["pv"].cumsum() / cv
    return f


def intraday(min1: Sequence[Mapping[str, Any]], today: str, mkt30: float | None = None,
             now_slot: int | None = None) -> dict[str, Any]:
    """Today's intraday behaviour vs this stock's own recent sessions at the SAME time of day.

    Uses completed 5-min bars only. Baselines: median per-slot volume, median |30-min return| and median 30-min
    range over the previous (up to 20) sessions. Returns current state + detected events (latest first)."""
    df = pd.DataFrame(list(min1))
    if df.empty:
        return {}
    df["session"] = df["t"].str[:10]
    hist = df[df["session"] < today]
    tod = df[df["session"] == today]
    if tod.empty:
        return {}
    f = _five(tod)
    if now_slot is not None:
        f = f[f["slot"] < now_slot]
    f = f[f["n"] >= 5] if len(f) > 1 else f  # completed 5-min bars
    if len(f) < 4:
        return {}
    sessions = sorted(hist["session"].unique())[-20:]
    base_v, r30, g30, cumv = {}, [], [], {}
    for s in sessions:
        h = _five(hist[hist["session"] == s])
        for sl, v in zip(h["slot"], h["v"]):
            base_v.setdefault(sl, []).append(v)
        c = h.set_index("slot")["c"]
        for sl in c.index:
            if sl - 6 in c.index:
                r30.append(abs(c[sl] / c[sl - 6] - 1) * 100)
        hh, ll = h.set_index("slot")["h"], h.set_index("slot")["l"]
        for sl in hh.index:
            w = [x for x in range(sl - 5, sl + 1) if x in hh.index]
            if len(w) == 6:
                g30.append((hh[w].max() - ll[w].min()) / c[sl] * 100)
        cs = h.set_index("slot")["v"].clip(lower=0).cumsum()
        for sl, v in cs.items():
            cumv.setdefault(sl, []).append(v)
    med = lambda xs: float(np.median(xs)) if len(xs) else np.nan  # noqa: E731
    last = f.iloc[-1]
    sl = int(last["slot"])
    hhmm = lambda s_: f"{(9 * 60 + 15 + (s_ + 1) * 5) // 60:02d}:{(9 * 60 + 15 + (s_ + 1) * 5) % 60:02d}"  # noqa: E731
    at = hhmm(sl)
    pace = float(f["v"].clip(lower=0).sum()) / med(cumv.get(sl, [])) if med(cumv.get(sl, [])) else np.nan
    t30, typ_r, typ_g = f.tail(6), med(r30), med(g30)
    ret30 = (float(t30["c"].iloc[-1]) / float(t30["o"].iloc[0]) - 1) * 100
    ret_day = (float(f["c"].iloc[-1]) / float(f["o"].iloc[0]) - 1) * 100
    rng30 = (float(t30["h"].max()) - float(t30["l"].min())) / float(t30["c"].iloc[-1]) * 100
    t15 = f.tail(3)
    bv = sum(med(base_v.get(int(s_), [])) for s_ in t15["slot"])
    vol15 = float(t15["v"].clip(lower=0).sum()) / bv if bv and np.isfinite(bv) else np.nan
    above = (f["c"] > f["vwap"]).to_numpy()
    ev = []

    def add(kind, side, headline, detail, sev="info"):
        ev.append({"type": kind, "side": side, "at": at, "headline": headline, "detail": detail, "severity": sev})

    if len(above) >= 6 and not above[-3:].any() and above[-6:-3].sum() >= 2:
        add("VWAP_LOST", "down", f"lost VWAP ({float(f['vwap'].iloc[-1]):,.2f})",
            f"3 completed 5-min bars closed below VWAP after trading above it · day {ret_day:+.2f}%", "warn")
    if len(above) >= 6 and above[-3:].all() and (~above[-6:-3]).sum() >= 2:
        add("VWAP_RECLAIM", "up", f"reclaimed VWAP ({float(f['vwap'].iloc[-1]):,.2f})",
            f"3 completed 5-min bars closed above VWAP after trading below it · day {ret_day:+.2f}%", "good")
    if np.isfinite(vol15) and vol15 >= 2.0:
        r15 = (float(t15["c"].iloc[-1]) / float(t15["o"].iloc[0]) - 1) * 100
        add("VOLUME_SURGE", "up" if r15 >= 0 else "down",
            f"volume surge {vol15:.1f}x usual for this time ({'buying' if r15 >= 0 else 'selling'})",
            f"last 15 min volume vs the median of the same slots over {len(sessions)} sessions · price {r15:+.2f}% in 15 min",
            "good" if r15 >= 0 else "warn")
    if np.isfinite(typ_r) and abs(ret30) >= max(typ_r * 1.5, 0.3) and np.sign(ret30) != np.sign(ret_day - ret30) \
            and abs(ret_day - ret30) >= typ_r:
        add("REVERSAL", "up" if ret30 > 0 else "down",
            f"reversing {'up' if ret30 > 0 else 'down'} ({ret30:+.2f}% in 30 min)",
            f"earlier today {ret_day - ret30:+.2f}%; a typical 30-min move is ±{typ_r:.2f}%", "warn")
    if np.isfinite(typ_g) and rng30 >= 2.0 * typ_g:
        add("RANGE_EXPANSION", "up" if ret30 >= 0 else "down", f"range expansion ({rng30:.2f}% in 30 min)",
            f"{rng30 / typ_g:.1f}x the typical 30-min range ({typ_g:.2f}%)", "warn")
    if mkt30 is not None and np.isfinite(typ_r) and abs(ret30) >= typ_r and abs(mkt30) >= 0.15 \
            and np.sign(ret30) != np.sign(mkt30):
        add("AGAINST_MARKET", "up" if ret30 > 0 else "down",
            f"moving against the market ({ret30:+.2f}% vs market {mkt30:+.2f}% in 30 min)",
            "stock-specific flow over the last 30 minutes", "info")
    state = {"at": at, "ret_day": round(ret_day, 2), "ret30": round(ret30, 2), "vol_pace": None if not np.isfinite(pace)
             else round(pace, 2), "vol15_x": None if not np.isfinite(vol15) else round(vol15, 2),
             "above_vwap": bool(above[-1]), "vwap": round(float(f["vwap"].iloc[-1]), 2), "typ30": None if not
             np.isfinite(typ_r) else round(typ_r, 2), "baseline_sessions": len(sessions)}
    tags = []
    if state["vol_pace"]:
        tags.append(f"volume pace {state['vol_pace']:.1f}x" + (" HEAVY" if state["vol_pace"] >= 1.5 else
                                                                " LIGHT" if state["vol_pace"] <= 0.7 else ""))
    tags.append("above VWAP" if state["above_vwap"] else "below VWAP")
    state["summary"] = " · ".join(tags)
    return {"state": state, "events": ev}


# ----------------------------------------------------------------------------- orchestration
def build(symbol_data: Mapping[str, tuple[pd.DataFrame, pd.DataFrame]], today: str,
          today_minutes: Mapping[str, pd.DataFrame] | None = None) -> dict[str, Any]:
    """symbol_data: symbol -> (daily[date,open,high,low,close,volume] completed sessions, minute[session,ts,ohlcv])."""
    rets = {}
    for s, (d, _m) in symbol_data.items():
        d = d.sort_values("date")
        r = (d["close"] / d["close"].shift(1) - 1) * 100
        r[r.abs() > 25] = np.nan
        rets[s] = pd.Series(r.to_numpy(), index=d["date"].astype(str))
    mkt = pd.DataFrame(rets).mean(axis=1, skipna=True)
    feats, profiles, all_tests = {}, {}, []
    for s, (d, m) in symbol_data.items():
        f = daily_features(d, mkt)
        f5 = session_minutes(m)
        mf = pd.DataFrame([{"date": k, **minute_features(v)} for k, v in f5.items()]) if f5 else \
            pd.DataFrame(columns=["date", "ac5", "open_follow", "vwap_above"])
        f = f.merge(mf, on="date", how="left")
        feats[s] = f
        ref = float(f["range"].tail(252).median()) if f["range"].notna().any() else np.nan
        prof = {k: window_profile(f, n, ref) for k, n in WINDOWS.items() if k != "D"}
        tm = (today_minutes or {}).get(s)
        if tm is not None and len(tm) >= 10 and len(d):
            t5 = session_minutes(tm.assign(session=today), min_bars=10)
            if today in t5:
                g = t5[today]
                pc = float(d.sort_values("date")["close"].iloc[-1])
                ret = (float(g["close"].iloc[-1]) / pc - 1) * 100
                rng = (float(g["high"].max()) - float(g["low"].min())) / pc * 100
                mfx = minute_features(g)
                direction = "UP" if ret / ref > 0.4 else "DOWN" if ret / ref < -0.4 else "FLAT"
                a5 = mfx["ac5"]
                char = "?" if not np.isfinite(a5) else \
                    "FOLLOW-THROUGH" if a5 > 0.05 else "CHOPPY" if a5 < -0.05 else "MIXED"
                vr = rng / ref if ref else np.nan
                vol = "HIGH-VOL" if vr > 1.3 else "LOW-VOL" if vr < 0.75 else "NORMAL-VOL"
                prof["D"] = {"n": len(g), "label": " · ".join(t for t in (direction, char, vol) if t != "?"), "direction": direction,
                             "character": char, "volatility": vol, "ret_pct": _f(ret), "range_pct": _f(rng),
                             "range_vs_year": _f(vr), "ac": _f(mfx["ac5"], 3), "vwap_above": _f(mfx["vwap_above"]),
                             "open_follow": _f(mfx["open_follow"]), "partial": True}
        profiles[s] = {k: prof[k] for k in WINDOWS if k in prof}
        for t in change_tests(f):
            all_tests.append({"symbol": s, **t})
    q = _bh([t["p"] for t in all_tests])
    for t, qq in zip(all_tests, q):
        t["q"] = round(float(qq), 4)
    # market-wide share per feature & direction (raw p < 0.05, same sign)
    share: dict[tuple[str, int], int] = {}
    for t in all_tests:
        if t["p"] < 0.05:
            key = (t["feature"], 1 if t["effect"] > 0 else -1)
            share[key] = share.get(key, 0) + 1
    nsym = max(1, len(symbol_data))
    changes = []
    for t in all_tests:
        sign = 1 if t["effect"] > 0 else -1
        frac = share.get((t["feature"], sign), 0) / nsym
        confirmed = t["q"] < 0.10 and abs(t["effect"]) >= 0.5 and t["persist"] >= 4
        emerging = not confirmed and t["p"] < 0.05 and abs(t["effect"]) >= 0.5 and t["persist"] >= 3
        if not (confirmed or emerging):
            continue
        scope = "MARKET-WIDE" if frac >= 0.5 else "STOCK-SPECIFIC"
        t.update(status="CONFIRMED" if confirmed else "EMERGING", scope=scope, market_share=round(frac, 2))
        t["text"] = (f"{t['phrase']} since {t['since']} — recent {t['recent']:g} vs usual {t['usual']:g} "
                     f"({t['unit']}); {t['persist']}/5 recent sessions; "
                     f"{'shared by most of the market' if scope == 'MARKET-WIDE' else 'specific to this stock'}"
                     f"{'' if confirmed else ' (emerging, not yet confirmed)'}")
        changes.append(t)
    changes.sort(key=lambda c: (c["status"] != "CONFIRMED", -abs(c["effect"])))
    mk = pd.DataFrame({"date": mkt.index, "close": np.cumprod(1 + mkt.fillna(0).to_numpy() / 100)})
    mkt_feat = daily_features(mk.assign(open=mk["close"], high=mk["close"], low=mk["close"], volume=np.nan),
                              mkt.rename(None))
    mkt_prof = {k: window_profile(mkt_feat.assign(range=np.nan), n, np.nan) for k, n in WINDOWS.items() if k != "D"}
    market_changes = [{"feature": f, "direction": "up" if sg > 0 else "down", "symbols": c, "share": round(c / nsym, 2),
                       "phrase": FEATURES[f][0 if sg > 0 else 1]}
                      for (f, sg), c in sorted(share.items(), key=lambda kv: -kv[1]) if c / nsym >= 0.5]
    last = {s: str(d["date"].max()) for s, (d, _m) in symbol_data.items() if len(d)}
    return {"contract": CONTRACT, "today": today, "last_completed_session": max(last.values()) if last else None,
            "windows": WINDOWS, "profiles": profiles, "changes": changes, "market": {
                "profile": {k: {kk: v.get(kk) for kk in ("label", "ret_pct", "direction", "character", "n")}
                            for k, v in mkt_prof.items()}, "changes": market_changes},
            "tests": len(all_tests), "method": __doc__}
