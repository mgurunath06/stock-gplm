"""AI assistance for the DSL strategy library: translate prose into definitions, propose new hypotheses.

Governance (same gates as :mod:`stock_gplm.app.ai`): ``ai.enabled``; remote providers need
``ai.allow_remote`` and ``--allow-remote``; keys only from the provider's environment variable; spend
limits; every call is audited in ``api_calls``. What is sent: the DSL function list, the strategy
prose to translate, or **aggregate** results (rule text and summary metrics such as trade count, mean
net return, win rate, p-value). Never candles, prices, private files or credentials — the owner
approved aggregate-only sharing on 2026-11-15. Replies are parsed as JSON, validated by the library
schema and parser, and saved under ``<home>/strategy-library/discovered/ai``; they are then backtested
by the same engine as every other strategy. Nothing in a reply is executed as code.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from ..llm.redaction import market_rows, redact
from .ai import AIRefused, _extract_text, _http, _spent
from .db import AuditDB, sha, utc_now
from .strategy_dsl import FUNCTIONS, SERIES
from .strategy_library import StrategyConfigError, StrategyLibrary, normalize, validate

Transport = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]

_SCHEMA_HELP = (
    'Each strategy: {"name": str, "description": str, "direction": "long"|"short", "entry": "<DSL expression>", '
    '"exit": {"max_hold_bars": int, "stop_atr": number|null, "target_atr": number|null, "trail_atr": number|null, '
    '"when": "<DSL expression>"|null}, "params": {"<name>": {"default": number, "grid": [numbers]}}, '
    '"applicability": {"symbols": "*" or [SYMBOL...], "timeframes": ["1d"|"1w"|"15m"|...]}, '
    '"assumptions": [str], "fidelity": "EXACT"|"APPROXIMATION"}. Parameters are referenced as $name.')


def dsl_reference() -> str:
    funcs = "\n".join(f"- {k}: {v[2]}" for k, v in sorted(FUNCTIONS.items()))
    return ("Strategy DSL: expressions over bars; operators + - * / > < >= <= == != and or not; x[n] = n bars ago; "
            f"series: {', '.join(sorted(SERIES))}. Functions (all causal):\n{funcs}")


def _call(db: AuditDB, cfg: Mapping[str, Any], provider: str, prompt: str, purpose: str, *, allow_remote: bool,
          allow_unknown_cost: bool, transport: Transport | None, environ: Mapping[str, str] | None) -> tuple[str, str]:
    env = environ if environ is not None else os.environ
    if not cfg.get("enabled"):
        raise AIRefused("AI assistance is disabled (ai.enabled=false)")
    p = (cfg.get("providers") or {}).get(provider)
    if not p or not p.get("model"):
        raise AIRefused(f"provider {provider!r} is not configured with a model")
    if not p.get("local"):
        if not cfg.get("allow_remote") or not allow_remote:
            raise AIRefused("remote providers need ai.allow_remote=true in config and --allow-remote on the command")
        if (p.get("usd_per_1k_input") is None or p.get("usd_per_1k_output") is None) and not allow_unknown_cost:
            raise AIRefused("provider prices are not configured; set them or pass --allow-unknown-cost")
        month = datetime.now(timezone.utc).strftime("%Y-%m-01")
        if _spent(db, month) >= float(cfg.get("monthly_usd_limit", 0)):
            raise AIRefused("monthly AI spend limit reached")
    headers = {"content-type": "application/json"}
    if p.get("api_key_env"):
        key = env.get(p["api_key_env"])
        if not key:
            raise AIRefused(f"environment variable {p['api_key_env']} is not set")
        if p["kind"] == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            headers["authorization"] = f"Bearer {key}"
    prompt, redactions = redact(prompt)
    if market_rows(prompt):
        raise AIRefused("the prompt looks like it contains market-data rows; refusing to send it")
    if p["kind"] == "anthropic":
        body: dict[str, Any] = {"model": p["model"], "max_tokens": 8000, "messages": [{"role": "user", "content": prompt}]}
    elif p["kind"] == "ollama":
        body = {"model": p["model"], "stream": False, "messages": [{"role": "user", "content": prompt}]}
    else:
        body = {"model": p["model"], "messages": [{"role": "user", "content": prompt}]}
    url = p["base_url"].rstrip("/") + p["path"]
    t0 = time.monotonic()
    ok, err, resp, text, tin, tout = True, None, {}, "", None, None
    try:
        resp = (transport or _http)(url, headers, body, 180.0)
        text, tin, tout = _extract_text(p["kind"], resp)
    except Exception as exc:
        ok, err = False, f"{type(exc).__name__}: {str(exc)[:300]}"
    cost = None
    if p.get("usd_per_1k_input") is not None and p.get("usd_per_1k_output") is not None and tin and tout:
        cost = tin / 1000 * float(p["usd_per_1k_input"]) + tout / 1000 * float(p["usd_per_1k_output"])
    req_id = resp.get("_request_id") or resp.get("id") or f"local-{uuid.uuid4().hex[:12]}"
    with db.tx() as c:
        c.execute("INSERT INTO api_calls (ts_utc, provider, model, endpoint, purpose, request_id, input_tokens, "
                  "output_tokens, cost_usd, cost_basis, latency_ms, success, error, run_id, subject) VALUES "
                  "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (utc_now(), provider, p["model"], url, purpose, req_id, tin, tout, cost,
                   "ESTIMATED_FROM_CONFIGURED_PRICES" if cost is not None else "UNKNOWN", round((time.monotonic() - t0) * 1000, 1),
                   int(ok), err, db.run_id, purpose[:60]))
    db.event("AI_REQUEST", {"provider": provider, "model": p["model"], "purpose": purpose, "request_id": req_id,
                            "success": ok, "error": err, "prompt_redactions": redactions,
                            "sent": "DSL reference + strategy text / aggregate metrics only"},
             level="INFO" if ok else "ERROR")
    if not ok:
        raise AIRefused(f"provider call failed: {err}")
    return text, f"{provider}:{p['model']}:{req_id}"


def _parse(text: str) -> list[dict[str, Any]]:
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        doc = json.loads(text[start:end])
    except ValueError as exc:
        raise AIRefused(f"reply is not JSON: {exc}") from exc
    items = doc.get("strategies") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise AIRefused("reply has no 'strategies' list")
    return [x for x in items if isinstance(x, dict)]


def _store(lib: StrategyLibrary, items: Sequence[Mapping[str, Any]], *, origin: str, source: str, extra: Mapping[str, Any],
           original_text: str | None = None, reference_id: str | None = None) -> dict[str, Any]:
    accepted, rejected = [], []
    keep = ("name", "description", "direction", "entry", "exit", "params", "applicability", "assumptions", "fidelity")
    for i, it in enumerate(items):
        doc = {k: it[k] for k in keep if k in it}
        doc.update(origin=origin, category=extra.get("category", "AI hypothesis"), style=str(it.get("style", "AI")),
                   requires=["OHLCV"], enabled=True,
                   provenance={"method": "llm", "source": source, "generated_utc": utc_now(), **dict(extra)})
        if original_text:
            doc["original_text"] = original_text
        if reference_id:
            doc["reference_id"] = reference_id
            doc["interpretation"] = f"AI{i + 1}"
        doc["id"] = ("AI-" if origin == "AI_GENERATED" else "TR-") + sha({"e": doc.get("entry"), "x": doc.get("exit"),
                                                                           "a": doc.get("applicability")})[:10]
        try:
            validate(normalize(doc))
            sid, created = lib.save_discovered(doc)
            accepted.append({"id": sid, "created": created, "name": doc.get("name"), "entry": doc.get("entry")})
        except (StrategyConfigError, KeyError, TypeError) as exc:
            rejected.append({"index": i, "name": it.get("name"), "error": str(exc)})
    return {"accepted": accepted, "rejected": rejected}


def translate(db: AuditDB, lib: StrategyLibrary, cfg: Mapping[str, Any], provider: str, text: str, *,
              timeframes: Sequence[str], symbols: Sequence[str] | None = None, reference_id: str | None = None,
              variants: int = 2, allow_remote: bool = False, allow_unknown_cost: bool = False,
              transport: Transport | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Translate a natural-language strategy into 1..variants DSL interpretations (original text kept)."""
    prompt = (dsl_reference() + "\n\nTranslate the trading strategy below into precise DSL definitions. Use only the "
              "listed functions. If it is ambiguous, return up to " + str(variants) + " distinct interpretations and list "
              "every assumption. If it needs data the DSL cannot express (options, order book, fundamentals, delivery), "
              "return {\"strategies\": [], \"blocked\": \"<reason>\"}.\n" + _SCHEMA_HELP +
              f"\nTimeframes: {list(timeframes)}. Symbols: {list(symbols) if symbols else '*'}.\n"
              "Return only {\"strategies\": [...]}.\n\nSTRATEGY TEXT:\n" + text)
    reply, source = _call(db, cfg, provider, prompt, "translate strategy text to DSL", allow_remote=allow_remote,
                          allow_unknown_cost=allow_unknown_cost, transport=transport, environ=environ)
    items = _parse(reply)
    for it in items:
        it.setdefault("applicability", {"symbols": list(symbols) if symbols else "*", "timeframes": list(timeframes)})
    origin = "CATALOGUE_DERIVED" if reference_id else "USER_DEFINED"
    return _store(lib, items[:variants], origin=origin, source=source, extra={"category": "AI-assisted translation"},
                  original_text=text, reference_id=reference_id)


