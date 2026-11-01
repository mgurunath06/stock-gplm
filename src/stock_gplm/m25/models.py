"""Strict M2.5 configuration and session-cache contracts."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CONTRACT = "stock-gplm.m25-oracle-auth.v1"
CACHE_CONTRACT = "stock-gplm.m25-session-cache.v1"
MAX_CONFIG_BYTES = 64 * 1024
MAX_CACHE_BYTES = 16 * 1024
MAX_TOKEN_BYTES = 8 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_HOST = re.compile(r"^[a-z0-9.-]{1,253}$")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate configuration key: {key}")
        result[key] = value
    return result


def _absolute(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ValueError(f"{label} must be an absolute path")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError(f"{label} must not contain parent traversal")
    return value


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(f"{label} must be an integer between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class AuthConfigV1:
    client_id: str
    user_id: str
    expected_account_id: str
    cache_path: str
    lock_path: str
    audit_path: str
    status_path: str
    chrome_binary: str
    chromedriver_path: str
    raw_roots: tuple[str, ...]
    allowed_redirect_hosts: tuple[str, ...]
    cache_max_age_seconds: int = 23 * 3600
    lock_timeout_seconds: int = 30
    browser_timeout_seconds: int = 90
    totp_min_valid_seconds: int = 8
    contract: str = CONTRACT
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.contract != CONTRACT:
            raise ValueError("unsupported M2.5 authentication contract")
        for label, value in (
            ("client_id", self.client_id), ("user_id", self.user_id),
            ("expected_account_id", self.expected_account_id),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{label} contains unsupported characters")
        state_paths = tuple(
            Path(_absolute(value, label)).resolve()
            for label, value in (
                ("cache_path", self.cache_path), ("lock_path", self.lock_path),
                ("audit_path", self.audit_path), ("status_path", self.status_path),
            )
        )
        _absolute(self.chrome_binary, "chrome_binary")
        _absolute(self.chromedriver_path, "chromedriver_path")
        if len(set(state_paths)) != len(state_paths):
            raise ValueError("M2.5 state paths must be distinct")
        if not self.raw_roots:
            raise ValueError("raw_roots must explicitly identify protected collector trees")
        raw = tuple(Path(_absolute(item, "raw_roots item")).resolve() for item in self.raw_roots)
        for state in state_paths:
            if any(state == root or root in state.parents for root in raw):
                raise ValueError("authentication state must remain outside raw collector trees")
        if not self.allowed_redirect_hosts:
            raise ValueError("allowed_redirect_hosts must not be empty")
        for host in self.allowed_redirect_hosts:
            if not isinstance(host, str) or _HOST.fullmatch(host) is None:
                raise ValueError("allowed redirect host is invalid")
        _positive_int(self.cache_max_age_seconds, "cache_max_age_seconds", 23 * 3600)
        _positive_int(self.lock_timeout_seconds, "lock_timeout_seconds", 30)
        _positive_int(self.browser_timeout_seconds, "browser_timeout_seconds", 90)
        _positive_int(self.totp_min_valid_seconds, "totp_min_valid_seconds", 20)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AuthConfigV1":
        expected = {
            "schema_version", "contract", "client_id", "user_id",
            "expected_account_id", "cache_path", "lock_path", "audit_path",
            "status_path", "chrome_binary", "chromedriver_path", "raw_roots",
            "allowed_redirect_hosts", "cache_max_age_seconds", "lock_timeout_seconds",
            "browser_timeout_seconds", "totp_min_valid_seconds",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("M2.5 config has missing or unsupported fields")
        if not isinstance(value["raw_roots"], list) or not isinstance(
            value["allowed_redirect_hosts"], list
        ):
            raise ValueError("raw_roots and allowed_redirect_hosts must be arrays")
        return cls(
            **{
                key: tuple(item) if key in {"raw_roots", "allowed_redirect_hosts"} else item
                for key, item in value.items()
            }
        )

    @classmethod
    def load(cls, path: Path) -> "AuthConfigV1":
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("M2.5 config must be a bounded regular file")
        info = path.stat()
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
            raise ValueError("M2.5 config ownership or write permissions are unsafe")
        return cls.from_dict(json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates,
        ))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["raw_roots"] = list(self.raw_roots)
        value["allowed_redirect_hosts"] = list(self.allowed_redirect_hosts)
        return value


@dataclass(frozen=True)
class SessionRecordV1:
    token: str
    uid: str
    account_id: str
    issued_at_epoch: float
    validated_at_epoch: float
    auth_flow: str = "shoonya-oauth-selenium-v1"
    contract: str = CACHE_CONTRACT
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.contract != CACHE_CONTRACT:
            raise ValueError("unsupported M2.5 session-cache contract")
        if not isinstance(self.token, str) or not self.token or len(self.token.encode()) > MAX_TOKEN_BYTES:
            raise ValueError("session token is absent or exceeds the configured bound")
        for label, value in (("uid", self.uid), ("account_id", self.account_id)):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"session {label} is invalid")
        if type(self.issued_at_epoch) not in {int, float} or type(self.validated_at_epoch) not in {int, float}:
            raise ValueError("session timestamps must be numeric")
        if self.issued_at_epoch <= 0 or self.validated_at_epoch < self.issued_at_epoch:
            raise ValueError("session timestamps are inconsistent")
        if self.auth_flow != "shoonya-oauth-selenium-v1":
            raise ValueError("unsupported authentication flow")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SessionRecordV1":
        expected = {
            "schema_version", "contract", "token", "uid", "account_id",
            "issued_at_epoch", "validated_at_epoch", "created_at", "auth_flow",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("session cache has missing or unsupported fields")
        compatible = dict(value)
        created_at = compatible.pop("created_at")
        if created_at != compatible["issued_at_epoch"]:
            raise ValueError("legacy created_at alias disagrees with issued_at_epoch")
        return cls(**compatible)

    @classmethod
    def from_legacy_dict(cls, value: dict[str, Any]) -> "SessionRecordV1":
        if not isinstance(value, dict) or set(value) != {"token", "uid", "account_id", "created_at"}:
            raise ValueError("unsupported legacy session cache")
        created = value["created_at"]
        return cls(
            token=value["token"], uid=value["uid"], account_id=value["account_id"],
            issued_at_epoch=created, validated_at_epoch=created,
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["created_at"] = self.issued_at_epoch
        return value
