"""Strict M2.4 request and resource-limit contracts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

CURRENT_EQ_SYMBOLS = 2715
CURRENT_BASELINE_COMPLETE = 1584
CURRENT_BASELINE_PARTIAL = 1131
CURRENT_MASTER_FILE = "NSE_symbols.txt.zip"
CURRENT_MASTER_SHA256 = "129b0eb840d854bceb181da6f0dca67fc5f25a355f6f1325677b95f005f4aee9"
COLLECTOR_WINDOW_COUNT = 5


def _date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD") from exc


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class CollectorWindow:
    name: str
    start_date: str
    end_date: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("collector window name must be nonempty")
        if _date(self.start_date, "collector window start") > _date(
            self.end_date, "collector window end"
        ):
            raise ValueError("collector window start is after end")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CollectorWindow":
        if not isinstance(value, dict) or set(value) != {"name", "start_date", "end_date"}:
            raise ValueError("collector window has missing or unsupported fields")
        return cls(value["name"], value["start_date"], value["end_date"])


@dataclass(frozen=True)
class SyncLimits:
    max_payload_bytes: int
    max_sidecar_bytes: int
    max_triplets: int
    max_total_bytes: int
    minimum_free_bytes: int
    chunk_bytes: int
    max_attempts: int
    parallel_transfers: int

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            _positive_int(getattr(self, field), f"limits.{field}")
        if self.max_payload_bytes > 16 * 1024 * 1024:
            raise ValueError("limits.max_payload_bytes exceeds the M1 compressed limit")
        if self.max_sidecar_bytes > 64 * 1024:
            raise ValueError("limits.max_sidecar_bytes exceeds the M1 sidecar limit")
        if self.chunk_bytes > 1024 * 1024:
            raise ValueError("limits.chunk_bytes must not exceed 1 MiB")
        if self.max_attempts > 5:
            raise ValueError("limits.max_attempts must not exceed 5")
        if self.parallel_transfers > 4:
            raise ValueError("limits.parallel_transfers must not exceed 4")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncLimits":
        expected = {
            "max_payload_bytes", "max_sidecar_bytes", "max_triplets",
            "max_total_bytes", "minimum_free_bytes", "chunk_bytes",
            "max_attempts", "parallel_transfers",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("sync limits have missing or unsupported fields")
        return cls(**value)


@dataclass(frozen=True)
class SyncRequestV1:
    master_file: str
    master_sha256: str
    destination_root: str
    collector_windows: tuple[CollectorWindow, ...]
    limits: SyncLimits
    baseline_complete_symbols: int
    baseline_partial_symbols: int
    daily_sync_start: str | None = None
    expected_eq_symbols: int = CURRENT_EQ_SYMBOLS
    transport_profile: str = "windows-openssh-sne-cloud"
    contract: str = "stock-gplm.m24-sync-request.v1"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.contract != "stock-gplm.m24-sync-request.v1":
            raise ValueError("unsupported M2.4 sync request contract")
        if self.transport_profile != "windows-openssh-sne-cloud":
            raise ValueError("M2.4 transport profile must be windows-openssh-sne-cloud")
        if not isinstance(self.destination_root, str) or not self.destination_root.startswith("/"):
            raise ValueError("destination_root must be an absolute WSL path")
        if not isinstance(self.master_file, str) or Path(self.master_file).name != self.master_file:
            raise ValueError("master_file must be one filename")
        if "\x00" in self.destination_root:
            raise ValueError("destination_root contains a null byte")
        if len(self.master_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.master_sha256):
            raise ValueError("master_sha256 must be lowercase SHA-256")
        if len(self.collector_windows) != COLLECTOR_WINDOW_COUNT:
            raise ValueError("M2.4 requires exactly five collector windows")
        names = [item.name for item in self.collector_windows]
        if len(names) != len(set(names)):
            raise ValueError("collector window names must be unique")
        ordered = sorted(self.collector_windows, key=lambda item: item.start_date)
        for left, right in zip(ordered, ordered[1:]):
            if date.fromisoformat(right.start_date) <= date.fromisoformat(left.end_date):
                raise ValueError("collector windows must not overlap")
        if type(self.expected_eq_symbols) is not int or self.expected_eq_symbols <= 0:
            raise ValueError("expected_eq_symbols must be positive")
        if self.baseline_complete_symbols < 0 or self.baseline_partial_symbols < 0:
            raise ValueError("baseline symbol counts must be nonnegative")
        if self.baseline_complete_symbols + self.baseline_partial_symbols != self.expected_eq_symbols:
            raise ValueError("baseline complete and partial counts must reconcile to the universe")
        if self.daily_sync_start is not None:
            _date(self.daily_sync_start, "daily_sync_start")

    @property
    def window_ranges(self) -> set[tuple[str, str]]:
        return {(item.start_date, item.end_date) for item in self.collector_windows}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncRequestV1":
        expected = {
            "schema_version", "contract", "transport_profile", "master_file",
            "master_sha256", "destination_root", "expected_eq_symbols",
            "baseline_complete_symbols", "baseline_partial_symbols",
            "collector_windows", "limits", "daily_sync_start",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("sync request has missing or unsupported fields")
        windows = value["collector_windows"]
        if not isinstance(windows, list):
            raise ValueError("collector_windows must be an array")
        return cls(
            schema_version=value["schema_version"], contract=value["contract"],
            transport_profile=value["transport_profile"], master_file=value["master_file"],
            master_sha256=value["master_sha256"], destination_root=value["destination_root"],
            expected_eq_symbols=value["expected_eq_symbols"],
            baseline_complete_symbols=value["baseline_complete_symbols"],
            baseline_partial_symbols=value["baseline_partial_symbols"],
            daily_sync_start=value["daily_sync_start"],
            collector_windows=tuple(CollectorWindow.from_dict(item) for item in windows),
            limits=SyncLimits.from_dict(value["limits"]),
        )

    @classmethod
    def load(cls, path: Path, *, require_current_universe: bool = True) -> "SyncRequestV1":
        if Path(path).stat().st_size > 64 * 1024:
            raise ValueError("sync request exceeds 64 KiB")
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        request = cls.from_dict(value)
        if require_current_universe and (
            request.expected_eq_symbols != CURRENT_EQ_SYMBOLS
            or request.baseline_complete_symbols != CURRENT_BASELINE_COMPLETE
            or request.baseline_partial_symbols != CURRENT_BASELINE_PARTIAL
            or request.master_file != CURRENT_MASTER_FILE
            or request.master_sha256 != CURRENT_MASTER_SHA256
        ):
            raise ValueError("sync request does not pin the approved master and 2715-symbol 1584/1131 baseline")
        return request

    def digest(self) -> str:
        raw = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()
