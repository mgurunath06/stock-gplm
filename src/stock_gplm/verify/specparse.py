"""Independent evaluator for the catalogue's original pseudo-code (a deliberately small subset).

It shares no code with :mod:`stock_gplm.app.strategy_dsl`: its own tokenizer and parser, and the
pure-Python indicators of :mod:`stock_gplm.verify.reference`. If a rule uses anything outside the
subset, :class:`Unsupported` is raised and the strategy is audited structurally only.

Subset::

    SMA(n) | SMA(series, n) | EMA(..) | WMA(..) | HMA(..) | RSI(n) | ATR(n) | ADX(n) | CCI(n) | MFI(n)
    MAX(series, n) | MIN(series, n) | SUM(series, n) | ROC(n) | ABS(x) | VWMA(n) | PCT_B(n, k)
    Close Open High Low Volume Range Body Upper_Wick Lower_Wick
    x[t] x[t-k]      a crosses above b      a crosses below b      AND OR      65% (= 0.65)
    chained comparisons  a > b > c  ==  a > b AND b > c

Seeding follows the DSL convention (first-bar seed) so that only *translation* differences show
up here; seeding is audited separately in the indicator section.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Sequence

from . import reference as R

NAN = math.nan


class Unsupported(ValueError):
    pass


_TOK = re.compile(r"\s*(?:(?P<num>\d+\.\d+|\d+)(?P<pct>%)?|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                  r"|(?P<op>>=|<=|==|!=|[-+*/()\[\],<>]))")
SERIES = {"close", "open", "high", "low", "volume", "range", "body", "upper_wick", "lower_wick"}
FUNCS = {"sma", "ema", "wma", "hma", "rsi", "atr", "adx", "cci", "mfi", "max", "min", "sum", "roc", "abs", "vwma",
         "pct_b"}
CMP = {">", "<", ">=", "<=", "==", "!="}


def tokenize(text: str) -> list[tuple[str, Any]]:
    text = text.strip().rstrip(".;")
    pos, out = 0, []
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOK.match(text, pos)
        if not m or m.end() == pos:
            raise Unsupported(f"character {text[pos:pos + 10]!r}")
        if m.group("num") is not None:
            v = float(m.group("num"))
            out.append(("num", v / 100 if m.group("pct") else v))
        elif m.group("name") is not None:
            w = m.group("name").lower()
            out.append(("kw", w) if w in ("and", "or", "crosses", "above", "below", "t") else ("name", w))
        else:
            out.append(("op", m.group("op")))
        pos = m.end()
    return out


class Parser:
    def __init__(self, text: str):
        self.t = tokenize(text)
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, Any] | None:
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def take(self, val: Any = None) -> tuple[str, Any]:
        tok = self.peek()
        if tok is None or (val is not None and tok[1] != val):
            raise Unsupported(f"expected {val}, got {tok}")
        self.i += 1
        return tok

    def parse(self) -> tuple:
        n = self.or_()
        if self.peek() is not None:
            raise Unsupported(f"trailing {self.peek()}")
        return n

    def or_(self) -> tuple:
        n = self.and_()
        while self.peek() == ("kw", "or"):
            self.take()
            n = ("or", n, self.and_())
        return n

    def and_(self) -> tuple:
        n = self.cmp()
        while self.peek() == ("kw", "and"):
            self.take()
            n = ("and", n, self.cmp())
        return n

    def cmp(self) -> tuple:
        terms, ops = [self.sum_()], []
        while True:
            tok = self.peek()
            if tok and tok[0] == "op" and tok[1] in CMP:
                ops.append(self.take()[1])
            elif tok == ("kw", "crosses"):
                self.take()
                d = self.take()[1]
                if d not in ("above", "below"):
                    raise Unsupported("crosses must be followed by above/below")
                ops.append("xa" if d == "above" else "xb")
            else:
                break
            terms.append(self.sum_())
        if not ops:
            return terms[0]
        node = ("cmp", ops[0], terms[0], terms[1])
        for k in range(1, len(ops)):
            node = ("and", node, ("cmp", ops[k], terms[k], terms[k + 1]))
        return node

    def sum_(self) -> tuple:
        n = self.prod()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            n = ("bin", op, n, self.prod())
        return n

    def prod(self) -> tuple:
        n = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            n = ("bin", op, n, self.unary())
        return n

    def unary(self) -> tuple:
        if self.peek() == ("op", "-"):
            self.take()
            return ("bin", "*", ("num", -1.0), self.unary())
        return self.postfix()

    def postfix(self) -> tuple:
        n = self.primary()
        while self.peek() == ("op", "["):
            self.take()
            self.take("t")
            k = 0
            if self.peek() == ("op", "-"):
                self.take()
                tok = self.take()
                if tok[0] != "num":
                    raise Unsupported(f"symbolic lag t-{tok[1]}")
                k = int(tok[1])
            self.take("]")
            n = ("lag", n, k)
        return n

    def primary(self) -> tuple:
        tok = self.peek()
        if tok is None:
            raise Unsupported("unexpected end")
        if tok[0] == "num":
            self.take()
            return ("num", tok[1])
        if tok == ("op", "("):
            self.take()
            n = self.or_()
            self.take(")")
            return n
        if tok[0] == "name":
            self.take()
            name = tok[1]
            if self.peek() == ("op", "("):
                if name not in FUNCS:
                    raise Unsupported(f"function {name}")
                self.take()
                args = [self.or_()]
                while self.peek() == ("op", ","):
                    self.take()
                    args.append(self.or_())
                self.take(")")
                return ("call", name, tuple(args))
            if name in SERIES:
                return ("var", name)
            raise Unsupported(f"name {name}")
        raise Unsupported(f"token {tok}")


def parse(text: str) -> tuple:
    return Parser(text).parse()


class Evaluator:
    def __init__(self, cols: dict[str, list[float]]):
        self.d = cols
        self.n = len(cols["c"])
        self.cache: dict[str, Any] = {}

    def series(self, name: str) -> list[float]:
        o, h, lo, c = self.d["o"], self.d["h"], self.d["l"], self.d["c"]
        return {"close": lambda: c, "open": lambda: o, "high": lambda: h, "low": lambda: lo,
                "volume": lambda: self.d["v"], "range": lambda: [a - b for a, b in zip(h, lo)],
                "body": lambda: [abs(a - b) for a, b in zip(c, o)],
                "upper_wick": lambda: [a - max(b, x) for a, b, x in zip(h, o, c)],
                "lower_wick": lambda: [min(b, x) - a for a, b, x in zip(lo, o, c)]}[name]()

    def const(self, node: tuple) -> float:
        if node[0] != "num":
            raise Unsupported("period must be a number")
        return node[1]

    def ev(self, node: tuple) -> Any:
        key = repr(node)
        if key in self.cache:
            return self.cache[key]
        k = node[0]
        if k == "num":
            out: Any = node[1]
        elif k == "var":
            out = self.series(node[1])
        elif k == "lag":
            out = R.lag(self.arr(self.ev(node[1])), node[2])
        elif k == "call":
            out = self.call(node[1], node[2])
        elif k == "bin":
            a, b = self.ev(node[2]), self.ev(node[3])
            f: Callable[[float, float], float] = {"+": lambda x, y: x + y, "-": lambda x, y: x - y,
                                                  "*": lambda x, y: x * y, "/": R.div}[node[1]]
            if not isinstance(a, list) and not isinstance(b, list):
                out = f(a, b)
            else:
                out = [f(x, y) if R.fin(x) and R.fin(y) else NAN for x, y in zip(self.arr(a), self.arr(b))]
        elif k == "cmp":
            a, b = self.arr(self.ev(node[2])), self.arr(self.ev(node[3]))
            op = node[1]
            if op == "xa":
                out = R.cross_above(a, b)
            elif op == "xb":
                out = R.cross_below(a, b)
            else:
                f2 = {">": lambda x, y: x > y, "<": lambda x, y: x < y, ">=": lambda x, y: x >= y,
                      "<=": lambda x, y: x <= y, "==": lambda x, y: abs(x - y) <= 1e-9 * max(1.0, abs(x), abs(y)),
                      "!=": lambda x, y: x != y}[op]
                out = [R.fin(x) and R.fin(y) and f2(x, y) for x, y in zip(a, b)]
        elif k in ("and", "or"):
            a, b = self.ev(node[1]), self.ev(node[2])
            out = [(x and y) if k == "and" else (x or y) for x, y in zip(a, b)]
        else:
            raise Unsupported(k)
        self.cache[key] = out
        return out

    def arr(self, v: Any) -> list[float]:
        if isinstance(v, list):
            return [float(x) for x in v]
        return [float(v)] * self.n

    def call(self, name: str, args: Sequence[tuple]) -> list[float]:
        d = self.d
        if name == "abs":
            return [abs(x) if R.fin(x) else NAN for x in self.arr(self.ev(args[0]))]
        if name in ("rsi", "atr", "adx", "cci", "mfi", "roc") and len(args) == 1:
            n = int(self.const(args[0]))
            return {"rsi": lambda: R.rsi(d["c"], n), "atr": lambda: R.atr(d["h"], d["l"], d["c"], n),
                    "adx": lambda: R.dmi(d["h"], d["l"], d["c"], n)[0], "cci": lambda: R.cci(d["h"], d["l"], d["c"], n),
                    "mfi": lambda: R.mfi(d["h"], d["l"], d["c"], d["v"], n), "roc": lambda: R.roc(d["c"], n)}[name]()
        if name == "pct_b":
            n = int(self.const(args[0]))
            k = self.const(args[1]) if len(args) > 1 else 2.0
            return R.pctb(d["c"], n, k)
        if name in ("sma", "ema", "wma", "hma", "vwma", "max", "min", "sum"):
            if len(args) == 1:
                x, n = d["c"], int(self.const(args[0]))
            elif len(args) == 2:
                x, n = self.arr(self.ev(args[0])), int(self.const(args[1]))
            else:
                raise Unsupported(f"{name} arity")
            return {"sma": lambda: R.sma(x, n), "ema": lambda: R.ema(x, n), "wma": lambda: R.wma(x, n),
                    "hma": lambda: R.hma(x, n), "vwma": lambda: R.vwma(x, d["v"], n),
                    "max": lambda: R.highest(x, n), "min": lambda: R.lowest(x, n),
                    "sum": lambda: R.rsum(x, n)}[name]()
        raise Unsupported(f"{name} with {len(args)} args")


def signal(text: str, cols: dict[str, list[float]]) -> list[bool]:
    ev = Evaluator(cols)
    out = ev.ev(parse(text))
    if not isinstance(out, list) or (out and not isinstance(out[0], bool)):
        raise Unsupported("rule is not a condition")
    return out


# --------------------------------------------------------------------------- structural extraction

SYNONYMS: dict[str, tuple[str, ...]] = {
    "sma": ("sma",), "ema": ("ema",), "wma": ("wma",), "hma": ("hma",), "rsi": ("rsi", "ift_rsi", "laguerre_rsi"),
    "atr": ("atr", "tr"), "adx": ("adx",), "macd": ("macd", "macd_signal", "macd_hist"), "vwma": ("vwma",),
    "vwap": ("vwap", "rvwap", "anchored_vwap"), "cci": ("cci",), "mfi": ("mfi",), "obv": ("obv",),
    "kama": ("kama",), "supertrend": ("supertrend", "supertrend_dir"), "psar": ("psar",), "sar": ("psar",),
    "pct_b": ("pctb",), "cmf": ("cmf",), "tsi": ("tsi",), "trix": ("trix",), "cmo": ("cmo",), "dpo": ("dpo",),
    "uo": ("uo",), "rvi": ("rvi", "rvi_signal"), "kst": ("kst",), "stc": ("stc",), "nvi": ("nvi",), "pvi": ("pvi",),
    "ad_line": ("ad",), "willr": ("willr",), "williams": ("willr",), "stoch": ("stoch_k", "stoch_d"),
    "stochrsi": ("stochrsi_k", "stochrsi_d"), "aroon": ("aroon_up", "aroon_down"), "donchian": ("highest", "lowest"),
    "keltner": ("kc_upper", "kc_lower"), "ichimoku": ("midpoint", "senkou_a", "senkou_b"), "hv": ("hv",),
    "linreg": ("linreg", "slope"), "zscore": ("zscore",), "z_score": ("zscore",), "mcginley": ("mcginley",),
    "dema": ("dema",), "tema": ("tema",), "vortex": ("vi_plus", "vi_minus"), "chop": ("chop",), "ulcer": ("ulcer",),
    "fisher": ("fisher",), "max": ("highest", "max", "cummax", "swing_high"), "min": ("lowest", "min", "cummin",
                                                                                    "swing_low"),
    "roc": ("roc", "change"), "er": ("er",), "hurst": ("hurst",), "kalman": ("kalman",),
}


def spec_tokens(text: str) -> dict[str, Any]:
    """Indicator families and numeric constants a faithful translation should carry."""
    low = text.lower()
    fams = sorted({k for k in SYNONYMS if re.search(r"(?<![a-z_])" + re.escape(k) + r"(?![a-z])", low)})
    nums: list[float] = []
    text = re.sub(r"\b\d{1,2}:\d{2}\b", " ", text)  # clock times (session windows) are not parameters
    text = re.sub(r"\(\s*\d+m\s*\)|\b\d+m chart\b", " ", text)  # bar-size annotations such as Close(15m)
    for m in re.finditer(r"(?<![A-Za-z_\d.])(\d+(?:\.\d+)?)(%|x)?", text):
        v = float(m.group(1))
        before = text[max(0, m.start() - 2):m.start()]
        if before.endswith("t-") and v == 1:  # [t-1] in a cross definition becomes cross_*()/[1]
            continue
        nums.append(v / 100 if m.group(2) == "%" else v)
    return {"families": fams, "numbers": nums}


def number_present(v: float, pool: Sequence[float]) -> bool:
    cands = {v, v * 100, v / 100, 1 + v, 1 - v, v - 1}
    return any(abs(p - c) <= 1e-9 * max(1.0, abs(c)) for p in pool for c in cands)


__all__ = ["Evaluator", "SYNONYMS", "Unsupported", "number_present", "parse", "signal", "spec_tokens"]
