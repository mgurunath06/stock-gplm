"""Independent reference indicators: plain Python loops over lists, no NumPy and no pandas.

Written from the textbook definitions, not from the engine's code, so a shared bug cannot hide.
Missing values are ``nan``. Recursive smoothers take a ``seed``:

* ``"sma"``   - seed with the simple average of the first n defined inputs (Wilder / TA-Lib
  convention; the GPLM DSL convention since strategy-dsl.v2). Default.
* ``"first"`` - start the recursion at the first defined input (pandas ``ewm(adjust=False)``; the
  DSL v1 convention), kept to quantify the v1 -> v2 change.
"""
from __future__ import annotations

import math
from typing import Sequence

NAN = float("nan")
L = list[float]


def fin(v: float) -> bool:
    return v is not None and not math.isnan(v) and not math.isinf(v)


def lag(x: Sequence[float], k: int) -> L:
    return [x[i - k] if i - k >= 0 else NAN for i in range(len(x))]


def sma(x: Sequence[float], n: int) -> L:
    out = []
    for i in range(len(x)):
        w = x[i - n + 1:i + 1] if i >= n - 1 else []
        out.append(sum(w) / n if len(w) == n and all(fin(v) for v in w) else NAN)
    return out


def _smooth(x: Sequence[float], n: int, alpha: float, seed: str) -> L:
    out = [NAN] * len(x)
    first = next((i for i, v in enumerate(x) if fin(v)), None)
    if first is None:
        return out
    if seed == "sma":
        first = next((i for i in range(len(x) - n + 1) if all(fin(v) for v in x[i:i + n])), None)
        if first is None:
            return out
        prev = sum(x[first:first + n]) / n
        out[first + n - 1] = prev
        for i in range(first + n, len(x)):
            prev = alpha * x[i] + (1 - alpha) * prev
            out[i] = prev
        return out
    prev = x[first]
    for i in range(first, len(x)):
        if i > first:
            prev = alpha * x[i] + (1 - alpha) * prev
        if i >= first + n - 1:
            out[i] = prev
    return out


def ema(x: Sequence[float], n: int, seed: str = "sma") -> L:
    return _smooth(x, n, 2.0 / (n + 1), seed)


def rma(x: Sequence[float], n: int, seed: str = "sma") -> L:
    return _smooth(x, n, 1.0 / n, seed)


def wma(x: Sequence[float], n: int) -> L:
    den = n * (n + 1) / 2
    out = []
    for i in range(len(x)):
        if i < n - 1:
            out.append(NAN)
            continue
        w = x[i - n + 1:i + 1]
        out.append(sum((k + 1) * v for k, v in enumerate(w)) / den if all(fin(v) for v in w) else NAN)
    return out


