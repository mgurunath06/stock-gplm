"""M6.2 — structure, statistics and regime primitives that were tracked as NOT_IMPLEMENTED.

Every function is causal: the value at bar ``t`` uses only bars ``<= t``. Pivots are the DSL's
confirmed fractal swings (a pivot at ``p`` becomes known at ``p + k``). Model-based primitives
(HMM, kNN analogues, meta-labelling) refit only on data whose outcomes had matured by the refit bar.

Registered on import of :mod:`stock_gplm.app.strategy_dsl` (imported at its end).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .strategy_dsl import Context, DSLError, _arr, _bool, _f, _n, _swings, fn, rolling_max, rolling_min

PATTERNS = ("double_top", "double_bottom", "head_shoulders", "inverse_head_shoulders", "ascending_triangle",
            "descending_triangle", "symmetric_triangle", "box")
HARMONICS = ("gartley", "bat", "butterfly", "crab")
# Asymptotic Dickey-Fuller critical values, regression with constant (MacKinnon): 1%, 5%, 10%.
ADF_CRITICAL = {0.01: -3.43, 0.05: -2.86, 0.10: -2.57}


# --------------------------------------------------------------------------- pivots known at each bar

def _alternating(c: Context, k: int) -> list[tuple[int, int, float, int]]:
    """Alternating confirmed pivots sorted by confirmation: (pivot idx, confirm idx, price, +1 high / -1 low).
    Consecutive same-side pivots keep the more extreme one (standard zig-zag reduction)."""
    key = f"__alt{k}"
    if key in c.cache:
        return c.cache[key]
    piv = [(p, cf, px, 1) for p, cf, px in _swings(c.high, k, True)] + \
          [(p, cf, px, -1) for p, cf, px in _swings(c.low, k, False)]
    piv.sort(key=lambda x: (x[1], x[0]))
    out: list[tuple[int, int, float, int]] = []
    for q in piv:
        if out and out[-1][3] == q[3]:
            if (q[3] > 0 and q[2] >= out[-1][2]) or (q[3] < 0 and q[2] <= out[-1][2]):
                out[-1] = q
            continue
        out.append(q)
    c.cache[key] = out
    return out


def _known(c: Context, k: int) -> list[list[tuple[int, int, float, int]]]:
    """For each bar t, the list of alternating pivots confirmed by t (shared prefix lists; read-only)."""
    key = f"__known{k}"
    if key in c.cache:
        return c.cache[key]
    alt = _alternating(c, k)
    res: list[list[tuple[int, int, float, int]]] = []
    hist: list[tuple[int, int, float, int]] = []
    j = 0
    for t in range(c.n):
        changed = False
        while j < len(alt) and alt[j][1] <= t:
            q = alt[j]
            if hist and hist[-1][3] == q[3]:
                hist = hist[:-1] + [q]
            else:
                hist = hist + [q]
            j += 1
            changed = True
        res.append(hist if changed or not res else res[-1])
    c.cache[key] = res
    return res


def _close_break(c: Context, level: np.ndarray, up: bool) -> np.ndarray:
    prev = np.concatenate(([np.nan], c.close[:-1]))
    lv_prev = np.concatenate(([np.nan], level[:-1]))
    with np.errstate(invalid="ignore"):
        return (c.close > level) & (prev <= lv_prev) if up else (c.close < level) & (prev >= lv_prev)


# --------------------------------------------------------------------------- order blocks (break of structure)

def _order_blocks(c: Context, k: int, bullish: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    key = f"__ob{k}{bullish}"
    if key in c.cache:
        return c.cache[key]
    known = _known(c, k)
    hi = np.full(c.n, np.nan)
    lo = np.full(c.n, np.nan)
    bos = np.zeros(c.n, bool)
    cur_h = cur_l = math.nan
    used: set[int] = set()
    for t in range(c.n):
        piv = known[t]
        side = 1 if bullish else -1
        last = next((q for q in reversed(piv) if q[3] == side), None)
        if last is not None and last[0] not in used:
            lvl = last[2]
            broke = c.close[t] > lvl if bullish else c.close[t] < lvl
            if broke:
                used.add(last[0])
                bos[t] = True
                # last opposing candle before the impulse (between the pivot and t)
                for i in range(t, last[0] - 1, -1):
                    opposing = c.close[i] < c.open[i] if bullish else c.close[i] > c.open[i]
                    if opposing:
                        cur_h, cur_l = float(c.high[i]), float(c.low[i])
                        break
        hi[t], lo[t] = cur_h, cur_l
    c.cache[key] = (bos, hi, lo)
    return c.cache[key]


@fn("bos", 2, 2, 'break of structure: close beyond the last confirmed swing high ("up") or low ("down") bos(k, "up")')
def _bos(c: Context, k: Any, side: Any) -> np.ndarray:
    if side not in ("up", "down"):
        raise DSLError('bos side must be "up" or "down"')
    return _order_blocks(c, _n(k, lo=1, hi=50), side == "up")[0]


@fn("order_block_high", 2, 2, 'high of the last opposing candle before the latest break of structure order_block_high(k, "up")')
def _obh(c: Context, k: Any, side: Any) -> np.ndarray:
    if side not in ("up", "down"):
        raise DSLError('order block side must be "up" or "down"')
    return _order_blocks(c, _n(k, lo=1, hi=50), side == "up")[1]


@fn("order_block_low", 2, 2, 'low of the last opposing candle before the latest break of structure order_block_low(k, "up")')
def _obl(c: Context, k: Any, side: Any) -> np.ndarray:
    if side not in ("up", "down"):
        raise DSLError('order block side must be "up" or "down"')
    return _order_blocks(c, _n(k, lo=1, hi=50), side == "up")[2]


# --------------------------------------------------------------------------- VCP (volatility contraction)

def _pullbacks(piv: list[tuple[int, int, float, int]]) -> list[float]:
    """Depths of high->low pullbacks (fraction of the high), oldest first."""
    out = []
    for a, b in zip(piv, piv[1:]):
        if a[3] > 0 and b[3] < 0 and a[2] > 0:
            out.append((a[2] - b[2]) / a[2])
    return out


@fn("vcp_contractions", 1, 2, "number of consecutive shrinking swing pullbacks ending at the latest one vcp_contractions(k[, max=6])")
def _vcp_count(c: Context, k: Any, mx: Any = 6) -> np.ndarray:
    k, mx = _n(k, lo=1, hi=50), _n(mx, lo=2, hi=20)
    known = _known(c, k)
    out = np.full(c.n, np.nan)
    for t in range(c.n):
        d = _pullbacks(known[t][-2 * mx - 1:])
        if not d:
            continue
        cnt = 1
        for i in range(len(d) - 1, 0, -1):
            if d[i] < d[i - 1]:
                cnt += 1
            else:
                break
        out[t] = float(min(cnt, mx))
    return out


@fn("vcp_depth", 1, 2, "depth (fraction) of the j-th most recent swing pullback vcp_depth(k[, j=1])")
def _vcp_depth(c: Context, k: Any, j: Any = 1) -> np.ndarray:
    k, j = _n(k, lo=1, hi=50), _n(j, lo=1, hi=20)
    known = _known(c, k)
    out = np.full(c.n, np.nan)
    for t in range(c.n):
        d = _pullbacks(known[t][-2 * j - 2:])
        if len(d) >= j:
            out[t] = d[-j]
    return out


# --------------------------------------------------------------------------- trendlines and pitchfork

def _line_at(p1: tuple[int, float], p2: tuple[int, float], t: int) -> float:
    (i1, y1), (i2, y2) = p1, p2
    if i2 == i1:
        return math.nan
    return y1 + (y2 - y1) * (t - i1) / (i2 - i1)


@fn("trendline", 2, 2, 'line through the two latest confirmed swing highs ("high") or lows ("low"), extended to this bar trendline(k, "high")')
def _trendline(c: Context, k: Any, side: Any) -> np.ndarray:
    if side not in ("high", "low"):
        raise DSLError('trendline side must be "high" or "low"')
    k = _n(k, lo=1, hi=50)
    s = 1 if side == "high" else -1
    known = _known(c, k)
    out = np.full(c.n, np.nan)
    for t in range(c.n):
        pts = [q for q in known[t] if q[3] == s][-2:]
        if len(pts) == 2:
            out[t] = _line_at((pts[0][0], pts[0][2]), (pts[1][0], pts[1][2]), t)
    return out


@fn("trendline_slope", 2, 2, 'per-bar slope (fraction of price) of trendline(k, side)')
def _trendline_slope(c: Context, k: Any, side: Any) -> np.ndarray:
    tl = _trendline(c, k, side)
    prev = np.concatenate(([np.nan], tl[:-1]))
    with np.errstate(invalid="ignore", divide="ignore"):
        return (tl - prev) / c.close


@fn("pitchfork", 2, 2, 'Andrews pitchfork line ("median", "upper", "lower") from the three latest alternating pivots pitchfork(k, "median")')
def _pitchfork(c: Context, k: Any, which: Any) -> np.ndarray:
    if which not in ("median", "upper", "lower"):
        raise DSLError('pitchfork line must be "median", "upper" or "lower"')
    k = _n(k, lo=1, hi=50)
    known = _known(c, k)
    out = np.full(c.n, np.nan)
    for t in range(c.n):
        piv = known[t][-3:]
        if len(piv) < 3:
            continue
        (a, _, pa, _), (b, _, pb, _), (cc, _, pc, _) = piv
        mid = ((b + cc) / 2.0, (pb + pc) / 2.0)
        if mid[0] == a:
            continue
        slope = (mid[1] - pa) / (mid[0] - a)
        if which == "median":
            out[t] = pa + slope * (t - a)
        elif which == "upper":
            hi_i, hi_p = (b, pb) if pb >= pc else (cc, pc)
            out[t] = hi_p + slope * (t - hi_i)
        else:
            lo_i, lo_p = (b, pb) if pb < pc else (cc, pc)
            out[t] = lo_p + slope * (t - lo_i)
    return out


# --------------------------------------------------------------------------- chart-pattern geometry

def _near(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)


def _pattern_level(piv: list[tuple[int, int, float, int]], name: str, tol: float) -> tuple[float, bool] | None:
    """(breakout level, breakout is upward) when the latest pivots form ``name``; else None."""
    p = [q[2] for q in piv]
    s = [q[3] for q in piv]
    if name in ("double_top", "double_bottom") and len(piv) >= 3:
        top = name == "double_top"
        if s[-3:] == ([1, -1, 1] if top else [-1, 1, -1]) and _near(p[-3], p[-1], tol):
            return p[-2], not top  # neckline
    if name in ("head_shoulders", "inverse_head_shoulders") and len(piv) >= 5:
        top = name == "head_shoulders"
        seq = [1, -1, 1, -1, 1] if top else [-1, 1, -1, 1, -1]
        if s[-5:] == seq:
            ls, n1, hd, n2, rs = p[-5:]
            ok = (hd > max(ls, rs)) if top else (hd < min(ls, rs))
            if ok and _near(ls, rs, tol):
                return (n1 + n2) / 2.0, not top
    if name in ("ascending_triangle", "descending_triangle", "symmetric_triangle", "box") and len(piv) >= 4:
        highs = [q[2] for q in piv if q[3] > 0][-2:]
        lows = [q[2] for q in piv if q[3] < 0][-2:]
        if len(highs) < 2 or len(lows) < 2:
            return None
        flat_h, flat_l = _near(highs[0], highs[1], tol), _near(lows[0], lows[1], tol)
        if name == "ascending_triangle" and flat_h and lows[1] > lows[0] * (1 + tol):
            return max(highs), True
        if name == "descending_triangle" and flat_l and highs[1] < highs[0] * (1 - tol):
            return min(lows), False
        if name == "symmetric_triangle" and highs[1] < highs[0] * (1 - tol) and lows[1] > lows[0] * (1 + tol):
            return highs[1], True
        if name == "box" and flat_h and flat_l:
            return max(highs), True
    return None


@fn("pattern", 2, 3, 'breakout bar of a pivot-geometry chart pattern pattern("double_bottom", k[, tol=0.03]); names: '
    + ", ".join(PATTERNS))
def _pattern(c: Context, name: Any, k: Any, tol: Any = 0.03) -> np.ndarray:
    if name not in PATTERNS:
        raise DSLError(f"pattern must be one of {PATTERNS}")
    k, tol = _n(k, lo=1, hi=50), _f(tol, "tol")
    known = _known(c, k)
    out = np.zeros(c.n, bool)
    last_fire: Any = None
    for t in range(1, c.n):
        piv = known[t]
        got = _pattern_level(piv[-6:], name, tol)
        if got is None:
            continue
        level, up = got
        anchor = piv[-1][0]
        if anchor == last_fire:
            continue
        crossed = (c.close[t] > level >= c.close[t - 1]) if up else (c.close[t] < level <= c.close[t - 1])
        if crossed and t > piv[-1][1] - 1:
            out[t] = True
            last_fire = anchor
    return out


@fn("pattern_level", 2, 3, 'breakout/neckline level of the pattern currently formed by the latest pivots (NaN if none)')
def _pattern_lvl(c: Context, name: Any, k: Any, tol: Any = 0.03) -> np.ndarray:
    if name not in PATTERNS:
        raise DSLError(f"pattern must be one of {PATTERNS}")
    k, tol = _n(k, lo=1, hi=50), _f(tol, "tol")
    known = _known(c, k)
    out = np.full(c.n, np.nan)
    for t in range(c.n):
        got = _pattern_level(known[t][-6:], name, tol)
        if got is not None:
            out[t] = got[0]
    return out


# --------------------------------------------------------------------------- harmonic XABCD geometry

_HARM = {  # AB/XA, BC/AB, CD/BC, AD/XA ranges
    "gartley": ((0.568, 0.668), (0.382, 0.886), (1.13, 1.618), (0.736, 0.836)),
    "bat": ((0.382, 0.5), (0.382, 0.886), (1.618, 2.618), (0.836, 0.936)),
    "butterfly": ((0.736, 0.836), (0.382, 0.886), (1.618, 2.618), (1.227, 1.618)),
    "crab": ((0.382, 0.618), (0.382, 0.886), (2.24, 3.618), (1.568, 1.668)),
}


@fn("harmonic", 2, 3, 'harmonic XABCD completion on the latest five alternating pivots harmonic("gartley", k[, tol=0.05]); '
    '+1 bullish (D is a low), -1 bearish, 0 none; names: ' + ", ".join(HARMONICS))
def _harmonic(c: Context, name: Any, k: Any, tol: Any = 0.05) -> np.ndarray:
    if name not in HARMONICS:
        raise DSLError(f"harmonic must be one of {HARMONICS}")
    k, tol = _n(k, lo=1, hi=50), _f(tol, "tol")
    known = _known(c, k)
    out = np.zeros(c.n)
    rng = _HARM[name]
    for t in range(c.n):
        piv = known[t][-5:]
        if len(piv) < 5:
            continue
        X, A, B, C, D = (q[2] for q in piv)
        xa, ab, bc, cd = abs(A - X), abs(B - A), abs(C - B), abs(D - C)
        if min(xa, ab, bc) <= 0:
            continue
        r = (ab / xa, bc / ab, cd / bc, abs(D - A) / xa)
        if all(lo * (1 - tol) <= v <= hi * (1 + tol) for v, (lo, hi) in zip(r, rng)):
            out[t] = 1.0 if piv[-1][3] < 0 else -1.0
    return out


# --------------------------------------------------------------------------- point & figure

def _pnf(c: Context, box: float, rev: int) -> tuple[np.ndarray, np.ndarray]:
    key = f"__pnf{box}{rev}"
    if key in c.cache:
        return c.cache[key]
    direction = np.full(c.n, np.nan)
    boxes = np.full(c.n, np.nan)
    d, top, bot = 0, math.nan, math.nan
    for t in range(c.n):
        h, lo = c.high[t], c.low[t]
        if not (math.isfinite(h) and math.isfinite(lo)) or h <= 0 or lo <= 0:
            if t:
                direction[t], boxes[t] = direction[t - 1], boxes[t - 1]
            continue
        if d == 0:
            if not math.isfinite(top):
                top, bot = h, lo
            top, bot = max(top, h), min(bot, lo)
            if math.log(top / bot) >= rev * math.log(1 + box):
                d = 1 if c.close[t] >= (top + bot) / 2 else -1
        elif d == 1:
            if h > top:
                top = h
            elif lo <= top / (1 + box) ** rev:
                d, bot = -1, lo
        else:
            if lo < bot:
                bot = lo
            elif h >= bot * (1 + box) ** rev:
                d, top = 1, h
        if d:
            direction[t] = d
            boxes[t] = math.floor(math.log(top / bot) / math.log(1 + box))
    c.cache[key] = (direction, boxes)
    return c.cache[key]


@fn("pnf_direction", 0, 2, "point-and-figure column: +1 X (rising), -1 O (falling) pnf_direction([box_pct=1, reversal=3])")
def _pnf_dir(c: Context, box: Any = 1.0, rev: Any = 3) -> np.ndarray:
    return _pnf(c, _f(box, "box_pct") / 100.0, _n(rev, lo=1, hi=10))[0]


@fn("pnf_boxes", 0, 2, "boxes spanned by the current point-and-figure column pnf_boxes([box_pct=1, reversal=3])")
def _pnf_boxes(c: Context, box: Any = 1.0, rev: Any = 3) -> np.ndarray:
    return _pnf(c, _f(box, "box_pct") / 100.0, _n(rev, lo=1, hi=10))[1]


@fn("pnf_breakout", 0, 3, "point-and-figure double/triple-top breakout: the rising X column exceeds the highest of the "
    "previous `columns` X-column tops by at least one box pnf_breakout([box_pct=1, reversal=3, columns=2])")
def _pnf_breakout(c: Context, box: Any = 1.0, rev: Any = 3, cols: Any = 2) -> np.ndarray:
    b, r, k = _f(box, "box_pct") / 100.0, _n(rev, lo=1, hi=10), _n(cols, lo=1, hi=5)
    direction, _ = _pnf(c, b, r)
    out = np.zeros(c.n, bool)
    tops: list[float] = []
    col_top = -math.inf
    fired = False
    prev = np.nan
    for t in range(c.n):
        d = direction[t]
        if d == 1 and prev == -1:  # a new X column starts
            col_top, fired = -math.inf, False
        if d == -1 and prev == 1 and math.isfinite(col_top):  # an X column ended
            tops.append(col_top)
        if d == 1:
            col_top = max(col_top, float(c.high[t]))
            if not fired and len(tops) >= k and col_top >= max(tops[-k:]) * (1 + b):
                out[t], fired = True, True
        prev = d
    return out


# --------------------------------------------------------------------------- ADF test statistic

def adf_t(y: np.ndarray, lags: int = 1) -> float:
    """Augmented Dickey-Fuller t-statistic (constant, ``lags`` lagged differences) by OLS."""
    y = np.asarray(y, float)
    if not np.isfinite(y).all() or len(y) < lags + 10:
        return math.nan
    dy = np.diff(y)
    T = len(dy) - lags
    X = [np.ones(T), y[lags:-1]]
    for i in range(1, lags + 1):
        X.append(dy[lags - i:len(dy) - i])
    X = np.column_stack(X)
    Y = dy[lags:]
    try:
        beta, *_ = np.linalg.lstsq(X, Y, rcond=None)
        resid = Y - X @ beta
        s2 = float(resid @ resid) / (T - X.shape[1])
        cov = s2 * np.linalg.inv(X.T @ X)
        se = math.sqrt(cov[1, 1])
    except (np.linalg.LinAlgError, ValueError):
        return math.nan
    return float(beta[1] / se) if se > 0 else math.nan


@fn("adf_stat", 2, 3, "rolling augmented Dickey-Fuller t-statistic adf_stat(x, n[, lags=1]); compare with adf_critical")
def _adf_stat(c: Context, x: Any, n: Any, lags: Any = 1) -> np.ndarray:
    x, n, lags = _arr(c, x), _n(n, lo=30, hi=2000), _n(lags, lo=0, hi=10)
    out = np.full(c.n, np.nan)
    for t in range(n - 1, c.n):
        out[t] = adf_t(x[t - n + 1:t + 1], lags)
    return out


@fn("adf_critical", 1, 1, "asymptotic ADF critical value (constant) for a level of 0.01, 0.05 or 0.10")
def _adf_crit(c: Context, level: Any) -> np.ndarray:
    lv = _f(level, "level")
    if lv not in ADF_CRITICAL:
        raise DSLError("adf_critical level must be 0.01, 0.05 or 0.10")
    return np.full(c.n, ADF_CRITICAL[lv])


# --------------------------------------------------------------------------- 2-state Gaussian HMM (causal)

def _hmm_fit(r: np.ndarray, iters: int = 30) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Baum-Welch for a 2-state Gaussian HMM on returns. State 0 = lower mean."""
    mu = np.array([np.percentile(r, 30), np.percentile(r, 70)])
    sd = np.array([r.std() or 1e-4] * 2)
    A = np.array([[0.95, 0.05], [0.05, 0.95]])
    pi = np.array([0.5, 0.5])
    T = len(r)
    for _ in range(iters):
        B = np.exp(-0.5 * ((r[:, None] - mu) / sd) ** 2) / (sd * math.sqrt(2 * math.pi)) + 1e-300
        al = np.zeros((T, 2))
        sc = np.zeros(T)
        al[0] = pi * B[0]
        sc[0] = al[0].sum()
        al[0] /= sc[0]
        for t in range(1, T):
            al[t] = (al[t - 1] @ A) * B[t]
            sc[t] = al[t].sum()
            al[t] /= sc[t]
        be = np.ones((T, 2))
        for t in range(T - 2, -1, -1):
            be[t] = (A @ (B[t + 1] * be[t + 1])) / sc[t + 1]
        g = al * be
        g /= g.sum(axis=1, keepdims=True)
        xi = np.zeros((2, 2))
        for t in range(T - 1):
            m = al[t][:, None] * A * (B[t + 1] * be[t + 1])[None, :]
            xi += m / m.sum()
        A = xi / xi.sum(axis=1, keepdims=True)
        pi = g[0]
        w = g.sum(axis=0) + 1e-12
        mu = (g * r[:, None]).sum(axis=0) / w
        sd = np.sqrt((g * (r[:, None] - mu) ** 2).sum(axis=0) / w) + 1e-8
    order = np.argsort(mu)
    return mu[order], sd[order], A[np.ix_(order, order)], pi[order]


