"""Read-only transport adapters for M2.4."""
from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

HOST_ALIAS = "sne-cloud"
PROFILE = "windows-openssh-sne-cloud"
WINDOWS_SSH = "/mnt/c/Windows/System32/OpenSSH/ssh.exe"
WINDOWS_SFTP = "/mnt/c/Windows/System32/OpenSSH/sftp.exe"
MARKER_LIST_TIMEOUT_SECONDS = 120
MAX_MARKER_LIST_BYTES = 4 * 1024 * 1024
MAX_FILE_LIST_BYTES = 16 * 1024 * 1024
MAX_BATCH_FETCH_FILES = 512
BATCH_TIMEOUT_SECONDS = 1800
MAX_BATCH_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_REPORTED_STDERR_CHARS = 300
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


def _is_payload(relative_path: str) -> bool:
    return relative_path.endswith(".json.gz")


def _run_bounded(
    command: list[str], *, timeout: float, max_bytes: int
) -> tuple[int, str, str]:
    """Run without a shell, killing the child on a time or output-size breach."""
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    captured: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransientTransportError("remote command exceeded its time bound")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise TransportError("remote command exceeded its output bound")
                    captured[key.data].append(chunk)
        remaining = deadline - time.monotonic()
        try:
            returncode = process.wait(max(remaining, 0))
        except subprocess.TimeoutExpired as exc:
            raise TransientTransportError("remote command exceeded its time bound") from exc
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    return (
        returncode,
        b"".join(captured["stdout"]).decode("utf-8", "replace"),
        b"".join(captured["stderr"]).decode("utf-8", "replace"),
    )


def _stderr_excerpt(value: str) -> str:
    cleaned = "".join(c if c.isprintable() else " " for c in value).strip()
    return cleaned[:MAX_REPORTED_STDERR_CHARS]