def hma(x: Sequence[float], n: int) -> L:
    a, b = wma(x, max(1, n // 2)), wma(x, n)
    return wma([2 * p - q for p, q in zip(a, b)], max(1, int(round(math.sqrt(n)))))


def highest(x: Sequence[float], n: int) -> L:
    return [max(x[i - n + 1:i + 1]) if i >= n - 1 and all(fin(v) for v in x[i - n + 1:i + 1]) else NAN
            for i in range(len(x))]


def lowest(x: Sequence[float], n: int) -> L:
    return [min(x[i - n + 1:i + 1]) if i >= n - 1 and all(fin(v) for v in x[i - n + 1:i + 1]) else NAN
            for i in range(len(x))]


def rsum(x: Sequence[float], n: int) -> L:
    return [sum(x[i - n + 1:i + 1]) if i >= n - 1 and all(fin(v) for v in x[i - n + 1:i + 1]) else NAN
            for i in range(len(x))]


def _solve3(a: list[list[float]], b: list[float]) -> list[float]:
    """Small independent Gaussian-elimination solver used by the polynomial reference."""
    m = [row[:] + [rhs] for row, rhs in zip(a, b)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        div_ = m[col][col]
        for j in range(col, 4):
            m[col][j] /= div_
        for r in range(3):
            if r == col:
                continue
            factor = m[r][col]
            for j in range(col, 4):
                m[r][j] -= factor * m[col][j]
    return [m[i][3] for i in range(3)]


def quadratic(x: Sequence[float], n: int) -> tuple[L, L, L, L]:
    """Rolling OLS coefficients for a*t^2+b*t+c and the fitted value at t=n-1."""
    out = [[NAN] * len(x) for _ in range(4)]
    t = [float(i) for i in range(n)]
    design = [[u * u, u, 1.0] for u in t]
    normal = [[sum(row[i] * row[j] for row in design) for j in range(3)] for i in range(3)]
    for end in range(n - 1, len(x)):
        y = x[end - n + 1:end + 1]
        if not all(fin(v) for v in y):
            continue
        rhs = [sum(row[k] * yy for row, yy in zip(design, y)) for k in range(3)]
        aa, bb, cc = _solve3(normal, rhs)
        out[0][end], out[1][end], out[2][end] = aa, bb, cc
        out[3][end] = aa * (n - 1) ** 2 + bb * (n - 1) + cc
    return out[0], out[1], out[2], out[3]


def dtw_vcp(x: Sequence[float], n: int, band: int) -> L:
    """Independent list-loop DTW to the registered contracting-oscillation VCP template."""
    target = [0.5 * i / (n - 1) + (1 - i / (n - 1)) * math.sin(4 * math.pi * i / (n - 1))
              for i in range(n)]
    tm = sum(target) / n
    ts = math.sqrt(sum((v - tm) ** 2 for v in target) / n)
    target = [(v - tm) / ts for v in target]
    out = [NAN] * len(x)
    for end in range(n - 1, len(x)):
        w = list(x[end - n + 1:end + 1])
        if not all(fin(v) for v in w):
            continue
        mean = sum(w) / n
        std = math.sqrt(sum((v - mean) ** 2 for v in w) / n)
        if std == 0:
            continue
        w = [(v - mean) / std for v in w]
        table = [[float("inf")] * (n + 1) for _ in range(n + 1)]
        table[0][0] = 0.0
        for i in range(1, n + 1):
            for j in range(max(1, i - band), min(n, i + band) + 1):
                table[i][j] = (w[i - 1] - target[j - 1]) ** 2 + min(
                    table[i - 1][j], table[i][j - 1], table[i - 1][j - 1])
        out[end] = math.sqrt(table[n][n] / n)
    return out


def weis_wave(c: Sequence[float], v: Sequence[float], threshold: float) -> tuple[L, L, L]:
    """Reference causal percentage-ZigZag direction/current/previous-same-direction volumes."""
    direction = [0.0] * len(c)
    current = [NAN] * len(c)
    previous = [NAN] * len(c)
    if not c:
        return direction, current, previous
    d, extreme_i, wave_start = 0, 0, 0
    extreme = c[0]
    prev_up = prev_down = NAN
    for i, price in enumerate(c):
        if d == 0:
            if price >= extreme * (1 + threshold):
                d, extreme, extreme_i = 1, price, i
            elif price <= extreme * (1 - threshold):
                d, extreme, extreme_i = -1, price, i
        elif d > 0:
            if price >= extreme:
                extreme, extreme_i = price, i
            elif price <= extreme * (1 - threshold):
                prev_up = sum(x for x in v[wave_start:extreme_i + 1] if fin(x))
                d, wave_start, extreme, extreme_i = -1, extreme_i + 1, price, i
        else:
            if price <= extreme:
                extreme, extreme_i = price, i
            elif price >= extreme * (1 + threshold):
                prev_down = sum(x for x in v[wave_start:extreme_i + 1] if fin(x))
                d, wave_start, extreme, extreme_i = 1, extreme_i + 1, price, i
        direction[i] = float(d)
        current[i] = sum(x for x in v[wave_start:i + 1] if fin(x))
        previous[i] = prev_up if d > 0 else prev_down if d < 0 else NAN
    return direction, current, previous


def stdev(x: Sequence[float], n: int) -> L:
    """Population standard deviation (ddof = 0), the Bollinger convention."""
    out = []
    for i in range(len(x)):
        w = x[i - n + 1:i + 1] if i >= n - 1 else []
        if len(w) == n and all(fin(v) for v in w):
            m = sum(w) / n
            out.append(math.sqrt(sum((v - m) ** 2 for v in w) / n))
        else:
            out.append(NAN)
    return out


def div(a: float, b: float) -> float:
    if not (fin(a) and fin(b)) or b == 0:
        return NAN
    return a / b


def roc(x: Sequence[float], n: int) -> L:
    return [div(x[i], x[i - n]) - 1 if i >= n else NAN for i in range(len(x))]


def rsi(x: Sequence[float], n: int, seed: str = "sma") -> L:
    """Wilder RSI. Flat window (no gains, no losses) = 50; no losses = 100."""
    d = [NAN] + [x[i] - x[i - 1] for i in range(1, len(x))]
    up = [NAN if not fin(v) else max(v, 0.0) for v in d]
    dn = [NAN if not fin(v) else max(-v, 0.0) for v in d]
    ru, rd = rma(up, n, seed), rma(dn, n, seed)
    out = []
    for u, v in zip(ru, rd):
        if not (fin(u) and fin(v)):
            out.append(NAN)
        elif v == 0:
            out.append(50.0 if u == 0 else 100.0)
        else:
            out.append(100 - 100 / (1 + u / v))
    return out


def true_range(h: Sequence[float], lo: Sequence[float], c: Sequence[float], first_bar: bool = False) -> L:
    """TR. TA-Lib: undefined on bar 0 (no previous close); ``first_bar=True`` uses high - low there."""
    out = [h[0] - lo[0] if first_bar else NAN]
    for i in range(1, len(c)):
        out.append(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])))
    return out


