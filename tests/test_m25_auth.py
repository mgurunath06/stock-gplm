from __future__ import annotations

from stock_gplm import locking as fcntl
import json
import os
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from stock_gplm.m25.browser import (
    PAGE_READY_TIMEOUT_SECONDS, capture_authorization_code, extract_authorization_code,
    wait_for_login_form,
)
from stock_gplm.m25.credentials import FileCredentialProvider, _private_permissions
from stock_gplm.m25.errors import (
    BrowserPageNotReady, LoginRequired, SecurityBoundaryError, ValidationInconclusive,
)
from stock_gplm.m25.models import AuthConfigV1, SessionRecordV1
from stock_gplm.m25.security import AuditLogger, ExclusiveAuthLock, SessionCache
from stock_gplm.m25.session import AuthManager, Validation, validate_session
from stock_gplm.m25.totp import generate_totp, seconds_remaining


def _config(tmp_path: Path) -> AuthConfigV1:
    state = tmp_path / "state"
    raw = tmp_path / "raw"
    return AuthConfigV1(
        client_id="TEST_U", user_id="TEST", expected_account_id="TEST",
        cache_path=str(state / "session.json"), lock_path=str(state / "auth.lock"),
        audit_path=str(state / "auth.jsonl"), status_path=str(state / "status.json"),
        chrome_binary="/usr/bin/chromium", chromedriver_path="/usr/bin/chromedriver",
        raw_roots=(str(raw),), allowed_redirect_hosts=("api.shoonya.com",),
        cache_max_age_seconds=82800, lock_timeout_seconds=1,
        browser_timeout_seconds=60, totp_min_valid_seconds=8,
    )


def _credentials(tmp_path: Path) -> FileCredentialProvider:
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    values = {
        "shoonya_password": "correct horse battery staple",
        "shoonya_totp_secret": "JBSWY3DPEHPK3PXP",
        "shoonya_api_secret": "api-secret-value",
    }
    for name, value in values.items():
        path = directory / name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
    return FileCredentialProvider(directory)


class FakeApi:
    def __init__(self, validation=None, exchange=None):
        self.validation = validation if validation is not None else {"stat": "Ok"}
        self.exchange = exchange
        self.injected = None

    def injectOAuthHeader(self, token, uid, account_id):
        self.injected = (token, uid, account_id)

    def get_limits(self, *args, **kwargs):
        if isinstance(self.validation, Exception):
            raise self.validation
        return self.validation

    def getAccessToken(self, code, secret, client_id, uid):
        return self.exchange

    def get_daily_price_series(self, **kwargs):
        return [kwargs]

    def get_time_price_series(self, **kwargs):
        return [kwargs]

    def place_order(self, **kwargs):
        raise AssertionError("must not be exposed")


class FakeBrowser:
    def __init__(self):
        self.calls = 0

    def acquire_code(self, credentials):
        self.calls += 1
        assert credentials.password == "correct horse battery staple"
        return "one-time-code"


class Factory:
    def __init__(self, *apis):
        self.apis = list(apis)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.apis.pop(0)


def test_provider_validation_output_is_never_logged(capsys):
    class NoisyApi:
        def get_limits(self):
            print("never-log-provider-token")
            return {"stat": "Ok"}

    assert validate_session(NoisyApi()) is Validation.VALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _network_entry(url: str) -> dict:
    return {
        "message": json.dumps({
            "message": {"method": "Network.requestWillBeSent", "params": {"request": {"url": url}}}
        })
    }


def test_config_rejects_state_inside_raw_tree(tmp_path):
    config = _config(tmp_path)
    value = config.to_dict()
    value["cache_path"] = str(tmp_path / "raw" / "session.json")
    with pytest.raises(ValueError, match="outside raw"):
        AuthConfigV1.from_dict(value)


def test_config_loader_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="duplicate"):
        AuthConfigV1.load(path)


def test_totp_rfc_vector_and_remaining_window():
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert generate_totp(seed, at_time=59, digits=8) == "94287082"
    assert seconds_remaining(at_time=59) == 1
    assert seconds_remaining(at_time=60) == 30


def test_credentials_are_loaded_only_from_private_regular_files(tmp_path):
    provider = _credentials(tmp_path)
    value = provider.load()
    assert value.api_secret == "api-secret-value"
    password = provider.directory / "shoonya_password"
    password.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        provider.load()


def test_systemd_root_service_group_credential_permissions_are_accepted(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    monkeypatch.setattr(os, "getegid", lambda: 1001)
    directory = SimpleNamespace(st_uid=0, st_gid=1001, st_mode=stat.S_IFDIR | 0o550)
    credential = SimpleNamespace(st_uid=0, st_gid=1001, st_mode=stat.S_IFREG | 0o440)
    assert _private_permissions(directory, directory=True)
    assert _private_permissions(credential, directory=False)


def test_systemd_root_group_credential_permissions_are_accepted(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    monkeypatch.setattr(os, "getegid", lambda: 1001)
    directory = SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o550)
    credential = SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o440)
    assert _private_permissions(directory, directory=True)
    assert _private_permissions(credential, directory=False)


