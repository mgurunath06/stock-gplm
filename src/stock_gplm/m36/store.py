"""Immutable local store for manually downloaded NSE reference sources.

Nothing in this module touches the network. NSE's Terms of Use prohibit systematic
or automated data collection, so every source enters the store as a file a person
downloaded in a browser. Registration copies the bytes, makes them read-only and
records where, when and for which dates the file was obtained. A record is never
overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ACQUISITION_CONTRACT = "stock-gplm.m36-source-acquisition.v1"
ACQUISITION_FILE = "acquisition.json"
MAX_SOURCE_BYTES = 64 * 1024 * 1024
OFFICIAL_HOSTS = ("nseindia.com", "nseindia.in")
TERMS = {
    "terms_of_use": "https://www.nseindia.com/static/nse-terms-of-use",
    "copyright": "https://www.nseindia.com/static/nse-copyright",
    "basis": "manual browser download for personal, non-commercial research; "
             "no automated or systematic collection",
}
WEEKLY_RULE = "NO_SESSION_ON_SATURDAY_OR_SUNDAY_EXCEPT_ANNOUNCED_SPECIAL_SESSIONS"

# Component -> allowed scopes. A scope states what the file claims to cover; only a
# full-market scope can support COMPLETE coverage downstream.
COMPONENTS: dict[str, frozenset[str]] = {
    "nse-weekly-schedule": frozenset({"WEEKLY_SCHEDULE"}),
    "nse-calendar-source": frozenset({"TRADING_HOLIDAYS", "SPECIAL_SESSIONS"}),
    "nse-calendar-transcription": frozenset({"TRADING_HOLIDAYS", "SPECIAL_SESSIONS"}),
    "nse-corporate-actions": frozenset({"ALL_EQUITY_SYMBOLS", "SELECTED_SYMBOLS"}),
    "nse-equity-list": frozenset({"ALL_LISTED_EQUITIES"}),
    "nse-etf-list": frozenset({"ALL_LISTED_ETFS"}),
    "nse-symbol-changes": frozenset({"ALL_SYMBOL_CHANGES"}),
    "nse-delisted-securities": frozenset({"ALL_DELISTED_SECURITIES"}),
}
TRANSCRIPTION = "nse-calendar-transcription"


@dataclass(frozen=True)
class SourceRecord:
    """One registered source file and its acquisition metadata."""

    component: str
    sha256: str
    path: Path
    acquisition: Mapping[str, Any]

    @property
    def scope(self) -> str:
        return str(self.acquisition["scope"])

    @property
    def applicable_start(self) -> str:
        return str(self.acquisition["applicable_start"])

    @property
    def applicable_end(self) -> str:
        return str(self.acquisition["applicable_end"])

    def summary(self) -> dict[str, Any]:
        keys = ("component", "sha256", "file_name", "source_url", "downloaded_at_utc", "scope",
                "applicable_start", "applicable_end", "acquisition_method")
        return {key: self.acquisition.get(key) for key in keys}


def _utc(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must carry a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _day(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc


def official_url(url: Any) -> str:
    """Accept only https URLs on an NSE-operated host. This records provenance; it fetches nothing."""
    if not isinstance(url, str):
        raise ValueError("source_url must be a string")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS):
        raise ValueError(f"source_url must be an https URL on an official NSE host: {url}")
    return url


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_acquisition(
    component: str, metadata: Mapping[str, Any], *, now: datetime,
    registered: Mapping[str, SourceRecord] | None = None,
) -> dict[str, Any]:
    """Return normalised acquisition metadata or raise; never fill a missing field."""
    if component not in COMPONENTS:
        raise ValueError(f"unknown component {component!r}; expected one of {sorted(COMPONENTS)}")
    scope = metadata.get("scope")
    if scope not in COMPONENTS[component]:
        raise ValueError(f"{component} scope must be one of {sorted(COMPONENTS[component])}")
    start = _day(metadata.get("applicable_start"), "applicable_start")
    end = _day(metadata.get("applicable_end"), "applicable_end")
    if start > end:
        raise ValueError("applicable_start is after applicable_end")
    downloaded = _utc(metadata.get("downloaded_at_utc"), "downloaded_at_utc")
    if downloaded > now:
        raise ValueError("downloaded_at_utc is in the future")
    result: dict[str, Any] = {
        "scope": scope, "applicable_start": start.isoformat(), "applicable_end": end.isoformat(),
        "downloaded_at_utc": _iso(downloaded), "notes": str(metadata.get("notes") or ""),
    }
    if component == TRANSCRIPTION:
        # A transcription is derived by a person from registered official documents.
        derived = metadata.get("derived_from")
        if not isinstance(derived, list) or not derived or len(set(derived)) != len(derived):
            raise ValueError("a transcription needs a non-empty, unique derived_from list of source SHA-256s")
        for digest in derived:
            source = (registered or {}).get(digest)
            if source is None or source.component != "nse-calendar-source":
                raise ValueError(f"derived_from {digest} is not a registered nse-calendar-source")
            if source.scope != scope:
                raise ValueError(f"derived_from {digest} has scope {source.scope}, not {scope}")
        result |= {"derived_from": sorted(derived), "source_url": None,
                   "acquisition_method": "MANUAL_TRANSCRIPTION_OF_REGISTERED_SOURCES"}
    else:
        result |= {"source_url": official_url(metadata.get("source_url")),
                   "acquisition_method": "MANUAL_BROWSER_DOWNLOAD"}
    if component == "nse-weekly-schedule":
        if metadata.get("declared_rule") != WEEKLY_RULE:
            raise ValueError(f"nse-weekly-schedule must declare declared_rule={WEEKLY_RULE}")
        result["declared_rule"] = WEEKLY_RULE
    publication = metadata.get("publication_date")
    if publication is not None:
        if _day(publication, "publication_date") > downloaded.date():
            raise ValueError("publication_date is after the download")
        result["publication_date"] = publication
    else:
        result["publication_date"] = None
    return result


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    path = path.resolve()
    return any(path == root.resolve() or root.resolve() in path.parents for root in roots)


def register_source(
    source: Path, *, store_root: Path, component: str, metadata: Mapping[str, Any],
    protected_roots: tuple[Path, ...] = (), now: datetime | None = None,
) -> SourceRecord:
    """Copy one manually downloaded file into the store and make it read-only.

    Registering identical bytes and metadata twice returns the existing record; any
    difference is refused rather than overwritten.
    """
    now = now or datetime.now(timezone.utc)
    source, store_root = Path(source), Path(store_root)
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"source must be a regular file: {source}")
    if _inside(store_root, protected_roots):
        raise ValueError("evidence store must be outside the immutable raw tree")
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("source exceeds the configured size limit")
    if source.stat().st_size == 0:
        raise ValueError("source file is empty")
    records, errors = load_store(store_root)
    if errors:
        raise ValueError(f"evidence store has integrity errors; resolve them first: {errors[0]}")
    digest = _sha256(source)
    normalised = validate_acquisition(
        component, metadata, now=now, registered={r.sha256: r for r in records},
    )
    acquisition = {
        "schema_version": 1, "contract": ACQUISITION_CONTRACT, "component": component,
        "file_name": source.name, "sha256": digest, "size_bytes": source.stat().st_size,
        **normalised, "registered_at_utc": _iso(now), "access_terms": TERMS,
    }
    target = store_root / component / digest
    if target.exists():
        existing = json.loads((target / ACQUISITION_FILE).read_text(encoding="utf-8"))
        ignore = {"registered_at_utc"}
        if {k: v for k, v in existing.items() if k not in ignore} != {
                k: v for k, v in acquisition.items() if k not in ignore}:
            raise FileExistsError(f"{component}/{digest} is already registered with different metadata")
        return _record(target)
    staging = store_root / component / f".{digest}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        payload = staging / source.name
        shutil.copyfile(source, payload)
        if _sha256(payload) != digest:
            raise ValueError("source changed while it was being copied")
        (staging / ACQUISITION_FILE).write_text(
            json.dumps(acquisition, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for path in staging.iterdir():
            os.chmod(path, 0o444)
        os.rename(staging, target)
        os.chmod(target, 0o555)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return _record(target)


def _record(directory: Path) -> SourceRecord:
    acquisition = json.loads((directory / ACQUISITION_FILE).read_text(encoding="utf-8"))
    if acquisition.get("contract") != ACQUISITION_CONTRACT:
        raise ValueError(f"{directory}: unsupported acquisition contract")
    if acquisition.get("sha256") != directory.name or acquisition.get("component") != directory.parent.name:
        raise ValueError(f"{directory}: acquisition does not match its location")
    name = acquisition.get("file_name")
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError(f"{directory}: invalid file_name")
    payload = directory / name
    if not payload.is_file() or _sha256(payload) != directory.name:
        raise ValueError(f"{directory}: source bytes do not match the recorded SHA-256")
    extras = sorted(p.name for p in directory.iterdir() if p.name not in {name, ACQUISITION_FILE})
    if extras:
        raise ValueError(f"{directory}: unexpected files {extras}")
    return SourceRecord(acquisition["component"], directory.name, payload, acquisition)


def load_store(store_root: Path) -> tuple[list[SourceRecord], list[str]]:
    """Every registered record, re-verified; integrity failures are returned, never skipped silently."""
    records: list[SourceRecord] = []
    errors: list[str] = []
    store_root = Path(store_root)
    if not store_root.exists():
        return records, errors
    for component_dir in sorted(p for p in store_root.iterdir() if p.is_dir()):
        if component_dir.name not in COMPONENTS:
            errors.append(f"{component_dir}: unknown component directory")
            continue
        for directory in sorted(component_dir.iterdir()):
            if directory.name.startswith("."):
                errors.append(f"{directory}: incomplete registration")
                continue
            try:
                records.append(_record(directory))
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                errors.append(str(exc))
    return records, errors
