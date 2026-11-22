"""Live health check for the LLM providers used by strategy tuning.

Order (owner, 2026-11-22): local Ollama -> MWAPI / StartupAPI -> OpenRouter (last, costliest).

Per provider:
  1. key   - is the key variable set (from the environment or the project ``.env``)? Value never shown.
  2. list  - can we list models with that key? (free; 404 = provider has no list endpoint, not a failure)
  3. ping  - a 1-line chat ("reply OK", max 8 output tokens, cost ~0) with the model from
             ``config/research/providers.json`` or ``--model name=ID``.

This is an explicit owner-run (or assistant-run) diagnostic, so it calls the network directly and does
not need ``enabled``/``live_calls_authorized`` in the registry. Nothing but the ping text is sent.

    python -m stock_gplm.llm.health                       # ollama, mwapi, startupapi, openrouter
    python -m stock_gplm.llm.health --models              # also print every model ID each provider offers
    python -m stock_gplm.llm.health --only mwapi --model mwapi=claude-sonnet-4-5
    python -m stock_gplm.llm.health --no-ping             # key + list only (zero cost)

Exit code 0 when every checked provider passes, 1 otherwise. A JSON copy of the result is written to
``reports/llm/health/<utc-stamp>.json`` (relative to the working directory).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import envfile
from .registry import Provider, load_registry

DEFAULT_ORDER = ["ollama", "mwapi", "startupapi", "openrouter"]
DEFAULT_REGISTRY = Path(__file__).resolve().parents[3] / "config" / "research" / "providers.json"
LOCAL_FALLBACK_MODEL = "qwen3:14b"
UA = "stock-gplm-health/1"
PING = "Reply with exactly the word OK."
UNSET = {"", "SET_MODEL_ID", "SET_LOCAL_MODEL"}


def _scrub(text: str, secret: str | None) -> str:
    text = " ".join(str(text).split())[:240]
    return text.replace(secret, "***") if secret else text


def _request(method: str, url: str, headers: dict[str, str], body: Any, timeout: float) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": UA, "Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - configured provider URL
            raw = r.read(4 * 1024 * 1024)
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read(4096).decode("utf-8", "replace")
        return e.code, raw


def _headers(p: Provider, key: str | None) -> dict[str, str]:
    if not key:
        return {}
    if p.protocol == "anthropic":
        # Third-party Anthropic-style proxies differ: some read x-api-key, some Bearer. Send both (same host).
        return {"x-api-key": key, "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def _api_root(p: Provider) -> str:
    """Base including the version segment: https://x/v1 (OpenAI-style bases already carry /v1)."""
    base = p.base_url.rstrip("/")
    if p.protocol == "anthropic" and not base.endswith("/v1"):
        return base + "/v1"
    return base


def list_models(p: Provider, key: str | None) -> tuple[str, list[str], str]:
    if p.protocol == "ollama":
        url = p.base_url.rstrip("/") + "/api/tags"
    else:
        url = _api_root(p) + "/models"
    code, body = _request("GET", url, _headers(p, key), None, min(p.timeout_seconds, 30))
    if code == 200 and isinstance(body, dict):
        items = body.get("models") if p.protocol == "ollama" else body.get("data", body.get("models", []))
        ids = sorted(str(m.get("name") or m.get("id")) for m in (items or []) if isinstance(m, dict))
        return "ok", ids, ""
    if code == 404:
        return "n/a", [], "no model-list endpoint"
    return "FAIL", [], f"HTTP {code}: {_scrub(body, key)}"


def ping(p: Provider, key: str | None, model: str) -> tuple[str, str, float]:
    msgs = [{"role": "user", "content": PING}]
    if p.protocol == "anthropic":
        url, body = _api_root(p) + "/messages", {"model": model, "max_tokens": 8, "messages": msgs}
    elif p.protocol == "ollama":
        url = p.base_url.rstrip("/") + "/api/chat"
        body = {"model": model, "messages": msgs, "stream": False, "think": False,
                "options": {"num_predict": 8}, "keep_alive": "30m"}
    else:
        url, body = _api_root(p) + "/chat/completions", {"model": model, "max_tokens": 8, "messages": msgs}
    t0 = time.monotonic()
    code, reply = _request("POST", url, _headers(p, key), body, p.timeout_seconds)
    secs = time.monotonic() - t0
    if code != 200 or not isinstance(reply, dict):
        return "FAIL", f"HTTP {code}: {_scrub(reply, key)}", secs
    try:
        if p.protocol == "anthropic":
            text = "".join(c.get("text", "") for c in reply.get("content", []) if isinstance(c, dict))
        elif p.protocol == "ollama":
            text = reply["message"]["content"]
        else:
            text = reply["choices"][0]["message"]["content"] or ""
        served = reply.get("model", model)
    except (KeyError, IndexError, TypeError):
        return "FAIL", f"unexpected reply: {_scrub(json.dumps(reply), key)}", secs
    return "ok", f"{served}: {_scrub(text, key)!r}", secs