@fn("hmm_bull_prob", 0, 3, "filtered probability of the higher-mean state of a 2-state Gaussian HMM on log returns, "
    "refit on the trailing window every `refit` bars hmm_bull_prob([window=500, refit=100, min=200])")
def _hmm(c: Context, window: Any = 500, refit: Any = 100, mn: Any = 200) -> np.ndarray:
    window, refit, mn = _n(window, lo=50, hi=5000), _n(refit, lo=1, hi=5000), _n(mn, lo=30, hi=5000)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.diff(np.log(c.close), prepend=np.nan)
    out = np.full(c.n, np.nan)
    params = None
    alpha = None
    next_fit = 0
    for t in range(c.n):
        if t >= next_fit:
            w = r[max(0, t - window + 1):t + 1]
            w = w[np.isfinite(w)]
            if len(w) >= mn:
                params = _hmm_fit(w)
                alpha = None
            next_fit = t + refit
        if params is None or not math.isfinite(r[t]):
            continue
        mu, sd, A, pi = params
        b = np.exp(-0.5 * ((r[t] - mu) / sd) ** 2) / sd + 1e-300
        alpha = (pi if alpha is None else alpha @ A) * b
        alpha = alpha / alpha.sum()
        out[t] = float(alpha[1])
    return out


# --------------------------------------------------------------------------- kNN analogue search

