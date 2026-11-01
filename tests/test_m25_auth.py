from __future__ import annotations

import fcntl
import json
import os
import select
import signal
import stat
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

import stock_gplm.m25.browser as browser_module
from stock_gplm.m25.browser import (
    BROWSER_CLEANUP_TIMEOUT_SECONDS, BROWSER_TOTAL_TIMEOUT_SECONDS,
    CHROMEDRIVER_STARTUP_TIMEOUT_SECONDS, PAGE_READY_TIMEOUT_SECONDS, _command,
    SeleniumOAuthBrowser, authenticate_once, capture_authorization_code,
    extract_authorization_code, wait_for_login_form,
)
from stock_gplm.m25.credentials import FileCredentialProvider, _private_permissions
from stock_gplm.m25.errors import (
    AmbiguousAuthenticationOutcome, BrowserPageNotReady, LoginRequired,
    OperationDeadlineExceeded, ProviderTimeout, SecurityBoundaryError,
    ValidationInconclusive,
)
from stock_gplm.m25.deadline import AUTH_OPERATION_TIMEOUT_SECONDS, authentication_deadline
from stock_gplm.m25.models import AuthConfigV1, SessionRecordV1
from stock_gplm.m25.security import AuditLogger, ExclusiveAuthLock, SessionCache
from stock_gplm.m25.session import AuthManager, Validation, validate_session
from stock_gplm.m25.noren import PROVIDER_HTTP_TIMEOUT, _BoundedRequests
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


def _read_process_report(descriptor: int, timeout: float = 3.0) -> tuple[int, int, int]:
    readable, _, _ = select.select([descriptor], [], [], timeout)
    if not readable:
        raise AssertionError("lifecycle subprocess did not report readiness")
    raw = os.read(descriptor, 128)
    if not raw:
        raise AssertionError("lifecycle subprocess exited before reporting readiness")
    worker, descendant, process_group = (int(item) for item in raw.decode("ascii").split())
    return worker, descendant, process_group