def run(names: list[str], models: dict[str, str], *, do_ping: bool, show_models: bool,
        registry_path: Path) -> list[dict[str, Any]]:
    env_meta = envfile.load()  # fills GPLM_* from the project .env; returns names only
    reg = load_registry(registry_path)
    rows: list[dict[str, Any]] = []
    print(f".env: {env_meta['path'] or 'not found'}  loaded: {', '.join(env_meta['loaded']) or '-'}")
    for name in names:
        p = reg.providers.get(name)
        if p is None:
            rows.append({"provider": name, "result": "FAIL", "note": "not in providers.json"})
            continue
        key = os.environ.get(p.api_key_env) if p.api_key_env else None
        row: dict[str, Any] = {"provider": name, "protocol": p.protocol, "base_url": p.base_url,
                               "key": "local" if p.local else ("set" if key else "MISSING")}
        if not p.local and not key:
            row.update(result="FAIL", note=f"set {p.api_key_env} in .env")
            rows.append(row)
            continue
        try:
            row["list"], ids, note = list_models(p, key)
            row["models_count"] = len(ids)
            if show_models:
                row["models"] = ids
            model = models.get(name) or (p.model if p.model not in UNSET else "")
            if p.protocol == "ollama" and not model:
                model = LOCAL_FALLBACK_MODEL
            if not model and ids:
                pref = [i for i in ids if "sonnet" in i.lower()] or [i for i in ids if "claude" in i.lower()]
                model = (pref or ids)[0]
                note = (note + "; " if note else "") + f"no model in config, pinged {model}"
            row["model"] = model or "-"
            if do_ping and model:
                row["ping"], row["reply"], secs = ping(p, key, model)
                row["seconds"] = round(secs, 1)
            elif do_ping:
                row["ping"] = "skipped"
                note = (note + "; " if note else "") + f"give --model {name}=ID"
            # With ping, the chat answer decides (some proxies refuse /models but chat fine).
            ok = row.get("ping") == "ok" if do_ping else row["list"] != "FAIL"
            row.update(result="ok" if ok else "FAIL", note=note)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            row.update(result="FAIL", note=_scrub(f"{type(e).__name__}: {getattr(e, 'reason', e)}", key))
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma list of providers (default: ollama,mwapi,startupapi,openrouter)")
    ap.add_argument("--model", action="append", default=[], metavar="NAME=ID", help="model to ping per provider")
    ap.add_argument("--models", action="store_true", help="print every model ID each provider lists")
    ap.add_argument("--no-ping", action="store_true", help="key + model list only (no chat call)")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    a = ap.parse_args(argv)
    names = [n.strip() for n in a.only.split(",")] if a.only else DEFAULT_ORDER
    models = dict(m.split("=", 1) for m in a.model if "=" in m)
    rows = run(names, models, do_ping=not a.no_ping, show_models=a.models, registry_path=a.registry)

    print(f"\n{'provider':<11} {'key':<8} {'list':<5} {'#models':>7} {'ping':<8} {'secs':>5}  model / reply / note")
    for r in rows:
        print(f"{r['provider']:<11} {r.get('key', '-'):<8} {r.get('list', '-'):<5} {r.get('models_count', '-'):>7} "
              f"{r.get('ping', '-'):<8} {r.get('seconds', '-'):>5}  {r.get('reply') or r.get('model', '')}"
              f"{'  | ' + r['note'] if r.get('note') else ''}")
        for mid in r.get("models", []):
            print(f"{'':>13}- {mid}")
    bad = [r["provider"] for r in rows if r.get("result") != "ok"]
    print(f"\nRESULT: {'ALL OK' if not bad else 'FAIL: ' + ', '.join(bad)}")

    out = Path("reports/llm/health")
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out / f"{stamp}.json").write_text(json.dumps({"utc": stamp, "rows": rows}, indent=2), encoding="utf-8")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
