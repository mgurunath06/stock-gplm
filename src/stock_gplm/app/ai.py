"""Optional AI-assisted strategy hypotheses (never required, never trusted as code).

Governance, all enforced before any byte is sent:

* ``ai.enabled`` must be true and the provider must be configured; remote (non-local) providers
  additionally need ``ai.allow_remote`` *and* the ``--allow-remote`` flag on the command.
* Keys come only from the environment variable the provider names; they are never logged.
* Monthly and per-run spend limits are checked against the ``api_calls`` table. When a provider's
  prices are not configured, cost is recorded as ``NULL`` with basis ``UNKNOWN`` — never invented —
  and remote calls then need ``--allow-unknown-cost``.
* Only the feature catalogue (names, families, kinds), the symbol and the timeframe are sent: no
  candles, no private strategies, no credentials. :func:`stock_gplm.llm.redaction.redact` scrubs the
  prompt as a second line of defence.
* Replies are parsed as JSON and every proposed strategy goes through
  :func:`stock_gplm.app.library.validate`. Valid ones enter the library **inactive** with origin
  ``ai:<provider>``; they are backtested like any other hypothesis once a person activates them.
  Nothing in a reply is executed.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from ..llm.redaction import market_rows, redact
from .db import AuditDB, utc_now
from .library import Library, StrategyError, validate

Transport = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]


class AIRefused(RuntimeError):
    """The request was blocked by configuration, permissions or budget."""


def _http(url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=dict(headers), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL comes from local config
        out = json.loads(resp.read(4 * 1024 * 1024))
        out["_request_id"] = resp.headers.get("request-id") or resp.headers.get("x-request-id")
        return out


def build_prompt(symbol: str, timeframe: str, catalog: Sequence[Mapping[str, Any]], n: int) -> str:
    lines = [f"- {c['feature']} ({c['family']}, {c['kind']})" for c in catalog[:400]]
    return (
        "You propose testable equity trading-rule HYPOTHESES as JSON only. They will be validated and "
        "backtested; they are not advice. Use ONLY these features (exact keys) with operators gt, lt, "
        "cross_above, cross_below, or is (binary features, no threshold). Prefix a feature with '1w:' or '1d:' "
        "for a higher timeframe when it exists in the list.\n"
        f"Symbol: {symbol}. Timeframe: {timeframe}.\nFeatures:\n" + "\n".join(lines) +
        f"\nReturn a JSON object {{\"strategies\": [...]}} with at most {n} items. Each item: "
        "{\"name\": str, \"description\": str, \"timeframe\": \"" + timeframe + "\", \"direction\": \"long\", "
        "\"entry\": {\"logic\": \"AND\", \"conditions\": [{\"feature\": str, \"op\": str, \"threshold\": number|null}]}, "
        "\"exit\": {\"max_hold_bars\": int, \"stop_atr\": number|null, \"target_atr\": number|null, \"trail_atr\": number|null}}. "
        "No prose outside the JSON.")


def _extract_text(kind: str, resp: Mapping[str, Any]) -> tuple[str, int | None, int | None]:
    if kind == "anthropic":
        text = "".join(b.get("text", "") for b in resp.get("content", []) if isinstance(b, Mapping))
        u = resp.get("usage") or {}
        return text, u.get("input_tokens"), u.get("output_tokens")
    if kind == "openai_compatible":
        text = resp["choices"][0]["message"]["content"]
        u = resp.get("usage") or {}
        return text, u.get("prompt_tokens"), u.get("completion_tokens")
    if kind == "ollama":
        return resp.get("message", {}).get("content", ""), resp.get("prompt_eval_count"), resp.get("eval_count")
    raise AIRefused(f"unsupported provider kind {kind}")


def _spent(db: AuditDB, since: str, run_id: str | None = None) -> float:
    if run_id:
        r = db.one("SELECT COALESCE(SUM(cost_usd),0) s FROM api_calls WHERE run_id=? AND cost_usd IS NOT NULL", (run_id,))
    else:
        r = db.one("SELECT COALESCE(SUM(cost_usd),0) s FROM api_calls WHERE ts_utc >= ? AND cost_usd IS NOT NULL", (since,))
    return float(r["s"]) if r else 0.0


def generate(db: AuditDB, lib: Library, cfg: Mapping[str, Any], provider: str, symbol: str, timeframe: str,
             catalog: Sequence[Mapping[str, Any]], *, allow_remote: bool, allow_unknown_cost: bool = False,
             transport: Transport | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    if not cfg.get("enabled"):
        raise AIRefused("AI assistance is disabled (ai.enabled=false). Deterministic discovery needs no AI.")
    p = (cfg.get("providers") or {}).get(provider)
    if not p:
        raise AIRefused(f"provider {provider!r} is not configured")
    if not p.get("model"):
        raise AIRefused(f"provider {provider!r} has no model configured")
    if not p.get("local"):
        if not cfg.get("allow_remote") or not allow_remote:
            raise AIRefused("remote providers need ai.allow_remote=true in config and --allow-remote on the command")
        unknown = p.get("usd_per_1k_input") is None or p.get("usd_per_1k_output") is None
        if unknown and not allow_unknown_cost:
            raise AIRefused("provider prices are not configured, so spend cannot be capped; set them or pass "
                            "--allow-unknown-cost")
    month = datetime.now(timezone.utc).strftime("%Y-%m-01")
    if _spent(db, month) >= float(cfg.get("monthly_usd_limit", 0)) and not p.get("local"):
        raise AIRefused(f"monthly AI spend limit {cfg.get('monthly_usd_limit')} USD reached")
    if _spent(db, month, db.run_id) >= float(cfg.get("per_run_usd_limit", 0)) and not p.get("local"):
        raise AIRefused(f"per-run AI spend limit {cfg.get('per_run_usd_limit')} USD reached")
    headers = {"content-type": "application/json"}
    key_env = p.get("api_key_env")
    if key_env:
        key = env.get(key_env)
        if not key:
            raise AIRefused(f"environment variable {key_env} is not set")
        if p["kind"] == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            headers["authorization"] = f"Bearer {key}"
    prompt, redactions = redact(build_prompt(symbol, timeframe, catalog, int(cfg.get("max_hypotheses", 10))))
    if market_rows(prompt):
        raise AIRefused("the prompt looks like it contains market-data rows; refusing to send it")
    if p["kind"] == "anthropic":
        body: dict[str, Any] = {"model": p["model"], "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]}
    elif p["kind"] == "ollama":
        body = {"model": p["model"], "stream": False, "messages": [{"role": "user", "content": prompt}]}
    else:
        body = {"model": p["model"], "messages": [{"role": "user", "content": prompt}]}
    url = p["base_url"].rstrip("/") + p["path"]
    t0 = time.monotonic()
    ok, err, resp = True, None, {}
    try:
        resp = (transport or _http)(url, headers, body, 120.0)
        text, tin, tout = _extract_text(p["kind"], resp)
    except Exception as exc:
        ok, err, text, tin, tout = False, f"{type(exc).__name__}: {str(exc)[:300]}", "", None, None
    ms = (time.monotonic() - t0) * 1000
    if p.get("usd_per_1k_input") is not None and p.get("usd_per_1k_output") is not None and tin is not None and tout is not None:
        cost, basis = tin / 1000 * float(p["usd_per_1k_input"]) + tout / 1000 * float(p["usd_per_1k_output"]), "ESTIMATED_FROM_CONFIGURED_PRICES"
    else:
        cost, basis = None, "UNKNOWN (prices or token counts not supplied)"
    req_id = resp.get("_request_id") or resp.get("id") or f"local-{uuid.uuid4().hex[:12]}"
    with db.tx() as c:
        c.execute("INSERT INTO api_calls (ts_utc, provider, model, endpoint, purpose, request_id, input_tokens, "
                  "output_tokens, cost_usd, cost_basis, latency_ms, success, error, run_id, subject) VALUES "
                  "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (utc_now(), provider, p["model"], url, f"strategy hypotheses for {symbol} {timeframe}", req_id, tin,
                   tout, cost, basis, round(ms, 1), int(ok), err, db.run_id, symbol))
    db.event("AI_REQUEST", {"provider": provider, "model": p["model"], "endpoint": url, "request_id": req_id,
                            "input_tokens": tin, "output_tokens": tout, "cost_usd": cost, "cost_basis": basis,
                            "latency_ms": round(ms, 1), "success": ok, "error": err, "prompt_redactions": redactions,
                            "sent": "feature catalogue, symbol, timeframe only"},
             subject=symbol, level="INFO" if ok else "ERROR")
    if not ok:
        return {"ok": False, "error": err, "accepted": [], "rejected": []}
    accepted, rejected = [], []
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        items = json.loads(text[start:end]).get("strategies", [])
    except (ValueError, AttributeError) as exc:
        return {"ok": True, "accepted": [], "rejected": [{"error": f"reply is not the requested JSON: {exc}"}]}
    known = {c["feature"] for c in catalog}
    for i, item in enumerate(items[: int(cfg.get("max_hypotheses", 10))]):
        try:
            if not isinstance(item, Mapping):
                raise StrategyError("not an object")
            item = {k: item[k] for k in ("name", "description", "timeframe", "direction", "entry", "exit") if k in item}
            item["scope"] = {"symbols": [symbol], "timeframes": [timeframe]}
            doc = validate(item)
            missing = [c["feature"] for c in doc["entry"]["conditions"] if c["feature"] not in known]
            if missing:
                raise StrategyError(f"features not in the catalogue: {missing}")
            res = lib.add(doc, immutable=True, origin=f"ai:{provider}", active=False,
                          provenance={"request_id": req_id, "model": p["model"], "review": "PENDING_HUMAN_REVIEW"})
            accepted.append(res)
        except StrategyError as exc:
            rejected.append({"index": i, "error": str(exc)})
    db.event("AI_HYPOTHESES", {"provider": provider, "accepted": len(accepted), "rejected": rejected}, subject=symbol)
    return {"ok": True, "request_id": req_id, "accepted": accepted, "rejected": rejected, "cost_usd": cost,
            "cost_basis": basis}


def mock_transport(reply: Mapping[str, Any]) -> Transport:
    def _t(url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
        return reply
    return _t