class ReadOnlyTransport(Protocol):
    def list_markers(self, symbols: tuple[str, ...]) -> tuple[str, ...]: ...
    def list_files(self) -> dict[str, int]: ...
    def fetch(self, relative_path: str, destination: Path, *, resume: bool) -> int: ...
    def fetch_many(
        self, requests: tuple[tuple[str, Path], ...], *, resume_payloads: bool = False,
    ) -> None: ...


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

    def list_files(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self.root.glob("*/*"):
            relative = item.relative_to(self.root).as_posix()
            if (
                _REMOTE_RELATIVE.fullmatch(relative) is not None
                and item.is_file() and not item.is_symlink()
                and not item.parent.is_symlink()
            ):
                result[relative] = item.stat().st_size
        return result

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

    def fetch_many(
        self, requests: tuple[tuple[str, Path], ...], *, resume_payloads: bool = False,
    ) -> None:
        for relative_path, destination in requests:
            self.fetch(
                relative_path, destination,
                resume=resume_payloads and _is_payload(relative_path),
            )


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
            returncode, stdout, stderr = _run_bounded(
                command, timeout=BATCH_TIMEOUT_SECONDS, max_bytes=MAX_BATCH_OUTPUT_BYTES,
            )
            if returncode != 0:
                raise TransientTransportError(
                    f"Windows SFTP command failed: {_stderr_excerpt(stderr)}"
                )
            if stderr.strip():
                raise TransportError(
                    "Windows SFTP reported an error despite a zero exit status: "
                    f"{_stderr_excerpt(stderr)}"
                )
            return stdout.replace("\r", "")
        finally:
            batch.unlink(missing_ok=True)

    def list_markers(self, symbols: tuple[str, ...]) -> tuple[str, ...]:
        if not symbols:
            raise TransportError("marker enumeration requires a nonempty pinned symbol set")
        root = self.config.remote_root.rstrip("/")
        # Windows SFTP does not expand a wildcard inside quotes. The old per-symbol
        # `-ls` both quoted the wildcard and suppressed every listing error. A
        # root-wide two-level SFTP glob is pathologically slow on the real tree,
        # so marker names are enumerated by one fixed, read-only remote `find`.
        # remote_root is restricted to a shell-safe alphabet by TransportConfigV1.
        # The remote shell re-parses these words, so the glob must stay quoted.
        returncode, stdout, stderr = _run_bounded(
            [
                self.config.ssh_executable, *self.SECURITY_OPTIONS, HOST_ALIAS,
                "find", root, "-mindepth", "2", "-maxdepth", "2", "-type", "f",
                "-name", "'*.json.gz.done'", "-print",
            ],
            timeout=MARKER_LIST_TIMEOUT_SECONDS, max_bytes=MAX_MARKER_LIST_BYTES,
        )
        if returncode != 0:
            raise TransientTransportError("remote marker enumeration failed")
        if stderr.strip():
            raise TransportError("remote marker enumeration reported an error")
        output = stdout.replace("\r", "")
        markers: set[str] = set()
        prefix = root + "/"
        for line in output.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if not candidate.startswith(prefix):
                raise TransportError("SFTP marker listing returned a path outside remote_root")
            candidate = candidate.removeprefix(prefix)
            if _REMOTE_RELATIVE.fullmatch(candidate) is None or not candidate.endswith(
                ".json.gz.done"
            ):
                raise TransportError("SFTP marker listing returned an unsupported path")
            markers.add(candidate)
        if not markers:
            raise TransportError("remote marker enumeration returned zero publications")
        return tuple(sorted(markers))

    def list_files(self) -> dict[str, int]:
        """Return sizes of regular publication files; symlinks are never listed."""
        root = self.config.remote_root.rstrip("/")
        returncode, stdout, stderr = _run_bounded(
            [
                self.config.ssh_executable, *self.SECURITY_OPTIONS, HOST_ALIAS,
                "find", root, "-mindepth", "2", "-maxdepth", "2", "-type", "f",
                "-printf", "'%s %P\\n'",
            ],
            timeout=MARKER_LIST_TIMEOUT_SECONDS, max_bytes=MAX_FILE_LIST_BYTES,
        )
        if returncode != 0:
            raise TransientTransportError("remote file enumeration failed")
        if stderr.strip():
            raise TransportError("remote file enumeration reported an error")
        files: dict[str, int] = {}
        for line in stdout.replace("\r", "").splitlines():
            size, separator, relative = line.partition(" ")
            if not separator or not size.isdecimal():
                raise TransportError("remote file enumeration returned a malformed line")
            # Other names in the tree are never fetched, so they are not rejected here.
            if _REMOTE_RELATIVE.fullmatch(relative) is not None:
                files[relative] = int(size)
        if not files:
            raise TransportError("remote file enumeration returned zero publications")
        return files

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

    def fetch_many(
        self, requests: tuple[tuple[str, Path], ...], *, resume_payloads: bool = False,
    ) -> None:
        """Fetch bounded batches with one fail-closed SFTP session each.

        SFTP batch mode aborts on the first failed command, so a missing source
        fails the whole session and no partial batch is reported as success.
        """
        for offset in range(0, len(requests), MAX_BATCH_FETCH_FILES):
            batch = requests[offset : offset + MAX_BATCH_FETCH_FILES]
            commands: list[str] = []
            destinations: list[Path] = []
            for relative_path, destination_input in batch:
                validate_relative_path(relative_path)
                destination = Path(destination_input)
                destination.parent.mkdir(parents=True, exist_ok=True)
                local = self._windows_path(destination)
                remote = f"{self.config.remote_root.rstrip('/')}/{relative_path}"
                resume = resume_payloads and _is_payload(relative_path) and destination.exists()
                commands.append(f'{"reget" if resume else "get"} "{remote}" "{local}"')
                destinations.append(destination)
            self._run_batch(commands)
            if any(not destination.is_file() for destination in destinations):
                raise TransportError("Windows SFTP batch omitted a requested local file")
