"""Credential loading from a protected Oracle/systemd credential directory."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .totp import normalize_seed

MAX_SECRET_BYTES = 4096


def _private_permissions(info: os.stat_result, *, directory: bool) -> bool:
    """Accept private files and systemd's root:service-group credential mounts."""
    if info.st_uid not in {0, os.geteuid()}:
        return False
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o007:
        return False
    group_mode = mode & 0o070
    if not group_mode:
        return True
    allowed_group_mode = 0o050 if directory else 0o040
    return (
        info.st_uid == 0
        and info.st_gid in {0, os.getegid()}
        and group_mode & ~allowed_group_mode == 0
    )


@dataclass(frozen=True, repr=False)
class Credentials:
    password: str
    totp_secret: str
    api_secret: str


class FileCredentialProvider:
    """Read three fixed credential files without accepting values on argv."""

    NAMES = {
        "password": "shoonya_password",
        "totp_secret": "shoonya_totp_secret",
        "api_secret": "shoonya_api_secret",
    }

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    @staticmethod
    def _read(path: Path) -> str:
        if path.is_symlink():
            raise ValueError("credential file must not be a symlink")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
                raise ValueError("credential file ownership or type is unsafe")
            if not _private_permissions(info, directory=False) or not 0 < info.st_size <= MAX_SECRET_BYTES:
                raise ValueError("credential file permissions or size are unsafe")
            raw = os.read(descriptor, MAX_SECRET_BYTES + 1)
        finally:
            os.close(descriptor)
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("credential file is not UTF-8") from exc
        value = value.rstrip("\r\n")
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("credential value must be one nonempty line")
        return value

    def load(self) -> Credentials:
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError("credential directory must be a real directory")
        info = self.directory.stat()
        if not _private_permissions(info, directory=True):
            raise ValueError("credential directory ownership or permissions are unsafe")
        values = {key: self._read(self.directory / name) for key, name in self.NAMES.items()}
        normalize_seed(values["totp_secret"])
        return Credentials(**values)
