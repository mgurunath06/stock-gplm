"""Lock-protected cached-session reuse and bounded fresh OAuth orchestration."""
from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stderr, redirect_stdout
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from .credentials import FileCredentialProvider
from .errors import (
    AuthError, LoginBlocked, LoginRequired, SecurityBoundaryError, TokenExchangeRejected,
    ValidationInconclusive,
)
from .models import AuthConfigV1, SessionRecordV1
from .security import AuditLogger, ExclusiveAuthLock, SessionCache, new_run_id


class Browser(Protocol):
    def acquire_code(self, credentials: Any) -> str: ...


class Validation(Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    INCONCLUSIVE = "INCONCLUSIVE"


def _provider_call(call: Callable[..., Any], *args: Any) -> Any:
    """Discard provider SDK stdout/stderr so responses cannot enter service logs."""
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return call(*args)


def validate_session(api: Any) -> Validation:
    try:
        response = _provider_call(api.get_limits)
    except Exception:
        return Validation.INCONCLUSIVE
    if isinstance(response, dict) and response.get("stat") == "Ok":
        return Validation.VALID
    message = str(response.get("emsg", "")) if isinstance(response, dict) else ""
    lowered = message.lower()
    if "session expired" in lowered or "invalid session key" in lowered:
        return Validation.INVALID
    return Validation.INCONCLUSIVE


class ReadOnlyShoonyaClient:
    """Expose only the provider reads used by the existing Oracle collector."""

    __slots__ = ("__api",)

    def __init__(self, api: Any) -> None:
        self.__api = api

    def get_limits(self, *args: Any, **kwargs: Any) -> Any:
        return self.__api.get_limits(*args, **kwargs)

    def get_daily_price_series(self, *args: Any, **kwargs: Any) -> Any:
        return self.__api.get_daily_price_series(*args, **kwargs)

    def get_time_price_series(self, *args: Any, **kwargs: Any) -> Any:
        return self.__api.get_time_price_series(*args, **kwargs)


# 2026-11-21: cross-run guard for unattended browser logins (the 30-minute timer would otherwise resubmit
# credentials after every failure). One automatic attempt per RETRY_GAP_SECONDS; after MAX_AUTO_FAILURES
# consecutive failures no automatic attempt is made until a login succeeds (a manual login refreshes the
# cache, so the guard is never reached) or the attempts file is deleted.
RETRY_GAP_SECONDS = 6 * 3600
MAX_AUTO_FAILURES = 2


class LoginAttempts:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def check(self, now: float) -> None:
        data = self.load()
        failures = int(data.get("consecutive_failures", 0))
        if failures >= MAX_AUTO_FAILURES:
            raise LoginBlocked(f"{failures} automatic logins failed; log in manually or delete {self.path}")
        last = float(data.get("last_attempt_epoch", 0) or 0)
        if failures and now - last < RETRY_GAP_SECONDS:
            raise LoginBlocked("last automatic login failed less than 6 hours ago")

    def started(self, now: float) -> None:
        data = self.load()
        data["last_attempt_epoch"] = now
        self.save(data)

    def failed(self, error_class: str) -> None:
        data = self.load()
        data["consecutive_failures"] = int(data.get("consecutive_failures", 0)) + 1
        data["last_error"] = error_class
        self.save(data)

    def succeeded(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class AuthManager:
    def __init__(
        self, *, config: AuthConfigV1, credentials: FileCredentialProvider,
        browser: Browser, api_factory: Callable[[], Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.browser = browser
        self.api_factory = api_factory
        self.clock = clock
        self.cache = SessionCache(Path(config.cache_path))
        self.audit = AuditLogger(Path(config.audit_path), Path(config.status_path))
        self.attempts = LoginAttempts(Path(config.status_path).with_name("shoonya_auth_attempts.json"))

    def _inject_cached(self, record: SessionRecordV1) -> tuple[Any, Validation]:
        if record.uid != self.config.user_id or record.account_id != self.config.expected_account_id:
            raise SecurityBoundaryError("cached Shoonya identity does not match configured identity")
        now = self.clock()
        if record.issued_at_epoch > now + 300:
            raise SecurityBoundaryError("cached Shoonya session timestamp is in the future")
        api = self.api_factory()
        api.injectOAuthHeader(record.token, record.uid, record.account_id)
        return api, validate_session(api)

    def ensure_session(self, *, allow_oauth_login: bool) -> ReadOnlyShoonyaClient:
        run_id = new_run_id()
        login_started = False  # any failure after this point counts against the automatic-login guard
        self.audit.event("AUTH_STARTED", run_id=run_id)
        try:
            with ExclusiveAuthLock(Path(self.config.lock_path), self.config.lock_timeout_seconds):
                self.audit.event("AUTH_LOCK_ACQUIRED", run_id=run_id)
                record = self.cache.load()
                now = self.clock()
                if record is not None and now - record.issued_at_epoch < self.config.cache_max_age_seconds:
                    api, result = self._inject_cached(record)
                    if result is Validation.VALID:
                        self.audit.event("CACHE_VALIDATED", run_id=run_id, cache_age_seconds=int(now - record.issued_at_epoch))
                        self.audit.status("VALID_REUSED", run_id=run_id)
                        return ReadOnlyShoonyaClient(api)
                    if result is Validation.INCONCLUSIVE:
                        raise ValidationInconclusive("cached Shoonya session validation was inconclusive")
                    self.audit.event("CACHE_EXPLICITLY_INVALID", run_id=run_id)
                elif record is not None:
                    self.audit.event("CACHE_EXPIRED", run_id=run_id, cache_age_seconds=int(now - record.issued_at_epoch))
                else:
                    self.audit.event("CACHE_MISSING", run_id=run_id)

                if not allow_oauth_login:
                    raise LoginRequired("fresh OAuth login is required but was not explicitly enabled")
                self.attempts.check(self.clock())
                credential_values = self.credentials.load()
                self.attempts.started(self.clock())
                login_started = True
                self.audit.event("OAUTH_BROWSER_STARTED", run_id=run_id)
                authorization_code = self.browser.acquire_code(credential_values)
                self.audit.event("OAUTH_CODE_CAPTURED", run_id=run_id, captured=True)
                api = self.api_factory()
                try:
                    response = _provider_call(
                        api.getAccessToken,
                        authorization_code, credential_values.api_secret,
                        self.config.client_id, self.config.user_id,
                    )
                finally:
                    authorization_code = ""
                if not isinstance(response, (tuple, list)) or len(response) < 4:
                    raise TokenExchangeRejected("Shoonya token exchange returned an unsupported response")
                token, uid, _, account_id = response[:4]
                if not all(isinstance(item, str) and item for item in (token, uid, account_id)):
                    raise TokenExchangeRejected("Shoonya token exchange omitted required identity fields")
                if uid != self.config.user_id or account_id != self.config.expected_account_id:
                    raise SecurityBoundaryError("fresh Shoonya token identity does not match configuration")
                api.injectOAuthHeader(token, uid, account_id)
                if validate_session(api) is not Validation.VALID:
                    raise ValidationInconclusive("fresh Shoonya token did not pass read-only validation")
                issued = self.clock()
                self.cache.save(SessionRecordV1(
                    token=token, uid=uid, account_id=account_id,
                    issued_at_epoch=issued, validated_at_epoch=issued,
                ))
                self.attempts.succeeded()
                self.audit.event("CACHE_COMMITTED", run_id=run_id)
                self.audit.status("REFRESHED", run_id=run_id)
                return ReadOnlyShoonyaClient(api)
        except Exception as exc:
            classification = exc.classification if isinstance(exc, AuthError) else type(exc).__name__.upper()
            if login_started:
                self.attempts.failed(classification)
            self.audit.event("AUTH_FAILED", run_id=run_id, error_class=classification)
            self.audit.status(classification, run_id=run_id)
            raise
