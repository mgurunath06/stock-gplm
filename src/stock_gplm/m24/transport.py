"""Read-only transport adapters for M2.4."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

HOST_ALIAS = "sne-cloud"
PROFILE = "windows-openssh-sne-cloud"
WINDOWS_SSH = "/mnt/c/Windows/System32/OpenSSH/ssh.exe"
WINDOWS_SFTP = "/mnt/c/Windows/System32/OpenSSH/sftp.exe"
_REMOTE_ROOT = re.compile(r"^/[A-Za-z0-9._/-]+$")
_REMOTE_RELATIVE = re.compile(
    r"^[A-Z0-9&.-]+-EQ/\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}"
    r"(?:\.json\.gz|\.meta\.json|\.json\.gz\.done)$"
)


class TransportError(RuntimeError):
    """Permanent transport or security failure."""


class TransientTransportError(TransportError):
    """Retryable transport interruption."""


def validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or _REMOTE_RELATIVE.fullmatch(value) is None:
        raise TransportError(f"unsupported remote publication path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in value):
        raise TransportError("unsafe remote publication path")
    return value


class ReadOnlyTransport(Protocol):
    def list_markers(self, symbols: tuple[str, ...]) -> tuple[str, ...]: ...
    def fetch(self, relative_path: str, destination: Path, *, resume: bool) -> int: ...


@dataclass(frozen=True)
class TransportConfigV1:
    remote_root: str
    ssh_executable: str = WINDOWS_SSH
    sftp_executable: str = WINDOWS_SFTP
    host_alias: str = HOST_ALIAS
    profile: str = PROFILE
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.profile != PROFILE or self.host_alias != HOST_ALIAS:
            raise ValueError("unsupported M2.4 transport profile")
        if _REMOTE_ROOT.fullmatch(self.remote_root) is None or ".." in PurePosixPath(self.remote_root).parts:
            raise ValueError("remote_root contains unsupported or unsafe characters")
        if self.ssh_executable != WINDOWS_SSH or self.sftp_executable != WINDOWS_SFTP:
            raise ValueError("transport executables must be the pinned Windows OpenSSH binaries")

    @classmethod
    def load(cls, path: Path) -> "TransportConfigV1":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        expected = {
            "schema_version", "profile", "host_alias", "remote_root",
            "ssh_executable", "sftp_executable",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("transport config has missing or unsupported fields")
        return cls(**value)


class LocalFixtureTransport:
    """Synthetic read-only transport used by tests; never follows symlinks."""

    def __init__(self, root: Path, *, interrupt_after_bytes: int | None = None) -> None:
        self.root = Path(root).resolve()
        self.interrupt_after_bytes = interrupt_after_bytes
        self._interrupted = False

    def _source(self, relative_path: str) -> Path:
        validate_relative_path(relative_path)
        source = self.root.joinpath(*PurePosixPath(relative_path).parts)
        if source.is_symlink() or not source.is_file() or self.root not in source.resolve().parents:
            raise TransportError(f"source is absent, unsafe, or not a regular file: {relative_path}")
        return source

    def list_markers(self, symbols: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for symbol in symbols:
            directory = self.root / symbol
            if directory.is_symlink() or not directory.is_dir():
                continue
            for item in sorted(directory.iterdir(), key=lambda path: path.name):
                if item.is_file() and not item.is_symlink() and item.name.endswith(".json.gz.done"):
                    result.append(f"{symbol}/{item.name}")
        return tuple(result)

    def fetch(self, relative_path: str, destination: Path, *, resume: bool) -> int:
        source = self._source(relative_path)
        destination = Path(destination)
        offset = destination.stat().st_size if resume and destination.exists() else 0
        mode = "ab" if offset else "wb"
        written = 0
        with source.open("rb") as incoming, destination.open(mode) as outgoing:
            incoming.seek(offset)
            while chunk := incoming.read(64 * 1024):
                if (
                    self.interrupt_after_bytes is not None and not self._interrupted
                    and relative_path.endswith(".json.gz")
                    and written + len(chunk) > self.interrupt_after_bytes
                ):
                    keep = max(0, self.interrupt_after_bytes - written)
                    if keep:
                        outgoing.write(chunk[:keep])
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                    self._interrupted = True
                    raise TransientTransportError("synthetic interrupted transfer")
                outgoing.write(chunk)
                written += len(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        return offset + written


class WindowsOpenSSHTransport:
    """Windows OpenSSH SFTP adapter with a fixed alias and fail-closed options."""

    SECURITY_OPTIONS = (
        "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
        "-oClearAllForwardings=yes", "-oForwardAgent=no", "-oForwardX11=no",
        "-oRequestTTY=no", "-oConnectTimeout=15", "-oServerAliveInterval=15",
        "-oServerAliveCountMax=2",
    )

    def __init__(self, config: TransportConfigV1) -> None:
        self.config = config

    def preflight(self) -> dict[str, object]:
        interop = Path("/proc/sys/fs/binfmt_misc/WSLInterop")
        executables = (Path(self.config.ssh_executable), Path(self.config.sftp_executable))
        if not interop.exists() or any(not item.is_file() for item in executables):
            raise TransportError("Windows OpenSSH or WSL interop is unavailable")
        command = [self.config.ssh_executable, *self.SECURITY_OPTIONS, "-G", HOST_ALIAS]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise TransportError("unable to expand the sne-cloud OpenSSH profile")
        settings: dict[str, str] = {}
        for line in result.stdout.replace("\r", "").splitlines():
            key, _, value = line.partition(" ")
            if key in {
                "host", "batchmode", "stricthostkeychecking", "clearallforwardings",
                "forwardagent", "forwardx11", "requesttty",
            }:
                settings[key] = value
        required = {
            "host": HOST_ALIAS, "batchmode": "yes", "stricthostkeychecking": "true",
            "clearallforwardings": "yes", "forwardagent": "no", "forwardx11": "no",
            "requesttty": "false",
        }
        # OpenSSH versions may render yes/no instead of true/false.
        normalized = {key: ({"yes": "true", "no": "false"}.get(value, value)) for key, value in settings.items()}
        normalized_required = {key: ({"yes": "true", "no": "false"}.get(value, value)) for key, value in required.items()}
        if any(normalized.get(key) != value for key, value in normalized_required.items()):
            raise TransportError("effective Windows OpenSSH security options are not fail-closed")
        return {
            "schema_version": 1, "profile": PROFILE, "host_alias": HOST_ALIAS,
            "wsl_interop": True, "ssh_executable": str(executables[0]),
            "sftp_executable": str(executables[1]), "effective_security": settings,
            "network_connection_attempted": False,
        }

    @staticmethod
    def _windows_path(path: Path) -> str:
        result = subprocess.run(
            ["wslpath", "-w", str(Path(path).resolve())],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise TransportError("unable to translate WSL staging path for Windows SFTP")
        translated = result.stdout.strip().replace("\\", "/")
        if '"' in translated or any(ord(c) < 32 for c in translated):
            raise TransportError("translated Windows path is unsafe for SFTP batch mode")
        return translated

    def _run_batch(self, commands: list[str]) -> str:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as stream:
            for command in commands:
                stream.write(command + "\n")
            batch = Path(stream.name)
        try:
            batch_windows = self._windows_path(batch)
            command = [
                self.config.sftp_executable, *self.SECURITY_OPTIONS,
                "-b", batch_windows, HOST_ALIAS,
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise TransientTransportError("Windows SFTP command failed")
            return result.stdout.replace("\r", "")
        finally:
            batch.unlink(missing_ok=True)

    def list_markers(self, symbols: tuple[str, ...]) -> tuple[str, ...]:
        commands = [f'-ls -1 "{self.config.remote_root}/{symbol}/*.json.gz.done"' for symbol in symbols]
        output = self._run_batch(commands)
        markers: set[str] = set()
        prefix = self.config.remote_root.rstrip("/") + "/"
        for line in output.splitlines():
            candidate = line.strip()
            if candidate.startswith(prefix):
                candidate = candidate.removeprefix(prefix)
            if _REMOTE_RELATIVE.fullmatch(candidate) and candidate.endswith(".json.gz.done"):
                markers.add(candidate)
        return tuple(sorted(markers))

    def fetch(self, relative_path: str, destination: Path, *, resume: bool) -> int:
        validate_relative_path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        local = self._windows_path(destination)
        remote = f"{self.config.remote_root.rstrip('/')}/{relative_path}"
        verb = "reget" if resume and destination.exists() else "get"
        self._run_batch([f'{verb} "{remote}" "{local}"'])
        if not destination.is_file():
            raise TransportError("Windows SFTP did not create the requested local file")
        return destination.stat().st_size