@pytest.mark.parametrize(
    ("mode", "directory"),
    ((0o557, True), (0o570, True), (0o447, False), (0o460, False)),
)
def test_systemd_credential_permissions_reject_world_or_group_write(monkeypatch, mode, directory):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    monkeypatch.setattr(os, "getegid", lambda: 1001)
    kind = stat.S_IFDIR if directory else stat.S_IFREG
    info = SimpleNamespace(st_uid=0, st_gid=1001, st_mode=kind | mode)
    assert not _private_permissions(info, directory=directory)


def test_systemd_credential_permissions_reject_unexpected_group(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    monkeypatch.setattr(os, "getegid", lambda: 1001)
    info = SimpleNamespace(st_uid=0, st_gid=2000, st_mode=stat.S_IFREG | 0o440)
    assert not _private_permissions(info, directory=False)


def test_cache_round_trip_and_legacy_migration(tmp_path):
    path = tmp_path / "state" / "session.json"
    cache = SessionCache(path)
    record = SessionRecordV1(
        token="session-token", uid="TEST", account_id="TEST",
        issued_at_epoch=1000.0, validated_at_epoch=1000.0,
    )
    cache.save(record)
    assert cache.load() == record
    assert path.stat().st_mode & 0o777 == 0o600
    compatibility = json.loads(path.read_text(encoding="utf-8"))
    assert compatibility["created_at"] == compatibility["issued_at_epoch"]
    assert {"token", "uid", "account_id", "created_at"} <= set(compatibility)
    path.write_text(json.dumps({
        "token": "old-token", "uid": "TEST", "account_id": "TEST", "created_at": 900.0,
    }), encoding="utf-8")
    path.chmod(0o600)
    assert cache.load().token == "old-token"


def test_lock_is_exclusive_and_persistent(tmp_path):
    path = tmp_path / "state" / "auth.lock"
    with ExclusiveAuthLock(path, 1):
        with path.open("r+", encoding="utf-8") as stream:
            with pytest.raises(BlockingIOError):
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert path.exists()
    with path.open("r+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def test_redirect_code_accepts_exact_registered_loopback_uri():
    entries = [_network_entry("http://127.0.0.1/?code=expected")]
    assert extract_authorization_code(entries, {"api.shoonya.com"}) == "expected"


@pytest.mark.parametrize("url", (
    "http://localhost/?code=wrong-host",
    "http://127.0.0.2/?code=wrong-host",
    "http://127.0.0.1:80/?code=explicit-port",
    "http://127.0.0.1/callback?code=wrong-path",
    "http://127.0.0.1/?code=value&state=extra",
    "http://127.0.0.1/?code=one&code=two",
    "http://127.0.0.1/?state=missing-code",
    "http://127.0.0.1/?code=",
    "http://user@127.0.0.1/?code=user-info",
    "http://127.0.0.1/?code=value#fragment",
))
def test_loopback_redirect_rejects_unapproved_variants(url):
    assert extract_authorization_code([_network_entry(url)], {"api.shoonya.com"}) is None


def test_redirect_code_preserves_narrow_https_support():
    entries = [
        _network_entry("http://api.shoonya.com/OAuthlogin/authorize/oauth?code=http"),
        _network_entry("https://evil.example/OAuthlogin/authorize/oauth?code=host"),
        _network_entry("https://api.shoonya.com/callback?code=path"),
        _network_entry("https://api.shoonya.com:443/OAuthlogin/authorize/oauth?code=port"),
        _network_entry("https://api.shoonya.com/OAuthlogin/authorize/oauth?code=expected"),
    ]
    assert extract_authorization_code(entries, {"api.shoonya.com"}) == "expected"
    assert extract_authorization_code(entries[:4], {"api.shoonya.com"}) is None


def test_selenium_capture_reads_only_performance_log():
    class Driver:
        def __init__(self):
            self.requested = []

        def get_log(self, name):
            self.requested.append(name)
            return [_network_entry("http://127.0.0.1/?code=selenium-code")]

    driver = Driver()
    assert capture_authorization_code(driver, {"api.shoonya.com"}) == "selenium-code"
    assert driver.requested == ["performance"]


def test_valid_cache_is_reused_without_browser_or_exchange(tmp_path):
    config = _config(tmp_path)
    SessionCache(Path(config.cache_path)).save(SessionRecordV1(
        token="cached-token", uid="TEST", account_id="TEST",
        issued_at_epoch=1000.0, validated_at_epoch=1000.0,
    ))
    api = FakeApi()
    browser = FakeBrowser()
    manager = AuthManager(
        config=config, credentials=_credentials(tmp_path), browser=browser,
        api_factory=Factory(api), clock=lambda: 1100.0,
    )
    client = manager.ensure_session(allow_oauth_login=True)
    assert browser.calls == 0
    assert api.injected == ("cached-token", "TEST", "TEST")
    assert client.get_daily_price_series(exchange="NSE") == [{"exchange": "NSE"}]
    assert not hasattr(client, "place_order")


def test_inconclusive_cache_validation_never_triggers_duplicate_login(tmp_path):
    config = _config(tmp_path)
    SessionCache(Path(config.cache_path)).save(SessionRecordV1(
        token="cached-token", uid="TEST", account_id="TEST",
        issued_at_epoch=1000.0, validated_at_epoch=1000.0,
    ))
    browser = FakeBrowser()
    manager = AuthManager(
        config=config, credentials=_credentials(tmp_path), browser=browser,
        api_factory=Factory(FakeApi(validation=TimeoutError())), clock=lambda: 1100.0,
    )
    with pytest.raises(ValidationInconclusive):
        manager.ensure_session(allow_oauth_login=True)
    assert browser.calls == 0


def test_missing_cache_requires_explicit_oauth_authorization(tmp_path):
    config = _config(tmp_path)
    browser = FakeBrowser()
    manager = AuthManager(
        config=config, credentials=_credentials(tmp_path), browser=browser,
        api_factory=Factory(FakeApi()), clock=lambda: 1100.0,
    )
    with pytest.raises(LoginRequired):
        manager.ensure_session(allow_oauth_login=False)
    assert browser.calls == 0


def test_fresh_login_validates_identity_and_commits_cache(tmp_path):
    config = _config(tmp_path)
    api = FakeApi(exchange=("new-token", "TEST", "ignored-refresh", "TEST"))
    browser = FakeBrowser()
    manager = AuthManager(
        config=config, credentials=_credentials(tmp_path), browser=browser,
        api_factory=Factory(api), clock=lambda: 2000.0,
    )
    manager.ensure_session(allow_oauth_login=True)
    record = SessionCache(Path(config.cache_path)).load()
    assert browser.calls == 1
    assert record.token == "new-token"
    assert api.injected == ("new-token", "TEST", "TEST")


def test_fresh_login_wrong_account_is_rejected_without_cache(tmp_path):
    config = _config(tmp_path)
    api = FakeApi(exchange=("new-token", "TEST", "ignored-refresh", "OTHER"))
    manager = AuthManager(
        config=config, credentials=_credentials(tmp_path), browser=FakeBrowser(),
        api_factory=Factory(api), clock=lambda: 2000.0,
    )
    with pytest.raises(SecurityBoundaryError):
        manager.ensure_session(allow_oauth_login=True)
    assert not Path(config.cache_path).exists()


def test_audit_redacts_sensitive_field_names(tmp_path):
    audit = tmp_path / "state" / "audit.jsonl"
    status = tmp_path / "state" / "status.json"
    logger = AuditLogger(audit, status)
    logger.event(
        "TEST_EVENT", run_id="run", token="never-write-me",
        redirect_url="https://example.test/?code=never-write-me", detail="safe",
    )
    content = audit.read_text(encoding="utf-8")
    assert "never-write-me" not in content
    assert '"detail":"safe"' in content


def test_cache_rejects_duplicate_keys_and_unsafe_permissions(tmp_path):
    path = tmp_path / "session.json"
    path.write_text('{"token":"a","token":"b"}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="duplicate"):
        SessionCache(path).load()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        SessionCache(path).load()


def test_cache_rejects_dangling_symlink(tmp_path):
    path = tmp_path / "session.json"
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="symlink"):
        SessionCache(path).load()


class _FormElement:
    def __init__(self, *, displayed=True, enabled=True):
        self._displayed, self._enabled = displayed, enabled

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled


class _FormDriver:
    """Returns scripted find_elements results; Exception instances are raised."""

    def __init__(self, script):
        self.script = list(script)
        self.selectors = []

    def find_elements(self, by, selector):
        self.selectors.append((by, selector))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _fake_time():
    now = [0.0]

    def sleeper(seconds):
        now[0] += seconds

    return sleeper, lambda: now[0]


def test_login_form_wait_survives_slow_render_and_transient_driver_stalls():
    sleeper, monotonic = _fake_time()
    driver = _FormDriver([
        [], TimeoutError("driver read timeout"), [_FormElement(displayed=False)],
        [_FormElement(enabled=False)], [_FormElement()],
    ])
    wait_for_login_form(driver, 100, transient=(TimeoutError,), sleeper=sleeper, monotonic=monotonic)
    assert driver.selectors == [("css selector", "input[type='password']")] * 5
    assert monotonic() == 4.0


def test_login_form_wait_is_bounded_and_classified():
    sleeper, monotonic = _fake_time()
    driver = _FormDriver([[] for _ in range(50)])
    with pytest.raises(BrowserPageNotReady) as caught:
        wait_for_login_form(driver, 5, sleeper=sleeper, monotonic=monotonic)
    assert caught.value.classification == "BROWSER_PAGE_NOT_READY"
    assert monotonic() == 5.0


def test_login_form_wait_does_not_swallow_unlisted_errors():
    sleeper, monotonic = _fake_time()
    with pytest.raises(ValueError):
        wait_for_login_form(_FormDriver([ValueError("bug")]), 5, sleeper=sleeper, monotonic=monotonic)


def test_page_ready_bound_covers_observed_oracle_render_time():
    assert 90 < PAGE_READY_TIMEOUT_SECONDS < 300
