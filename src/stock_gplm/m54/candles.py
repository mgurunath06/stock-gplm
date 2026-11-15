"""Read-only inventory and loading of every local daily and minute candle partition.

One :class:`AccessPolicy` governs every partition, daily and minute alike:

* ``M32_LOCKED_HOLDOUT`` (real data): the cutoff is the earliest locked-holdout session
  of a manifest-verified M3.2 split plan. A partition whose declared window ends on or
  after the cutoff is **excluded whole**; its payload is never opened or decoded. Only
  its sidecar metadata (marker and meta JSON: byte count, record count, SHA-256,
  session type) is read so coverage can be reported. This is the same file-level
  control the M5.4 daily loader uses; it is not relaxed to row-level filtering.
* ``SYNTHETIC_FIXTURE`` (tests and integration only): the cutoff is declared by a
  ``SYNTHETIC_FIXTURE.json`` marker written by :mod:`stock_gplm.m54.fixture`. A root
  without that marker, or any root inside the real raw tree, can never use this mode.

Permitted daily partitions are validated by :func:`stock_gplm.ingestion.daily.validate_daily`
and permitted minute partitions by the M3.4 ``ShoonyaMinuteV1`` adapter
(:func:`stock_gplm.m3.minute.verify_publication` and ``validate_segment``). Nothing is
repaired or filled: rejected and quarantined partitions are listed with their reasons.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ..m3.minute import MinuteSchemaError, SessionPolicy, validate_segment, verify_publication
from ..mvp.data import load_symbol, pin_holdout

IST = timezone(timedelta(hours=5, minutes=30))
FIXTURE_CONTRACT = "stock-gplm.m54-synthetic-fixture.v1"
FIXTURE_MARKER = "SYNTHETIC_FIXTURE.json"
_DAILY = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json\.gz$")
_MINUTE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json\.gz$")
MAX_SIDECAR_BYTES = 64 * 1024
MAX_MINUTE_PAYLOAD = 1024 * 1024


@dataclass(frozen=True)
class AccessPolicy:
    """The single research boundary applied to every partition."""

    basis: str  # M32_LOCKED_HOLDOUT | SYNTHETIC_FIXTURE
    cutoff: str  # first forbidden session date; partitions ending on/after it are excluded whole
    detail: dict[str, Any]
    start: str | None = None  # optional user window (can only narrow, never widen)
    end: str | None = None

    def partition(self, window_start: str, window_end: str) -> tuple[bool, str]:
        if window_end >= self.cutoff:
            return False, (f"EXCLUDED_HOLDOUT: window ends {window_end}, on/after the {self.basis} cutoff "
                           f"{self.cutoff}; payload not opened")
        if self.start and window_end < self.start:
            return False, f"OUTSIDE_REQUESTED_RANGE: window ends before --start {self.start}"
        if self.end and window_start > self.end:
            return False, f"OUTSIDE_REQUESTED_RANGE: window starts after --end {self.end}"
        return True, "PERMITTED"

    def in_range(self, session: str) -> bool:
        return session < self.cutoff and (not self.start or session >= self.start) and (not self.end or session <= self.end)

    def to_dict(self) -> dict[str, Any]:
        return {"basis": self.basis, "cutoff": self.cutoff, "start": self.start, "end": self.end, **self.detail}


def _fixture_marker(root: Path) -> dict[str, Any] | None:
    path = Path(root) / FIXTURE_MARKER
    if not path.is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("contract") != FIXTURE_CONTRACT or doc.get("synthetic") is not True:
        raise ValueError(f"{path} is not a {FIXTURE_CONTRACT} marker")
    return doc


def resolve_policy(data_cfg: dict[str, Any], daily_root: Path, minute_roots: Sequence[Path], real_raw_tree: Path,
                   start: str | None, end: str | None) -> AccessPolicy:
    """Choose the boundary. Synthetic mode needs a marker in *every* root and no root in the real tree."""
    roots = [Path(daily_root).resolve(), *(Path(r).resolve() for r in minute_roots)]
    markers = [_fixture_marker(r) for r in roots if r.exists()]
    real = Path(real_raw_tree).resolve()
    if markers and all(m is not None for m in markers):
        if any(r == real or real in r.parents for r in roots):
            raise ValueError("a synthetic-fixture marker inside the real raw tree is refused")
        cutoffs = {m["declared_cutoff"] for m in markers if m}
        if len(cutoffs) != 1:
            raise ValueError("synthetic-fixture roots declare different cutoffs")
        return AccessPolicy("SYNTHETIC_FIXTURE", cutoffs.pop(), {"fixture_id": markers[0]["fixture_id"],  # type: ignore[index]
                                                                  "note": "SYNTHETIC data; not market evidence"}, start, end)
    if any(m is not None for m in markers):
        raise ValueError("synthetic-fixture markers must be present in every data root or in none")
    pin = pin_holdout(Path(data_cfg["m32_run"]), data_cfg["m32_run_id"], data_cfg["split_plan_sha256"])
    return AccessPolicy("M32_LOCKED_HOLDOUT", pin.cutoff, {"holdout_pin": pin.to_dict()}, start, end)


def _sidecar(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


@dataclass
class DailyResult:
    bars: pd.DataFrame
    partitions: list[dict[str, Any]]
    issues: list[str]
    status: str


def load_daily(daily_root: Path, symbol: str, policy: AccessPolicy) -> DailyResult:
    """Inventory every daily partition, then load the permitted ones through the M5.4 loader."""
    directory = Path(daily_root) / symbol
    partitions: list[dict[str, Any]] = []
    if not directory.is_dir():
        return DailyResult(pd.DataFrame(), partitions, ["daily symbol directory absent"], "MISSING")
    for path in sorted(directory.iterdir()):
        m = _DAILY.fullmatch(path.name)
        if not m:
            continue
        ok, reason = policy.partition(*m.groups())
        marker = _sidecar(Path(f"{path}.done")) or {}
        partitions.append({"timeframe": "1d", "file": str(path), "window": list(m.groups()), "permitted": ok,
                           "disposition": "PENDING" if ok else reason, "bytes": marker.get("bytes"),
                           "records_declared": marker.get("records"), "sha256_declared": marker.get("sha256")})
    # load_symbol opens only segments whose window ends before the cutoff (file-level control).
    data = load_symbol(Path(daily_root), symbol, policy.cutoff)
    opened = {s["file"]: s for s in data.segments_opened}
    for p in partitions:
        name = Path(p["file"]).name
        if p["permitted"] and name in opened:
            s = opened[name]
            p.update({"disposition": s["disposition"], "sha256": s["sha256"], "records": s["records"]})
        elif p["permitted"] and name not in opened:
            p.update({"permitted": False, "disposition": "NOT_OPENED_BY_LOADER"})
        elif not p["permitted"] and name in opened and p["disposition"].startswith("OUTSIDE_REQUESTED_RANGE"):
            s = opened[name]  # opened (pre-cutoff) but outside the user range: rows are dropped below
            p.update({"sha256": s["sha256"], "records": s["records"]})
    df = pd.DataFrame(list(data.bars))
    if not df.empty:
        df = df[[policy.in_range(d) for d in df["date"]]].reset_index(drop=True)
        if (df["date"] >= policy.cutoff).any():
            raise AssertionError("a daily bar at or after the cutoff was loaded")
    status = data.status if not df.empty or data.status != "ACCEPTED" else "MISSING"
    return DailyResult(df, partitions, list(data.issues), status)


@dataclass
class MinuteResult:
    bars: pd.DataFrame  # one row per accepted regular-session minute
    partitions: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _minute_bars(report: Any) -> list[dict[str, Any]]:
    rows = []
    for b in report.bars:
        if "OFF_SESSION" in b.flags:
            continue
        rows.append({"start_ist": b.start_ist.replace(tzinfo=None), "open": float(b.open), "high": float(b.high),
                     "low": float(b.low), "close": float(b.close), "volume": float(b.volume)})
    return rows


def load_minute(minute_roots: Sequence[Path], symbol: str, policy: AccessPolicy, session_policy: SessionPolicy,
                accept_decisions: Sequence[str] = ("ACCEPT", "REVIEW")) -> MinuteResult:
    """Inventory every minute partition across all roots; load permitted, consistent, accepted sessions only.

    The same session found in several roots is a duplicate: identical declared SHA-256 values are
    de-duplicated (first root wins, recorded); different values mark the session ``CONFLICTING_DUPLICATE``
    and neither copy is used.
    """
    result = MinuteResult(pd.DataFrame())
    by_session: dict[str, list[dict[str, Any]]] = {}
    for root in minute_roots:
        directory = Path(root) / symbol
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            m = _MINUTE.fullmatch(path.name)
            if not m:
                continue
            session = m.group(1)
            ok, reason = policy.partition(session, session)
            marker = _sidecar(Path(f"{path}.done")) or {}
            meta = _sidecar(path.with_name(f"{session}.meta.json")) or {}
            row = {"timeframe": "1m", "file": str(path), "root": str(root), "session": session, "permitted": ok,
                   "disposition": "PENDING" if ok else reason, "bytes": marker.get("bytes"),
                   "records_declared": marker.get("records"), "sha256_declared": marker.get("sha256"),
                   "session_type": meta.get("session_type"), "quality_status": meta.get("quality_status")}
            by_session.setdefault(session, []).append(row)
            result.partitions.append(row)
    frames: list[dict[str, Any]] = []
    for session in sorted(by_session):
        copies = by_session[session]
        if len(copies) > 1:
            shas = {c["sha256_declared"] for c in copies}
            dup = {"session": session, "copies": [c["file"] for c in copies],
                   "identical_declared_sha256": len(shas) == 1 and None not in shas}
            result.duplicates.append(dup)
            if not dup["identical_declared_sha256"]:
                for c in copies:
                    if c["permitted"]:
                        c.update({"permitted": False, "disposition": "CONFLICTING_DUPLICATE: copies differ"})
                continue
            for c in copies[1:]:
                if c["permitted"]:
                    c.update({"permitted": False, "disposition": f"DUPLICATE_OF {copies[0]['file']}"})
        primary = copies[0]
        if not primary["permitted"]:
            continue
        path = Path(primary["file"])
        try:
            if path.stat().st_size > MAX_MINUTE_PAYLOAD:
                raise MinuteSchemaError("payload exceeds its bound")
            payload = path.read_bytes()
            meta = path.with_name(f"{session}.meta.json").read_bytes()
            marker = Path(f"{path}.done").read_bytes()
            pub = verify_publication(payload, meta, marker, symbol, session)
            report = validate_segment(payload, symbol, session, session_policy)
        except (MinuteSchemaError, OSError, ValueError) as exc:
            primary.update({"permitted": False, "disposition": f"REJECT_PUBLICATION: {exc}"})
            continue
        primary.update({"sha256": pub.sha256, "records": report.records, "decision": report.decision,
                        "regular_bars": report.regular_bars, "off_session_bars": report.off_session_bars,
                        "missing_regular_minutes": len(report.missing_regular_minutes),
                        "flags": report.flag_counts})
        if report.decision not in accept_decisions:
            primary.update({"permitted": False, "disposition": f"{report.decision}: {sorted(set(report.issues))}"})
            continue
        primary["disposition"] = report.decision
        for r in _minute_bars(report):
            r["session"] = session
            frames.append(r)
    if frames:
        df = pd.DataFrame(frames).sort_values("start_ist").reset_index(drop=True)
        if (df["session"] >= policy.cutoff).any():
            raise AssertionError("a minute bar at or after the cutoff was loaded")
        result.bars = df
    return result


def reconcile(daily: pd.DataFrame, minute: pd.DataFrame) -> dict[str, Any]:
    """Compare minute aggregates with daily candles on shared sessions. Reports only; never repairs."""
    if daily.empty or minute.empty:
        return {"shared_sessions": 0}
    agg = minute.groupby("session").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                        close=("close", "last"), volume=("volume", "sum"))
    d = daily.set_index("date")
    shared = sorted(set(agg.index) & set(d.index))
    if not shared:
        return {"shared_sessions": 0}
    rel = {c: (agg.loc[shared, c] / d.loc[shared, c] - 1).abs() for c in ("open", "high", "low", "close")}
    return {"shared_sessions": len(shared), "minute_only_sessions": len(set(agg.index) - set(d.index)),
            "daily_only_sessions_in_minute_span": len([s for s in d.index if agg.index.min() <= s <= agg.index.max()
                                                       and s not in agg.index]),
            **{f"{c}_median_abs_rel_diff": float(v.median()) for c, v in rel.items()},
            **{f"{c}_sessions_over_0.5pct": int((v > 0.005).sum()) for c, v in rel.items()},
            "note": "minute aggregates are not substituted for daily candles; differences are reported only"}


def daily_session_gaps(dates: Sequence[str]) -> dict[str, Any]:
    """Calendar gaps between consecutive sessions. Weekends/holidays are not inferred (no calendar evidence)."""
    if len(dates) < 2:
        return {"gaps_over_4_calendar_days": []}
    ds = [date.fromisoformat(x) for x in dates]
    gaps = [{"after": a.isoformat(), "before": b.isoformat(), "calendar_days": (b - a).days}
            for a, b in zip(ds, ds[1:]) if (b - a).days > 4]
    return {"gaps_over_4_calendar_days": gaps,
            "note": "calendar gaps are listed, not explained: no registered NSE holiday calendar is used"}


def fingerprint(partitions: Sequence[dict[str, Any]]) -> list[list[Any]]:
    """Stable identity of the inputs: permitted partitions by content hash, excluded ones by name only."""
    out = []
    for p in sorted(partitions, key=lambda x: x["file"]):
        out.append([Path(p["file"]).name, p.get("session") or p.get("window"), p["permitted"],
                    p.get("sha256") if p["permitted"] else None, p["disposition"].split(":")[0]])
    return out


def ist_ns(session: str, clock: str) -> int:
    """Nanoseconds since the epoch for an IST wall-clock time on a session date."""
    dt = datetime.fromisoformat(f"{session}T{clock}").replace(tzinfo=IST)
    return int(dt.timestamp() * 1_000_000_000)


def coverage(partitions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Summary counts by timeframe and disposition."""
    out: dict[str, Any] = {}
    for p in partitions:
        tf = p["timeframe"]
        c = out.setdefault(tf, {"partitions": 0, "permitted": 0, "excluded": {}, "first": None, "last": None})
        c["partitions"] += 1
        key = p.get("session") or p["window"][1]
        first = p.get("session") or p["window"][0]
        c["first"] = first if c["first"] is None or first < c["first"] else c["first"]
        c["last"] = key if c["last"] is None or key > c["last"] else c["last"]
        if p["permitted"]:
            c["permitted"] += 1
        else:
            reason = p["disposition"].split(":")[0]
            c["excluded"][reason] = c["excluded"].get(reason, 0) + 1
    return out