def atr(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int, seed: str = "sma",
        first_bar: bool = False) -> L:
    return rma(true_range(h, lo, c, first_bar), n, seed)


def macd(x: Sequence[float], fast: int = 12, slow: int = 26, sig: int = 9, seed: str = "sma") -> tuple[L, L, L]:
    ef, es = ema(x, fast, seed), ema(x, slow, seed)
    line = [a - b for a, b in zip(ef, es)]
    signal = ema(line, sig, seed)
    return line, signal, [a - b for a, b in zip(line, signal)]


def dmi(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int, seed: str = "sma",
        first_bar: bool = False) -> tuple[L, L, L]:
    """(ADX, +DI, -DI), Wilder."""
    pdm, mdm = [NAN], [NAN]
    for i in range(1, len(h)):
        up, dn = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    a = atr(h, lo, c, n, seed, first_bar)
    sp, sm = rma(pdm, n, seed), rma(mdm, n, seed)
    pdi = [100 * div(p, t) for p, t in zip(sp, a)]
    mdi = [100 * div(m, t) for m, t in zip(sm, a)]
    dx = [100 * div(abs(p - m), p + m) for p, m in zip(pdi, mdi)]
    return rma(dx, n, seed), pdi, mdi


def stoch_k(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int, smooth: int = 3) -> L:
    hh, ll = highest(h, n), lowest(lo, n)
    raw = [100 * div(ci - l_, hi - l_) for ci, hi, l_ in zip(c, hh, ll)]
    return sma(raw, smooth)


def willr(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int) -> L:
    hh, ll = highest(h, n), lowest(lo, n)
    return [-100 * div(hi - ci, hi - l_) for ci, hi, l_ in zip(c, hh, ll)]


def cci(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int) -> L:
    tp = [(a + b + d) / 3 for a, b, d in zip(h, lo, c)]
    out = []
    for i in range(len(tp)):
        if i < n - 1:
            out.append(NAN)
            continue
        w = tp[i - n + 1:i + 1]
        m = sum(w) / n
        md = sum(abs(v - m) for v in w) / n
        out.append(div(tp[i] - m, 0.015 * md))
    return out


def mfi(h: Sequence[float], lo: Sequence[float], c: Sequence[float], v: Sequence[float], n: int) -> L:
    """Textbook MFI: n money flows, each needs a previous typical price (first value at bar n)."""
    tp = [(a + b + d) / 3 for a, b, d in zip(h, lo, c)]
    out = [NAN] * len(tp)
    for i in range(n, len(tp)):
        pos = neg = 0.0
        for j in range(i - n + 1, i + 1):
            mf = tp[j] * v[j]
            if tp[j] > tp[j - 1]:
                pos += mf
            elif tp[j] < tp[j - 1]:
                neg += mf
        out[i] = NAN if pos + neg == 0 else (100.0 if neg == 0 else 100 - 100 / (1 + pos / neg))
    return out


def obv(c: Sequence[float], v: Sequence[float]) -> L:
    out, acc = [], 0.0
    for i in range(len(c)):
        if i > 0:
            acc += v[i] if c[i] > c[i - 1] else (-v[i] if c[i] < c[i - 1] else 0.0)
        out.append(acc)
    return out


def cmf(h: Sequence[float], lo: Sequence[float], c: Sequence[float], v: Sequence[float], n: int) -> L:
    mfv = []
    for a, b, d, w in zip(h, lo, c, v):
        m = div((d - b) - (a - d), a - b)
        mfv.append((m if fin(m) else 0.0) * w)
    num, den = rsum(mfv, n), rsum(list(v), n)
    return [div(p, q) for p, q in zip(num, den)]


def vwma(x: Sequence[float], v: Sequence[float], n: int) -> L:
    num, den = rsum([a * b for a, b in zip(x, v)], n), rsum(list(v), n)
    return [div(p, q) for p, q in zip(num, den)]


