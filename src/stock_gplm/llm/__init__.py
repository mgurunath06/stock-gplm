"""Opt-in, provider-agnostic LLM assistance (local Ollama or approved cloud APIs).

LLM output is advisory text. Nothing in the training, validation, gating or ranking
path imports this package, so a model reply cannot change a gate, a split, a label,
a metric or a risk rule.
"""
from .client import (
    Advisory, BudgetExceeded, LLMClient, LLMDisabled, ProviderConfig, UploadRefused, load_config,
)
from .redaction import redact

__all__ = [
    "Advisory", "BudgetExceeded", "LLMClient", "LLMDisabled", "ProviderConfig", "UploadRefused",
    "load_config", "redact",
]
