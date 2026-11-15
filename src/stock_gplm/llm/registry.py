"""Research-provider registry: configuration, safe key checks and mock health checks.

``config/research/providers.json`` (contract ``stock-gplm.research-providers.v1``)
lists providers by protocol (``anthropic``, ``openai``, ``ollama``; ``gemini`` is a
declared future adapter). New providers need only a config entry. Credentials are
never in this file: each provider names an environment variable, which
:mod:`stock_gplm.llm.envfile` may fill from the private ``.env``.

A live request needs **all** of: the registry not in offline mode, the provider
``enabled``, ``live_calls_authorized`` true, a configured key for remote providers,
and the caller's explicit ``allow_live`` flag. Per-run request, token and spend caps
are enforced in addition to the client's daily limits. Health checks run against a
mock transport by default and never touch the network.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .client import LLMClient, LLMConfig, ProviderConfig

CONTRACT = "stock-gplm.research-providers.v1"
PROTOCOL_KIND = {"anthropic": "anthropic_compatible", "openai": "openai_compatible", "ollama": "ollama"}
FUTURE_PROTOCOLS = {"gemini"}
ROLES = {"hypothesis_generation", "strategy_research", "critique", "interpretation", "strategy_proposal", "audio"}
_ENV_NAME = re.compile(r"^GPLM_[A-Z0-9_]+_API_KEY$")
_SECRETISH = re.compile(r"(sk-[A-Za-z0-9]{8,}|Bearer\s+\S+|api[_-]?key\s*[:=])", re.I)
_REQUIRED = {"protocol", "base_url", "model", "enabled", "live_calls_authorized", "local", "roles",
             "timeout_seconds", "max_retries", "rate_limit_per_minute", "max_requests_per_run",
             "max_tokens_per_run", "max_usd_per_run", "pricing_usd_per_1k"}


class LiveCallRefused(RuntimeError):
    """A live provider request was not explicitly authorized."""


@dataclass(frozen=True)
class Provider:
    name: str
    protocol: str
    base_url: str
    model: str
    enabled: bool
    live_calls_authorized: bool
    local: bool
    api_key_env: str | None
    roles: tuple[str, ...]
    timeout_seconds: float
    max_retries: int
    rate_limit_per_minute: int
    max_requests_per_run: int
    max_tokens_per_run: int
    max_usd_per_run: float
    pricing: Mapping[str, float]
    path: str | None = None


@dataclass(frozen=True)
class Registry:
    offline: bool
    providers: dict[str, Provider]
    limits: Mapping[str, Any]
    audit_log: str


def _scan_for_secrets(value: Any, where: str = "") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in {"api_key", "key", "token", "secret", "password", "authorization"}:
                raise ValueError(f"{where}{k}: credentials must not appear in provider config; use api_key_env")
            _scan_for_secrets(v, f"{where}{k}.")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_for_secrets(v, f"{where}{i}.")
    elif isinstance(value, str) and _SECRETISH.search(value):
        raise ValueError(f"{where.rstrip('.')}: value looks like a credential; keep secrets in .env only")


def load_registry(path: Path) -> Registry:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("contract") != CONTRACT:
        raise ValueError(f"{path} is not a {CONTRACT} document")
    _scan_for_secrets(raw)
    providers: dict[str, Provider] = {}
    for name, item in raw["providers"].items():
        missing = _REQUIRED - set(item)
        if missing:
            raise ValueError(f"provider {name} lacks {sorted(missing)}")
        proto = item["protocol"]
        if proto not in PROTOCOL_KIND and proto not in FUTURE_PROTOCOLS:
            raise ValueError(f"provider {name}: unsupported protocol {proto}")
        env = item.get("api_key_env")
        if not item["local"] and not env:
            raise ValueError(f"remote provider {name} must name api_key_env")
        if env is not None and not _ENV_NAME.match(env):
            raise ValueError(f"provider {name}: api_key_env must match GPLM_*_API_KEY")
        if set(item["roles"]) - ROLES:
            raise ValueError(f"provider {name}: unknown roles {sorted(set(item['roles']) - ROLES)}")
        if not item["local"] and item["enabled"] and item["max_usd_per_run"] <= 0:
            raise ValueError(f"enabled paid provider {name} needs a positive max_usd_per_run")
        providers[name] = Provider(name, proto, item["base_url"].rstrip("/"), item["model"], bool(item["enabled"]),
                                   bool(item["live_calls_authorized"]), bool(item["local"]), env, tuple(item["roles"]),
                                   float(item["timeout_seconds"]), int(item["max_retries"]),
                                   int(item["rate_limit_per_minute"]), int(item["max_requests_per_run"]),
                                   int(item["max_tokens_per_run"]), float(item["max_usd_per_run"]),
                                   dict(item["pricing_usd_per_1k"]), item.get("path"))
    return Registry(bool(raw.get("offline", True)), providers, dict(raw["limits"]), str(raw["audit_log"]))


def check(registry: Registry, environ: Mapping[str, str]) -> list[dict[str, Any]]:
    """Report configuration state per provider. Values are never read beyond presence."""
    rows = []
    for p in registry.providers.values():
        configured = bool(p.api_key_env and environ.get(p.api_key_env))
        live = (not registry.offline and p.enabled and p.live_calls_authorized and (p.local or configured)
                and p.protocol in PROTOCOL_KIND)
        rows.append({"provider": p.name, "protocol": p.protocol, "local": p.local, "enabled": p.enabled,
                     "live_calls_authorized": p.live_calls_authorized, "api_key_env": p.api_key_env,
                     "key_configured": configured if p.api_key_env else None,
                     "adapter": "implemented" if p.protocol in PROTOCOL_KIND else "future",
                     "live_possible": live, "max_usd_per_run": p.max_usd_per_run})
    return rows


def to_llm_config(registry: Registry, names: list[str] | None = None) -> LLMConfig:
    chosen = {n: p for n, p in registry.providers.items() if (names is None or n in names) and p.protocol in PROTOCOL_KIND}
    providers = {n: ProviderConfig(n, PROTOCOL_KIND[p.protocol], p.base_url, p.local, p.enabled, p.api_key_env,
                                   {p.model: dict(p.pricing)}, p.path) for n, p in chosen.items()}
    lim = registry.limits
    return LLMConfig(True, providers, int(lim["max_output_tokens"]), int(lim["daily_token_limit"]),
                     float(lim["daily_usd_limit"]), int(lim.get("max_market_rows_remote", 0)),
                     Path(registry.audit_log), float(lim.get("timeout_seconds", 60)), {})


def authorize_live(registry: Registry, name: str, environ: Mapping[str, str], allow_live: bool) -> Provider:
    p = registry.providers.get(name)
    if p is None:
        raise LiveCallRefused(f"unknown provider {name}")
    reasons = []
    if registry.offline:
        reasons.append("registry is in offline mode")
    if not p.enabled:
        reasons.append("provider is disabled")
    if not p.live_calls_authorized:
        reasons.append("live_calls_authorized is false")
    if p.protocol not in PROTOCOL_KIND:
        reasons.append(f"protocol {p.protocol} has no adapter yet")
    if not p.local and not (p.api_key_env and environ.get(p.api_key_env)):
        reasons.append(f"{p.api_key_env} is not configured")
    if not allow_live:
        reasons.append("caller did not pass --allow-live")
    if reasons:
        raise LiveCallRefused(f"live call to {name} refused: " + "; ".join(reasons))
    return p


class RunBudget:
    """Per-run request/token/spend caps for one provider (in addition to daily client limits)."""

    def __init__(self, provider: Provider):
        self.p, self.requests, self.tokens, self.usd = provider, 0, 0, 0.0

    def before(self, max_output_tokens: int) -> None:
        if self.requests + 1 > self.p.max_requests_per_run:
            raise LiveCallRefused(f"{self.p.name}: max_requests_per_run {self.p.max_requests_per_run} reached")
        worst = max_output_tokens * self.p.pricing.get("output_per_1k", 0) / 1000
        if self.usd + worst > self.p.max_usd_per_run and not self.p.local:
            raise LiveCallRefused(f"{self.p.name}: max_usd_per_run {self.p.max_usd_per_run} would be exceeded")
        if self.tokens + max_output_tokens > self.p.max_tokens_per_run:
            raise LiveCallRefused(f"{self.p.name}: max_tokens_per_run would be exceeded")

    def after(self, tokens: int, usd: float) -> None:
        self.requests += 1
        self.tokens += tokens
        self.usd += usd


def mock_transport(url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    text = "MOCK_OK"
    if url.endswith("/v1/messages") or "anthropic-version" in headers:
        return {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 5, "output_tokens": 2}}
    if url.endswith("/api/chat"):
        return {"message": {"content": text}, "prompt_eval_count": 5, "eval_count": 2}
    return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}


def mock_health(registry: Registry, audit_dir: Path) -> list[dict[str, Any]]:
    """Exercise request shaping for every implemented adapter offline, with a dummy key."""
    rows = []
    for p in registry.providers.values():
        if p.protocol not in PROTOCOL_KIND:
            rows.append({"provider": p.name, "status": "SKIPPED_FUTURE_ADAPTER"})
            continue
        cfg = to_llm_config(registry, [p.name])
        spec = cfg.providers[p.name]
        cfg.providers[p.name] = ProviderConfig(spec.name, spec.kind, spec.base_url, spec.local, True, spec.api_key_env,
                                               spec.models, spec.path)
        cfg.audit_log = Path(audit_dir) / "mock-health-audit.jsonl"
        seen: dict[str, Any] = {}

        def transport(url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
            seen.update({"url": url, "auth_header": sorted(k for k in headers if k.lower() in {"authorization", "x-api-key"})})
            return mock_transport(url, headers, body, timeout)

        env = {p.api_key_env: "MOCK-KEY-NOT-REAL"} if p.api_key_env else {}
        reply = LLMClient(cfg, transport=transport, environ=env).complete(provider=p.name, model=p.model,
                                                                           purpose="mock-health", prompt="ping")
        rows.append({"provider": p.name, "status": "MOCK_OK" if reply.text == "MOCK_OK" else "MOCK_FAILED",
                     "url": seen.get("url"), "auth_headers": seen.get("auth_header"), "network": "none"})
    return rows