def bollinger(x: Sequence[float], n: int = 20, k: float = 2.0) -> tuple[L, L, L]:
    m, s = sma(x, n), stdev(x, n)
    return [a + k * b for a, b in zip(m, s)], m, [a - k * b for a, b in zip(m, s)]


def pctb(x: Sequence[float], n: int = 20, k: float = 2.0) -> L:
    up, _, lw = bollinger(x, n, k)
    return [div(v - b, a - b) for v, a, b in zip(x, up, lw)]


def zscore(x: Sequence[float], n: int) -> L:
    return [div(v - m, s) for v, m, s in zip(x, sma(x, n), stdev(x, n))]


def ols_slope(x: Sequence[float], n: int) -> L:
    out = []
    for i in range(len(x)):
        if i < n - 1:
            out.append(NAN)
            continue
        w = x[i - n + 1:i + 1]
        tm = (n - 1) / 2
        ym = sum(w) / n
        num = sum((t - tm) * (y - ym) for t, y in enumerate(w))
        den = sum((t - tm) ** 2 for t in range(n))
        out.append(num / den)
    return out


def percentrank(x: Sequence[float], n: int) -> L:
    out = [NAN] * len(x)
    for i in range(n, len(x)):
        w = x[i - n:i]
        out[i] = sum(1 for v in w if v < x[i]) / n
    return out


def cross_above(a: Sequence[float], b: Sequence[float]) -> list[bool]:
    return [i > 0 and _gt(a[i], b[i]) and _le(a[i - 1], b[i - 1]) for i in range(len(a))]


def cross_below(a: Sequence[float], b: Sequence[float]) -> list[bool]:
    return [i > 0 and _lt(a[i], b[i]) and _ge(a[i - 1], b[i - 1]) for i in range(len(a))]


def _gt(a: float, b: float) -> bool:
    return fin(a) and fin(b) and a > b


def _lt(a: float, b: float) -> bool:
    return fin(a) and fin(b) and a < b


def _ge(a: float, b: float) -> bool:
    return fin(a) and fin(b) and a >= b


def _le(a: float, b: float) -> bool:
    return fin(a) and fin(b) and a <= b


def supertrend(h: Sequence[float], lo: Sequence[float], c: Sequence[float], n: int, mult: float,
               seed: str = "sma") -> tuple[L, L]:
    """Supertrend (line, direction +1/-1), the common TradingView definition on (high+low)/2."""
    a = atr(h, lo, c, n, seed)
    N = len(c)
    fu, fl, line, d = [NAN] * N, [NAN] * N, [NAN] * N, [NAN] * N
    for i in range(N):
        if not fin(a[i]):
            continue
        mid = (h[i] + lo[i]) / 2
        ub, lb = mid + mult * a[i], mid - mult * a[i]
        if i == 0 or not fin(fu[i - 1]):
            fu[i], fl[i], d[i] = ub, lb, 1.0
        else:
            fu[i] = ub if (ub < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = lb if (lb > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            d[i] = (-1.0 if c[i] < fl[i] else 1.0) if d[i - 1] == 1 else (1.0 if c[i] > fu[i] else -1.0)
        line[i] = fl[i] if d[i] == 1 else fu[i]
    return line, d


def bh(pvalues: Sequence[float], m: int | None = None) -> L:
    """Benjamini-Hochberg step-up q-values (textbook loop)."""
    k = len(pvalues)
    m = max(m or k, k)
    order = sorted(range(k), key=lambda i: (pvalues[i], i))
    q = [0.0] * k
    running = 1.0
    for rank in range(k, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = min(running, 1.0)
    return q


def t_one_sided_p(x: Sequence[float]) -> tuple[float, float]:
    """(t statistic, one-sided p for mean > 0) with Student-t tail via the regularised incomplete beta."""
    n = len(x)
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))
    t = m / (sd / math.sqrt(n))
    df = n - 1
    xb = df / (df + t * t)
    tail = 0.5 * _betainc(df / 2, 0.5, xb)
    return t, tail if t > 0 else 1 - tail


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b) by Lentz continued fraction (Numerical Recipes)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x > (a + 1) / (a + b + 2):
        return 1 - _betainc(b, a, 1 - x)
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    f = d
    for i in range(1, 400):
        m = i
        num = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        d = 1 + num * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + num / c if abs(1 + num / c) > tiny else tiny
        f *= d * c
        num = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1 + num * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + num / c if abs(1 + num / c) > tiny else tiny
        delta = d * c
        f *= delta
        if abs(delta - 1) < 1e-14:
            break
    return math.exp(lbeta) * f / a
