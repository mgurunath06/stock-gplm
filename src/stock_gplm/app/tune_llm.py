"""LLM side of strategy fine-tuning: prompts, provider calls, strict validation of proposed edits.

What is sent (owner approved aggregate-only sharing 2026-11-15): the strategy's DSL text, its exit settings,
and AGGREGATE statistics of its TUNE-window trades (counts, win rates, averages per feature bucket). Never
candles, prices, stock-level rows, keys - and never anything from the CONFIRM window.

Edits allowed ("fine-tune, not change radically", owner 2026-11-22), at most MAX_EDITS per variant:
  * add_filter  - one or two extra AND conditions on the entry;
  * entry       - a light rewrite of the entry (numbers / one clause), similarity >= MIN_SIMILARITY and the
                  owner liquidity floor kept verbatim;
  * exit        - rr, max_swing_pct, lookback, max_hold_bars within fixed ranges.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from typing import Any, Mapping, Sequence

from ..llm import envfile
from ..llm.health import _api_root, _headers, _request
from ..llm.registry import Provider

MAX_EDITS = 2
MIN_SIMILARITY = 0.75
MAX_FILTER_NODES = 30
DENY_FUNCS = {"ref_symbol", "feature", "meta_prob", "knn_analogue", "hmm_bull_prob", "dtw_distance",
              "adf_stat", "hurst"}  # unavailable data or too slow for pooled tuning
EXIT_RANGES = {"rr": (0.3, 4.0), "max_swing_pct": (0.005, 0.08), "lookback": (3, 60), "max_hold_bars": (2, 120),
               "trail_atr": (1.0, 6.0)}


# ------------------------------------------------------------------ providers
class LLMError(RuntimeError):
    pass


def call(p: Provider, model: str, system: str, prompt: str, *, max_tokens: int = 3000,
         temperature: float = 0.5) -> dict[str, Any]:
    """One chat call. Returns {text, in_tokens, out_tokens, usd, seconds}. Keys never leave this function."""
    import os
    envfile.load()
    key = os.environ.get(p.api_key_env) if p.api_key_env else None
    if not p.local and not key:
        raise LLMError(f"{p.api_key_env} not set")
    t0 = time.monotonic()
    if p.protocol == "ollama":
        body: dict[str, Any] = {"model": model, "stream": False, "format": "json", "keep_alive": "30m",
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                                "options": {"num_ctx": 16384, "temperature": temperature, "num_predict": max_tokens}}
        # Always off: qwen3.6 (and other thinking models) otherwise spend the whole num_predict on thinking and
        # return empty content (2026-11-22: 23 of 24 calls empty). Models without the flag answer 400 -> retried.
        body["think"] = False
        code, r = _request("POST", p.base_url.rstrip("/") + "/api/chat", {}, body, max(p.timeout_seconds, 600))
        if code == 400 and "think" in str(r):
            body.pop("think", None)
            code, r = _request("POST", p.base_url.rstrip("/") + "/api/chat", {}, body, max(p.timeout_seconds, 600))
        if code != 200 or not isinstance(r, dict):
            raise LLMError(f"ollama HTTP {code}: {str(r)[:300]}")
        text, tin, tout = r["message"]["content"], int(r.get("prompt_eval_count", 0)), int(r.get("eval_count", 0))
    elif p.protocol == "anthropic":
        body = {"model": model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
                "messages": [{"role": "user", "content": prompt}]}
        code, r = _request("POST", _api_root(p) + "/messages", _headers(p, key), body, max(p.timeout_seconds, 180))
        if code != 200 or not isinstance(r, dict):
            raise LLMError(f"{p.name} HTTP {code}: {str(r)[:300].replace(key or '#', '***')}")
        text = "".join(c.get("text", "") for c in r.get("content", []) if isinstance(c, dict))
        u = r.get("usage", {})
        tin, tout = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
    else:
        body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        code, r = _request("POST", _api_root(p) + "/chat/completions", _headers(p, key), body,
                           max(p.timeout_seconds, 180))
        if code != 200 or not isinstance(r, dict):
            raise LLMError(f"{p.name} HTTP {code}: {str(r)[:300].replace(key or '#', '***')}")
        text = r["choices"][0]["message"]["content"] or ""
        u = r.get("usage", {})
        tin, tout = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
    price = p.pricing or {}
    usd = 0.0 if p.local else (tin * float(price.get("input_per_1k", 0)) + tout * float(price.get("output_per_1k", 0))) / 1000
    return {"text": text, "in_tokens": tin, "out_tokens": tout, "usd": round(usd, 5),
            "seconds": round(time.monotonic() - t0, 1)}


def parse_json(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise LLMError("no JSON object in reply")
    return json.loads(text[a:b + 1])


# ------------------------------------------------------------------ prompts
SYSTEM = (
    "You are a careful quantitative trading researcher fine-tuning an EXISTING rule-based NSE cash-equity strategy. "
    "Goals: mainly raise the recency-weighted WIN RATE towards 70-80% with profit factor >= 1.3 after costs; "
    "secondly find more PROFITABLE versions at any win rate. Keep enough trades. Recent trades matter most. Make SMALL, plausible, economically sensible edits - never "
    "replace the strategy's idea. Prefer filters that remove the losing contexts shown in the loser analysis, and "
    "exit changes (lower rr = closer target = higher win rate, but watch profit factor). Avoid thresholds that keep "
    "only a tiny fraction of trades (overfitting). Reply with ONE JSON object only, no prose outside it.")

OUTPUT_SCHEMA = (
    '{"variants": [{"label": "<= 6 words, readable", "aim": "win" or "profit", "rationale": "one sentence", '
    '"add_filter": "<DSL boolean condition, ANDed onto the entry>" or null, '
    '"entry": "<full lightly edited entry>" or null, '
    '"exit": {"rr": number, "max_swing_pct": number|null, "lookback": int, "max_hold_bars": int, '
    '"trail_atr": number|null} or null}]}')


def _fmt_stats(s: Mapping[str, Any]) -> str:
    if not s or not s.get("trades"):
        return "no trades"
    return (f"trades {s['trades']} (effective {s['n_eff']:.0f}), recent-weighted win {s['win_w']:.1%} / "
            f"avg {100 * s['avg_w']:+.2f}% / PF {min(s['pf_w'], 99):.2f}; plain win {s['win']:.1%} / "
            f"avg {100 * s['avg']:+.2f}%; positive periods {s.get('periods_pos', 0)}/{s.get('periods', 0)}")


def build_prompt(strategy: Mapping[str, Any], tf: str, base: Mapping[str, Any], analysis: str,
                 history: Sequence[Mapping[str, Any]], k: int, dsl_help: str, stage: str) -> str:
    ex = strategy["exit"]
    sw = ex.get("swing_stop") or {}
    hist = "\n".join(
        f"- [{h['label']}] {h['change']} -> {_fmt_stats(h['tune'])}" for h in history[-25:]) or "- none yet"
    return f"""STRATEGY "{strategy['name']}" ({strategy['id']}), timeframe {tf}, direction {strategy.get('direction', 'long')}
