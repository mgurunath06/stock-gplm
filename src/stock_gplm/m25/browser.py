"""Fail-closed, single-submission Selenium adapter for Shoonya OAuth."""
from __future__ import annotations

import json
import multiprocessing
import os
import select
import signal
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import parse_qsl, urlencode, urlparse

from .credentials import Credentials
from .errors import (
    AmbiguousAuthenticationOutcome,
    BrowserPageNotReady,
    BrowserSessionUnresolved,
    InteractionRequired,
    SecurityBoundaryError,
    UiContractChanged,
)
from .models import AuthConfigV1
from .totp import generate_totp, seconds_remaining

AUTHORIZE_ENDPOINT = "https://api.shoonya.com/OAuthlogin/investor-entry-level/login"
EXPECTED_LOGIN_ORIGIN = "https://api.shoonya.com"
EXPECTED_LOGIN_PATH = "/OAuthlogin/investor-entry-level/login"
EXPECTED_TITLE_MARKER = "SHOONYA"
REGISTERED_LOOPBACK_REDIRECT = "http://127.0.0.1"
APPROVED_HTTPS_REDIRECT_PATHS = frozenset({"/OAuthlogin/authorize/oauth"})

# These phase limits form the browser portion of the documented 360-second
# end-to-end service budget. They are constants so config cannot enlarge it.
CHROMEDRIVER_STARTUP_TIMEOUT_SECONDS = 25
PAGE_READY_TIMEOUT_SECONDS = 150
BROWSER_CLEANUP_TIMEOUT_SECONDS = 10
BROWSER_TOTAL_TIMEOUT_SECONDS = 280
PROCESS_GROUP_BOOTSTRAP_TIMEOUT_SECONDS = 5
PROCESS_GROUP_POLL_SECONDS = 0.05
PROCESS_GROUP_SIGTERM_GRACE_SECONDS = 2
PROCESS_GROUP_READY = "PROCESS_GROUP_READY"


class AuthenticationState(Enum):
    STARTING = "STARTING"
    PAGE_VERIFIED = "PAGE_VERIFIED"
    CREDENTIALS_FILLED = "CREDENTIALS_FILLED"
    SUBMITTED = "SUBMITTED"
    CODE_OBSERVED = "CODE_OBSERVED"


@dataclass
class AuthenticationAttempt:
    """Linear fail-closed state with a hard one-submission invariant."""

    state: AuthenticationState = AuthenticationState.STARTING
    submissions: int = 0

    def transition(self, expected: AuthenticationState, target: AuthenticationState) -> None:
        if self.state is not expected:
            raise SecurityBoundaryError("invalid authentication state transition")
        self.state = target

    def begin_submission(self) -> None:
        if self.state is not AuthenticationState.CREDENTIALS_FILLED or self.submissions != 0:
            raise SecurityBoundaryError("authentication attempt cannot submit credentials again")
        self.submissions = 1
        self.state = AuthenticationState.SUBMITTED


class _CommandExpired(TimeoutError):
    """A Selenium command did not resolve; the session must be abandoned."""


@contextmanager
def _command_alarm(seconds: float) -> Iterator[None]:
    """Interrupt one child-process command; never used in the service parent."""
    if seconds <= 0:
        raise _CommandExpired("browser command deadline expired")

    def expired(_signum: int, _frame: Any) -> None:
        raise _CommandExpired("browser command deadline expired")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _command(call: Callable[[], Any], deadline: float, monotonic: Callable[[], float]) -> Any:
    with _command_alarm(deadline - monotonic()):
        return call()


def _approved_redirect(target: Any, allowed_hosts: set[str]) -> bool:
    """Allow the registered loopback URI or a narrowly approved HTTPS target."""
    if target.username is not None or target.password is not None or target.fragment or target.params:
        return False
    try:
        port = target.port
    except ValueError:
        return False
    if port is not None:
        return False
    if target.scheme == "http":
        return target.hostname == "127.0.0.1" and target.path in {"", "/"}
    return (
        target.scheme == "https"
        and target.hostname in allowed_hosts
        and target.path in APPROVED_HTTPS_REDIRECT_PATHS
    )


