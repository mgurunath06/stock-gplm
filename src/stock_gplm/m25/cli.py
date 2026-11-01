"""Oracle-only CLI for M2.5 session preflight, status, and renewal."""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from .browser import SeleniumOAuthBrowser
from .credentials import FileCredentialProvider
from .errors import AuthError
from .models import AuthConfigV1
from .noren import NorenApiFactory
from .security import SessionCache
from .session import AuthManager


def _executable(path: str, label: str) -> dict[str, object]:
    item = Path(path)
    if item.is_symlink() or not item.is_file():
        raise ValueError(f"{label} must be a real file")
    info = item.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(item, os.X_OK):
        raise ValueError(f"{label} must be executable")
    result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        raise ValueError(f"{label} version check failed")
    return {"path": path, "version": (result.stdout or result.stderr).strip()[:256]}


def _ntp() -> bool:
    result = subprocess.run(
        ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "yes"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "status", "ensure"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
        if name in {"preflight", "ensure"}:
            command.add_argument("--credentials-dir", type=Path)
    commands.choices["ensure"].add_argument("--allow-oauth-login", action="store_true")
    args = parser.parse_args()
    try:
        config = AuthConfigV1.load(args.config)
        if args.command == "status":
            record = SessionCache(Path(config.cache_path)).load()
            now = time.time()
            output = {
                "schema_version": 1, "cache_present": record is not None,
                "locally_eligible": bool(
                    record is not None and 0 <= now - record.issued_at_epoch < config.cache_max_age_seconds
                ),
                "cache_age_seconds": int(now - record.issued_at_epoch) if record is not None else None,
                "server_validation_attempted": False,
            }
        else:
            directory = args.credentials_dir or (
                Path(os.environ["CREDENTIALS_DIRECTORY"])
                if "CREDENTIALS_DIRECTORY" in os.environ else None
            )
            if directory is None:
                raise ValueError("credentials directory must be explicit or provided by systemd")
            provider = FileCredentialProvider(directory)
            if args.command == "preflight":
                provider.load()
                try:
                    import selenium
                except ImportError as exc:
                    raise ValueError("selenium is not installed") from exc
                if not _ntp():
                    raise ValueError("Oracle clock is not NTP synchronized")
                output = {
                    "schema_version": 1, "contract": config.contract,
                    "credentials": "VALIDATED_NOT_EXPOSED", "ntp_synchronized": True,
                    "chrome": _executable(config.chrome_binary, "Chrome"),
                    "chromedriver": _executable(config.chromedriver_path, "chromedriver"),
                    "selenium_version": selenium.__version__,
                    "network_attempted": False, "login_attempted": False,
                }
            else:
                manager = AuthManager(
                    config=config, credentials=provider,
                    browser=SeleniumOAuthBrowser(config), api_factory=NorenApiFactory(),
                )
                manager.ensure_session(allow_oauth_login=args.allow_oauth_login)
                output = {
                    "schema_version": 1, "session": "READY",
                    "oauth_login_allowed": args.allow_oauth_login,
                }
    except (AuthError, FileNotFoundError, ImportError, OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        classification = exc.classification if isinstance(exc, AuthError) else type(exc).__name__.upper()
        print(json.dumps({"schema_version": 1, "status": "ERROR", "classification": classification}), file=sys.stderr)
        return 3
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