Description: {strategy.get('description', '')}
Entry (DSL): {strategy['entry']}
Exit: stop {100 * float(sw.get('buffer', 0.002)):.1f}% beyond the last swing (lookback {sw.get('lookback', 20)} bars, \
max swing distance {sw.get('max_swing_pct')}), target {sw.get('rr', 2.0)} x risk, time exit after {ex.get('max_hold_bars')} bars\
{'; intraday square-off 15:15, no entries after 14:45' if tf != '1d' else ''}{'; exit rule: ' + ex['when'] if ex.get('when') else ''}

CURRENT RESULT (tuning window, pooled over many NSE stocks, after costs): {_fmt_stats(base)}

LOSER ANALYSIS (tuning window; feature value at the signal bar; buckets are quintiles of the strategy's own trades):
{analysis}

ALREADY TRIED (tuning-window results; do not repeat, learn from them):
{hist}

TASK ({stage}): propose {k} DIFFERENT variants: about two thirds aiming at a HIGHER WIN RATE (target 70-80%,
PF >= 1.3) and the rest aiming at HIGHER PROFIT per trade (higher average / PF, any win rate). Set "aim" on each. Each variant may make at most {MAX_EDITS} edits in total
(each extra AND condition = 1 edit, an exit change = 1 edit, a light entry rewrite = 1 edit).
Use EITHER add_filter (preferred: only the new condition) OR entry (a light rewrite), never the same change in both.
Price-unit values (atr, stdev, macd, distances between averages) must be divided by close so one threshold fits
all stocks, e.g. atr(14) / close < 0.03 ; stdev(close, 20) / close < 0.02.
Rules: keep the liquidity floor clause exactly; use only the DSL below; no ref_symbol(), feature(); bench(x) = x on the
official Nifty 50 (daily only, data from 2024 - avoid on intraday);
filters must be causal expressions on this timeframe (x[1] = previous bar); exit ranges rr 0.3-4,
max_swing_pct 0.005-0.08 or null, lookback 3-60, max_hold_bars 2-120, trail_atr 1-6 or null (trailing stop that
many ATR(14) below the highest price since entry - use it when losers were often up 1-2% before losing).
Output JSON exactly like: {OUTPUT_SCHEMA}

{dsl_help}
"""


def dsl_help(intraday: bool) -> str:
    """Compact DSL reference: series + most useful functions (full list is too long for a 14B model)."""
    from .strategy_dsl import FUNCTIONS, SERIES
    useful = ["abs", "adx", "atr", "barssince", "bb_lower", "bb_upper", "bb_width", "between", "cci", "change", "cmf",
              "count", "cross_above", "cross_below", "day_of_week", "ema", "highest", "htf", "iff", "lowest", "macd",
              "macd_hist", "macd_signal", "max", "median", "mfi", "min", "obv", "pctb", "percentrank", "plus_di",
              "minus_di", "roc", "rsi", "rising", "falling", "slope", "sma", "stdev", "supertrend", "vwap", "wma",
              "williams_r", "stoch_k", "stoch_d", "hv", "er", "chop", "nz", "prev_day_high", "prev_day_low",
              "prev_day_close", "kc_upper", "kc_lower", "donchian_high", "donchian_low", "zscore"]
    if intraday:
        useful += ["minute_of_session", "bar_of_session", "session_open", "session_high", "session_low", "day_open",
                   "or_high", "or_low", "slot_rvol", "avg_day_volume", "anchored_vwap"]
    lines = [f"- {k}: {FUNCTIONS[k][2]}" for k in useful if k in FUNCTIONS]
    return ("DSL: operators + - * / > < >= <= == != and or not; x[n] = value n bars ago; series: "
            + ", ".join(sorted(SERIES)) + ". Functions:\n" + "\n".join(lines)
            + '\nExamples: close > ema(close, 200) ; rsi(close, 14) < 70 ; volume > 1.5 * median(volume, 20) ; '
            + 'atr(14) / close < 0.03 (atr takes ONLY the length) ; htf(close > ema(close, 50), "1d")' + (' ; minute_of_session >= 30' if intraday else ''))


# ------------------------------------------------------------------ validation
def _clauses(cond: str) -> int:
    """Top-level AND-ed clauses of a condition (rough edit count)."""
    depth, n, i, s = 0, 1, 0, cond.lower()
    while i < len(s):
        c = s[i]
        depth += c == "("
        depth -= c == ")"
        if depth == 0 and s.startswith(" and ", i):
            n += 1
            i += 5
            continue
        i += 1
    return n


def dsl_balanced(s: str) -> bool:
    depth = 0
    for c in s:
        depth += (c == "(") - (c == ")")
        if depth < 0:
            return False
    return depth == 0


_ATR_SERIES_ARG = re.compile(r"\batr\(\s*(?:close|high|low|open|hl2|hlc3|ohlc4)\s*,\s*(\d+)\s*\)")


def repair_dsl(s: str) -> str:
    """Fix common small-model slips that would otherwise waste a proposal: atr(close, 14) -> atr(14)."""
    return _ATR_SERIES_ARG.sub(r"atr(\1)", s)


def liquidity_floor(entry: str) -> str:
    m = re.match(r"\s*(median\(volume \* close, 20\)\[1\] >= \d+ and close\[1\] >= \d+)", entry)
    return m.group(1) if m else ""


def validate_variant(parent: Mapping[str, Any], v: Mapping[str, Any], tf: str) -> tuple[dict[str, Any] | None, str]:
    """Return (doc-like {entry, exit, change, edits}, '') or (None, reason)."""
    from . import strategy_dsl as dsl
    from .strategy_library import StrategyConfigError, validate

    entry = parent["entry"]
    edits, change = 0, []
    v = dict(v)
    for key in ("entry", "add_filter"):
        if v.get(key):
            v[key] = repair_dsl(str(v[key]))
    new_entry = str(v.get("entry") or "").strip()
    if new_entry.startswith(entry) and new_entry != entry:
        # Model wrote "original and (filter)" as a full entry: treat the tail as the filter (once).
        tail = new_entry[len(entry):].strip()
        if tail.lower().startswith("and "):
            tail = tail[4:].strip()
            if tail.startswith("(") and tail.endswith(")") and dsl_balanced(tail[1:-1]):
                tail = tail[1:-1].strip()
            f = str(v.get("add_filter") or "").strip()
            v["add_filter"] = f if f and (f == tail or f in tail) else (f"{f} and {tail}" if f else tail)
            if f and f in tail and f != tail:
                v["add_filter"] = tail
        v["entry"] = None
    elif new_entry and v.get("add_filter") and str(v["add_filter"]).strip() in new_entry:
        v["add_filter"] = None  # filter already inside the rewritten entry
    if v.get("entry") and str(v["entry"]).strip() != entry:
        new = str(v["entry"]).strip()
        floor = liquidity_floor(entry)
        if floor and floor not in new:
            return None, "entry rewrite dropped the liquidity floor"
        sim = difflib.SequenceMatcher(None, entry, new).ratio()
        if sim < MIN_SIMILARITY:
            return None, f"entry rewrite too different (similarity {sim:.2f})"
        entry, edits = new, edits + 1
        change.append(f"entry -> {new}")
    if v.get("add_filter"):
        f = str(v["add_filter"]).strip().rstrip(";")
        try:
            node = dsl.parse(f)
        except dsl.DSLError as exc:
            return None, f"filter does not parse: {exc}"
        if dsl.node_size(node) > MAX_FILTER_NODES:
            return None, "filter too complex"
        edits += _clauses(f)
        entry = f"{entry} and ({f})"
        change.append(f"AND {f}")
    ex = json.loads(json.dumps(parent["exit"]))
    if v.get("exit"):
        e = v["exit"]
        sw = dict(ex.get("swing_stop") or {"buffer": 0.002, "rr": 2.0, "lookback": 20})
        parts = []
        for key in ("rr", "max_swing_pct", "lookback"):
            if key in e and e[key] != sw.get(key):
                if e[key] is None:
                    if key == "max_swing_pct":
                        sw.pop(key, None)
                        parts.append("max swing none")
                    continue
                lo, hi = EXIT_RANGES[key]
                val = min(max(float(e[key]), lo), hi)
                sw[key] = int(round(val)) if key == "lookback" else round(val, 4)
                parts.append(f"{key} {sw[key]}")
        if "trail_atr" in e and e["trail_atr"] != ex.get("trail_atr"):
            if e["trail_atr"] in (None, "", "null"):
                if ex.pop("trail_atr", None) is not None:
                    parts.append("no trailing stop")
            else:
                lo, hi = EXIT_RANGES["trail_atr"]
                try:
                    ex["trail_atr"] = round(min(max(float(e["trail_atr"]), lo), hi), 2)
                    parts.append(f"trail {ex['trail_atr']:g} ATR")
                except (TypeError, ValueError):
                    pass
        if e.get("max_hold_bars") and int(e["max_hold_bars"]) != ex.get("max_hold_bars"):
            lo, hi = EXIT_RANGES["max_hold_bars"]
            ex["max_hold_bars"] = int(min(max(int(e["max_hold_bars"]), lo), hi))
            parts.append(f"hold {ex['max_hold_bars']}")
        if parts:
            ex["swing_stop"] = sw
            edits += 1
            change.append("exit " + ", ".join(parts))
    if not change:
        return None, "no change"
    if edits > MAX_EDITS:
        return None, f"{edits} edits > {MAX_EDITS}"
    doc = {**{k: parent[k] for k in parent if k not in ("entry", "exit")}, "entry": entry, "exit": ex}
    doc["applicability"] = {**(parent.get("applicability") or {}), "timeframes": [tf]}
    try:
        enode, _, _ = validate(doc)
    except StrategyConfigError as exc:
        return None, f"invalid: {exc}"
    bad = dsl.functions_used(enode) & DENY_FUNCS
    if bad:
        return None, f"uses unavailable functions {sorted(bad)}"
    return {"entry": entry, "exit": ex, "change": "; ".join(change), "edits": edits}, ""