def propose(db: AuditDB, lib: StrategyLibrary, cfg: Mapping[str, Any], provider: str,
            results: Sequence[Mapping[str, Any]], *, n: int, timeframes: Sequence[str], allow_remote: bool = False,
            allow_unknown_cost: bool = False, transport: Transport | None = None,
            environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Ask for new hypotheses given aggregate results (rule text + summary metrics; no market data)."""
    keep = ("symbol", "timeframe", "strategy_id", "entry_rule", "direction", "classification", "val_trades",
            "val_mean_net", "val_win_rate", "val_p_value")
    lines = [json.dumps({k: r.get(k) for k in keep}, default=str) for r in results[:60]]
    prompt = (dsl_reference() + "\n\nBelow are AGGREGATE backtest results of existing rules on Indian NSE cash "
              "equities (validation segment, after costs). Learn what works and what fails, then propose " + str(n) +
              " NEW, materially different, testable hypotheses (stock-specific when the evidence is stock-specific). "
              "Avoid duplicating listed rules.\n" + _SCHEMA_HELP + "\nAllowed timeframes: " + str(list(timeframes)) +
              "\nReturn only {\"strategies\": [...]}.\n\nRESULTS:\n" + "\n".join(lines))
    reply, source = _call(db, cfg, provider, prompt, "propose strategies from aggregate results",
                          allow_remote=allow_remote, allow_unknown_cost=allow_unknown_cost, transport=transport,
                          environ=environ)
    return _store(lib, _parse(reply)[:n], origin="AI_GENERATED", source=source,
                  extra={"category": "AI hypothesis (results-driven)", "results_sent": len(lines)})


__all__ = ["dsl_reference", "propose", "translate"]