def extract_authorization_code(entries: Iterable[dict[str, Any]], allowed_hosts: set[str]) -> str | None:
    """Extract one bounded code from an exact approved redirect contract."""
    for entry in entries:
        try:
            message = json.loads(entry["message"])["message"]
            if message.get("method") != "Network.requestWillBeSent":
                continue
            target = urlparse(message["params"]["request"]["url"])
            if not _approved_redirect(target, allowed_hosts):
                continue
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True)
            if len(pairs) != 1 or pairs[0][0] != "code":
                continue
            code = pairs[0][1]
            if 0 < len(code) <= 2048 and not any(ord(character) < 0x20 for character in code):
                return code
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def capture_authorization_code(driver: Any, allowed_hosts: set[str]) -> str | None:
    """Read Selenium performance logs without exposing their redirect URLs."""
    return extract_authorization_code(driver.get_log("performance"), allowed_hosts)


# One browser-side predicate defines the expected page identity and control
# readiness. It is also executed immediately before filling and immediately
# before the single click, eliminating a Python-side origin-check/fill gap.
_CONTRACT_PREAMBLE = r"""
const expectedOrigin = arguments[0];
const expectedPath = arguments[1];
const titleMarker = arguments[2];
const inputs = Array.from(document.querySelectorAll(
  "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])"));
const buttons = Array.from(document.querySelectorAll("button"))
  .filter((item) => item.textContent.trim() === "LOGIN");
const interactable = (item) => {
  const style = window.getComputedStyle(item);
  const rect = item.getBoundingClientRect();
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  const topmost = document.elementFromPoint(centerX, centerY);
  return !item.matches(":disabled") && !item.readOnly && style.display !== "none" &&
    style.visibility !== "hidden" && style.pointerEvents !== "none" &&
    style.opacity !== "0" && rect.width > 0 && rect.height > 0 &&
    centerX >= 0 && centerY >= 0 && centerX <= window.innerWidth &&
    centerY <= window.innerHeight && (topmost === item || item.contains(topmost));
};
const types = inputs.map((item) => (item.type || "text").toLowerCase());
const locationOk = window.location.origin === expectedOrigin &&
  window.location.pathname === expectedPath && !window.location.username &&
  !window.location.password && !window.location.port;
const titleOk = document.title.toUpperCase().includes(titleMarker);
const typesOk = types.length === 3 && ["text", "email", "tel"].includes(types[0]) &&
  types[1] === "password" && ["text", "tel", "number", "password"].includes(types[2]);
const challenge = /captcha|recaptcha|hcaptcha/i.test(document.body?.innerText || "");
"""

_PAGE_CONTRACT_SCRIPT = _CONTRACT_PREAMBLE + r"""
return {locationOk, titleOk, typesOk, challenge,
  inputsReady: typesOk && inputs.every(interactable),
  buttonReady: buttons.length === 1 && interactable(buttons[0])};
"""

_FILL_SCRIPT = _CONTRACT_PREAMBLE + r"""
if (!(locationOk && titleOk && typesOk && !challenge &&
      inputs.every(interactable) && buttons.length === 1 && interactable(buttons[0]))) {
  return {filled: false};
}
const values = [arguments[3], arguments[4], arguments[5]];
inputs.forEach((item, index) => {
  const setter = Object.getOwnPropertyDescriptor(
    Object.getPrototypeOf(item), "value").set;
  setter.call(item, values[index]);
  item.dispatchEvent(new Event("input", {bubbles: true}));
  item.dispatchEvent(new Event("change", {bubbles: true}));
});
return {filled: true};
"""

_SUBMIT_SCRIPT = _CONTRACT_PREAMBLE + r"""
if (!(locationOk && titleOk && typesOk && !challenge &&
      inputs.every(interactable) && buttons.length === 1 && interactable(buttons[0]))) {
  return {submitted: false};
}
buttons[0].click();
return {submitted: true};
"""


