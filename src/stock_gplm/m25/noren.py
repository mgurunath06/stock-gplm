"""Lazy Oracle runtime adapters for the installed Shoonya OAuth SDK."""
from __future__ import annotations

from typing import Any


class NorenApiFactory:
    def __call__(self) -> Any:
        try:
            from NorenRestApiPy.NorenApi import NorenApi
        except ImportError as exc:
            raise RuntimeError("NorenRestApiOAuth is not installed in the Oracle environment") from exc
        return NorenApi(
            host="https://api.shoonya.com/NorenWClientAPI/",
            websocket="wss://api.shoonya.com/NorenWSAPI/",
        )
