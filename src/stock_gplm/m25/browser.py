"""Fail-closed Selenium adapter for Shoonya's documented hosted OAuth login."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse

from .credentials import Credentials
from .errors import BrowserPageNotReady, InteractionRequired, UiContractChanged
from .models import AuthConfigV1
from .totp import generate_totp, seconds_remaining

AUTHORIZE_ENDPOINT = "https://api.shoonya.com/OAuthlogin/investor-entry-level/login"
REGISTERED_LOOPBACK_REDIRECT = "http://127.0.0.1"
APPROVED_HTTPS_REDIRECT_PATHS = frozenset({"/OAuthlogin/authorize/oauth"})
# The hosted login form is rendered by ~10 MB of JavaScript. On the burstable Oracle
# VM the hypervisor steals 70-78% of CPU while Chrome loads it, so the form took
# ~90 s to appear (docs/m25-oracle-browser-timeout-investigation-2026-11-01.md).
PAGE_READY_TIMEOUT_SECONDS = 150
PASSWORD_CSS = "input[type='password']"


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


def wait_for_login_form(
    driver: Any, timeout_seconds: float, *, transient: tuple[type[BaseException], ...] = (),
    sleeper: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Poll until a displayed, enabled password input exists or the bound expires.

    ChromeDriver can stall past its HTTP read timeout while a CPU-starved renderer
    executes the page bundle; such ``transient`` errors are retried inside the bound.
    """
    deadline = monotonic() + timeout_seconds
    while True:
        try:
            if any(
                item.is_displayed() and item.is_enabled()
                for item in driver.find_elements("css selector", PASSWORD_CSS)
            ):
                return
        except transient:
            pass
        if monotonic() >= deadline:
            raise BrowserPageNotReady("Shoonya login form did not become usable within the bound")
        sleeper(1.0)


def _has_challenge(page_source: str) -> bool:
    lowered = page_source.lower()
    return any(marker in lowered for marker in ("captcha", "recaptcha", "hcaptcha"))


class SeleniumOAuthBrowser:
    """Drive only the documented login UI; never bypass interactive challenges."""

    INPUTS_CSS = "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])"
    SUBMIT_XPATH = "//button[normalize-space()='LOGIN']"

    def __init__(
        self, config: AuthConfigV1, *, sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.sleeper = sleeper
        self.clock = clock
        self.monotonic = monotonic

    def _inputs(self, driver: Any) -> list[Any]:
        visible = [item for item in driver.find_elements("css selector", self.INPUTS_CSS) if item.is_displayed()]
        if len(visible) != 3:
            raise UiContractChanged("Shoonya login page does not expose exactly three expected inputs")
        kinds = [(item.get_attribute("type") or "text").lower() for item in visible]
        if kinds[0] not in {"text", "email", "tel"} or kinds[1] != "password" or kinds[2] not in {
            "text", "tel", "number", "password",
        }:
            raise UiContractChanged("Shoonya login input types changed")
        return visible

    @staticmethod
    def _fill(element: Any, value: str) -> None:
        element.click()
        element.clear()
        element.send_keys(value)

    def acquire_code(self, credentials: Credentials) -> str:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.service import Service
            from selenium.common.exceptions import WebDriverException
            from selenium.webdriver.common.by import By
            from urllib3.exceptions import HTTPError
        except ImportError as exc:
            raise RuntimeError("Selenium is not installed in the Oracle authentication environment") from exc

        query = urlencode({"api_key": self.config.client_id, "route_to": self.config.user_id})
        url = f"{AUTHORIZE_ENDPOINT}?{query}"
        allowed = set(self.config.allowed_redirect_hosts)
        with tempfile.TemporaryDirectory(prefix="stock-gplm-auth-browser-") as profile:
            Path(profile).chmod(0o700)
            options = webdriver.ChromeOptions()
            options.binary_location = self.config.chrome_binary
            # Do not wait for the window load event (fonts, icons, Google services);
            # readiness is the explicit form wait below.
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
            driver = webdriver.Chrome(
                service=Service(executable_path=self.config.chromedriver_path), options=options,
            )
            try:
                driver.get(url)
                wait_for_login_form(
                    driver, PAGE_READY_TIMEOUT_SECONDS, transient=(WebDriverException, HTTPError),
                    sleeper=self.sleeper, monotonic=self.monotonic,
                )
                deadline = self.monotonic() + self.config.browser_timeout_seconds
                if _has_challenge(driver.page_source):
                    raise InteractionRequired("Shoonya presented an interactive challenge")
                previous_totp = None
                for attempt in range(2):
                    remaining = seconds_remaining(at_time=self.clock())
                    if remaining < self.config.totp_min_valid_seconds:
                        self.sleeper(remaining + 0.25)
                    inputs = self._inputs(driver)
                    otp = generate_totp(credentials.totp_secret, at_time=self.clock())
                    if otp == previous_totp:
                        self.sleeper(seconds_remaining(at_time=self.clock()) + 0.25)
                        otp = generate_totp(credentials.totp_secret, at_time=self.clock())
                    previous_totp = otp
                    self._fill(inputs[0], self.config.user_id)
                    self._fill(inputs[1], credentials.password)
                    self._fill(inputs[2], otp)
                    driver.find_element(By.XPATH, self.SUBMIT_XPATH).click()
                    attempt_deadline = min(deadline, self.monotonic() + 35)
                    while self.monotonic() < attempt_deadline:
                        code = capture_authorization_code(driver, allowed)
                        if code is not None:
                            return code
                        if _has_challenge(driver.page_source):
                            raise InteractionRequired("Shoonya presented an interactive challenge")
                        self.sleeper(0.5)
                    if attempt == 0 and self.monotonic() < deadline:
                        continue
                raise UiContractChanged("Shoonya OAuth redirect code was not observed within the bound")
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