def _check_contract(result: Any) -> bool:
    if not isinstance(result, dict):
        raise UiContractChanged("Shoonya login page identity check returned an unsupported result")
    if result.get("challenge"):
        raise InteractionRequired("Shoonya presented an interactive challenge")
    if result.get("locationOk") is False:
        raise SecurityBoundaryError("Shoonya login page origin or path was unexpected")
    # With page-load strategy ``none``, title, inputs, and the button legitimately
    # do not exist during early SPA rendering. They may only become eligible
    # together within the readiness bound; credentials are never supplied while
    # any identity or control predicate is false.
    return bool(
        result.get("titleOk")
        and result.get("typesOk")
        and result.get("inputsReady")
        and result.get("buttonReady")
    )


def wait_for_login_form(
    driver: Any,
    timeout_seconds: float,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for exact page identity and every control to be interactable.

    A Selenium exception is deliberately not retried: command completion is then
    ambiguous, so the isolated browser session must be discarded by the parent.
    """
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        result = _command(
            lambda: driver.execute_script(
                _PAGE_CONTRACT_SCRIPT,
                EXPECTED_LOGIN_ORIGIN,
                EXPECTED_LOGIN_PATH,
                EXPECTED_TITLE_MARKER,
            ),
            deadline,
            monotonic,
        )
        if _check_contract(result):
            return
        sleeper(min(1.0, max(0.0, deadline - monotonic())))
    raise BrowserPageNotReady("Shoonya login form did not become usable within the bound")


def authenticate_once(
    driver: Any,
    config: AuthConfigV1,
    credentials: Credentials,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Perform exactly one credential submission on one verified page."""
    attempt = AuthenticationAttempt()
    query = urlencode({"api_key": config.client_id, "route_to": config.user_id})
    page_deadline = monotonic() + PAGE_READY_TIMEOUT_SECONDS
    _command(lambda: driver.get(f"{AUTHORIZE_ENDPOINT}?{query}"), page_deadline, monotonic)
    wait_for_login_form(
        driver,
        max(0.0, page_deadline - monotonic()),
        sleeper=sleeper,
        monotonic=monotonic,
    )
    attempt.transition(AuthenticationState.STARTING, AuthenticationState.PAGE_VERIFIED)

    auth_deadline = monotonic() + config.browser_timeout_seconds
    remaining = seconds_remaining(at_time=clock())
    if remaining < config.totp_min_valid_seconds:
        sleeper(min(remaining + 0.25, max(0.0, auth_deadline - monotonic())))
    if monotonic() >= auth_deadline:
        raise BrowserPageNotReady("TOTP alignment exhausted the authentication bound")
    otp = generate_totp(credentials.totp_secret, at_time=clock())
    filled = _command(
        lambda: driver.execute_script(
            _FILL_SCRIPT,
            EXPECTED_LOGIN_ORIGIN,
            EXPECTED_LOGIN_PATH,
            EXPECTED_TITLE_MARKER,
            config.user_id,
            credentials.password,
            otp,
        ),
        auth_deadline,
        monotonic,
    )
    if not isinstance(filled, dict) or filled.get("filled") is not True:
        raise SecurityBoundaryError("Shoonya page changed before credentials could be entered")
    attempt.transition(AuthenticationState.PAGE_VERIFIED, AuthenticationState.CREDENTIALS_FILLED)

    # Advance before the click. A timeout is ambiguous and can never lead to a retry.
    attempt.begin_submission()
    try:
        submitted = _command(
            lambda: driver.execute_script(
                _SUBMIT_SCRIPT,
                EXPECTED_LOGIN_ORIGIN,
                EXPECTED_LOGIN_PATH,
                EXPECTED_TITLE_MARKER,
            ),
            auth_deadline,
            monotonic,
        )
    except BaseException as exc:
        raise AmbiguousAuthenticationOutcome(
            "credential submission command did not resolve"
        ) from exc
    if not isinstance(submitted, dict) or submitted.get("submitted") is not True:
        raise AmbiguousAuthenticationOutcome("credential submission outcome is ambiguous")

    allowed = set(config.allowed_redirect_hosts)
    while monotonic() < auth_deadline:
        try:
            code = _command(
                lambda: capture_authorization_code(driver, allowed),
                auth_deadline,
                monotonic,
            )
        except BaseException as exc:
            raise AmbiguousAuthenticationOutcome(
                "post-submission observation command did not resolve"
            ) from exc
        if code is not None:
            attempt.transition(AuthenticationState.SUBMITTED, AuthenticationState.CODE_OBSERVED)
            return code
        sleeper(min(0.5, max(0.0, auth_deadline - monotonic())))
    if attempt.state is AuthenticationState.SUBMITTED:
        raise AmbiguousAuthenticationOutcome(
            "no approved OAuth redirect was observed after the single submission"
        )
    raise BrowserPageNotReady("Shoonya OAuth flow exceeded the authentication bound")


def _close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch_process_group(
    lifecycle_read_fd: int,
    ready_write_fd: int,
    worker_pid: int,
    process_group: int,
    inherited_fds: tuple[int, ...],
) -> None:
    """Kill the complete worker group if its worker or service parent disappears."""
    for descriptor in inherited_fds:
        if descriptor not in {lifecycle_read_fd, ready_write_fd}:
            _close_fd(descriptor)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        os.write(ready_write_fd, b"1")
    finally:
        _close_fd(ready_write_fd)

    while True:
        if os.getppid() != worker_pid:
            break
        readable, _, _ = select.select(
            [lifecycle_read_fd], [], [], PROCESS_GROUP_POLL_SECONDS,
        )
        if readable:
            # The service parent never writes. EOF, data, or a descriptor error
            # is therefore a terminal lifecycle signal.
            try:
                os.read(lifecycle_read_fd, 1)
            except OSError:
                pass
            break

    _close_fd(lifecycle_read_fd)
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        os._exit(0)
    time.sleep(PROCESS_GROUP_SIGTERM_GRACE_SECONDS)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # SIGKILL includes this watchdog. Reaching the exit is harmless if the
    # process group disappeared between the two signal operations.
    os._exit(0)


def _establish_process_group_lifecycle(
    lifecycle_read_fd: int,
    lifecycle_write_fd: int,
    *,
    inherited_fds: tuple[int, ...] = (),
) -> tuple[int, int]:
    """Create a new session and a verified watchdog before any descendant starts."""
    # The forked worker inherited the service parent's write end. Closing it is
    # the first operation, so parent death is observable even before setsid().
    _close_fd(lifecycle_write_fd)
    os.setsid()
    worker_pid = os.getpid()
    process_group = os.getpgrp()
    if process_group != worker_pid:
        raise BrowserSessionUnresolved("browser worker did not become its process-group leader")

    ready_read_fd, ready_write_fd = os.pipe2(os.O_CLOEXEC)
    watchdog_pid = os.fork()
    if watchdog_pid == 0:
        _close_fd(ready_read_fd)
        _watch_process_group(
            lifecycle_read_fd,
            ready_write_fd,
            worker_pid,
            process_group,
            inherited_fds,
        )
        os._exit(127)

    _close_fd(ready_write_fd)
    try:
        readable, _, _ = select.select(
            [ready_read_fd], [], [], PROCESS_GROUP_BOOTSTRAP_TIMEOUT_SECONDS,
        )
        if not readable or os.read(ready_read_fd, 1) != b"1":
            raise BrowserSessionUnresolved("browser lifecycle watchdog did not become ready")
    finally:
        _close_fd(ready_read_fd)
    _close_fd(lifecycle_read_fd)
    return process_group, watchdog_pid


def _worker(
    connection: Any,
    config: AuthConfigV1,
    credentials: Credentials,
    lifecycle_read_fd: int,
    lifecycle_write_fd: int,
    result_parent_fd: int,
    bootstrap_ack_read_fd: int,
    bootstrap_ack_write_fd: int,
) -> None:
    """Own Selenium and all descendants in a watchdog-protected process group."""
    _close_fd(result_parent_fd)
    _close_fd(bootstrap_ack_write_fd)
    try:
        process_group, _watchdog_pid = _establish_process_group_lifecycle(
            lifecycle_read_fd,
            lifecycle_write_fd,
            inherited_fds=(
                connection.fileno(),
                result_parent_fd,
                bootstrap_ack_read_fd,
            ),
        )
        connection.send((PROCESS_GROUP_READY, str(process_group)))
        readable, _, _ = select.select(
            [bootstrap_ack_read_fd], [], [], PROCESS_GROUP_BOOTSTRAP_TIMEOUT_SECONDS,
        )
        if not readable or os.read(bootstrap_ack_read_fd, 1) != b"1":
            raise BrowserSessionUnresolved("browser parent did not confirm process-group identity")
    except BaseException:
        try:
            connection.send((BrowserSessionUnresolved.classification, ""))
        except BaseException:
            pass
        connection.close()
        _close_fd(bootstrap_ack_read_fd)
        return
    _close_fd(bootstrap_ack_read_fd)

    driver = None
    session_resolved = True
    result: tuple[str, str] = (BrowserSessionUnresolved.classification, "")
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        with tempfile.TemporaryDirectory(prefix="stock-gplm-auth-browser-") as profile:
            Path(profile).chmod(0o700)
            options = webdriver.ChromeOptions()
            options.binary_location = config.chrome_binary
            options.page_load_strategy = "none"
            for argument in (
                "--headless=new", "--disable-dev-shm-usage", "--disable-extensions",
                "--disable-sync", "--no-first-run", "--no-default-browser-check",
                "--password-store=basic", "--disable-background-networking",
                "--disable-features=AutofillServerCommunication",
                "--window-size=1920,1080", f"--user-data-dir={profile}",
            ):
                options.add_argument(argument)
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            startup_deadline = time.monotonic() + CHROMEDRIVER_STARTUP_TIMEOUT_SECONDS
            driver = _command(
                lambda: webdriver.Chrome(
                    service=Service(executable_path=config.chromedriver_path), options=options,
                ),
                startup_deadline,
                time.monotonic,
            )
            result = ("OK", authenticate_once(driver, config, credentials))
    except _CommandExpired:
        session_resolved = False
        result = (BrowserSessionUnresolved.classification, "")
    except AmbiguousAuthenticationOutcome:
        session_resolved = False
        result = (AmbiguousAuthenticationOutcome.classification, "")
    except (BrowserPageNotReady, InteractionRequired, SecurityBoundaryError, UiContractChanged) as exc:
        result = (exc.classification, "")
    except BaseException:
        # Driver errors can embed page or redirect data, so omit their messages.
        session_resolved = False
        result = (BrowserSessionUnresolved.classification, "")
    finally:
        if driver is not None and session_resolved:
            try:
                cleanup_deadline = time.monotonic() + BROWSER_CLEANUP_TIMEOUT_SECONDS
                _command(driver.quit, cleanup_deadline, time.monotonic)
            except BaseException:
                result = (BrowserSessionUnresolved.classification, "")
        try:
            connection.send(result)
        except BaseException:
            pass
        connection.close()


_ERRORS = {
    AmbiguousAuthenticationOutcome.classification: AmbiguousAuthenticationOutcome,
    BrowserPageNotReady.classification: BrowserPageNotReady,
    BrowserSessionUnresolved.classification: BrowserSessionUnresolved,
    InteractionRequired.classification: InteractionRequired,
    SecurityBoundaryError.classification: SecurityBoundaryError,
    UiContractChanged.classification: UiContractChanged,
}


def _terminate_process_group(
    process: multiprocessing.Process,
    lifecycle_write_fd: int,
    process_group: int | None,
) -> None:
    """Trigger watchdog cleanup, enforce SIGKILL, and prove the group is empty."""
    deadline = time.monotonic() + BROWSER_CLEANUP_TIMEOUT_SECONDS
    _close_fd(lifecycle_write_fd)

    if process_group is None:
        # Before PROCESS_GROUP_READY, _worker() is forbidden from spawning
        # Selenium or any other descendant. Terminating only the worker is safe.
        if process.is_alive():
            process.terminate()
        process.join(max(0.0, deadline - time.monotonic()) / 2)
        if process.is_alive():
            process.kill()
        process.join(max(0.0, deadline - time.monotonic()))
        if process.is_alive():
            raise BrowserSessionUnresolved("unready browser worker survived forced cleanup")
        return

    # The readiness handshake proves setsid() completed and pgid == worker pid.
    # Closing the lifecycle pipe asks the watchdog to send SIGTERM. After its
    # grace period, both watchdog and parent independently enforce SIGKILL.
    grace_deadline = min(
        deadline,
        time.monotonic() + PROCESS_GROUP_SIGTERM_GRACE_SECONDS + PROCESS_GROUP_POLL_SECONDS,
    )
    while _process_group_exists(process_group) and time.monotonic() < grace_deadline:
        process.join(min(PROCESS_GROUP_POLL_SECONDS, grace_deadline - time.monotonic()))
    if _process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass

    while _process_group_exists(process_group) and time.monotonic() < deadline:
        process.join(min(PROCESS_GROUP_POLL_SECONDS, deadline - time.monotonic()))
    process.join(max(0.0, deadline - time.monotonic()))
    if _process_group_exists(process_group) or process.is_alive():
        raise BrowserSessionUnresolved("browser process group did not terminate within cleanup bound")


class SeleniumOAuthBrowser:
    """Run one OAuth attempt in an isolated, killable browser process group."""

    def __init__(self, config: AuthConfigV1) -> None:
        self.config = config

    def acquire_code(self, credentials: Credentials) -> str:
        try:
            context = multiprocessing.get_context("fork")
        except ValueError as exc:
            raise RuntimeError("M2.5 requires Linux fork isolation for Selenium") from exc
        parent, child = context.Pipe(duplex=False)
        lifecycle_read_fd, lifecycle_write_fd = os.pipe2(os.O_CLOEXEC)
        bootstrap_ack_read_fd, bootstrap_ack_write_fd = os.pipe2(os.O_CLOEXEC)
        process = context.Process(
            target=_worker,
            args=(
                child,
                self.config,
                credentials,
                lifecycle_read_fd,
                lifecycle_write_fd,
                parent.fileno(),
                bootstrap_ack_read_fd,
                bootstrap_ack_write_fd,
            ),
        )
        process.start()
        child.close()
        _close_fd(lifecycle_read_fd)
        _close_fd(bootstrap_ack_read_fd)
        process_group: int | None = None
        started = time.monotonic()
        try:
            bootstrap_timeout = min(
                PROCESS_GROUP_BOOTSTRAP_TIMEOUT_SECONDS,
                BROWSER_TOTAL_TIMEOUT_SECONDS,
            )
            if not parent.poll(bootstrap_timeout):
                raise BrowserSessionUnresolved("browser process-group bootstrap timed out")
            try:
                classification, payload = parent.recv()
            except (EOFError, OSError) as exc:
                raise BrowserSessionUnresolved(
                    "browser worker exited before its lifecycle handshake"
                ) from exc
            if classification != PROCESS_GROUP_READY:
                raise BrowserSessionUnresolved("browser process-group bootstrap failed closed")
            try:
                process_group = int(payload)
            except (TypeError, ValueError) as exc:
                raise BrowserSessionUnresolved("browser worker returned an invalid process group") from exc
            if process_group != process.pid:
                process_group = None
                raise BrowserSessionUnresolved("browser process-group identity did not match worker")
            try:
                os.write(bootstrap_ack_write_fd, b"1")
            except OSError as exc:
                raise BrowserSessionUnresolved(
                    "browser process-group acknowledgment failed"
                ) from exc
            finally:
                _close_fd(bootstrap_ack_write_fd)
                bootstrap_ack_write_fd = -1

            remaining = BROWSER_TOTAL_TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0 or not parent.poll(remaining):
                raise BrowserSessionUnresolved("isolated browser exceeded its wall-clock bound")
            try:
                classification, payload = parent.recv()
            except (EOFError, OSError) as exc:
                raise BrowserSessionUnresolved(
                    "isolated browser exited without a resolved result"
                ) from exc
            if classification == "OK":
                return payload
            error = _ERRORS.get(classification, BrowserSessionUnresolved)
            raise error("isolated browser authentication failed closed")
        finally:
            parent.close()
            _close_fd(bootstrap_ack_write_fd)
            _terminate_process_group(process, lifecycle_write_fd, process_group)