@fn("knn_analogue", 1, 4, "mean forward h-bar return of the k nearest past analogues of the last m returns "
    "(only analogues whose outcome had matured) knn_analogue(h[, k=10, m=5, lookback=1000])")
def _knn(c: Context, h: Any, k: Any = 10, m: Any = 5, lookback: Any = 1000) -> np.ndarray:
    h, k, m, lb = _n(h, lo=1, hi=250), _n(k, lo=1, hi=200), _n(m, lo=2, hi=50), _n(lookback, lo=50, hi=20000)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.diff(np.log(c.close), prepend=np.nan)
        fwd = np.full(c.n, np.nan)
        fwd[:c.n - h] = np.log(c.close[h:] / c.close[:c.n - h])
    W = np.full((c.n, m), np.nan)
    for j in range(m):
        W[m - 1:, j] = r[j:c.n - m + 1 + j]
    out = np.full(c.n, np.nan)
    ok = np.isfinite(W).all(axis=1)
    for t in range(c.n):
        if not ok[t]:
            continue
        hi = t - h  # analogue i's outcome close[i+h] is known at t only if i + h <= t
        lo = max(0, hi - lb)
        if hi - lo < k:
            continue
        cand = np.arange(lo, hi + 1)
        cand = cand[ok[cand] & np.isfinite(fwd[cand])]
        if len(cand) < k:
            continue
        d = ((W[cand] - W[t]) ** 2).sum(axis=1)
        nn = cand[np.argpartition(d, k - 1)[:k]]
        out[t] = float(fwd[nn].mean())
    return out


