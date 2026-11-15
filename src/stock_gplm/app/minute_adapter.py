"""Versioned ShoonyaMinute adapter for the GPLM application (``ShoonyaMinuteV1`` + ``CLOSING_PHASE_V2``).

Every payload is parsed by the unchanged M3.4 validator (:func:`stock_gplm.m3.minute.verify_publication`
and :func:`validate_segment`): strict fields, whole-minute stamps, IST/ssboe agreement, OHLC bounds,
duplicates and ordering. This module adds one documented interpretation on top.

Observed regime change (investigated 2026-11-15, see ``docs/m55-data-investigation.md``)
----------------------------------------------------------------------------------------
From session 2026-08-03 the provider's cumulative volume field ``v`` restarts at the 15:20 bar,
counts only post-15:20 trades, and (on most days) is restored to the full-day total at 15:28/15:29.
``intv`` equals ``diff(v)``, so 15:20 shows a large negative interval volume and the restored total
appears as a positive lump. Prices are unaffected: across the 61 RELIANCE sessions that overlap
official daily candles, the last bar's close equals the daily close exactly and the session
high/low match exactly. This is consistent with a separate closing phase (for example a closing
auction) but **no exchange document confirms it**, so the interpretation is labelled
``OBSERVED_NOT_AUTHORITATIVE``.

Adapter rules (nothing is filled or repaired; raw files are never written)
-------------------------------------------------------------------------
* Bars from 09:15 to 15:29 are regular; the 09:14 pre-open bar and anything else is dropped as
  ``OFF_SESSION`` (counted, never used).
* A session whose only blocking flag is negative interval volume **confined to the closing phase**
  (bars at or after ``closing_phase_start``, default 15:20) is accepted as ``ACCEPT_CLOSING_PHASE_V2``.
  Prices of every regular bar are used. Interval volumes of closing-phase bars are **withheld**
  (set to NaN in the analytical frame) because their meaning is unknown; the raw values remain in
  the raw file and in the audit trail.
* Negative interval volume before the closing phase, an unreadable or inconsistent publication, or
  any M3.4 REJECT keeps the session out (``QUARANTINE`` / ``REJECT``), listed with its reasons.
* Session day volume (only used for minute-derived daily candles) follows a rule that reconciles
  with official daily candles: the last cumulative value if the counter was restored, otherwise the
  cumulative value just before the reset plus the post-reset count. It is labelled
  ``MINUTE_DERIVED_CUMULATIVE`` and checked against official daily volume wherever both exist.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..m3.minute import MinuteSchemaError, SessionPolicy, validate_segment, verify_publication

ADAPTER_VERSION = "gplm-minute-adapter.v2"
MAX_PAYLOAD = 1024 * 1024
FLAG_BITS = {"ZERO_VOLUME": 1, "NEGATIVE_VOLUME": 2, "FLAT_OHLC": 4, "VOLUME_SPIKE": 8, "VWAP_OUTSIDE_RANGE": 16,
             "CUMULATIVE_VOLUME_BREAK": 32, "LARGE_BAR_MOVE": 64, "CLOSING_PHASE_VOLUME_WITHHELD": 128}


@dataclass
class SessionResult:
    symbol: str
    session: str
    decision: str  # ACCEPT | REVIEW | ACCEPT_CLOSING_PHASE_V2 | QUARANTINE | REJECT
    sha256: str | None = None
    records: int = 0
    regular_bars: int = 0
    off_session_bars: int = 0
    missing_regular_minutes: int = 0
    flag_counts: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    counter_reset_clock: str | None = None
    counter_restored: bool | None = None
    day: dict[str, float] | None = None  # minute-derived daily OHLCV
    # analytical arrays (regular bars only)
    start_ns: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    ohlc: np.ndarray = field(default_factory=lambda: np.zeros((0, 4)))
    volume: np.ndarray = field(default_factory=lambda: np.zeros(0))
    flags: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))

    @property
    def usable(self) -> bool:
        return self.decision in {"ACCEPT", "REVIEW", "ACCEPT_CLOSING_PHASE_V2"}

    def summary(self) -> dict[str, Any]:
        return {"session": self.session, "decision": self.decision, "sha256": self.sha256, "records": self.records,
                "regular_bars": self.regular_bars, "off_session_bars": self.off_session_bars,
                "missing_regular_minutes": self.missing_regular_minutes, "flags": self.flag_counts,
                "issues": self.issues, "counter_reset_clock": self.counter_reset_clock,
                "counter_restored": self.counter_restored, "adapter": ADAPTER_VERSION}


def regime_policy(first_bar: str = "09:15", last_bar: str = "15:29") -> SessionPolicy:
    """The observed session window, with no ``valid_through`` limit (owner-authorized interpretation)."""
    return SessionPolicy(first_bar, last_bar, "OBSERVED_NOT_AUTHORITATIVE",
                         "GPLM app: bars observed 09:15-15:29 in 2026 Shoonya minute files; closing-phase volume "
                         "interpretation documented in docs/m55-data-investigation.md; no exchange document supplied")


def adapt_file(path: Path, symbol: str, session: str, policy: SessionPolicy, closing_phase_start: str = "15:20"
               ) -> SessionResult:
    """Read one minute triplet (read-only) and return its analytical bars and decision."""
    res = SessionResult(symbol, session, "REJECT")
    try:
        if path.stat().st_size > MAX_PAYLOAD:
            raise MinuteSchemaError("payload exceeds its bound")
        payload = path.read_bytes()
        meta = path.with_name(f"{session}.meta.json").read_bytes()
        marker = Path(f"{path}.done").read_bytes()
        pub = verify_publication(payload, meta, marker, symbol, session)
        res.sha256 = pub.sha256
    except (MinuteSchemaError, OSError, ValueError) as exc:
        res.issues.append(f"REJECT_PUBLICATION: {exc}")
        return res
    report = validate_segment(payload, symbol, session, policy)
    res.records = report.records
    res.regular_bars, res.off_session_bars = report.regular_bars, report.off_session_bars
    res.missing_regular_minutes = len(report.missing_regular_minutes)
    res.flag_counts = dict(report.flag_counts)
    res.issues = list(report.issues)
    if report.decision == "REJECT":
        res.decision = "REJECT"
        return res
    bars = report.bars  # ascending, flagged
    regular = [b for b in bars if "OFF_SESSION" not in b.flags]
    if not regular:
        res.decision = "QUARANTINE"
        res.issues.append("NO_REGULAR_BARS")
        return res
    # Cumulative-counter behaviour over every bar of the file (pre-open included).
    cum = [b.cumulative_volume for b in bars]
    reset_idx = [i for i in range(1, len(bars)) if cum[i] < cum[i - 1]]
    negatives = [b for b in regular if b.volume < 0]
    decision = report.decision
    withheld_from: str | None = None
    if negatives:
        if all(b.clock >= closing_phase_start for b in negatives) and \
                all(bars[i].clock >= closing_phase_start for i in reset_idx):
            decision = "ACCEPT_CLOSING_PHASE_V2"
            withheld_from = closing_phase_start
            res.issues = [i for i in res.issues if i != "NEGATIVE_INTERVAL_VOLUME_MEANING_UNKNOWN"]
            res.issues.append("CLOSING_PHASE_COUNTER_RESET: closing-phase interval volumes withheld")
        else:
            res.decision = "QUARANTINE"
            res.issues.append("NEGATIVE_INTERVAL_VOLUME_BEFORE_CLOSING_PHASE")
            return res
    elif decision == "QUARANTINE":
        res.decision = "QUARANTINE"
        return res
    if reset_idx:
        res.counter_reset_clock = bars[reset_idx[0]].clock
        pre = cum[reset_idx[0] - 1]
        res.counter_restored = cum[-1] >= pre
        day_volume = float(cum[-1] if res.counter_restored else pre + cum[-1])
    else:
        day_volume = float(cum[-1])
    n = len(regular)
    res.start_ns = np.array([int(b.start_utc.timestamp()) * 1_000_000_000 for b in regular], dtype=np.int64)
    res.ohlc = np.array([[float(b.open), float(b.high), float(b.low), float(b.close)] for b in regular])
    vol = np.array([float(b.volume) for b in regular])
    flags = np.zeros(n, dtype=np.int32)
    for i, b in enumerate(regular):
        for f in b.flags:
            flags[i] |= FLAG_BITS.get(f, 0)
        if withheld_from is not None and b.clock >= withheld_from:
            vol[i] = np.nan
            flags[i] |= FLAG_BITS["CLOSING_PHASE_VOLUME_WITHHELD"]
    res.volume, res.flags = vol, flags
    o, h, lo, c = res.ohlc[0, 0], res.ohlc[:, 1].max(), res.ohlc[:, 2].min(), res.ohlc[-1, 3]
    res.day = {"open": float(o), "high": float(h), "low": float(lo), "close": float(c), "volume": day_volume}
    res.decision = decision
    if withheld_from is not None:
        res.flag_counts["CLOSING_PHASE_VOLUME_WITHHELD"] = int((flags & FLAG_BITS["CLOSING_PHASE_VOLUME_WITHHELD"]).astype(bool).sum())
    return res
