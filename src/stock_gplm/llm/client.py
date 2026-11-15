"""Provider-agnostic chat client with budgets, spend caps, redaction and audit logging.

Configuration names providers and the environment variable that holds each key;
keys are never read from files or written anywhere. Every request is checked against
the enabled flag, the provider allow-list, the upload policy and the token and spend
budgets before any byte is sent. The audit log stores hashes and counts, not prompts.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .redaction import market_rows, redact

CONFIG_CONTRACT = "stock-gplm.llm-config.v1"
KINDS = frozenset({"ollama", "openai_compatible", "anthropic_compatible"})
Transport = Callable[[str, Mapping[str, str], Mapping[str, Any], float], Mapping[str, Any]]


class LLMDisabled(RuntimeError):
    """LLM use is off, the provider is not enabled, or its key is absent."""


class BudgetExceeded(RuntimeError):
    """A token or spend limit would be exceeded."""


class UploadRefused(RuntimeError):
    """The prompt carries data that may not leave the machine without approval."""


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str  # "ollama", "openai_compatible" or "anthropic_compatible"
    base_url: str
    local: bool
    enabled: bool
    api_key_env: str | None
    models: Mapping[str, Mapping[str, float]]  # model -> usd per 1k input / output tokens
    path: str | None = None  # request path override (e.g. "/v1/messages")


@dataclass(frozen=True)
class Advisory:
    """A model reply. It is research text, never an input to gates or rankings."""

    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_usd: float
    authority: str = "ADVISORY_ONLY"


@dataclass
class LLMConfig:
    enabled: bool
    providers: dict[str, ProviderConfig]
    max_output_tokens: int
    daily_token_limit: int
    daily_usd_limit: float
    max_market_rows_remote: int
    audit_log: Path
    timeout_seconds: float = 60.0
    raw: Mapping[str, Any] = field(default_factory=dict)


def load_config(path: Path) -> LLMConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("contract") != CONFIG_CONTRACT:
        raise ValueError("unsupported LLM config contract")
    providers = {}
    for name, item in raw["providers"].items():
        if item["kind"] not in KINDS:
            raise ValueError(f"unsupported provider kind for {name}")
        if not item["local"] and not item.get("api_key_env"):
            raise ValueError(f"remote provider {name} must name an api_key_env")
        if "api_key" in item:
            raise ValueError("API keys belong in environment variables or a secret store, not config")
        providers[name] = ProviderConfig(
            name, item["kind"], item["base_url"].rstrip("/"), bool(item["local"]), bool(item["enabled"]),
            item.get("api_key_env"), item.get("models", {}), item.get("path"),
        )
    limits = raw["limits"]
    return LLMConfig(
        bool(raw["enabled"]), providers, int(limits["max_output_tokens"]), int(limits["daily_token_limit"]),
        float(limits["daily_usd_limit"]), int(limits["max_market_rows_remote"]),
        Path(raw["audit_log"]), float(limits.get("timeout_seconds", 60)), raw,
    )


def _http(url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float) -> Mapping[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured URL
        return json.loads(response.read(4 * 1024 * 1024))


class LLMClient:
    """Send redacted prompts to a configured provider within budget, and audit each call."""

    def __init__(self, config: LLMConfig, *, transport: Transport = _http,
                 environ: Mapping[str, str] | None = None, clock: Callable[[], datetime] | None = None):
        self.config, self.transport = config, transport
        self.environ = os.environ if environ is None else environ
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _usage_today(self) -> tuple[int, float]:
        today, tokens, usd = self.clock().date().isoformat(), 0, 0.0
        if self.config.audit_log.exists():
            for line in self.config.audit_log.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry["utc"].startswith(today) and entry["outcome"] == "OK":
                    tokens += entry["input_tokens"] + entry["output_tokens"]
                    usd += entry["estimated_usd"]
        return tokens, usd

    def _audit(self, entry: dict[str, Any]) -> None:
        self.config.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with self.config.audit_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"utc": self.clock().isoformat(), **entry}, sort_keys=True) + "\n")

    def complete(self, *, provider: str, model: str, purpose: str, prompt: str,
                 system: str = "", approved_data_upload: bool = False) -> Advisory:
        cfg = self.config
        if not cfg.enabled:
            raise LLMDisabled("LLM assistance is disabled in configuration")
        spec = cfg.providers.get(provider)
        if spec is None or not spec.enabled:
            raise LLMDisabled(f"provider {provider} is not enabled")
        if model not in spec.models:
            raise LLMDisabled(f"model {model} is not approved for {provider}")
        text, counts = redact(f"{system}\n\n{prompt}" if system else prompt)
        base = {"provider": provider, "model": model, "purpose": purpose, "local": spec.local,
                "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(), "redactions": counts}
        rows = market_rows(text)
        if not spec.local and rows > cfg.max_market_rows_remote and not approved_data_upload:
            self._audit({**base, "outcome": "REFUSED_MARKET_DATA_UPLOAD", "market_rows": rows,
                         "input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0})
            raise UploadRefused(f"{rows} market-data rows may not go to a remote provider without approval")
        estimate_in = max(1, len(text) // 4)
        price = spec.models[model]
        worst_usd = (estimate_in * price.get("input_per_1k", 0) + cfg.max_output_tokens * price.get("output_per_1k", 0)) / 1000
        tokens, usd = self._usage_today()
        if tokens + estimate_in + cfg.max_output_tokens > cfg.daily_token_limit or usd + worst_usd > cfg.daily_usd_limit:
            self._audit({**base, "outcome": "REFUSED_BUDGET", "input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0})
            raise BudgetExceeded("daily token or spend limit would be exceeded")
        headers = {"Content-Type": "application/json"}
        if spec.api_key_env:
            key = self.environ.get(spec.api_key_env)
            if not key:
                raise LLMDisabled(f"environment variable {spec.api_key_env} is not set")
            if spec.kind == "anthropic_compatible":
                headers["x-api-key"] = key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {key}"
        messages = [{"role": "user", "content": text}]
        if spec.kind == "anthropic_compatible":
            url = f"{spec.base_url}{spec.path or '/v1/messages'}"
            body = {"model": model, "messages": messages, "max_tokens": cfg.max_output_tokens}
        elif spec.kind == "ollama":
            url = f"{spec.base_url}{spec.path or '/api/chat'}"
            body = {"model": model, "messages": messages, "stream": False,
                    "options": {"num_predict": cfg.max_output_tokens}}
        else:
            url = f"{spec.base_url}{spec.path or '/chat/completions'}"
            body = {"model": model, "messages": messages, "max_tokens": cfg.max_output_tokens}
        try:
            reply = self.transport(url, headers, body, cfg.timeout_seconds)
        except Exception as exc:
            self._audit({**base, "outcome": f"ERROR {type(exc).__name__}", "input_tokens": 0,
                         "output_tokens": 0, "estimated_usd": 0.0})
            raise
        if spec.kind == "anthropic_compatible":
            answer = "".join(part.get("text", "") for part in reply["content"] if part.get("type", "text") == "text")
            usage = reply.get("usage", {})
            used_in, used_out = int(usage.get("input_tokens", estimate_in)), int(usage.get("output_tokens", 0))
        elif spec.kind == "ollama":
            answer = reply["message"]["content"]
            used_in, used_out = int(reply.get("prompt_eval_count", estimate_in)), int(reply.get("eval_count", 0))
        else:
            answer = reply["choices"][0]["message"]["content"]
            usage = reply.get("usage", {})
            used_in, used_out = int(usage.get("prompt_tokens", estimate_in)), int(usage.get("completion_tokens", 0))
        cost = (used_in * price.get("input_per_1k", 0) + used_out * price.get("output_per_1k", 0)) / 1000
        self._audit({**base, "outcome": "OK", "input_tokens": used_in, "output_tokens": used_out,
                     "estimated_usd": round(cost, 6),
                     "response_sha256": hashlib.sha256(answer.encode()).hexdigest()})
        return Advisory(answer, provider, model, used_in, used_out, round(cost, 6))
