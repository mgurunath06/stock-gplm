"""Local-LLM strategy proposer (research ML method `llm`). LOCAL Ollama only - never a *-cloud model.

Per lane (stock x timeframe) a local model proposes entry rules in the strategy DSL with a one-line reason.
The backtest engine is the judge: every rule is scored with the genetic-search fitness (Evolver.fitness) on the
DISCOVERY segment only; results go back to the model for the next round. The validation segment and the test
period are never shown to the model. Selected rules then go through the normal research validation
(BH multiple testing over the lane family) exactly like evolved rules - research.py does that part.

Model: env GPLM_LLM_MODEL (default qwen3-coder:30b); Ollama at env OLLAMA_URL (default 127.0.0.1:11434).
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any

import numpy as np

from . import strategy_dsl as dsl
from .strategy_evolve import _EXITS, Evolver, Genome, _jaccard

LLM_CONTRACT = "stock-gplm.llm-propose.v1"
DEFAULT_MODEL = "qwen3-coder:30b"
ROUNDS = {"quick": (2, 6), "standard": (3, 8), "deep": (4, 10)}  # rounds, ideas per round

_EXAMPLES = [
    "cross_above(close, vwap) and volume > 1.5 * sma(volume, 20)",
    "low <= ema(close, 50) and close > ema(close, 50) and ema(close, 50) > ema(close, 200)",
    "close > or_high(15) and slot_rvol(10) > 2",
    "rsi(close, 2) < 10 and close > sma(close, 200)",
    "(high - low) == lowest(high - low, 7) and close > ema(close, 20)",
]


def _model() -> str:
    m = os.environ.get("GPLM_LLM_MODEL", DEFAULT_MODEL)
    if m.endswith("-cloud") or ":cloud" in m:
        raise ValueError(f"refusing cloud model {m}: local models only")
    return m


def chat(system: str, user: str, model: str, timeout: int = 900) -> str:
    url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"
    body: dict[str, Any] = {"model": model, "stream": False, "format": "json", "keep_alive": "30m",
                            "options": {"temperature": 0.6, "num_ctx": int(os.environ.get("GPLM_LLM_CTX", "16384"))},
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if "qwen3" in model and "coder" not in model:
        body["think"] = False
    for attempt in (1, 2):
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())["message"]["content"]
        except urllib.error.HTTPError as exc:
            if attempt == 1 and "think" in body and exc.code == 400:
                body.pop("think")
                continue
            raise
    return ""


def _functions(intraday: bool) -> str:
    skip = set(dsl.LAZY) | set(dsl.BENCHMARK_FUNCS) | (set() if intraday else set(dsl.INTRADAY_ONLY))
    return "\n".join(f"- {n}: {d}" for n, (_, _, d) in sorted(dsl.FUNCTIONS.items()) if n not in skip)


def lane_facts(ev: Evolver, symbol: str, tf: str) -> dict[str, Any]:
    """Descriptive statistics of the DISCOVERY segment only."""
    a, b = ev.seg
    ctx = ev.li.ctx

    def arr(expr: str) -> np.ndarray | None:
        try:
            return np.asarray(dsl.evaluate(dsl.parse(expr), ctx), float)[a:b]
        except (dsl.DSLError, dsl.MissingData):
            return None

    c = arr("close")
    r = np.diff(np.log(c)) if c is not None and len(c) > 2 else np.array([])
    pct = lambda x: None if x is None else round(float(np.nanmean(x)) * 100, 1)  # noqa: E731
    f: dict[str, Any] = {"stock": symbol, "timeframe": tf, "bars": int(b - a),
                         "period": [ev.li.market.label(a), ev.li.market.label(b - 1)],
                         "total_return_pct": round(float(c[-1] / c[0] - 1) * 100, 1) if c is not None else None,
                         "bar_return_sd_pct": round(float(np.std(r)) * 100, 3) if r.size else None,
                         "lag1_autocorr": round(float(np.corrcoef(r[1:], r[:-1])[0, 1]), 3) if r.size > 10 else None,
                         "pct_bars_close_above_ema50": pct(arr("close > ema(close, 50)")),
                         "pct_bars_close_above_ema200": pct(arr("close > ema(close, 200)")),
                         "atr_pct_of_price": pct(arr("atr(14) / close"))}
    if ctx.intraday:
        f["pct_bars_close_above_vwap"] = pct(arr("close > vwap"))
        g = arr("abs(day_open - prev_day_close) / prev_day_close")
        f["avg_abs_gap_pct"] = pct(g)
    return f


_PROBES = [  # simple events measured on the discovery segment -> the stock's "behaviour fingerprint"
    "low <= ema(close, 20) and close > ema(close, 20)", "low <= ema(close, 50) and close > ema(close, 50)",
    "low <= ema(close, 200) and close > ema(close, 200)", "cross_above(close, ema(close, 50))",
    "cross_above(ema(close, 9), ema(close, 21))", "close > highest(high, 20)[1]", "close > highest(high, 55)[1]",
    "close < lowest(low, 20)[1]", "low < lowest(low, 20)[1] and close > lowest(low, 20)[1]",
    "rsi(close, 2) < 10", "rsi(close, 2) > 90", "rsi(close, 14) < 30", "zscore(close, 20) < -2",
    "pctb(close, 20, 2) < 0", "(high - low) == lowest(high - low, 7)", "high[1] < high[2] and low[1] > low[2]",
    "falling(close, 3)", "rising(close, 3)", "close > high[1]", "close < low[1]",
    "volume > 2 * sma(volume, 20) and close > open", "volume > 2 * sma(volume, 20) and close < open",
    "supertrend_dir(10, 3) == 1 and supertrend_dir(10, 3)[1] == -1",
    "macd_hist(close, 12, 26, 9) > 0 and macd_hist(close, 12, 26, 9)[1] <= 0",
    "low <= prev_day_low and close > prev_day_low", "cross_above(close, prev_day_high)",
    "(close - low) / (high - low) < 0.2", "(close - low) / (high - low) > 0.8",
    # intraday only (rejected on daily lanes)
    "low < vwap and close > vwap", "cross_below(close, vwap)", "cross_above(close, or_high(15))",
    "cross_above(close, or_high(30))", "slot_rvol(10) > 2 and close > open", "slot_rvol(10) > 2 and close < open",
    "minute_of_session <= 30 and close > open", "minute_of_session <= 30 and close < open",
    "minute_of_session >= 300 and close > open", "day_open > prev_day_high", "day_open < prev_day_low",
]


def fingerprint(ev: Evolver) -> dict[str, list[dict[str, Any]]]:
    """Net/excess per trade after each probe event (long, exit 1), discovery segment only."""
    rows = []
    for e in _PROBES:
        if ev._mask(e) is None:
            continue
        g = Genome(atoms=[e], direction="long", exit_idx=1)
        ev.fitness(g)
        st = g.stats
        if (st.get("trades") or 0) >= 8 and "mean_net" in st:
            rows.append({"event": e, "trades": st["trades"], "net_pct": round(st["mean_net"] * 100, 3),
                         "excess_pct": round(st["excess_mean"] * 100, 3)})
    rows.sort(key=lambda r: -r["excess_pct"])
    return {"strongest_after": rows[:10], "weakest_after": rows[-6:][::-1]}


def _prompt_system(intraday: bool) -> str:
    exits = "\n".join(f"  {i}: {json.dumps(e)}" for i, e in enumerate(_EXITS))
    return ("You are a quantitative researcher proposing LONG entry rules for one NSE cash-equity stock on one "
            "timeframe. Rules are written in a small expression language (DSL) evaluated bar by bar on completed "
            "bars only. Use ONLY these functions and the series open, high, low, close, volume; x[k] = value k bars "
            "ago; operators + - * / > < >= <= == and or not.\n" + _functions(intraday) +
            "\nExamples:\n" + "\n".join(f"  {e}" for e in _EXAMPLES) +
            "\nExit choices (by index):\n" + exits +
            "\nRules must fire often enough to trade (at least ~15 times in the period) but be selective. Costs are "
            "~0.2% per round trip, so tiny edges lose. Return JSON only: {\"rules\": [{\"entry\": \"<dsl>\", "
            "\"exit\": <index>, \"why\": \"<one line: the behaviour of THIS stock you are exploiting>\"}]}")


def propose(ev: Evolver, symbol: str, tf: str, budget: str = "quick", model: str | None = None) -> dict[str, Any]:
    model = model or _model()
    rounds, ideas = ROUNDS.get(budget, ROUNDS["quick"])
    facts = lane_facts(ev, symbol, tf)
    facts["behaviour_fingerprint"] = fingerprint(ev)  # what THIS stock did after simple events
    system = _prompt_system(bool(ev.li.ctx.intraday))
    tried: list[dict[str, Any]] = []
    genomes: dict[str, tuple[Genome, str]] = {}
    calls, errors = 0, 0
    for rnd in range(1, rounds + 1):
        if rnd == 1:
            user = (f"Stock facts (discovery period):\n{json.dumps(facts)}\n'behaviour_fingerprint' shows mean net "
                    "and excess return per trade (vs random entry) after simple events on THIS stock. Build on the "
                    "strongest ones (combine, add filters such as trend, volume or time of day), avoid the weakest. "
                    f"Propose {ideas} diverse rules.")
        else:
            ok = sorted([t for t in tried if t.get("trades")], key=lambda t: -(t.get("score") or -99))[:12]
            bad = [t for t in tried if t.get("error")][-6:]
            user = (f"Stock facts:\n{json.dumps(facts)}\nResults so far (net = mean net return per trade after "
                    f"costs; excess = vs a random-entry baseline with the same exit; halves = positive in both halves "
                    f"of the period):\n{json.dumps(ok)}\nRejected (fix syntax or fire more often):\n{json.dumps(bad)}\n"
                    f"Propose {ideas} rules: refine the best ones and add new ideas. Do not repeat a tried rule.")
        try:
            raw = chat(system, user, model)
            calls += 1
            rules = json.loads(raw).get("rules") or []
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, AttributeError) as exc:
            errors += 1
            tried.append({"round": rnd, "error": f"model call failed: {str(exc)[:120]}"})
            continue
        for rule in rules[: ideas + 2]:
            entry = re.sub(r"\s+", " ", str((rule or {}).get("entry") or "")).strip()
            if any(t.get("entry") == entry for t in tried):
                continue  # repeated idea: free, not re-scored
            why = str((rule or {}).get("why") or "")[:200]
            try:
                xi = int((rule or {}).get("exit", 2))
            except (TypeError, ValueError):
                xi = 2
            xi = min(max(xi, 0), len(_EXITS) - 1)
            rec: dict[str, Any] = {"round": rnd, "entry": entry, "exit": xi}
            try:
                dsl.parse(entry)
                if ev._mask(entry) is None:
                    raise dsl.DSLError("cannot evaluate on this lane")
            except Exception as exc:  # any malformed model output is just a rejected idea
                rec["error"] = str(exc)[:160]
                tried.append(rec)
                continue
            g = Genome(atoms=[entry], direction="long", exit_idx=xi)
            fit = ev.fitness(g)
            st = g.stats
            rec.update(trades=st.get("trades"), score=round(fit, 3) if math.isfinite(fit) else None,
                       net_pct=round(st["mean_net"] * 100, 3) if "mean_net" in st else None,
                       excess_pct=round(st["excess_mean"] * 100, 3) if "excess_mean" in st else None,
                       halves=st.get("halves_positive"))
            tried.append(rec)
            genomes.setdefault(g.key(), (g, why))
    # selection: same gates as the genetic search
    a, b = ev.seg
    chosen: list[Genome] = []
    whys: dict[str, str] = {}
    fired: list[np.ndarray] = []
    for g, why in sorted(genomes.values(), key=lambda x: -x[0].fitness):
        if not math.isfinite(g.fitness) or g.fitness <= 0 or not g.stats.get("halves_positive"):
            continue
        if (g.stats.get("mean_net") or 0.0) <= 0:
            continue
        m = ev.mask(g)[a:b]
        if any(_jaccard(m, f) > 0.7 for f in fired):
            continue
        chosen.append(g)
        whys[g.key()] = why
        fired.append(m)
        if len(chosen) >= ev.keep:
            break
    return {"contract": LLM_CONTRACT, "model": model, "rounds": rounds, "calls": calls, "errors": errors,
            "evaluated": len(genomes), "selected": chosen, "why": whys, "facts": facts, "log": tried}


def llm_doc(doc: dict[str, Any], why: str, model: str) -> dict[str, Any]:
    """Relabel a genome_doc as an LLM proposal (same id scheme, same validation path)."""
    d = dict(doc)
    d["name"] = d["name"].replace("GP ", "LLM ", 1)
    d["category"] = "ML: local LLM proposal"
    d["description"] = why
    prov = dict(d.get("provenance") or {})
    prov.update(method="llm", contract=LLM_CONTRACT, model=model, why=why)
    d["provenance"] = prov
    return d
