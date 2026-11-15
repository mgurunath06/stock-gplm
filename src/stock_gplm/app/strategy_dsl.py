"""GPLM strategy expression language (``stock-gplm.strategy-dsl.v1``).

A small, safe, declarative language for entry/exit rules. Text is tokenised and parsed by a
hand-written recursive-descent parser into a tuple AST; nothing is ever passed to ``eval``. Every
primitive is **causal**: the value at bar ``t`` uses only bars ``<= t`` of the lane being evaluated
(benchmark series are joined as-of their availability time).

Grammar (case-insensitive identifiers)::

    expr    := or
    or      := and ('or' and)*
    and     := not ('and' not)*
    not     := 'not' not | cmp
    cmp     := sum (CMP sum)*            # a > b > c  ==  (a > b) and (b > c)
    sum     := prod (('+'|'-') prod)*
    prod    := unary (('*'|'/') unary)*
    unary   := '-' unary | postfix
    postfix := primary ('[' expr ']')*   # x[n] = value n bars ago
    primary := NUMBER | STRING | '$'NAME | NAME '(' args ')' | NAME | '(' expr ')'

Series names: ``open high low close volume`` (plus ``hl2 hlc3 ohlc4 range body``). ``$name`` is a
strategy parameter substituted before evaluation. Function list: :data:`FUNCTIONS`.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

DSL_CONTRACT = "stock-gplm.strategy-dsl.v2"  # v2: TA-Lib/Wilder SMA-seeded smoothing


class DSLError(ValueError):
    """Invalid expression (syntax, unknown function, wrong arity or unsupported context)."""


# --------------------------------------------------------------------------- tokenizer / parser

_TOKEN = re.compile(r"\s*(?:(?P<num>\d+\.\d*|\.\d+|\d+)|(?P<str>\"[^\"]*\"|'[^']*')|(?P<param>\$[A-Za-z_][A-Za-z0-9_]*)"
                    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>>=|<=|==|!=|[-+*/()\[\],<>]))")
_CMP = (">", "<", ">=", "<=", "==", "!=")
_MAX_LEN = 4000
_MAX_DEPTH = 60


def tokenize(text: str) -> list[tuple[str, str]]:
    if len(text) > _MAX_LEN:
        raise DSLError(f"expression longer than {_MAX_LEN} characters")
    pos, out = 0, []
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise DSLError(f"unexpected character at {pos}: {text[pos:pos + 12]!r}")
        kind = m.lastgroup or ""
        val = m.group(kind)
        if kind == "name":
            low = val.lower()
            if low in ("and", "or", "not", "true", "false"):
                kind, val = "kw", low
            else:
                val = low
        out.append((kind, val))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0
        self.depth = 0

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, val: str | None = None) -> tuple[str, str]:
        t = self.peek()
        if t is None or (val is not None and t[1] != val):
            raise DSLError(f"expected {val or 'token'} at token {self.i}, got {t}")
        self.i += 1
        return t

    def parse(self) -> tuple:
        node = self.expr()
        if self.peek() is not None:
            raise DSLError(f"unexpected trailing token {self.peek()}")
        return node

    def expr(self) -> tuple:
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise DSLError("expression nested too deeply")
        try:
            return self.or_()
        finally:
            self.depth -= 1

    def or_(self) -> tuple:
        n = self.and_()
        while self.peek() == ("kw", "or"):
            self.take()
            n = ("or", n, self.and_())
        return n

    def and_(self) -> tuple:
        n = self.not_()
        while self.peek() == ("kw", "and"):
            self.take()
            n = ("and", n, self.not_())
        return n

    def not_(self) -> tuple:
        if self.peek() == ("kw", "not"):
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self) -> tuple:
        terms = [self.sum_()]
        ops = []
        while self.peek() is not None and self.peek()[0] == "op" and self.peek()[1] in _CMP:
            ops.append(self.take()[1])
            terms.append(self.sum_())
        if not ops:
            return terms[0]
        node = ("cmp", ops[0], terms[0], terms[1])
        for k in range(1, len(ops)):
            node = ("and", node, ("cmp", ops[k], terms[k], terms[k + 1]))
        return node

    def sum_(self) -> tuple:
        n = self.prod()
        while self.peek() is not None and self.peek()[1] in ("+", "-") and self.peek()[0] == "op":
            op = self.take()[1]
            n = ("bin", op, n, self.prod())
        return n

    def prod(self) -> tuple:
        n = self.unary()
        while self.peek() is not None and self.peek()[1] in ("*", "/") and self.peek()[0] == "op":
            op = self.take()[1]
            n = ("bin", op, n, self.unary())
        return n

    def unary(self) -> tuple:
        if self.peek() == ("op", "-"):
            self.take()
            inner = self.unary()
            return ("num", -inner[1]) if inner[0] == "num" else ("neg", inner)
        return self.postfix()

    def postfix(self) -> tuple:
        n = self.primary()
        while self.peek() == ("op", "["):
            self.take()
            k = self.expr()
            self.take("]")
            n = ("lag", n, k)
        return n

    def primary(self) -> tuple:
        t = self.peek()
        if t is None:
            raise DSLError("unexpected end of expression")
        kind, val = t
        if kind == "num":
            self.take()
            return ("num", float(val))
        if kind == "str":
            self.take()
            return ("str", val[1:-1])
        if kind == "param":
            self.take()
            return ("param", val[1:].lower())
        if kind == "kw" and val in ("true", "false"):
            self.take()
            return ("num", 1.0 if val == "true" else 0.0)
        if kind == "name":
            self.take()
            if self.peek() == ("op", "("):
                self.take()
                args: list[tuple] = []
                if self.peek() != ("op", ")"):
                    args.append(self.expr())
                    while self.peek() == ("op", ","):
                        self.take()
                        args.append(self.expr())
                self.take(")")
                return ("call", val, tuple(args))
            if val not in SERIES and val in FUNCTIONS and FUNCTIONS[val][0] == 0:
                return ("call", val, ())  # zero-argument function used as a bare name
            return ("var", val)
        if t == ("op", "("):
            self.take()
            n = self.expr()
            self.take(")")
            return n
        raise DSLError(f"unexpected token {t}")


def parse(text: str) -> tuple:
    """Parse an expression into an AST, checking functions, arity and series names."""
    if not isinstance(text, str) or not text.strip():
        raise DSLError("empty expression")
    node = _Parser(text).parse()
    check(node)
    return node


def unparse(node: tuple) -> str:
    """Canonical text form (used for caching, hashing and deduplication)."""
    k = node[0]
    if k == "num":
        v = node[1]
        return repr(int(v)) if float(v).is_integer() and abs(v) < 1e15 else repr(round(v, 10))
    if k == "str":
        return '"' + node[1] + '"'
    if k == "param":
        return "$" + node[1]
    if k == "var":
        return node[1]
    if k == "call":
        return f"{node[1]}({', '.join(unparse(a) for a in node[2])})"
    if k == "lag":
        return f"{unparse(node[1])}[{unparse(node[2])}]"
    if k == "neg":
        return f"-({unparse(node[1])})"
    if k == "not":
        return f"not ({unparse(node[1])})"
    if k in ("and", "or"):
        return f"({unparse(node[1])} {k} {unparse(node[2])})"
    if k in ("cmp", "bin"):
        return f"({unparse(node[2])} {node[1]} {unparse(node[3])})"
    raise DSLError(f"bad node {k}")


def params_used(node: tuple) -> set[str]:
    out: set[str] = set()

    def walk(n: tuple) -> None:
        if n[0] == "param":
            out.add(n[1])
        for c in n[1:]:
            if isinstance(c, tuple) and c and isinstance(c[0], str):
                walk(c)
            elif isinstance(c, tuple):
                for a in c:
                    if isinstance(a, tuple):
                        walk(a)
    walk(node)
    return out


def functions_used(node: tuple) -> set[str]:
    out: set[str] = set()

    def walk(n: Any) -> None:
        if not isinstance(n, tuple) or not n:
            return
        if n[0] == "call":
            out.add(n[1])
            for a in n[2]:
                walk(a)
            return
        for c in n[1:]:
            walk(c)
    walk(node)
    return out


def substitute(node: tuple, params: Mapping[str, Any]) -> tuple:
    k = node[0]
    if k == "param":
        if node[1] not in params:
            raise DSLError(f"parameter ${node[1]} has no value")
        v = params[node[1]]
        return ("str", v) if isinstance(v, str) else ("num", float(v))
    if k in ("num", "str", "var"):
        return node
    if k == "call":
        return ("call", node[1], tuple(substitute(a, params) for a in node[2]))
    if k in ("cmp", "bin"):
        return (k, node[1], substitute(node[2], params), substitute(node[3], params))
    return (k, *(substitute(c, params) for c in node[1:]))


def node_size(node: tuple) -> int:
    k = node[0]
    if k in ("num", "str", "var", "param"):
        return 1
    if k == "call":
        return 1 + sum(node_size(a) for a in node[2])
    if k in ("cmp", "bin"):
        return 1 + node_size(node[2]) + node_size(node[3])
    return 1 + sum(node_size(c) for c in node[1:])


# --------------------------------------------------------------------------- evaluation context


@dataclass
class Context:
    """Arrays for one lane. ``benchmarks`` maps a symbol to a Context aligned lazily on use."""

    timeframe: str
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    session: np.ndarray  # ISO date string per bar
    end_ns: np.ndarray
    intraday: bool = False
    minute_of_session: np.ndarray | None = None
    vwap_pv: np.ndarray | None = None
    features: Mapping[str, np.ndarray] = field(default_factory=dict)
    benchmark_loader: Callable[[str, str], "Context | None"] | None = None
    htf_loader: Callable[[str], "Context | None"] | None = None
    default_benchmark: str = "NIFTYBEES-EQ"
    avail_ns: np.ndarray | None = None
    cache: dict[str, Any] = field(default_factory=dict)
    _bench: dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.close)

    @classmethod
    def from_frame(cls, frame: pd.DataFrame, timeframe: str, intraday: bool, **kw: Any) -> "Context":
        f = frame.reset_index(drop=True)
        g = lambda c: f[c].to_numpy(dtype=float)  # noqa: E731
        return cls(timeframe, g("open"), g("high"), g("low"), g("close"), g("volume"),
                   f["session"].astype(str).to_numpy(), f["end_ns"].to_numpy(dtype=np.int64), intraday,
                   f["minute_of_session"].to_numpy(dtype=float) if "minute_of_session" in f else None,
                   f["vwap_pv"].to_numpy(dtype=float) if "vwap_pv" in f else None,
                   avail_ns=f["avail_ns"].to_numpy(dtype=np.int64) if "avail_ns" in f else None, **kw)


# --------------------------------------------------------------------------- numeric helpers

def _S(x: np.ndarray) -> pd.Series:
    return pd.Series(x, dtype=float)


def _n(v: Any, name: str = "period", lo: int = 1, hi: int = 5000) -> int:
    if isinstance(v, np.ndarray):
        raise DSLError(f"{name} must be a constant number")
    iv = int(round(float(v)))
    if not lo <= iv <= hi:
        raise DSLError(f"{name}={v} outside [{lo}, {hi}]")
    return iv


def _f(v: Any, name: str = "value") -> float:
    if isinstance(v, np.ndarray):
        raise DSLError(f"{name} must be a constant number")
    return float(v)


def _arr(ctx: Context, v: Any) -> np.ndarray:
    if isinstance(v, np.ndarray):
        return v.astype(float) if v.dtype == bool else v
    if isinstance(v, str):
        raise DSLError("string used where a series is required")
    return np.full(ctx.n, float(v))


def _bool(ctx: Context, v: Any) -> np.ndarray:
    if isinstance(v, np.ndarray):
        if v.dtype == bool:
            return v
        with np.errstate(invalid="ignore"):
            return np.nan_to_num(v, nan=0.0) != 0
    return np.full(ctx.n, bool(v))


def shift(x: np.ndarray, k: int) -> np.ndarray:
    if k == 0:
        return x
    if x.dtype == bool:
        out = np.zeros_like(x)
    else:
        out = np.full(len(x), np.nan)
    if k > 0:
        if k < len(x):
            out[k:] = x[:-k]
    else:
        raise DSLError("negative lag would read the future")
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    return _S(x).rolling(n, min_periods=n).mean().to_numpy()


# Initialisation convention (``stock-gplm.strategy-dsl.v2``, 2026-11-15): TA-Lib / Wilder.
# Exponential smoothers (EMA alpha = 2/(n+1); Wilder RMA alpha = 1/n) are seeded with the simple average of
# the first n defined inputs and report from that bar on. True range is undefined on the first bar (no
# previous close), so ATR(n) first reports at bar n. This matches TA-Lib's SMA, EMA, RSI and ATR and the
# M5.4 feature path (``m54.indicators._seeded_recursive``). Known residual differences from TA-Lib:
# its MACD aligns the fast EMA's seed with the slow one, and its ADX seeds Wilder sums with n-1 values;
# both differ only during warm-up. v1 seeded at the first bar (pandas ``ewm(adjust=False)``).
def _seeded(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    # seed at the end of the first run of n consecutive defined inputs (leading/sporadic NaNs are skipped)
    run = np.convolve(np.isfinite(x).astype(np.int64), np.ones(n, dtype=np.int64), "valid")
    full = np.nonzero(run == n)[0]
    if not full.size:
        return out
    first = int(full[0])
    seed_end = first + n - 1
    tail = x[seed_end:].copy()
    tail[0] = x[first:seed_end + 1].mean()
    vals = pd.Series(tail).ewm(alpha=alpha, adjust=False).mean().to_numpy()
    vals[np.isnan(tail)] = np.nan  # a missing input stays missing; it is never filled
    out[seed_end:] = vals
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return _seeded(x, n, 2.0 / (n + 1))


def rma(x: np.ndarray, n: int) -> np.ndarray:
    return _seeded(x, n, 1.0 / n)


def _windows(x: np.ndarray, n: int) -> np.ndarray:
    return np.lib.stride_tricks.sliding_window_view(x, n)


def wma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.arange(1, n + 1, dtype=float)
        out[n - 1:] = _windows(x, n) @ w / w.sum()
    return out


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    return _S(x).rolling(n, min_periods=n).max().to_numpy()


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    return _S(x).rolling(n, min_periods=n).min().to_numpy()


def rolling_sum(x: np.ndarray, n: int) -> np.ndarray:
    return _S(x).rolling(n, min_periods=n).sum().to_numpy()


def stdev(x: np.ndarray, n: int) -> np.ndarray:
    return _S(x).rolling(n, min_periods=n).std(ddof=0).to_numpy()


def _div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.asarray(a, dtype=float) / np.asarray(b, dtype=float)
    out[~np.isfinite(out)] = np.nan
    return out


def true_range(h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> np.ndarray:
    pc = shift(c, 1)
    tr = np.fmax(h - lo, np.fmax(np.abs(h - pc), np.abs(lo - pc)))
    if len(tr):
        tr[0] = np.nan  # TA-Lib: no true range without a previous close
    return tr


def _rsi_safe(x: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(x, prepend=np.nan)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    up[np.isnan(d)], dn[np.isnan(d)] = np.nan, np.nan
    ru, rd = rma(up, n), rma(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(rd == 0, np.where(ru == 0, 50.0, 100.0), 100 - 100 / (1 + ru / rd))
    out[np.isnan(ru) | np.isnan(rd)] = np.nan
    return out


def _dmi(ctx: Context, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    key = f"__dmi{n}"
    if key in ctx.cache:
        return ctx.cache[key]
    h, lo, c = ctx.high, ctx.low, ctx.close
    upm = h - shift(h, 1)
    dnm = shift(lo, 1) - lo
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    mdm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    pdm[0] = mdm[0] = np.nan
    atr = rma(true_range(h, lo, c), n)
    pdi = 100 * _div(rma(pdm, n), atr)
    mdi = 100 * _div(rma(mdm, n), atr)
    dx = 100 * _div(np.abs(pdi - mdi), pdi + mdi)
    adx = rma(dx, n)
    ctx.cache[key] = (adx, pdi, mdi)
    return adx, pdi, mdi


def _supertrend(ctx: Context, n: int, mult: float) -> tuple[np.ndarray, np.ndarray]:
    key = f"__st{n}_{mult}"
    if key in ctx.cache:
        return ctx.cache[key]
    h, lo, c = ctx.high, ctx.low, ctx.close
    atr = rma(true_range(h, lo, c), n)
    mid = (h + lo) / 2
    ub, lb = mid + mult * atr, mid - mult * atr
    N = len(c)
    fu, fl = np.full(N, np.nan), np.full(N, np.nan)
    line, dirn = np.full(N, np.nan), np.full(N, np.nan)
    for i in range(N):
        if not np.isfinite(atr[i]):
            continue
        if i == 0 or not np.isfinite(fu[i - 1]):
            fu[i], fl[i], dirn[i] = ub[i], lb[i], 1.0
        else:
            fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            if dirn[i - 1] == 1:
                dirn[i] = -1.0 if c[i] < fl[i] else 1.0
            else:
                dirn[i] = 1.0 if c[i] > fu[i] else -1.0
        line[i] = fl[i] if dirn[i] == 1 else fu[i]
    ctx.cache[key] = (line, dirn)
    return line, dirn


def _psar(ctx: Context, step: float, mx: float) -> np.ndarray:
    h, lo = ctx.high, ctx.low
    N = len(h)
    out = np.full(N, np.nan)
    if N < 3:
        return out
    up, af, ep, sar = True, step, h[0], lo[0]
    for i in range(1, N):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, lo[i - 1], lo[i - 2] if i > 1 else lo[i - 1])
            if lo[i] < sar:
                up, sar, ep, af = False, ep, lo[i], step
            elif h[i] > ep:
                ep, af = h[i], min(mx, af + step)
        else:
            sar = max(sar, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], step
            elif lo[i] < ep:
                ep, af = lo[i], min(mx, af + step)
        out[i] = sar
    return out


def _loop_kama(x: np.ndarray, n: int, fast: int, slow: int) -> np.ndarray:
    N = len(x)
    out = np.full(N, np.nan)
    fsc, ssc = 2 / (fast + 1), 2 / (slow + 1)
    vol = np.abs(np.diff(x, prepend=np.nan))
    for i in range(n, N):
        ch = abs(x[i] - x[i - n])
        v = np.nansum(vol[i - n + 1:i + 1])
        er = ch / v if v > 0 else 0.0
        sc = (er * (fsc - ssc) + ssc) ** 2
        prev = out[i - 1] if np.isfinite(out[i - 1]) else x[i - 1]
        out[i] = prev + sc * (x[i] - prev)
    return out


def _mcginley(x: np.ndarray, n: int) -> np.ndarray:
    N = len(x)
    out = np.full(N, np.nan)
    if N < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    for i in range(n, N):
        p = out[i - 1]
        if not (np.isfinite(p) and p > 0 and np.isfinite(x[i])):
            out[i] = x[i]
            continue
        r = x[i] / p
        out[i] = p + (x[i] - p) / max(1e-9, 0.6 * n * r ** 4)
    return out


def _linreg(x: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    N = len(x)
    val, slope, r2, se = (np.full(N, np.nan) for _ in range(4))
    if N < n or n < 3:
        return val, slope, r2, se
    W = _windows(x, n)
    t = np.arange(n, dtype=float)
    tc = t - t.mean()
    mx = W.mean(axis=1)
    b = (W @ tc) / (tc @ tc)
    a = mx - b * t.mean()
    xc = W - mx[:, None]
    sxx = (xc ** 2).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rr = (b ** 2) * (tc @ tc) / sxx
        resid = np.maximum(sxx - (b ** 2) * (tc @ tc), 0) / (n - 2)
    val[n - 1:] = a + b * (n - 1)
    slope[n - 1:] = b
    r2[n - 1:] = rr
    se[n - 1:] = np.sqrt(resid)
    return val, slope, r2, se


def _swings(h: np.ndarray, k: int, high: bool) -> list[tuple[int, int, float]]:
    """Confirmed fractal pivots: (pivot index, confirmation index, price)."""
    N = len(h)
    out = []
    W = 2 * k + 1
    if N < W:
        return out
    roll = rolling_max(h, W) if high else rolling_min(h, W)
    for c in range(W - 1, N):
        p = c - k
        if np.isfinite(h[p]) and h[p] == roll[c]:
            # first occurrence inside window only (avoid double pivots on flat tops)
            seg = h[c - W + 1:c + 1]
            if int(np.argmax(seg) if high else np.argmin(seg)) == k:
                out.append((p, c, float(h[p])))
    return out


def _swing_series(ctx: Context, k: int, j: int, high: bool, what: str = "price") -> np.ndarray:
    key = f"__sw{high}{k}"
    if key not in ctx.cache:
        ctx.cache[key] = _swings(ctx.high if high else ctx.low, k, high)
    piv = ctx.cache[key]
    N = ctx.n
    out = np.full(N, np.nan)
    hist: list[tuple[int, float]] = []
    pi = 0
    for t in range(N):
        while pi < len(piv) and piv[pi][1] <= t:
            hist.append((piv[pi][0], piv[pi][2]))
            pi += 1
        if len(hist) >= j:
            p_idx, price = hist[-j]
            out[t] = price if what == "price" else float(t - p_idx)
    return out


def _session_groups(ctx: Context) -> tuple[np.ndarray, np.ndarray]:
    key = "__sess"
    if key not in ctx.cache:
        s = ctx.session
        change = np.concatenate(([True], s[1:] != s[:-1]))
        gid = np.cumsum(change) - 1
        ctx.cache[key] = (gid, change)
    return ctx.cache[key]


def _session_cum(ctx: Context, x: np.ndarray, how: str) -> np.ndarray:
    gid, _ = _session_groups(ctx)
    s = pd.Series(x).groupby(gid)
    return {"max": s.cummax, "min": s.cummin, "sum": s.cumsum, "first": lambda: s.transform("first")}[how]().to_numpy(
        dtype=float)


def _prev_session(ctx: Context, what: str, k: int = 1) -> np.ndarray:
    """k-th previous session's high/low/close/open, known at every bar of the current session."""
    if not ctx.intraday:
        return shift({"high": ctx.high, "low": ctx.low, "close": ctx.close, "open": ctx.open}[what], k)
    gid, _ = _session_groups(ctx)
    df = pd.DataFrame({"g": gid, "h": ctx.high, "l": ctx.low, "c": ctx.close, "o": ctx.open})
    agg = df.groupby("g").agg(h=("h", "max"), l=("l", "min"), c=("c", "last"), o=("o", "first"))
    col = {"high": "h", "low": "l", "close": "c", "open": "o"}[what]
    prev = agg[col].shift(k).to_numpy()
    return prev[gid]


