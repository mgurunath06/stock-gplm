"""Fail-closed, single-submission Selenium adapter for Shoonya OAuth."""
from __future__ import annotations

import json
import multiprocessing
import os
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
CHROMEDRIVER_STARTUP_TIMEOUT_SECONDS = 30
PAGE_READY_TIMEOUT_SECONDS = 150
BROWSER_CLEANUP_TIMEOUT_SECONDS = 10
BROWSER_TOTAL_TIMEOUT_SECONDS = 280


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


def _worker(connection: Any, config: AuthConfigV1, credentials: Credentials) -> None:
    """Own Selenium and all descendants in a disposable process group."""
    os.setsid()
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


def _terminate_process_group(process: multiprocessing.Process) -> None:
    """Bound cleanup of the browser worker, ChromeDriver, and Chrome children."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    process.join(BROWSER_CLEANUP_TIMEOUT_SECONDS / 2)
    if process.is_alive():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.join(BROWSER_CLEANUP_TIMEOUT_SECONDS / 2)


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
        process = context.Process(target=_worker, args=(child, self.config, credentials))
        process.start()
        child.close()
        try:
            if not parent.poll(BROWSER_TOTAL_TIMEOUT_SECONDS):
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
            _terminate_process_group(process)