def _pid_is_running(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return False
    state = status.rsplit(")", 1)[1].strip().split()[0]
    return state != "Z"


def _wait_for_group_cleanup(
    worker: int,
    descendant: int,
    process_group: int,
    timeout: float = 8.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (
            not _pid_is_running(worker)
            and not _pid_is_running(descendant)
            and not browser_module._process_group_exists(process_group)
        ):
            return
        time.sleep(0.02)
    raise AssertionError("watchdog left a worker process group running")


def _lifecycle_worker_process(
    lifecycle_read_fd: int,
    lifecycle_write_fd: int,
    report_write_fd: int,
    ignore_sigterm: bool,
    exit_worker: bool,
) -> None:
    process_group, _watchdog_pid = browser_module._establish_process_group_lifecycle(
        lifecycle_read_fd,
        lifecycle_write_fd,
        inherited_fds=(report_write_fd,),
    )
    descendant = os.fork()
    if descendant == 0:
        _close_test_fd(report_write_fd)
        if ignore_sigterm:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            signal.pause()
    os.write(
        report_write_fd,
        f"{os.getpid()} {descendant} {process_group}".encode("ascii"),
    )
    _close_test_fd(report_write_fd)
    if exit_worker:
        os._exit(0)
    while True:
        signal.pause()


def _deadline_cleanup_worker(
    connection,
    _config,
    credentials,
    lifecycle_read_fd: int,
    lifecycle_write_fd: int,
    result_parent_fd: int,
    bootstrap_ack_read_fd: int,
    bootstrap_ack_write_fd: int,
) -> None:
    """Protocol-compatible worker with a real SIGTERM-resistant descendant."""
    _close_test_fd(result_parent_fd)
    _close_test_fd(bootstrap_ack_write_fd)
    process_group, _watchdog_pid = browser_module._establish_process_group_lifecycle(
        lifecycle_read_fd,
        lifecycle_write_fd,
        inherited_fds=(connection.fileno(), bootstrap_ack_read_fd, credentials.report_write_fd),
    )
    connection.send((browser_module.PROCESS_GROUP_READY, str(process_group)))
    readable, _, _ = select.select(
        [bootstrap_ack_read_fd], [], [], browser_module.PROCESS_GROUP_BOOTSTRAP_TIMEOUT_SECONDS,
    )
    if not readable or os.read(bootstrap_ack_read_fd, 1) != b"1":
        os._exit(124)
    _close_test_fd(bootstrap_ack_read_fd)

    descendant = os.fork()
    if descendant == 0:
        connection.close()
        _close_test_fd(credentials.report_write_fd)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            signal.pause()
    os.write(
        credentials.report_write_fd,
        f"{os.getpid()} {descendant} {process_group}".encode("ascii"),
    )
    _close_test_fd(credentials.report_write_fd)
    time.sleep(credentials.result_delay)
    connection.send(("BROWSER_SESSION_UNRESOLVED", ""))
    connection.close()
    while True:
        signal.pause()


def _close_test_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _premature_parent_process(report_write_fd: int) -> None:
    lifecycle_read_fd, lifecycle_write_fd = os.pipe2(os.O_CLOEXEC)
    inner_read_fd, inner_write_fd = os.pipe2(os.O_CLOEXEC)
    worker = os.fork()
    if worker == 0:
        _close_test_fd(inner_read_fd)
        _close_test_fd(report_write_fd)
        _lifecycle_worker_process(
            lifecycle_read_fd,
            lifecycle_write_fd,
            inner_write_fd,
            True,
            False,
        )
        os._exit(127)
    _close_test_fd(lifecycle_read_fd)
    _close_test_fd(inner_write_fd)
    try:
        reported_worker, descendant, process_group = _read_process_report(inner_read_fd)
        if reported_worker != worker:
            os._exit(126)
        os.write(
            report_write_fd,
            f"{worker} {descendant} {process_group}".encode("ascii"),
        )
    finally:
        _close_test_fd(inner_read_fd)
        _close_test_fd(report_write_fd)
    # Holding lifecycle_write_fd represents the live service parent. The test
    # kills this process with SIGKILL; kernel FD closure must trigger cleanup.
    while True:
        signal.pause()


def _startup_race_parent_process(report_write_fd: int) -> None:
    """Delay the worker before setsid so the service parent dies in that window."""
    lifecycle_read_fd, lifecycle_write_fd = os.pipe2(os.O_CLOEXEC)
    worker = os.fork()
    if worker == 0:
        time.sleep(0.25)
        process_group, _watchdog_pid = browser_module._establish_process_group_lifecycle(
            lifecycle_read_fd,
            lifecycle_write_fd,
            inherited_fds=(report_write_fd,),
        )
        descendant = os.fork()
        if descendant == 0:
            _close_test_fd(report_write_fd)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                signal.pause()
        os.write(
            report_write_fd,
            f"D {worker} {descendant} {process_group}\n".encode("ascii"),
        )
        _close_test_fd(report_write_fd)
        while True:
            signal.pause()
    _close_test_fd(lifecycle_read_fd)
    os.write(report_write_fd, f"S {worker}\n".encode("ascii"))
    _close_test_fd(report_write_fd)
    # Keep only lifecycle_write_fd open until the test kills this parent.
    while True:
        signal.pause()


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


@pytest.mark.parametrize(
    ("field", "value"), (("lock_timeout_seconds", 31), ("browser_timeout_seconds", 91)),
)
def test_config_cannot_expand_reviewed_timeout_budget(tmp_path, field, value):
    payload = _config(tmp_path).to_dict()
    payload[field] = value
    with pytest.raises(ValueError, match=field):
        AuthConfigV1.from_dict(payload)


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


READY = {
    "locationOk": True, "titleOk": True, "typesOk": True, "challenge": False,
    "inputsReady": True, "buttonReady": True,
}


class _ScriptDriver:
    """Scriptable atomic page-contract, fill, submit, and log commands."""

    def __init__(self, *, contracts=None, fill=None, submit=None, logs=None):
        self.contracts = list(contracts or [READY])
        self.fill = {"filled": True} if fill is None else fill
        self.submit = {"submitted": True} if submit is None else submit
        self.logs = list(logs or [])
        self.get_calls = 0
        self.contract_calls = 0
        self.fill_calls = 0
        self.submit_calls = 0
        self.log_calls = 0

    def get(self, _url):
        self.get_calls += 1

    def execute_script(self, script, *args):
        if "return {filled: true}" in script:
            self.fill_calls += 1
            value = self.fill
        elif "return {submitted: true}" in script:
            self.submit_calls += 1
            value = self.submit
        else:
            self.contract_calls += 1
            value = self.contracts.pop(0) if len(self.contracts) > 1 else self.contracts[0]
        if isinstance(value, BaseException):
            raise value
        return value

    def get_log(self, _name):
        self.log_calls += 1
        value = self.logs.pop(0) if self.logs else []
        if isinstance(value, BaseException):
            raise value
        return value


def _fake_time():
    now = [0.0]

    def sleeper(seconds):
        now[0] += seconds

    return sleeper, lambda: now[0]


def test_login_form_wait_allows_only_completed_not_ready_polls():
    sleeper, monotonic = _fake_time()
    not_ready = dict(READY, titleOk=False, typesOk=False, inputsReady=False)
    driver = _ScriptDriver(contracts=[not_ready, not_ready, READY])
    wait_for_login_form(driver, 100, sleeper=sleeper, monotonic=monotonic)
    assert driver.contract_calls == 3
    assert monotonic() == 2.0


def test_login_form_wait_is_bounded_and_classified():
    sleeper, monotonic = _fake_time()
    driver = _ScriptDriver(contracts=[dict(READY, buttonReady=False)])
    with pytest.raises(BrowserPageNotReady) as caught:
        wait_for_login_form(driver, 5, sleeper=sleeper, monotonic=monotonic)
    assert caught.value.classification == "BROWSER_PAGE_NOT_READY"
    assert monotonic() == 5.0


def test_login_form_wait_never_retries_an_unresolved_driver_command():
    sleeper, monotonic = _fake_time()
    with pytest.raises(ValueError):
        driver = _ScriptDriver(contracts=[ValueError("driver unresolved")])
        wait_for_login_form(driver, 5, sleeper=sleeper, monotonic=monotonic)
    assert driver.contract_calls == 1


def test_unexpected_origin_is_rejected_before_credentials():
    sleeper, monotonic = _fake_time()
    driver = _ScriptDriver(contracts=[dict(READY, locationOk=False)])
    with pytest.raises(SecurityBoundaryError):
        authenticate_once(
            driver, _config(Path("/tmp/m25-origin-test")),
            SimpleNamespace(password="p", totp_secret="JBSWY3DPEHPK3PXP"),
            sleeper=sleeper, clock=lambda: 60.0, monotonic=monotonic,
        )
    assert driver.fill_calls == driver.submit_calls == 0


def test_disabled_control_blocks_all_credential_entry():
    sleeper, monotonic = _fake_time()
    driver = _ScriptDriver(contracts=[dict(READY, inputsReady=False)])
    with pytest.raises(BrowserPageNotReady):
        authenticate_once(
            driver, _config(Path("/tmp/m25-disabled-test")),
            SimpleNamespace(password="p", totp_secret="JBSWY3DPEHPK3PXP"),
            sleeper=sleeper, clock=lambda: 60.0, monotonic=monotonic,
        )
    assert driver.fill_calls == driver.submit_calls == 0


def test_ambiguous_submission_is_never_resubmitted():
    sleeper, monotonic = _fake_time()
    driver = _ScriptDriver(submit=TimeoutError("unknown click outcome"))
    with pytest.raises(AmbiguousAuthenticationOutcome):
        authenticate_once(
            driver, _config(Path("/tmp/m25-submit-test")),
            SimpleNamespace(password="p", totp_secret="JBSWY3DPEHPK3PXP"),
            sleeper=sleeper, clock=lambda: 60.0, monotonic=monotonic,
        )
    assert driver.fill_calls == 1
    assert driver.submit_calls == 1
    assert driver.log_calls == 0


def test_no_redirect_after_submission_is_ambiguous_without_retry():
    sleeper, monotonic = _fake_time()
    driver = _ScriptDriver()
    with pytest.raises(AmbiguousAuthenticationOutcome):
        authenticate_once(
            driver, _config(Path("/tmp/m25-no-code-test")),
            SimpleNamespace(password="p", totp_secret="JBSWY3DPEHPK3PXP"),
            sleeper=sleeper, clock=lambda: 60.0, monotonic=monotonic,
        )
    assert driver.submit_calls == 1


def test_browser_command_stall_expires_without_second_command():
    calls = []

    def stall():
        calls.append("started")
        time.sleep(1)

    with pytest.raises(TimeoutError):
        _command(stall, time.monotonic() + 0.01, time.monotonic)
    assert calls == ["started"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux process lifecycle test")
@pytest.mark.parametrize("ignore_sigterm", (False, True))
def test_worker_exit_reaps_real_descendants_including_sigterm_resistant(ignore_sigterm):
    lifecycle_read_fd, lifecycle_write_fd = os.pipe2(os.O_CLOEXEC)
    report_read_fd, report_write_fd = os.pipe2(os.O_CLOEXEC)
    context = browser_module.multiprocessing.get_context("fork")
    process = context.Process(
        target=_lifecycle_worker_process,
        args=(
            lifecycle_read_fd,
            lifecycle_write_fd,
            report_write_fd,
            ignore_sigterm,
            True,
        ),
    )
    process.start()
    _close_test_fd(lifecycle_read_fd)
    _close_test_fd(report_write_fd)
    worker = descendant = process_group = -1
    try:
        worker, descendant, process_group = _read_process_report(report_read_fd)
        assert worker == process.pid == process_group
        process.join(3)
        assert not process.is_alive()
        # Keep the service-parent lifecycle writer open: this specifically proves
        # that watchdog detection of worker exit is sufficient on its own.
        _wait_for_group_cleanup(worker, descendant, process_group)
    finally:
        _close_test_fd(report_read_fd)
        _close_test_fd(lifecycle_write_fd)
        if process_group > 0 and browser_module._process_group_exists(process_group):
            os.killpg(process_group, signal.SIGKILL)
        if process.is_alive():
            process.kill()
        process.join(1)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux process lifecycle test")
def test_parent_cleanup_kills_real_sigterm_resistant_descendant():
    lifecycle_read_fd, lifecycle_write_fd = os.pipe2(os.O_CLOEXEC)
    report_read_fd, report_write_fd = os.pipe2(os.O_CLOEXEC)
    context = browser_module.multiprocessing.get_context("fork")
    process = context.Process(
        target=_lifecycle_worker_process,
        args=(lifecycle_read_fd, lifecycle_write_fd, report_write_fd, True, False),
    )
    process.start()
    _close_test_fd(lifecycle_read_fd)
    _close_test_fd(report_write_fd)
    worker = descendant = process_group = -1
    lifecycle_open = True
    try:
        worker, descendant, process_group = _read_process_report(report_read_fd)
        assert worker == process.pid == process_group
        browser_module._terminate_process_group(
            process,
            lifecycle_write_fd,
            process_group,
        )
        lifecycle_open = False
        assert not _pid_is_running(worker)
        assert not _pid_is_running(descendant)
        assert not browser_module._process_group_exists(process_group)
    finally:
        _close_test_fd(report_read_fd)
        if lifecycle_open:
            _close_test_fd(lifecycle_write_fd)
        if process_group > 0 and browser_module._process_group_exists(process_group):
            os.killpg(process_group, signal.SIGKILL)
        if process.is_alive():
            process.kill()
        process.join(1)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux process lifecycle test")
def test_application_deadline_is_deferred_until_sigterm_sigkill_cleanup_finishes(
    monkeypatch,
    tmp_path,
):
    report_read_fd, report_write_fd = os.pipe2(os.O_CLOEXEC)
    monkeypatch.setattr(browser_module, "_worker", _deadline_cleanup_worker)
    monkeypatch.setattr(browser_module, "PROCESS_GROUP_SIGTERM_GRACE_SECONDS", 0.35)
    monkeypatch.setattr(browser_module, "BROWSER_CLEANUP_TIMEOUT_SECONDS", 2)
    worker = descendant = process_group = -1
    try:
        with pytest.raises(OperationDeadlineExceeded):
            with authentication_deadline(0.2):
                SeleniumOAuthBrowser(_config(tmp_path)).acquire_code(SimpleNamespace(
                    report_write_fd=report_write_fd,
                    result_delay=0.05,
                ))
        _close_test_fd(report_write_fd)
        report_write_fd = -1
        worker, descendant, process_group = _read_process_report(report_read_fd)
        assert worker == process_group
        assert not _pid_is_running(worker)
        assert not _pid_is_running(descendant)
        assert not browser_module._process_group_exists(process_group)
    finally:
        _close_test_fd(report_read_fd)
        _close_test_fd(report_write_fd)
        if process_group > 0 and browser_module._process_group_exists(process_group):
            os.killpg(process_group, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux process lifecycle test")
def test_cleanup_accepts_a_process_group_that_already_disappeared():
    lifecycle_read_fd, lifecycle_write_fd = os.pipe2(os.O_CLOEXEC)
    report_read_fd, report_write_fd = os.pipe2(os.O_CLOEXEC)
    context = browser_module.multiprocessing.get_context("fork")
    process = context.Process(
        target=_lifecycle_worker_process,
        args=(lifecycle_read_fd, lifecycle_write_fd, report_write_fd, False, True),
    )
    process.start()
    _close_test_fd(lifecycle_read_fd)
    _close_test_fd(report_write_fd)
    worker = descendant = process_group = -1
    try:
        worker, descendant, process_group = _read_process_report(report_read_fd)
        assert worker == process.pid == process_group
        process.join(3)
        assert not process.is_alive()
        _wait_for_group_cleanup(worker, descendant, process_group)
        browser_module._terminate_process_group(process, lifecycle_write_fd, process_group)
        lifecycle_write_fd = -1
        assert not process.is_alive()
        assert not browser_module._process_group_exists(process_group)
    finally:
        _close_test_fd(report_read_fd)
        _close_test_fd(lifecycle_write_fd)
        if process_group > 0 and browser_module._process_group_exists(process_group):
            os.killpg(process_group, signal.SIGKILL)
        if process.is_alive():
            process.kill()
        process.join(1)


@pytest.mark.parametrize("unsafe_process_group", (0, -1))
def test_process_group_probe_rejects_ids_that_could_target_the_caller(
    unsafe_process_group,
):
    with pytest.raises(ValueError, match="must be positive"):
        browser_module._process_group_exists(unsafe_process_group)


def test_watchdog_requires_exact_session_and_group_identity(monkeypatch):
    monkeypatch.setattr(browser_module.os, "getpgrp", lambda: 101)
    monkeypatch.setattr(browser_module.os, "getsid", lambda _pid: 202)
    assert browser_module._watchdog_still_anchors_group(101, 202)
    assert not browser_module._watchdog_still_anchors_group(102, 202)
    assert not browser_module._watchdog_still_anchors_group(101, 203)


def test_group_leader_is_not_reaped_before_final_signal_decision(monkeypatch):
    events = []

    class TrackingProcess:
        def join(self, _timeout):
            events.append("join")

        def is_alive(self):
            return False

    group_live = True

    def group_exists(_process_group):
        return group_live

    def signal_group(_process_group, requested_signal):
        nonlocal group_live
        events.append(requested_signal)
        group_live = False

    monkeypatch.setattr(
        browser_module,
        "_process_group_exists",
        group_exists,
    )
    monkeypatch.setattr(browser_module, "PROCESS_GROUP_SIGTERM_GRACE_SECONDS", 0)
    monkeypatch.setattr(
        browser_module.os,
        "killpg",
        signal_group,
    )
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    _close_test_fd(read_fd)

    browser_module._terminate_process_group(TrackingProcess(), write_fd, 12345)

    assert events == [signal.SIGKILL, "join"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux process lifecycle test")
def test_premature_service_parent_death_reaps_worker_and_resistant_descendant():
    report_read_fd, report_write_fd = os.pipe2(os.O_CLOEXEC)
    supervisor = os.fork()
    if supervisor == 0:
        _close_test_fd(report_read_fd)
        _premature_parent_process(report_write_fd)
        os._exit(127)

    _close_test_fd(report_write_fd)
    worker = descendant = process_group = -1
    try:
        worker, descendant, process_group = _read_process_report(report_read_fd)
        assert worker == process_group
        assert _pid_is_running(worker)
        assert _pid_is_running(descendant)
        os.kill(supervisor, signal.SIGKILL)
        os.waitpid(supervisor, 0)
        supervisor = -1
        _wait_for_group_cleanup(worker, descendant, process_group)
    finally:
        _close_test_fd(report_read_fd)
        if supervisor > 0:
            try:
                os.kill(supervisor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(supervisor, 0)
        if process_group > 0 and browser_module._process_group_exists(process_group):
            os.killpg(process_group, signal.SIGKILL)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux process lifecycle test")
def test_parent_death_before_setsid_cannot_leave_a_process_group():
    report_read_fd, report_write_fd = os.pipe2(os.O_CLOEXEC)
    supervisor = os.fork()
    if supervisor == 0:
        _close_test_fd(report_read_fd)
        _startup_race_parent_process(report_write_fd)
        os._exit(127)

    _close_test_fd(report_write_fd)
    worker = descendant = -1
    stream = os.fdopen(report_read_fd, "rb", buffering=0)
    try:
        readable, _, _ = select.select([stream], [], [], 3)
        assert readable
        first = stream.readline().decode("ascii").split()
        assert first[0] == "S"
        worker = int(first[1])
        os.kill(supervisor, signal.SIGKILL)
        os.waitpid(supervisor, 0)
        supervisor = -1

        # The delayed worker may be killed before it can spawn its probe child,
        # or it may report the child just before the watchdog's SIGTERM. Both
        # schedules must converge to an empty pid==pgid process group.
        readable, _, _ = select.select([stream], [], [], 1)
        if readable:
            second = stream.readline().decode("ascii").split()
            if second:
                assert second[0] == "D"
                assert int(second[1]) == worker == int(second[3])
                descendant = int(second[2])
        _wait_for_group_cleanup(worker, descendant if descendant > 0 else worker, worker)
    finally:
        stream.close()
        if supervisor > 0:
            try:
                os.kill(supervisor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(supervisor, 0)
        if worker > 0 and browser_module._process_group_exists(worker):
            os.killpg(worker, signal.SIGKILL)


def test_authentication_wall_clock_deadline_expires():
    with pytest.raises(OperationDeadlineExceeded):
        with authentication_deadline(0.01):
            time.sleep(1)


def test_provider_transport_forces_connection_and_read_timeouts():
    calls = []

    class Timeout(Exception):
        pass

    class Requests:
        exceptions = SimpleNamespace(Timeout=Timeout)

        @staticmethod
        def post(*args, **kwargs):
            calls.append((args, kwargs))
            raise Timeout()

    with pytest.raises(ProviderTimeout):
        _BoundedRequests(Requests()).post("https://api.shoonya.test/path", data="x")
    assert calls[0][1]["timeout"] == PROVIDER_HTTP_TIMEOUT


def test_maximum_timeout_budget_matches_hardened_systemd_unit():
    service = Path("deploy/oracle/m25/stock-gplm-auth.service").read_text(encoding="utf-8")
    assert AUTH_OPERATION_TIMEOUT_SECONDS == 360
    assert browser_module.PROCESS_GROUP_BOOTSTRAP_TIMEOUT_SECONDS == 5
    assert CHROMEDRIVER_STARTUP_TIMEOUT_SECONDS == 25
    assert PAGE_READY_TIMEOUT_SECONDS == 150
    assert BROWSER_TOTAL_TIMEOUT_SECONDS == 280
    assert BROWSER_CLEANUP_TIMEOUT_SECONDS == 10
    assert browser_module.PROCESS_GROUP_SIGTERM_GRACE_SECONDS < BROWSER_CLEANUP_TIMEOUT_SECONDS
    assert 5 + 25 + 150 + 90 + 10 == BROWSER_TOTAL_TIMEOUT_SECONDS
    assert AUTH_OPERATION_TIMEOUT_SECONDS + BROWSER_CLEANUP_TIMEOUT_SECONDS < 390
    assert "TimeoutStartSec=390" in service
    assert "TimeoutStopSec=30" in service
    assert "KillMode=control-group" in service
    assert "--no-sandbox" not in Path("src/stock_gplm/m25/browser.py").read_text(encoding="utf-8")


def test_page_ready_bound_covers_observed_oracle_render_time():
    assert 90 < PAGE_READY_TIMEOUT_SECONDS < 300