def _need_intraday(ctx: Context, fn: str) -> None:
    if not ctx.intraday:
        raise DSLError(f"{fn}() needs an intraday lane")


def _align_bench(ctx: Context, sym: str) -> Context | None:
    if sym in ctx._bench:
        return ctx._bench[sym]
    b = ctx.benchmark_loader(sym, ctx.timeframe) if ctx.benchmark_loader else None
    ctx._bench[sym] = b
    return b


# --------------------------------------------------------------------------- function registry

FUNCTIONS: dict[str, tuple[int, int, str]] = {}  # name -> (min args, max args, doc)
_IMPL: dict[str, Callable[..., Any]] = {}
LAZY = {"bench", "ref_symbol", "htf"}
INTRADAY_ONLY = {"vwap", "vwap_std", "or_high", "or_low", "minute_of_session", "session_high", "session_low", "slot_rvol",
                 "window_high", "window_low",
                 "session_open", "bar_of_session"}
BENCHMARK_FUNCS = {"bench", "ref_symbol"}
HTF_FUNCS = {"htf"}
FEATURE_FUNCS = {"feature"}


def fn(name: str, lo: int, hi: int, doc: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(f: Callable[..., Any]) -> Callable[..., Any]:
        FUNCTIONS[name] = (lo, hi, doc)
        _IMPL[name] = f
        return f
    return deco


# moving averages / statistics
fn("sma", 2, 2, "simple moving average sma(x, n)")(lambda c, x, n: sma(_arr(c, x), _n(n)))
fn("ema", 2, 2, "exponential moving average ema(x, n)")(lambda c, x, n: ema(_arr(c, x), _n(n)))
fn("rma", 2, 2, "Wilder smoothing rma(x, n)")(lambda c, x, n: rma(_arr(c, x), _n(n)))
fn("wma", 2, 2, "linear-weighted moving average wma(x, n)")(lambda c, x, n: wma(_arr(c, x), _n(n)))


@fn("hma", 2, 2, "Hull moving average hma(x, n)")
def _hma(c: Context, x: Any, n: Any) -> np.ndarray:
    x, n = _arr(c, x), _n(n, lo=2)
    return wma(2 * wma(x, max(1, n // 2)) - wma(x, n), max(1, int(round(math.sqrt(n)))))


@fn("dema", 2, 2, "double EMA")
def _dema(c: Context, x: Any, n: Any) -> np.ndarray:
    e = ema(_arr(c, x), _n(n))
    return 2 * e - ema(e, _n(n))


@fn("tema", 2, 2, "triple EMA")
def _tema(c: Context, x: Any, n: Any) -> np.ndarray:
    e1 = ema(_arr(c, x), _n(n))
    e2 = ema(e1, _n(n))
    return 3 * e1 - 3 * e2 + ema(e2, _n(n))


fn("kama", 2, 4, "Kaufman adaptive MA kama(x, n[, fast=2, slow=30])")(
    lambda c, x, n, f=2, s=30: _loop_kama(_arr(c, x), _n(n), _n(f), _n(s)))
fn("mcginley", 2, 2, "McGinley dynamic mcginley(x, n)")(lambda c, x, n: _mcginley(_arr(c, x), _n(n)))
fn("vwma", 2, 2, "volume-weighted MA vwma(x, n)")(
    lambda c, x, n: _div(rolling_sum(_arr(c, x) * c.volume, _n(n)), rolling_sum(c.volume, _n(n))))
fn("rvwap", 1, 1, "rolling n-bar VWAP of typical price")(
    lambda c, n: _div(rolling_sum((c.high + c.low + c.close) / 3 * c.volume, _n(n)), rolling_sum(c.volume, _n(n))))
fn("highest", 2, 2, "rolling maximum over n bars including the current bar")(
    lambda c, x, n: rolling_max(_arr(c, x), _n(n)))
fn("lowest", 2, 2, "rolling minimum over n bars including the current bar")(
    lambda c, x, n: rolling_min(_arr(c, x), _n(n)))
fn("sum", 2, 2, "rolling sum")(lambda c, x, n: rolling_sum(_arr(c, x), _n(n)))
fn("stdev", 2, 2, "rolling population standard deviation")(lambda c, x, n: stdev(_arr(c, x), _n(n, lo=2)))
fn("zscore", 2, 2, "(x - sma(x,n)) / stdev(x,n)")(
    lambda c, x, n: _div(_arr(c, x) - sma(_arr(c, x), _n(n)), stdev(_arr(c, x), _n(n, lo=2))))
fn("roc", 2, 2, "rate of change as a fraction: x / x[n] - 1")(
    lambda c, x, n: _div(_arr(c, x), shift(_arr(c, x), _n(n))) - 1)
fn("change", 2, 2, "x - x[n]")(lambda c, x, n: _arr(c, x) - shift(_arr(c, x), _n(n)))
fn("abs", 1, 1, "absolute value")(lambda c, x: np.abs(_arr(c, x)))
fn("log", 1, 1, "natural log")(lambda c, x: np.log(np.where(_arr(c, x) > 0, _arr(c, x), np.nan)))
fn("sqrt", 1, 1, "square root")(lambda c, x: np.sqrt(np.where(_arr(c, x) >= 0, _arr(c, x), np.nan)))
fn("sign", 1, 1, "sign")(lambda c, x: np.sign(_arr(c, x)))
fn("max", 2, 8, "element-wise maximum of 2..8 series")(lambda c, *xs: np.fmax.reduce([_arr(c, x) for x in xs]))
fn("min", 2, 8, "element-wise minimum of 2..8 series")(lambda c, *xs: np.fmin.reduce([_arr(c, x) for x in xs]))
fn("cummax", 1, 1, "running maximum since the first bar in the data (an all-time high is bounded by data start)")(
    lambda c, x: np.fmax.accumulate(np.nan_to_num(_arr(c, x), nan=-np.inf)))
fn("cummin", 1, 1, "running minimum since the first bar in the data")(
    lambda c, x: np.fmin.accumulate(np.nan_to_num(_arr(c, x), nan=np.inf)))
fn("floor_to", 2, 2, "largest multiple of step <= x (round-number levels)")(
    lambda c, x, step: np.floor(_arr(c, x) / _f(step)) * _f(step))
fn("nz", 1, 2, "replace NaN with value (default 0)")(lambda c, x, v=0.0: np.nan_to_num(_arr(c, x), nan=_f(v)))
fn("iff", 3, 3, "iff(cond, a, b)")(lambda c, k, a, b: np.where(_bool(c, k), _arr(c, a), _arr(c, b)))
fn("between", 3, 3, "lo <= x <= hi")(
    lambda c, x, lo, hi: (_arr(c, x) >= _arr(c, lo)) & (_arr(c, x) <= _arr(c, hi)))


@fn("linreg", 2, 2, "rolling linear-regression fitted value at the current bar")
def _lr(c: Context, x: Any, n: Any) -> np.ndarray:
    return _linreg(_arr(c, x), _n(n, lo=3))[0]


@fn("slope", 2, 2, "rolling linear-regression slope per bar")
def _slope(c: Context, x: Any, n: Any) -> np.ndarray:
    return _linreg(_arr(c, x), _n(n, lo=3))[1]


@fn("r2", 2, 2, "rolling linear-regression R squared")
def _r2(c: Context, x: Any, n: Any) -> np.ndarray:
    return _linreg(_arr(c, x), _n(n, lo=3))[2]


@fn("stderr", 2, 2, "rolling linear-regression standard error")
def _se(c: Context, x: Any, n: Any) -> np.ndarray:
    return _linreg(_arr(c, x), _n(n, lo=3))[3]


fn("skew", 2, 2, "rolling skewness")(lambda c, x, n: _S(_arr(c, x)).rolling(_n(n, lo=3)).skew().to_numpy())
fn("correl", 3, 3, "rolling correlation")(
    lambda c, a, b, n: _S(_arr(c, a)).rolling(_n(n, lo=3)).corr(_S(_arr(c, b))).to_numpy())
fn("beta", 3, 3, "rolling beta of a on b")(
    lambda c, a, b, n: _div(_S(_arr(c, a)).rolling(_n(n, lo=3)).cov(_S(_arr(c, b))).to_numpy(),
                            _S(_arr(c, b)).rolling(_n(n, lo=3)).var().to_numpy()))
fn("autocorr", 2, 3, "rolling autocorrelation autocorr(x, n[, lag=1])")(
    lambda c, x, n, lag=1: _S(_arr(c, x)).rolling(_n(n, lo=3)).corr(_S(shift(_arr(c, x), _n(lag)))).to_numpy())


@fn("percentrank", 2, 2, "fraction (0..1) of the previous n values below the current value")
def _prank(c: Context, x: Any, n: Any) -> np.ndarray:
    x, n = _arr(c, x), _n(n, lo=2)
    out = np.full(len(x), np.nan)
    if len(x) > n:
        W = _windows(x, n + 1)
        with np.errstate(invalid="ignore"):
            out[n:] = (W[:, :-1] < W[:, -1:]).mean(axis=1)
        out[n:][np.isnan(W).any(axis=1)] = np.nan
    return out


# conditions over time
fn("cross_above", 2, 2, "a crosses above b on this bar")(
    lambda c, a, b: (_arr(c, a) > _arr(c, b)) & (shift(_arr(c, a), 1) <= shift(_arr(c, b), 1)))
fn("cross_below", 2, 2, "a crosses below b on this bar")(
    lambda c, a, b: (_arr(c, a) < _arr(c, b)) & (shift(_arr(c, a), 1) >= shift(_arr(c, b), 1)))
fn("count", 2, 2, "number of bars in the last n where cond is true")(
    lambda c, k, n: rolling_sum(_bool(c, k).astype(float), _n(n)))
fn("all_of", 2, 2, "cond true on each of the last n bars")(
    lambda c, k, n: rolling_sum(_bool(c, k).astype(float), _n(n)) >= _n(n))
fn("any_of", 2, 2, "cond true on at least one of the last n bars")(
    lambda c, k, n: rolling_sum(_bool(c, k).astype(float), _n(n)) >= 1)
fn("rising", 2, 2, "x rose bar-over-bar on each of the last n bars")(
    lambda c, x, n: rolling_sum((_arr(c, x) > shift(_arr(c, x), 1)).astype(float), _n(n)) >= _n(n))
fn("falling", 2, 2, "x fell bar-over-bar on each of the last n bars")(
    lambda c, x, n: rolling_sum((_arr(c, x) < shift(_arr(c, x), 1)).astype(float), _n(n)) >= _n(n))


@fn("barssince", 1, 1, "bars since cond was last true (NaN if never)")
def _bars_since(c: Context, k: Any) -> np.ndarray:
    b = _bool(c, k)
    idx = np.where(b, np.arange(len(b)), -1)
    last = np.maximum.accumulate(idx)
    out = (np.arange(len(b)) - last).astype(float)
    out[last < 0] = np.nan
    return out


@fn("valuewhen", 2, 2, "value of x on the most recent bar where cond was true")
def _valuewhen(c: Context, k: Any, x: Any) -> np.ndarray:
    b, x = _bool(c, k), _arr(c, x)
    s = pd.Series(np.where(b, x, np.nan)).ffill().to_numpy()
    return s


# classic indicators
fn("rsi", 2, 2, "Wilder RSI rsi(x, n)")(lambda c, x, n: _rsi_safe(_arr(c, x), _n(n)))
fn("tr", 0, 0, "true range")(lambda c: true_range(c.high, c.low, c.close))
fn("atr", 1, 1, "Wilder average true range atr(n)")(lambda c, n: rma(true_range(c.high, c.low, c.close), _n(n)))
fn("adx", 1, 1, "Wilder ADX adx(n)")(lambda c, n: _dmi(c, _n(n))[0])
fn("plus_di", 1, 1, "+DI(n)")(lambda c, n: _dmi(c, _n(n))[1])
fn("minus_di", 1, 1, "-DI(n)")(lambda c, n: _dmi(c, _n(n))[2])
fn("macd", 1, 3, "MACD line macd(x[, fast=12, slow=26])")(
    lambda c, x, f=12, s=26: ema(_arr(c, x), _n(f)) - ema(_arr(c, x), _n(s)))
fn("macd_signal", 1, 4, "MACD signal macd_signal(x[, 12, 26, 9])")(
    lambda c, x, f=12, s=26, g=9: ema(ema(_arr(c, x), _n(f)) - ema(_arr(c, x), _n(s)), _n(g)))


@fn("macd_hist", 1, 4, "MACD histogram macd_hist(x[, 12, 26, 9])")
def _mh(c: Context, x: Any, f: Any = 12, s: Any = 26, g: Any = 9) -> np.ndarray:
    m = ema(_arr(c, x), _n(f)) - ema(_arr(c, x), _n(s))
    return m - ema(m, _n(g))


fn("ppo", 1, 3, "percentage price oscillator ppo(x[, 12, 26])")(
    lambda c, x, f=12, s=26: 100 * _div(ema(_arr(c, x), _n(f)) - ema(_arr(c, x), _n(s)), ema(_arr(c, x), _n(s))))


@fn("stoch_k", 1, 2, "slow stochastic %K stoch_k(n[, smooth=3]) 0..100")
def _sk(c: Context, n: Any, sm: Any = 3) -> np.ndarray:
    hh, ll = rolling_max(c.high, _n(n)), rolling_min(c.low, _n(n))
    return sma(100 * _div(c.close - ll, hh - ll), _n(sm))


@fn("stoch_d", 1, 3, "stochastic %D stoch_d(n[, smooth=3, d=3])")
def _sd(c: Context, n: Any, sm: Any = 3, d: Any = 3) -> np.ndarray:
    return sma(_sk(c, n, sm), _n(d))


@fn("stochrsi_k", 0, 3, "StochRSI %K 0..1 stochrsi_k([rsi_n=14, stoch_n=14, smooth=3])")
def _srk(c: Context, rn: Any = 14, sn: Any = 14, sm: Any = 3) -> np.ndarray:
    r = _rsi_safe(c.close, _n(rn))
    return sma(_div(r - rolling_min(r, _n(sn)), rolling_max(r, _n(sn)) - rolling_min(r, _n(sn))), _n(sm))


fn("stochrsi_d", 0, 4, "StochRSI %D")(lambda c, rn=14, sn=14, sm=3, d=3: sma(_srk(c, rn, sn, sm), _n(d)))
fn("willr", 1, 1, "Williams %R (-100..0)")(
    lambda c, n: -100 * _div(rolling_max(c.high, _n(n)) - c.close, rolling_max(c.high, _n(n)) - rolling_min(c.low, _n(n))))


@fn("cci", 1, 1, "commodity channel index cci(n)")
def _cci(c: Context, n: Any) -> np.ndarray:
    n = _n(n, lo=2)
    tp = (c.high + c.low + c.close) / 3
    out = np.full(c.n, np.nan)
    if c.n >= n:
        W = _windows(tp, n)
        m = W.mean(axis=1)
        md = np.abs(W - m[:, None]).mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[n - 1:] = (tp[n - 1:] - m) / (0.015 * md)
    out[~np.isfinite(out)] = np.nan
    return out


@fn("mfi", 1, 1, "money flow index mfi(n)")
def _mfi(c: Context, n: Any) -> np.ndarray:
    """TA-Lib MFI: n money flows, each needing the previous typical price (first value at bar n).
    A window with no money flow at all (zero volume or a flat typical price) is undefined (NaN), not 100."""
    n = _n(n)
    tp = (c.high + c.low + c.close) / 3
    mf = tp * c.volume
    d = tp - shift(tp, 1)
    pos = rolling_sum(np.where(np.isnan(d), np.nan, np.where(d > 0, mf, 0.0)), n)
    neg = rolling_sum(np.where(np.isnan(d), np.nan, np.where(d < 0, mf, 0.0)), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(neg == 0, 100.0, 100 - 100 / (1 + pos / neg))
    out[(pos + neg) == 0] = np.nan
    out[np.isnan(pos) | np.isnan(neg)] = np.nan
    return out


def _mfm(c: Context) -> np.ndarray:
    return np.nan_to_num(_div((c.close - c.low) - (c.high - c.close), c.high - c.low), nan=0.0)


fn("cmf", 1, 1, "Chaikin money flow cmf(n)")(
    lambda c, n: _div(rolling_sum(_mfm(c) * c.volume, _n(n)), rolling_sum(c.volume, _n(n))))
fn("ad", 0, 0, "accumulation/distribution line")(lambda c: np.cumsum(_mfm(c) * c.volume))
fn("obv", 0, 0, "on-balance volume")(lambda c: np.cumsum(np.sign(np.nan_to_num(np.diff(c.close, prepend=c.close[0]))) * c.volume))
fn("pvt", 0, 0, "price-volume trend")(
    lambda c: np.cumsum(np.nan_to_num(_div(c.close - shift(c.close, 1), shift(c.close, 1))) * c.volume))


def _vol_index(c: Context, positive: bool) -> np.ndarray:
    r = np.nan_to_num(_div(c.close, shift(c.close, 1)) - 1)
    dv = np.diff(c.volume, prepend=c.volume[0])
    use = dv > 0 if positive else dv < 0
    return 1000 * np.cumprod(1 + np.where(use, r, 0.0))


fn("nvi", 0, 0, "negative volume index")(lambda c: _vol_index(c, False))
fn("pvi", 0, 0, "positive volume index")(lambda c: _vol_index(c, True))
fn("force", 1, 1, "Elder force index ema((close-close[1])*volume, n)")(
    lambda c, n: ema(np.nan_to_num(c.close - shift(c.close, 1)) * c.volume, _n(n)))


@fn("eom", 1, 1, "ease of movement (scaled by 1e6 volume) sma over n")
def _eom(c: Context, n: Any) -> np.ndarray:
    mid = (c.high + c.low) / 2
    box = _div(c.volume / 1e6, c.high - c.low)
    return sma(_div(mid - shift(mid, 1), box), _n(n))


@fn("kvo", 0, 2, "Klinger volume oscillator kvo([34, 55])")
def _kvo(c: Context, f: Any = 34, s: Any = 55) -> np.ndarray:
    tp = c.high + c.low + c.close
    trend = np.where(tp > shift(tp, 1), 1.0, -1.0)
    vf = c.volume * trend
    return ema(vf, _n(f)) - ema(vf, _n(s))


fn("kvo_signal", 0, 3, "Klinger signal kvo_signal([34, 55, 13])")(lambda c, f=34, s=55, g=13: ema(_kvo(c, f, s), _n(g)))


@fn("tsi", 1, 3, "true strength index tsi(x[, r=25, s=13]) in percent")
def _tsi(c: Context, x: Any, r: Any = 25, s: Any = 13) -> np.ndarray:
    m = np.diff(_arr(c, x), prepend=np.nan)
    return 100 * _div(ema(ema(m, _n(r)), _n(s)), ema(ema(np.abs(m), _n(r)), _n(s)))


@fn("kst", 0, 0, "Know Sure Thing (10/15/20/30 ROC, 10/10/10/15 SMA)")
def _kst(c: Context) -> np.ndarray:
    x = c.close
    r = lambda n: _div(x, shift(x, n)) - 1  # noqa: E731
    return 100 * (sma(r(10), 10) + 2 * sma(r(15), 10) + 3 * sma(r(20), 10) + 4 * sma(r(30), 15))


@fn("trix", 1, 1, "TRIX: percent change of triple-smoothed EMA of close")
def _trix(c: Context, n: Any) -> np.ndarray:
    e = ema(ema(ema(c.close, _n(n)), _n(n)), _n(n))
    return 100 * (_div(e, shift(e, 1)) - 1)


@fn("cmo", 1, 1, "Chande momentum oscillator")
def _cmo(c: Context, n: Any) -> np.ndarray:
    d = np.diff(c.close, prepend=np.nan)
    up = rolling_sum(np.where(d > 0, d, 0.0), _n(n))
    dn = rolling_sum(np.where(d < 0, -d, 0.0), _n(n))
    return 100 * _div(up - dn, up + dn)


@fn("stc", 0, 3, "Schaff trend cycle stc([fast=23, slow=50, cycle=10]) 0..100")
def _stc(c: Context, f: Any = 23, s: Any = 50, cy: Any = 10) -> np.ndarray:
    m = ema(c.close, _n(f)) - ema(c.close, _n(s))
    n = _n(cy)

    def stoch_smooth(v: np.ndarray) -> np.ndarray:
        lo, hi = rolling_min(v, n), rolling_max(v, n)
        k = 100 * _div(v - lo, hi - lo)
        out = np.full(len(v), np.nan)
        prev = np.nan
        for i in range(len(v)):
            if np.isfinite(k[i]):
                prev = k[i] if not np.isfinite(prev) else prev + 0.5 * (k[i] - prev)
            elif np.isfinite(prev) and np.isfinite(v[i]):
                pass
            out[i] = prev
        return out
    return stoch_smooth(stoch_smooth(m))


@fn("aroon_up", 1, 1, "Aroon up 0..100")
def _aru(c: Context, n: Any) -> np.ndarray:
    n = _n(n)
    out = np.full(c.n, np.nan)
    if c.n > n:
        W = _windows(c.high, n + 1)
        out[n:] = 100 * (n - np.argmax(W[:, ::-1] == W.max(axis=1)[:, None], axis=1)) / n
    return out


@fn("aroon_down", 1, 1, "Aroon down 0..100")
def _ard(c: Context, n: Any) -> np.ndarray:
    n = _n(n)
    out = np.full(c.n, np.nan)
    if c.n > n:
        W = _windows(c.low, n + 1)
        out[n:] = 100 * (n - np.argmax(W[:, ::-1] == W.min(axis=1)[:, None], axis=1)) / n
    return out


@fn("vi_plus", 1, 1, "vortex VI+")
def _vip(c: Context, n: Any) -> np.ndarray:
    return _div(rolling_sum(np.abs(c.high - shift(c.low, 1)), _n(n)), rolling_sum(true_range(c.high, c.low, c.close), _n(n)))


@fn("vi_minus", 1, 1, "vortex VI-")
def _vim(c: Context, n: Any) -> np.ndarray:
    return _div(rolling_sum(np.abs(c.low - shift(c.high, 1)), _n(n)), rolling_sum(true_range(c.high, c.low, c.close), _n(n)))


fn("supertrend", 2, 2, "Supertrend line supertrend(n, mult)")(lambda c, n, m: _supertrend(c, _n(n), _f(m))[0])
fn("supertrend_dir", 2, 2, "Supertrend direction +1/-1")(lambda c, n, m: _supertrend(c, _n(n), _f(m))[1])
fn("psar", 0, 2, "parabolic SAR psar([step=0.02, max=0.2])")(lambda c, s=0.02, m=0.2: _psar(c, _f(s), _f(m)))
fn("midpoint", 1, 1, "(highest(high,n) + lowest(low,n)) / 2 (Ichimoku Tenkan/Kijun)")(
    lambda c, n: (rolling_max(c.high, _n(n)) + rolling_min(c.low, _n(n))) / 2)
fn("senkou_a", 0, 3, "Ichimoku span A as plotted at this bar senkou_a([9, 26, 26])")(
    lambda c, a=9, b=26, d=26: shift(((rolling_max(c.high, _n(a)) + rolling_min(c.low, _n(a))) / 2 +
                                      (rolling_max(c.high, _n(b)) + rolling_min(c.low, _n(b))) / 2) / 2, _n(d)))
fn("senkou_b", 0, 2, "Ichimoku span B as plotted at this bar senkou_b([52, 26])")(
    lambda c, n=52, d=26: shift((rolling_max(c.high, _n(n)) + rolling_min(c.low, _n(n))) / 2, _n(d)))


def _bb(c: Context, x: Any, n: Any, k: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = _arr(c, x)
    m, s = sma(x, _n(n)), stdev(x, _n(n, lo=2))
    return m + _f(k) * s, m, m - _f(k) * s


fn("bb_upper", 1, 3, "Bollinger upper bb_upper(x[, 20, 2])")(lambda c, x, n=20, k=2: _bb(c, x, n, k)[0])
fn("bb_lower", 1, 3, "Bollinger lower")(lambda c, x, n=20, k=2: _bb(c, x, n, k)[2])
fn("bb_width", 1, 3, "Bollinger width (upper-lower)/middle")(
    lambda c, x, n=20, k=2: _div(_bb(c, x, n, k)[0] - _bb(c, x, n, k)[2], _bb(c, x, n, k)[1]))
fn("pctb", 1, 3, "Bollinger %B")(
    lambda c, x, n=20, k=2: _div(_arr(c, x) - _bb(c, x, n, k)[2], _bb(c, x, n, k)[0] - _bb(c, x, n, k)[2]))
fn("kc_upper", 0, 3, "Keltner upper ema(close,n) + k*atr(atr_n) kc_upper([20, 2, atr_n=n])")(
    lambda c, n=20, k=2, a=None: ema(c.close, _n(n)) + _f(k) * rma(true_range(c.high, c.low, c.close), _n(a if a is not None else n)))
fn("kc_lower", 0, 3, "Keltner lower")(
    lambda c, n=20, k=2, a=None: ema(c.close, _n(n)) - _f(k) * rma(true_range(c.high, c.low, c.close), _n(a if a is not None else n)))
fn("dpo", 1, 1, "causal detrended price oscillator close[n/2+1] - sma(close, n)")(
    lambda c, n: shift(c.close, _n(n) // 2 + 1) - sma(c.close, _n(n)))


def _swma(x: np.ndarray) -> np.ndarray:
    return (x + 2 * shift(x, 1) + 2 * shift(x, 2) + shift(x, 3)) / 6


fn("rvi", 1, 1, "relative vigor index")(
    lambda c, n: _div(sma(_swma(c.close - c.open), _n(n)), sma(_swma(c.high - c.low), _n(n))))
fn("rvi_signal", 1, 1, "RVI signal (symmetric 4-bar weighting)")(
    lambda c, n: _swma(_div(sma(_swma(c.close - c.open), _n(n)), sma(_swma(c.high - c.low), _n(n)))))
fn("chop", 1, 1, "choppiness index")(
    lambda c, n: 100 * np.log10(_div(rolling_sum(true_range(c.high, c.low, c.close), _n(n, lo=2)),
                                     rolling_max(c.high, _n(n, lo=2)) - rolling_min(c.low, _n(n, lo=2)))) / math.log10(_n(n, lo=2)))


@fn("mass_index", 0, 2, "mass index mass_index([9, 25])")
def _mass(c: Context, e: Any = 9, s: Any = 25) -> np.ndarray:
    e1 = ema(c.high - c.low, _n(e))
    return rolling_sum(_div(e1, ema(e1, _n(e))), _n(s))


@fn("ulcer", 1, 1, "ulcer index (percent)")
def _ulcer(c: Context, n: Any) -> np.ndarray:
    dd = 100 * (_div(c.close, rolling_max(c.close, _n(n))) - 1)
    return np.sqrt(sma(dd ** 2, _n(n)))


def _ann(c: Context) -> float:
    return {"1d": 248.0, "1w": 52.0}.get(c.timeframe, 248.0 * 375 / max(1, int(c.timeframe.rstrip("m")) if c.timeframe.endswith("m") else 1))


fn("hv", 1, 1, "annualised close-to-close volatility")(
    lambda c, n: stdev(np.log(_div(c.close, shift(c.close, 1))), _n(n, lo=2)) * math.sqrt(_ann(c)))
fn("parkinson", 1, 1, "annualised Parkinson high-low volatility")(
    lambda c, n: np.sqrt(sma(np.log(_div(c.high, c.low)) ** 2, _n(n)) / (4 * math.log(2)) * _ann(c)))
fn("chaikin_vol", 0, 2, "Chaikin volatility roc(ema(high-low, n), r) chaikin_vol([10, 10])")(
    lambda c, n=10, r=10: _div(ema(c.high - c.low, _n(n)), shift(ema(c.high - c.low, _n(n)), _n(r))) - 1)
fn("ao", 0, 0, "awesome oscillator")(lambda c: sma((c.high + c.low) / 2, 5) - sma((c.high + c.low) / 2, 34))


@fn("fisher", 1, 1, "Ehlers Fisher transform")
def _fisher(c: Context, n: Any) -> np.ndarray:
    n = _n(n)
    mid = (c.high + c.low) / 2
    hi, lo = rolling_max(mid, n), rolling_min(mid, n)
    out = np.full(c.n, np.nan)
    v, f = 0.0, 0.0
    for i in range(c.n):
        if not (np.isfinite(hi[i]) and np.isfinite(lo[i])):
            continue
        rng = hi[i] - lo[i]
        x = 0.0 if rng == 0 else 2 * ((mid[i] - lo[i]) / rng - 0.5)
        v = min(0.999, max(-0.999, 0.66 * x + 0.67 * v))
        f = 0.5 * math.log((1 + v) / (1 - v)) + 0.5 * f
        out[i] = f
    return out


@fn("ift_rsi", 1, 1, "inverse Fisher transform of RSI scaled to 0..100")
def _ift(c: Context, n: Any) -> np.ndarray:
    v = wma(0.1 * (_rsi_safe(c.close, _n(n)) - 50), 9)
    return 50 * (np.tanh(v) + 1)


def _wt(c: Context, n1: int, n2: int) -> np.ndarray:
    ap = (c.high + c.low + c.close) / 3
    esa = ema(ap, n1)
    d = ema(np.abs(ap - esa), n1)
    return ema(_div(ap - esa, 0.015 * d), n2)


fn("wt1", 0, 2, "WaveTrend line 1 wt1([10, 21])")(lambda c, a=10, b=21: _wt(c, _n(a), _n(b)))
fn("wt2", 0, 2, "WaveTrend line 2 (sma(wt1, 4))")(lambda c, a=10, b=21: sma(_wt(c, _n(a), _n(b)), 4))


@fn("demarker", 1, 1, "DeMarker 0..1")
def _dem(c: Context, n: Any) -> np.ndarray:
    dmax = np.fmax(c.high - shift(c.high, 1), 0)
    dmin = np.fmax(shift(c.low, 1) - c.low, 0)
    a, b = sma(dmax, _n(n)), sma(dmin, _n(n))
    return _div(a, a + b)


@fn("uo", 0, 3, "ultimate oscillator uo([7, 14, 28])")
def _uo(c: Context, a: Any = 7, b: Any = 14, d: Any = 28) -> np.ndarray:
    pc = shift(c.close, 1)
    bp = c.close - np.fmin(c.low, pc)
    tr = true_range(c.high, c.low, c.close)
    av = lambda n: _div(rolling_sum(bp, _n(n)), rolling_sum(tr, _n(n)))  # noqa: E731
    return 100 * (4 * av(a) + 2 * av(b) + av(d)) / 7


@fn("laguerre_rsi", 0, 1, "Ehlers Laguerre RSI 0..1 laguerre_rsi([gamma=0.5])")
def _lag(c: Context, g: Any = 0.5) -> np.ndarray:
    g = _f(g)
    out = np.full(c.n, np.nan)
    l0 = l1 = l2 = l3 = None
    for i, x in enumerate(c.close):
        if not np.isfinite(x):
            continue
        if l0 is None:
            l0 = l1 = l2 = l3 = x
            continue
        p0, p1, p2 = l0, l1, l2
        l0 = (1 - g) * x + g * p0
        l1 = -g * l0 + p0 + g * p1
        l2 = -g * l1 + p1 + g * p2
        l3 = -g * l2 + p2 + g * l3
        cu = max(l0 - l1, 0) + max(l1 - l2, 0) + max(l2 - l3, 0)
        cd = max(l1 - l0, 0) + max(l2 - l1, 0) + max(l3 - l2, 0)
        out[i] = cu / (cu + cd) if cu + cd > 0 else 0.0
    return out


@fn("cog", 1, 1, "Ehlers center of gravity")
def _cog(c: Context, n: Any) -> np.ndarray:
    n = _n(n)
    mid = (c.high + c.low) / 2
    out = np.full(c.n, np.nan)
    if c.n >= n:
        W = _windows(mid, n)[:, ::-1]
        w = np.arange(1, n + 1, dtype=float)
        out[n - 1:] = -_div(W @ w, W.sum(axis=1))
    return out


fn("gapo", 1, 1, "Gopalakrishnan range index")(
    lambda c, n: _div(np.log(rolling_max(c.high, _n(n, lo=2)) - rolling_min(c.low, _n(n, lo=2))), np.full(c.n, math.log(_n(n, lo=2)))))
fn("er", 1, 1, "Kaufman efficiency ratio over n bars")(
    lambda c, n: _div(np.abs(c.close - shift(c.close, _n(n))), rolling_sum(np.abs(np.diff(c.close, prepend=np.nan)), _n(n))))


@fn("vstop", 2, 2, "volatility stop vstop(n, mult): ATR trailing stop line on closes")
def _vstop(c: Context, n: Any, m: Any) -> np.ndarray:
    atr = rma(true_range(c.high, c.low, c.close), _n(n))
    mult = _f(m)
    out = np.full(c.n, np.nan)
    up, ext, stop = True, np.nan, np.nan
    for i in range(c.n):
        if not np.isfinite(atr[i]):
            continue
        x = c.close[i]
        if not np.isfinite(ext):
            ext, stop = x, x - mult * atr[i]
        if up:
            ext = max(ext, x)
            stop = max(stop, ext - mult * atr[i])
            if x < stop:
                up, ext, stop = False, x, x + mult * atr[i]
        else:
            ext = min(ext, x)
            stop = min(stop, ext + mult * atr[i])
            if x > stop:
                up, ext, stop = True, x, x - mult * atr[i]
        out[i] = stop
    return out


def _ha(c: Context) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if "__ha" in c.cache:
        return c.cache["__ha"]
    hc = (c.open + c.high + c.low + c.close) / 4
    ho = np.full(c.n, np.nan)
    if c.n:
        ho[0] = (c.open[0] + c.close[0]) / 2
        for i in range(1, c.n):
            ho[i] = (ho[i - 1] + hc[i - 1]) / 2
    hh = np.fmax(c.high, np.fmax(ho, hc))
    hl = np.fmin(c.low, np.fmin(ho, hc))
    c.cache["__ha"] = (ho, hh, hl, hc)
    return c.cache["__ha"]


fn("ha_open", 0, 0, "Heikin-Ashi open")(lambda c: _ha(c)[0])
fn("ha_high", 0, 0, "Heikin-Ashi high")(lambda c: _ha(c)[1])
fn("ha_low", 0, 0, "Heikin-Ashi low")(lambda c: _ha(c)[2])
fn("ha_close", 0, 0, "Heikin-Ashi close")(lambda c: _ha(c)[3])

# market structure (confirmed fractal pivots: a pivot with k bars each side becomes known k bars later)
fn("swing_high", 1, 2, "price of the j-th most recent confirmed swing high swing_high(k[, j=1])")(
    lambda c, k, j=1: _swing_series(c, _n(k), _n(j), True))
fn("swing_low", 1, 2, "price of the j-th most recent confirmed swing low swing_low(k[, j=1])")(
    lambda c, k, j=1: _swing_series(c, _n(k), _n(j), False))
fn("swing_high_age", 1, 2, "bars since the j-th most recent confirmed swing high pivot")(
    lambda c, k, j=1: _swing_series(c, _n(k), _n(j), True, "age"))
fn("swing_low_age", 1, 2, "bars since the j-th most recent confirmed swing low pivot")(
    lambda c, k, j=1: _swing_series(c, _n(k), _n(j), False, "age"))

# session / intraday
fn("prev_day_high", 0, 1, "high of the k-th previous session prev_day_high([k=1])")(
    lambda c, k=1: _prev_session(c, "high", _n(k, lo=1, hi=20)))
fn("prev_day_low", 0, 1, "low of the k-th previous session")(lambda c, k=1: _prev_session(c, "low", _n(k, lo=1, hi=20)))
fn("prev_day_close", 0, 1, "close of the k-th previous session")(
    lambda c, k=1: _prev_session(c, "close", _n(k, lo=1, hi=20)))
fn("day_open", 0, 0, "current session open")(lambda c: _session_cum(c, c.open, "first") if c.intraday else c.open)


@fn("vwap", 0, 0, "session VWAP (intraday lanes)")
def _vwap(c: Context) -> np.ndarray:
    _need_intraday(c, "vwap")
    pv = c.vwap_pv if c.vwap_pv is not None else (c.high + c.low + c.close) / 3 * c.volume
    return _div(_session_cum(c, pv, "sum"), _session_cum(c, c.volume, "sum"))


@fn("vwap_std", 0, 0, "volume-weighted standard deviation of price around the session VWAP")
def _vwap_std(c: Context) -> np.ndarray:
    _need_intraday(c, "vwap_std")
    tp = (c.high + c.low + c.close) / 3
    v = _session_cum(c, c.volume, "sum")
    m = _vwap(c)
    m2 = _div(_session_cum(c, tp * tp * c.volume, "sum"), v)
    return np.sqrt(np.fmax(m2 - m * m, 0))


@fn("minute_of_session", 0, 0, "minutes from 09:15 to the bar end")
def _mos(c: Context) -> np.ndarray:
    _need_intraday(c, "minute_of_session")
    if c.minute_of_session is not None:
        return c.minute_of_session
    return ((c.end_ns // 60_000_000_000 + 330) % 1440 - 555).astype(float)


fn("bar_of_session", 0, 0, "1-based bar index inside the session")(
    lambda c: (_need_intraday(c, "bar_of_session"), _session_cum(c, np.ones(c.n), "sum"))[1])
fn("session_high", 0, 0, "session high so far")(lambda c: (_need_intraday(c, "session_high"), _session_cum(c, c.high, "max"))[1])
fn("session_low", 0, 0, "session low so far")(lambda c: (_need_intraday(c, "session_low"), _session_cum(c, c.low, "min"))[1])
fn("session_open", 0, 0, "session open")(lambda c: (_need_intraday(c, "session_open"), _session_cum(c, c.open, "first"))[1])


def _opening_range(c: Context, minutes: int, high: bool) -> np.ndarray:
    _need_intraday(c, "or_high" if high else "or_low")
    mos = _mos(c)
    gid, _ = _session_groups(c)
    inside = mos <= minutes
    val = np.where(inside, c.high if high else c.low, np.nan)
    s = pd.Series(val).groupby(gid)
    agg = (s.cummax() if high else s.cummin()).to_numpy(dtype=float)
    # Known only once the range is complete (bars ending after the range).
    out = np.where(mos > minutes, pd.Series(agg).groupby(gid).ffill().to_numpy(dtype=float), np.nan)
    return out


fn("or_high", 1, 1, "opening-range high of the first m minutes (NaN until complete)")(
    lambda c, m: _opening_range(c, _n(m), True))
fn("or_low", 1, 1, "opening-range low of the first m minutes (NaN until complete)")(
    lambda c, m: _opening_range(c, _n(m), False))


@fn("avg_day_volume", 1, 1, "mean total volume of the previous n complete sessions")
def _adv(c: Context, n: Any) -> np.ndarray:
    if not c.intraday:
        return shift(sma(c.volume, _n(n)), 1)
    gid, _ = _session_groups(c)
    tot = pd.Series(c.volume).groupby(gid).sum()
    prev = tot.rolling(_n(n), min_periods=_n(n)).mean().shift(1).to_numpy()
    return prev[gid]


@fn("slot_rvol", 1, 1, "volume / median volume of the same intraday slot over the previous n sessions")
def _slot_rvol(c: Context, n: Any) -> np.ndarray:
    _need_intraday(c, "slot_rvol")
    slot = _mos(c)
    df = pd.DataFrame({"slot": slot, "v": c.volume})
    med = df.groupby("slot")["v"].transform(lambda s: s.shift(1).rolling(_n(n), min_periods=_n(n)).median())
    return _div(c.volume, med.to_numpy(dtype=float))


def _vprofile(c: Context, n: int, bins: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    key = f"__vp{n}_{bins}"
    if key in c.cache:
        return c.cache[key]
    tp = (c.high + c.low + c.close) / 3
    N = c.n
    poc, vah, val, dens = (np.full(N, np.nan) for _ in range(4))
    for t in range(n - 1, N):
        p = tp[t - n + 1:t + 1]
        v = c.volume[t - n + 1:t + 1]
        if not (np.isfinite(p).all() and np.isfinite(v).all()) or v.sum() <= 0:
            continue
        lo, hi = p.min(), p.max()
        if hi <= lo:
            continue
        edges = np.linspace(lo, hi, bins + 1)
        hist, _ = np.histogram(p, bins=edges, weights=v)
        k = int(hist.argmax())
        poc[t] = (edges[k] + edges[k + 1]) / 2
        order = np.argsort(hist)[::-1]
        cum = np.cumsum(hist[order]) / hist.sum()
        inside = order[:int(np.searchsorted(cum, 0.70)) + 1]
        vah[t], val[t] = edges[inside.max() + 1], edges[inside.min()]
        j = min(bins - 1, max(0, int(np.searchsorted(edges, c.close[t], side="right") - 1)))
        dens[t] = hist[j] / hist.max() if c.close[t] <= hi and c.close[t] >= lo else np.nan
    c.cache[key] = (poc, vah, val, dens)
    return c.cache[key]


fn("vp_poc", 1, 2, "volume-profile point of control over the last n bars (typical price x volume, [bins=24])")(
    lambda c, n, b=24: _vprofile(c, _n(n, lo=5), _n(b, lo=5, hi=200))[0])
fn("vp_vah", 1, 2, "volume-profile value-area high (70%)")(lambda c, n, b=24: _vprofile(c, _n(n, lo=5), _n(b, lo=5, hi=200))[1])
fn("vp_val", 1, 2, "volume-profile value-area low (70%)")(lambda c, n, b=24: _vprofile(c, _n(n, lo=5), _n(b, lo=5, hi=200))[2])
fn("vp_density", 1, 2, "volume share of the close's price bin relative to the POC bin (0..1)")(
    lambda c, n, b=24: _vprofile(c, _n(n, lo=5), _n(b, lo=5, hi=200))[3])


@fn("cusum_up", 2, 3, "one-sided CUSUM statistic of standardised returns cusum_up(x, n[, k=0.5])")
def _cusum(c: Context, x: Any, n: Any, k: Any = 0.5) -> np.ndarray:
    x = _arr(c, x)
    r = _div(x, shift(x, 1)) - 1
    z = _div(r - shift(sma(r, _n(n, lo=5)), 1), shift(stdev(r, _n(n, lo=5)), 1))
    out = np.full(c.n, np.nan)
    s = 0.0
    kk = _f(k)
    for i in range(c.n):
        if np.isfinite(z[i]):
            s = max(0.0, s + z[i] - kk)
            out[i] = s
    return out


def _time_window(c: Context, start: int, end: int, high: bool) -> np.ndarray:
    _need_intraday(c, "window_high" if high else "window_low")
    mos = _mos(c)
    gid, _ = _session_groups(c)
    inside = (mos > start) & (mos <= end)
    val = np.where(inside, c.high if high else c.low, np.nan)
    s = pd.Series(val).groupby(gid)
    agg = (s.cummax() if high else s.cummin()).to_numpy(dtype=float)
    return np.where(mos > end, pd.Series(agg).groupby(gid).ffill().to_numpy(dtype=float), np.nan)


fn("window_high", 2, 2, "session high of bars ending in (start, end] minutes after 09:15; NaN until the window closes")(
    lambda c, a, b: _time_window(c, _n(a, lo=0, hi=400), _n(b, lo=1, hi=400), True))
fn("window_low", 2, 2, "session low of bars ending in (start, end] minutes")(
    lambda c, a, b: _time_window(c, _n(a, lo=0, hi=400), _n(b, lo=1, hi=400), False))


@fn("anchored_vwap", 1, 1, "VWAP of typical price anchored at the most recent bar where cond was true")
def _avwap(c: Context, k: Any) -> np.ndarray:
    b = _bool(c, k)
    anchor = np.maximum.accumulate(np.where(b, np.arange(c.n), -1))
    tp = (c.high + c.low + c.close) / 3
    cpv = np.concatenate(([0.0], np.cumsum(np.nan_to_num(tp * c.volume))))
    cv = np.concatenate(([0.0], np.cumsum(np.nan_to_num(c.volume))))
    idx = np.arange(c.n)
    a = np.where(anchor >= 0, anchor, 0)
    out = _div(cpv[idx + 1] - cpv[a], cv[idx + 1] - cv[a])
    out[anchor < 0] = np.nan
    return out


@fn("kalman", 1, 3, "constant-velocity Kalman estimate of log price kalman(x[, q=1e-5, r=1e-3]), in price units")
def _kalman(c: Context, x: Any, q: Any = 1e-5, r: Any = 1e-3) -> np.ndarray:
    y = np.log(np.where(_arr(c, x) > 0, _arr(c, x), np.nan))
    qq, rr = _f(q), _f(r)
    out = np.full(c.n, np.nan)
    state = None
    P = np.eye(2)
    F = np.array([[1.0, 1.0], [0.0, 1.0]])
    Q = qq * np.array([[0.25, 0.5], [0.5, 1.0]])
    H = np.array([1.0, 0.0])
    for i in range(c.n):
        if not np.isfinite(y[i]):
            continue
        if state is None:
            state = np.array([y[i], 0.0])
            out[i] = math.exp(y[i])
            continue
        state = F @ state
        P = F @ P @ F.T + Q
        S = H @ P @ H + rr
        K = P @ H / S
        state = state + K * (y[i] - H @ state)
        P = (np.eye(2) - np.outer(K, H)) @ P
        out[i] = math.exp(state[0])
    return out


@fn("hurst", 1, 2, "rolling Hurst exponent from the scaling of aggregated return variance hurst(x[, n=100])")
def _hurst(c: Context, x: Any, n: Any = 100) -> np.ndarray:
    n = _n(n, lo=32)
    r = np.diff(np.log(np.where(_arr(c, x) > 0, _arr(c, x), np.nan)), prepend=np.nan)
    out = np.full(c.n, np.nan)
    lags = [1, 2, 4, 8, 16]
    lx = np.log(lags)
    for t in range(n, c.n):
        w = r[t - n + 1:t + 1]
        if not np.isfinite(w).all():
            continue
        cs = np.cumsum(w)
        v = [np.var(cs[L:] - cs[:-L]) for L in lags]
        if min(v) <= 0:
            continue
        out[t] = np.polyfit(lx, np.log(v), 1)[0] / 2
    return out


@fn("updown_entropy", 1, 2, "Shannon entropy (bits) of m-bar up/down patterns over n bars updown_entropy(n[, m=2])")
def _entropy(c: Context, n: Any, m: Any = 2) -> np.ndarray:
    n, m = _n(n, lo=8), _n(m, lo=1, hi=4)
    up = (np.diff(c.close, prepend=np.nan) > 0).astype(np.int64)
    code = np.zeros(c.n, dtype=np.int64)
    for k in range(m):
        lagged = np.concatenate((np.zeros(k, dtype=np.int64), up[:c.n - k])) if k else up
        code = code * 2 + lagged
    out = np.full(c.n, np.nan)
    for t in range(n + m, c.n):
        p = np.bincount(code[t - n + 1:t + 1], minlength=2 ** m) / n
        p = p[p > 0]
        out[t] = float(-(p * np.log2(p)).sum())
    return out


FUNCTIONS["htf"] = (2, 2, 'evaluate an expression on another timeframe of the same symbol, aligned as-of: htf(rsi(close, 14), "1w")')


# calendar (from the bar's own session date; no exchange calendar is inferred)
def _dates(c: Context) -> pd.DatetimeIndex:
    if "__dates" not in c.cache:
        c.cache["__dates"] = pd.to_datetime(pd.Series(c.session), errors="coerce")
    return c.cache["__dates"]


fn("day_of_month", 0, 0, "calendar day of month of the bar's session")(lambda c: _dates(c).dt.day.to_numpy(dtype=float))
fn("day_of_week", 0, 0, "0=Monday .. 6=Sunday")(lambda c: _dates(c).dt.dayofweek.to_numpy(dtype=float))
fn("month", 0, 0, "calendar month 1..12")(lambda c: _dates(c).dt.month.to_numpy(dtype=float))


@fn("session_of_month", 0, 0, "ordinal of this trading session within its month among sessions present in the data")
def _som(c: Context) -> np.ndarray:
    d = _dates(c)
    ym = (d.dt.year * 100 + d.dt.month).to_numpy()
    gid, first = _session_groups(c)
    sess_first = pd.Series(first.astype(float)).groupby(ym).cumsum().to_numpy()
    return sess_first


# lane features computed by the M5.4 feature space (for ML-derived rules)
@fn("feature", 1, 1, 'lane feature value feature("RSI(period=14).value")')
def _feature(c: Context, name: Any) -> np.ndarray:
    if not isinstance(name, str):
        raise DSLError("feature() takes a quoted feature name")
    if name not in c.features:
        raise DSLError(f"feature {name!r} not available on this lane")
    return np.asarray(c.features[name], dtype=float)


FUNCTIONS["bench"] = (1, 2, 'evaluate an expression on the benchmark (default NIFTYBEES-EQ proxy), aligned as-of: bench(roc(close, 126)[, "SYMBOL"])')
FUNCTIONS["ref_symbol"] = (2, 2, 'evaluate an expression on another local symbol: ref_symbol("BANKBEES", close)')

SERIES = {"open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4", "range", "body", "upper_wick",
          "lower_wick", "ret", "gap"}


def check(node: tuple) -> None:
    k = node[0]
    if k == "var":
        if node[1] not in SERIES:
            raise DSLError(f"unknown series {node[1]!r}; use one of {sorted(SERIES)} or a function call")
    elif k == "call":
        name, args = node[1], node[2]
        if name not in FUNCTIONS:
            raise DSLError(f"unknown function {name}()")
        lo, hi, _ = FUNCTIONS[name]
        if not lo <= len(args) <= hi:
            raise DSLError(f"{name}() takes {lo}..{hi} arguments, got {len(args)}")
        for a in args:
            check(a)
    elif k in ("cmp", "bin"):
        check(node[2])
        check(node[3])
    elif k in ("and", "or"):
        check(node[1])
        check(node[2])
    elif k in ("not", "neg"):
        check(node[1])
    elif k == "lag":
        check(node[1])
        check(node[2])


# --------------------------------------------------------------------------- evaluator


def _var(c: Context, name: str) -> np.ndarray:
    o, h, lo, cl = c.open, c.high, c.low, c.close
    return {"open": lambda: o, "high": lambda: h, "low": lambda: lo, "close": lambda: cl, "volume": lambda: c.volume,
            "hl2": lambda: (h + lo) / 2, "hlc3": lambda: (h + lo + cl) / 3, "ohlc4": lambda: (o + h + lo + cl) / 4,
            "range": lambda: h - lo, "body": lambda: np.abs(cl - o),
            "upper_wick": lambda: h - np.fmax(o, cl), "lower_wick": lambda: np.fmin(o, cl) - lo,
            "ret": lambda: _div(cl, shift(cl, 1)) - 1, "gap": lambda: _div(o, shift(cl, 1)) - 1}[name]()


def evaluate(node: tuple, ctx: Context) -> Any:
    """Evaluate a parameter-free AST. Results are memoised per canonical sub-expression."""
    k = node[0]
    if k == "num":
        return node[1]
    if k == "str":
        return node[1]
    if k == "param":
        raise DSLError(f"unsubstituted parameter ${node[1]}")
    key = unparse(node)
    if key in ctx.cache:
        return ctx.cache[key]
    if k == "var":
        out = _var(ctx, node[1])
    elif k == "call":
        name, args = node[1], node[2]
        if name in LAZY:
            out = _eval_lazy(name, args, ctx)
        else:
            vals = [evaluate(a, ctx) for a in args]
            out = _IMPL[name](ctx, *vals)
    elif k == "lag":
        x = evaluate(node[1], ctx)
        kk = evaluate(node[2], ctx)
        x = _arr(ctx, x) if not (isinstance(x, np.ndarray) and x.dtype == bool) else x
        out = shift(x, _n(kk, "lag", 0, 100000))
    elif k == "neg":
        out = -_arr(ctx, evaluate(node[1], ctx))
    elif k == "not":
        out = ~_bool(ctx, evaluate(node[1], ctx))
    elif k in ("and", "or"):
        a, b = _bool(ctx, evaluate(node[1], ctx)), _bool(ctx, evaluate(node[2], ctx))
        out = (a & b) if k == "and" else (a | b)
    elif k == "cmp":
        a, b = evaluate(node[2], ctx), evaluate(node[3], ctx)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            a = _arr(ctx, a)
        a = a.astype(float) if isinstance(a, np.ndarray) and a.dtype == bool else a
        b = b.astype(float) if isinstance(b, np.ndarray) and b.dtype == bool else b
        with np.errstate(invalid="ignore"):
            op = node[1]
            if op == "==":
                scale = np.fmax(np.abs(_arr(ctx, a)), np.abs(_arr(ctx, b)))
                out = np.abs(_arr(ctx, a) - _arr(ctx, b)) <= 1e-9 * np.fmax(scale, 1.0)
            else:
                out = {">": np.greater, "<": np.less, ">=": np.greater_equal, "<=": np.less_equal,
                       "!=": np.not_equal}[op](a, b)
        out = np.asarray(out, dtype=bool)
    elif k == "bin":
        a, b = evaluate(node[2], ctx), evaluate(node[3], ctx)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out = {"+": a + b, "-": a - b, "*": a * b, "/": (a / b if b else math.nan)}[node[1]]
            return out
        a, b = _arr(ctx, a), _arr(ctx, b)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            out = {"+": np.add, "-": np.subtract, "*": np.multiply, "/": _div}[node[1]](a, b)
    else:
        raise DSLError(f"bad node {k}")
    ctx.cache[key] = out
    return out


def _eval_lazy(name: str, args: Sequence[tuple], ctx: Context) -> np.ndarray:
    from ..m54.timeframes import align

    if name == "htf":
        tf = evaluate(args[1], ctx)
        if not isinstance(tf, str):
            raise DSLError("htf() timeframe must be a quoted string")
        key = "__htf" + tf
        if key not in ctx._bench:
            ctx._bench[key] = ctx.htf_loader(tf) if ctx.htf_loader else None
        h = ctx._bench[key]
        if h is None:
            raise MissingData(f"TIMEFRAME:{tf}", f"no {tf} bars available for htf()")
        vals = _arr(h, evaluate(args[0], h))
        return align(ctx.end_ns, h.avail_ns if h.avail_ns is not None else h.end_ns, vals)
    if name == "bench":
        sym = evaluate(args[1], ctx) if len(args) > 1 else ctx.default_benchmark
        expr = args[0]
    else:
        sym, expr = evaluate(args[0], ctx), args[1]
    if not isinstance(sym, str):
        raise DSLError(f"{name}() symbol must be a quoted string")
    sym = sym.upper() if sym.upper().endswith("-EQ") else sym.upper() + "-EQ"
    b = _align_bench(ctx, sym)
    if b is None:
        raise MissingData(f"BENCHMARK:{sym}", f"no local {ctx.timeframe} candles for {sym}")
    vals = _arr(b, evaluate(expr, b))
    avail = b.avail_ns if b.avail_ns is not None else b.end_ns
    return align(ctx.end_ns, avail, vals)


class MissingData(RuntimeError):
    def __init__(self, requirement: str, reason: str):
        super().__init__(reason)
        self.requirement = requirement


def compile_text(text: str, params: Mapping[str, Any] | None = None) -> tuple:
    node = parse(text)
    return substitute(node, params or {}) if params_used(node) else node


def signal(text_or_node: str | tuple, ctx: Context, params: Mapping[str, Any] | None = None) -> np.ndarray:
    node = compile_text(text_or_node, params) if isinstance(text_or_node, str) else \
        (substitute(text_or_node, params or {}) if params else text_or_node)
    out = evaluate(node, ctx)
    return _bool(ctx, out).copy()


def requirements(node: tuple) -> dict[str, bool]:
    f = functions_used(node)
    return {"intraday": bool(f & INTRADAY_ONLY), "benchmark": bool(f & BENCHMARK_FUNCS), "htf": bool(f & HTF_FUNCS),
            "features": bool(f & FEATURE_FUNCS)}


__all__ = ["DSL_CONTRACT", "Context", "DSLError", "FUNCTIONS", "MissingData", "SERIES", "compile_text", "evaluate",
           "functions_used", "node_size", "params_used", "parse", "requirements", "signal", "substitute", "unparse"]