# --------------------------------------------------------------------------- meta-labelling

@fn("meta_prob", 2, 4, "meta-labelling: probability that a primary signal's h-bar forward return is positive, from a "
    "logistic model on past signal bars whose outcomes had matured meta_prob(signal, h[, min=30, refit=50])")
def _meta(c: Context, sig: Any, h: Any, mn: Any = 30, refit: Any = 50) -> np.ndarray:
    s = _bool(c, sig)
    h, mn, refit = _n(h, lo=1, hi=250), _n(mn, lo=10, hi=5000), _n(refit, lo=1, hi=5000)
    with np.errstate(invalid="ignore", divide="ignore"):
        lr = np.diff(np.log(c.close), prepend=np.nan)
        f1 = np.log(c.close / np.concatenate((np.full(5, np.nan), c.close[:-5])))
        vol = np.array([np.nanstd(lr[max(0, i - 19):i + 1]) if i >= 19 else np.nan for i in range(c.n)])
        rng = (c.high - c.low) / c.close
        y = np.full(c.n, np.nan)
        y[:c.n - h] = (c.close[h:] > c.close[:c.n - h]).astype(float)
    X = np.column_stack([lr, f1, vol, rng])
    out = np.full(c.n, np.nan)
    w = None
    mu = sdv = None
    next_fit = 0
    idx = np.nonzero(s)[0]
    for t in idx:
        if t >= next_fit:
            past = idx[(idx + h <= t)]
            past = past[np.isfinite(X[past]).all(axis=1) & np.isfinite(y[past])]
            if len(past) >= mn and 0 < y[past].mean() < 1:
                Z = X[past]
                mu, sdv = Z.mean(axis=0), Z.std(axis=0) + 1e-12
                Z = np.column_stack([np.ones(len(past)), (Z - mu) / sdv])
                w = np.zeros(Z.shape[1])
                for _ in range(200):
                    p = 1 / (1 + np.exp(-Z @ w))
                    w -= 0.5 * (Z.T @ (p - y[past]) / len(past) + 1e-3 * np.r_[0, w[1:]])
            next_fit = t + refit
        if w is None or not np.isfinite(X[t]).all():
            continue
        z = np.r_[1.0, (X[t] - mu) / sdv]
        out[t] = float(1 / (1 + np.exp(-z @ w)))
    return out


__all__ = ["ADF_CRITICAL", "HARMONICS", "PATTERNS", "adf_t"]
