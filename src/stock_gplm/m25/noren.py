"""Lazy Shoonya SDK adapter with mandatory HTTP connection/read timeouts."""
from __future__ import annotations

import importlib
from typing import Any

from .errors import ProviderTimeout

PROVIDER_CONNECT_TIMEOUT_SECONDS = 5
PROVIDER_READ_TIMEOUT_SECONDS = 15
PROVIDER_HTTP_TIMEOUT = (
    PROVIDER_CONNECT_TIMEOUT_SECONDS,
    PROVIDER_READ_TIMEOUT_SECONDS,
)


class _BoundedRequests:
    """Delegate requests calls while forcing the reviewed timeout tuple."""

    def __init__(self, requests_module: Any) -> None:
        self._requests = requests_module

    def post(self, *args: Any, **kwargs: Any) -> Any:
        if "timeout" in kwargs:
            raise RuntimeError("Shoonya SDK attempted to override the enforced HTTP timeout")
        try:
            return self._requests.post(*args, timeout=PROVIDER_HTTP_TIMEOUT, **kwargs)
        except self._requests.exceptions.Timeout as exc:
            raise ProviderTimeout("Shoonya provider request exceeded its HTTP timeout") from exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._requests, name)


def _install_bounded_transport(module: Any) -> None:
    current = module.requests
    if not isinstance(current, _BoundedRequests):
        module.requests = _BoundedRequests(current)


class NorenApiFactory:
    def __call__(self) -> Any:
        try:
            module = importlib.import_module("NorenRestApiPy.NorenApi")
            api_class = module.NorenApi
        except ImportError as exc:
            raise RuntimeError("NorenRestApiOAuth is not installed in the Oracle environment") from exc
        _install_bounded_transport(module)
        return api_class(
            host="https://api.shoonya.com/NorenWClientAPI/",
            websocket="wss://api.shoonya.com/NorenWSAPI/",
        )
