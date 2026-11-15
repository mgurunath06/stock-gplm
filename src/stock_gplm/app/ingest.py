"""Incremental, read-only ingestion of every local Shoonya/NSE daily and minute partition.

For each symbol:

1. **Catalog.** Every partition file (daily windows and minute sessions in every minute root) is
   listed with its size, modification time and the SHA-256 declared by its ``.done`` marker. A file
   whose (bytes, mtime_ns, declared sha256) match the catalog row is *unchanged* and is not parsed
   again; its parsed arrays come from a per-file cache named by its hash.
2. **Validate.** Changed or new files go through the unchanged M2/M3.4 validators
   (:func:`stock_gplm.ingestion.daily.validate_daily`, :mod:`.minute_adapter`). Decisions and every
   issue are recorded in ``data_files`` and ``data_issues``; nothing is filled or repaired.
3. **Compose.** The canonical daily series takes official daily candles from accepted partitions.
   Dates on which overlapping partitions disagree are excluded and recorded. Sessions with an
   accepted minute file but no official daily candle get a ``MINUTE_DERIVED`` daily candle, labelled
   as such and reconciled against official candles wherever both exist. The canonical minute
   series is every usable session's regular bars.
4. **Fingerprint.** The composite is cached under a fingerprint of (adapter version, every file's
   name, hash and decision). An unchanged fingerprint reuses the cache: an evening run touches only
   new or changed files.

Raw files are opened read-only and never written. Reports and caches live under the GPLM home,
which must be outside the raw tree.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from ..ingestion.daily import validate_daily
from .db import AuditDB, jdump, sha, utc_now
from .minute_adapter import ADAPTER_VERSION, SessionResult, adapt_file, regime_policy
from .settings import REPO, Settings

IST = timezone(timedelta(hours=5, minutes=30))
_DAILY = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json\.gz$")
_MINUTE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json\.gz$")
INGEST_VERSION = f"gplm-ingest.v1+{ADAPTER_VERSION}"
FULL_SESSION_BARS = 375
LARGE_GAP = 0.10


def normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9&_.-]{1,40}", s):
        raise ValueError(f"invalid symbol {symbol!r}")
    return s if s.endswith("-EQ") else f"{s}-EQ"


def list_symbols(settings: Settings, exclude_etf: bool = True) -> dict[str, Any]:
    """Every collected ``-EQ`` directory (daily or minute). Survivorship-biased: current directories only."""
    names: set[str] = set()
    for root in (settings.daily_root, *settings.minute_roots):
        if root.is_dir():
            names |= {d.name for d in root.iterdir() if d.is_dir() and d.name.endswith("-EQ")}
    etf_path = REPO / "config" / "m37" / "etf-symbol-heuristic.json"
    etfs: list[str] = []
    if exclude_etf and etf_path.is_file():
        doc = json.loads(etf_path.read_text(encoding="utf-8"))
        pats = [re.compile(p) for p in doc["etf_patterns"]]
        keep = set(doc.get("company_exclusions", []))
        etfs = sorted(n for n in names if n.removesuffix("-EQ") not in keep
                      and any(p.search(n.removesuffix("-EQ")) for p in pats))
    return {"symbols": sorted(names - set(etfs)), "excluded_etf_like": etfs,
            "membership_basis": "CURRENT_DIRECTORIES_SURVIVORSHIP_BIASED"}


def _marker_sha(path: Path) -> tuple[str | None, int | None]:
    try:
        doc = json.loads(Path(f"{path}.done").read_text(encoding="utf-8"))
        return doc.get("sha256"), doc.get("records")
    except (OSError, ValueError):
        return None, None


@dataclass
class SymbolData:
    symbol: str
    daily: pd.DataFrame  # date, open, high, low, close, volume, source
    minute: pd.DataFrame  # start_ist, session, open, high, low, close, volume, flags
    fingerprint: str
    summary: dict[str, Any] = field(default_factory=dict)
    changed_files: int = 0

    @property
    def last_session(self) -> str | None:
        return str(self.daily["date"].iloc[-1]) if len(self.daily) else None


def window_data(data: SymbolData, start: str | None = None, end: str | None = None) -> SymbolData:
    """Restrict a symbol's candles to sessions in ``[start, end]`` (inclusive ISO dates).

    Applied before any lane, feature, label or score is built, so no row outside the window can
    influence results. A windowed copy gets its own fingerprint, so caches and final-segment locks
    are never shared with the unwindowed series. With no bounds the input is returned unchanged.
    """
    if not start and not end:
        return data

    def keep(col: pd.Series) -> pd.Series:
        s = col.astype(str)
        m = pd.Series(True, index=col.index)
        if start:
            m &= s >= start
        if end:
            m &= s <= end
        return m

    daily = data.daily[keep(data.daily["date"])].reset_index(drop=True)
    minute = data.minute[keep(data.minute["session"])].reset_index(drop=True) if len(data.minute) else data.minute
    summary = {**data.summary, "window": {"start": start, "end": end},
               "first": str(daily["date"].iloc[0]) if len(daily) else None,
               "last": str(daily["date"].iloc[-1]) if len(daily) else None, "daily_rows": len(daily),
               "minute_first": str(minute["session"].iloc[0]) if len(minute) else None,
               "minute_last": str(minute["session"].iloc[-1]) if len(minute) else None,
               "minute_sessions_used": int(minute["session"].nunique()) if len(minute) else 0}
    return SymbolData(data.symbol, daily, minute, sha({"fp": data.fingerprint, "start": start, "end": end})[:32],
                      summary, data.changed_files)


class Ingestor:
    """Ingest symbols into the catalog and canonical caches. One instance per run."""

    def __init__(self, settings: Settings, db: AuditDB, log: Callable[[str], None] = lambda _m: None,
                 now: datetime | None = None):
        self.s, self.db, self.log = settings, db, log
        cfg = settings.section("data")
        self.closing_phase_start = cfg.get("closing_phase_start", "15:20")
        self.policy = regime_policy(cfg.get("session_first_bar", "09:15"), cfg.get("session_last_bar", "15:29"))
        self.now = now or datetime.now(IST)
        self.parsed_dir = settings.cache_dir / "parsed"
        self.canon_dir = settings.cache_dir / "candles"

    # ------------------------------------------------------------------ catalog helpers
    def _issue(self, symbol: str, tf: str, period: str, code: str, severity: str, file: str,
               detail: Any = None) -> None:
        with self.db.tx() as c:
            new = c.execute("INSERT OR IGNORE INTO data_issues (symbol, timeframe, period, code, severity, file, "
                            "detail_json, first_seen_utc, run_id) VALUES (?,?,?,?,?,?,?,?,?)",
                            (symbol, tf, period, code, severity, file, jdump(detail), utc_now(),
                             self.db.run_id)).rowcount
        if new and severity in {"WARN", "ERROR"}:
            self.db.event("DATA_ISSUE", {"symbol": symbol, "timeframe": tf, "period": period, "code": code,
                                         "severity": severity, "file": file, "detail": detail},
                          subject=symbol, level="WARN" if severity == "WARN" else "ERROR")

    def _catalog(self, path: Path, symbol: str, tf: str, period: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        st = path.stat()
        declared, records = _marker_sha(path)
        row = self.db.one("SELECT * FROM data_files WHERE path=?", (str(path),))
        cur = {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": declared, "records": records}
        if row and row["bytes"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns and row["sha256"] == declared:
            return row, cur
        return None, cur

    def _record_file(self, path: Path, symbol: str, tf: str, period: str, cur: dict[str, Any], decision: str,
                     detail: Any, prior_exists: bool) -> None:
        now = utc_now()
        with self.db.tx() as c:
            c.execute("INSERT INTO data_files (path, symbol, timeframe, period, bytes, mtime_ns, sha256, records, "
                      "decision, detail_json, first_seen_utc, last_checked_utc, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(path) DO UPDATE SET bytes=excluded.bytes, mtime_ns=excluded.mtime_ns, "
                      "sha256=excluded.sha256, records=excluded.records, decision=excluded.decision, "
                      "detail_json=excluded.detail_json, last_checked_utc=excluded.last_checked_utc, run_id=excluded.run_id",
                      (str(path), symbol, tf, period, cur["bytes"], cur["mtime_ns"], cur["sha256"], cur["records"],
                       decision, jdump(detail), now, now, self.db.run_id))
        self.db.event("DATA_FILE_CHANGED" if prior_exists else "DATA_FILE_NEW",
                      {"path": str(path), "timeframe": tf, "period": period, "decision": decision,
                       "sha256": cur["sha256"], "bytes": cur["bytes"]}, subject=symbol,
                      level="INFO" if decision in {"ACCEPT", "REVIEW", "ACCEPT_CLOSING_PHASE_V2"} else "WARN")

    def _parsed_path(self, symbol: str, name: str, digest: str | None) -> Path:
        return self.parsed_dir / symbol / f"{name}.{(digest or 'nohash')[:16]}.npz"

    # ------------------------------------------------------------------ daily partitions
    def _daily_partition(self, path: Path, symbol: str) -> dict[str, Any]:
        m = _DAILY.fullmatch(path.name)
        assert m
        period = f"{m.group(1)}_{m.group(2)}"
        row, cur = self._catalog(path, symbol, "1d", period)
        cache = self._parsed_path(symbol, path.name, cur["sha256"])
        if row is not None and cache.is_file():
            with np.load(cache, allow_pickle=False) as z:
                arrays = {k: z[k] for k in z.files}
            return {"file": str(path), "period": period, "decision": row["decision"], "sha256": row["sha256"],
                    "arrays": arrays, "changed": False}
        report = validate_daily(path)
        decision = report.disposition
        issues = [{"code": i.code, "message": i.message, "record": i.record}
                  for i in report.publication_integrity.issues + report.content_quality.issues]
        candles = report.content_quality.normalized_candles if decision == "ACCEPT" else []
        arrays = {"date": np.array([c.trading_date for c in candles], dtype="U10"),
                  "ohlcv": np.array([[float(c.open), float(c.high), float(c.low), float(c.close), float(c.volume)]
                                     for c in candles]).reshape(-1, 5)}
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f".{cache.stem}.{os.getpid()}.npz")
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, cache)
        sha_actual = report.publication_integrity.sha256
        cur["sha256"] = cur["sha256"] or sha_actual
        self._record_file(path, symbol, "1d", period, cur, decision, {"issues": issues[:50], "sha256_actual": sha_actual},
                          self.db.one("SELECT 1 FROM data_files WHERE path=?", (str(path),)) is not None)
        if decision != "ACCEPT":
            self._issue(symbol, "1d", period, decision, "WARN", str(path), {"issues": issues[:20]})
        return {"file": str(path), "period": period, "decision": decision, "sha256": cur["sha256"], "arrays": arrays,
                "changed": True}

    # ------------------------------------------------------------------ minute partitions
    def _minute_partition(self, path: Path, symbol: str, session: str) -> dict[str, Any]:
        row, cur = self._catalog(path, symbol, "1m", session)
        cache = self._parsed_path(symbol, path.name, cur["sha256"])
        if row is not None and cache.is_file():
            with np.load(cache, allow_pickle=False) as z:
                arrays = {k: z[k] for k in z.files}
            detail = json.loads(row["detail_json"] or "{}")
            return {"file": str(path), "session": session, "decision": row["decision"], "sha256": row["sha256"],
                    "arrays": arrays, "summary": detail, "changed": False}
        res: SessionResult = adapt_file(path, symbol, session, self.policy, self.closing_phase_start)
        # a session dated today is complete only after the close plus a publication margin
        if res.usable and session == self.now.date().isoformat() and self.now.strftime("%H:%M") < "15:45":
            res.decision = "SESSION_NOT_COMPLETE"
            res.issues.append("session is today and the market has not closed")
        arrays = {"start_ns": res.start_ns, "ohlc": res.ohlc, "volume": res.volume, "flags": res.flags,
                  "day": np.array([res.day[k] for k in ("open", "high", "low", "close", "volume")]) if res.day
                  else np.zeros(0)}
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f".{cache.stem}.{os.getpid()}.npz")
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, cache)
        summary = res.summary()
        cur["sha256"] = cur["sha256"] or res.sha256
        self._record_file(path, symbol, "1m", session, cur, res.decision, summary,
                          self.db.one("SELECT 1 FROM data_files WHERE path=?", (str(path),)) is not None)
        if not res.usable:
            self._issue(symbol, "1m", session, res.decision, "WARN", str(path), {"issues": res.issues})
        else:
            if res.decision == "ACCEPT_CLOSING_PHASE_V2":
                self._issue(symbol, "1m", session, "CLOSING_PHASE_VOLUME_WITHHELD", "INFO", str(path),
                            {"reset_clock": res.counter_reset_clock, "restored": res.counter_restored})
            if res.flag_counts.get("NEGATIVE_VOLUME"):
                self._issue(symbol, "1m", session, "NEGATIVE_INTERVAL_VOLUME", "INFO", str(path),
                            {"bars": res.flag_counts["NEGATIVE_VOLUME"], "interpretation": "closing-phase counter reset"})
            if res.regular_bars < FULL_SESSION_BARS:
                self._issue(symbol, "1m", session, "INCOMPLETE_SESSION", "WARN", str(path),
                            {"regular_bars": res.regular_bars, "missing_minutes": res.missing_regular_minutes})
            if res.flag_counts.get("ZERO_VOLUME", 0) > 30:
                self._issue(symbol, "1m", session, "MANY_ZERO_VOLUME_BARS", "INFO", str(path),
                            {"bars": res.flag_counts["ZERO_VOLUME"]})
            if res.flag_counts.get("LARGE_BAR_MOVE"):
                self._issue(symbol, "1m", session, "LARGE_INTRADAY_BAR_MOVE", "WARN", str(path),
                            {"bars": res.flag_counts["LARGE_BAR_MOVE"]})
        return {"file": str(path), "session": session, "decision": res.decision, "sha256": cur["sha256"],
                "arrays": arrays, "summary": summary, "changed": True}

    # ------------------------------------------------------------------ one symbol
    def ingest(self, symbol: str, *, use_minute: bool = True) -> SymbolData:
        symbol = normalize_symbol(symbol)
        daily_parts: list[dict[str, Any]] = []
        ddir = self.s.daily_root / symbol
        if ddir.is_dir():
            for p in sorted(ddir.iterdir()):
                if _DAILY.fullmatch(p.name):
                    daily_parts.append(self._daily_partition(p, symbol))
        minute_by_session: dict[str, list[dict[str, Any]]] = {}
        if use_minute:
            for root in self.s.minute_roots:
                mdir = root / symbol
                if not mdir.is_dir():
                    continue
                for p in sorted(mdir.iterdir()):
                    m = _MINUTE.fullmatch(p.name)
                    if m:
                        minute_by_session.setdefault(m.group(1), []).append(self._minute_partition(p, symbol, m.group(1)))
        changed = sum(p["changed"] for p in daily_parts) + sum(p["changed"] for v in minute_by_session.values() for p in v)
        # resolve cross-root minute duplicates
        minute_parts: list[dict[str, Any]] = []
        for session in sorted(minute_by_session):
            copies = minute_by_session[session]
            if len(copies) > 1:
                shas = {c["sha256"] for c in copies}
                if len(shas) == 1 and None not in shas:
                    self._issue(symbol, "1m", session, "DUPLICATE_IDENTICAL", "INFO", copies[1]["file"],
                                {"kept": copies[0]["file"], "copies": [c["file"] for c in copies]})
                else:
                    self._issue(symbol, "1m", session, "CONFLICTING_DUPLICATE", "WARN", copies[0]["file"],
                                {"copies": [c["file"] for c in copies], "sha256": sorted(str(s) for s in shas)})
                    continue
            minute_parts.append(copies[0])
        fp = sha({"v": INGEST_VERSION, "daily": [[Path(p["file"]).name, p["sha256"], p["decision"]] for p in daily_parts],
                  "minute": [[Path(p["file"]).name, p["sha256"], p["decision"]] for p in minute_parts],
                  "closing_phase_start": self.closing_phase_start})
        cached = self.db.one("SELECT * FROM candle_cache WHERE symbol=? AND timeframe='composite'", (symbol,))
        canon = self.canon_dir / symbol / f"{fp[:24]}.npz"
        if cached and cached["fingerprint"] == fp and canon.is_file():
            data = self._load_canon(symbol, canon, fp, json.loads(cached["summary_json"] or "{}"))
            data.changed_files = changed
            return data
        data = self._compose(symbol, daily_parts, minute_parts, fp)
        data.changed_files = changed
        canon.parent.mkdir(parents=True, exist_ok=True)
        tmp = canon.with_name(f".{canon.stem}.{os.getpid()}.npz")
        d, m = data.daily, data.minute
        np.savez_compressed(tmp, d_date=d["date"].to_numpy(dtype="U10"),
                            d_ohlcv=d[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float).reshape(-1, 5),
                            d_source=d["source"].to_numpy(dtype="U16"),
                            m_start=(m["start_ns"].to_numpy(dtype=np.int64) if len(m) else np.zeros(0, dtype=np.int64)),
                            m_ohlcv=(m[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float).reshape(-1, 5)),
                            m_flags=(m["flags"].to_numpy(dtype=np.int32) if len(m) else np.zeros(0, dtype=np.int32)))
        os.replace(tmp, canon)
        with self.db.tx() as c:
            c.execute("INSERT INTO candle_cache VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol, timeframe) DO UPDATE SET "
                      "fingerprint=excluded.fingerprint, path=excluded.path, rows=excluded.rows, first=excluded.first, "
                      "last=excluded.last, built_utc=excluded.built_utc, run_id=excluded.run_id, summary_json=excluded.summary_json",
                      (symbol, "composite", fp, str(canon), len(d), data.summary.get("first"), data.summary.get("last"),
                       utc_now(), self.db.run_id, jdump(data.summary)))
        self.db.event("CANDLES_COMPOSED", {"fingerprint": fp, "path": str(canon), **{k: data.summary.get(k) for k in (
            "first", "last", "daily_rows", "minute_derived_rows", "minute_sessions_used", "minute_bars",
            "excluded_conflict_dates")}}, subject=symbol)
        return data

    def _load_canon(self, symbol: str, path: Path, fp: str, summary: dict[str, Any]) -> SymbolData:
        with np.load(path, allow_pickle=False) as z:
            d = pd.DataFrame(z["d_ohlcv"], columns=["open", "high", "low", "close", "volume"])
            d.insert(0, "date", z["d_date"].astype(str))
            d["source"] = z["d_source"].astype(str)
            m = self._minute_frame(z["m_start"], z["m_ohlcv"], z["m_flags"])
        return SymbolData(symbol, d, m, fp, summary)

    @staticmethod
    def _minute_frame(start: np.ndarray, ohlcv: np.ndarray, flags: np.ndarray) -> pd.DataFrame:
        m = pd.DataFrame(ohlcv.reshape(-1, 5), columns=["open", "high", "low", "close", "volume"])
        m.insert(0, "start_ns", start.astype(np.int64))
        ist = pd.to_datetime(m["start_ns"], unit="ns", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        m.insert(1, "start_ist", ist)
        m.insert(2, "session", ist.dt.strftime("%Y-%m-%d"))
        m["flags"] = flags.astype(np.int32)
        return m

    def _compose(self, symbol: str, daily_parts: Sequence[dict[str, Any]], minute_parts: Sequence[dict[str, Any]],
                 fp: str) -> SymbolData:
        by_date: dict[str, tuple[float, ...]] = {}
        conflicts: set[str] = set()
        for p in daily_parts:
            if p["decision"] != "ACCEPT":
                continue
            a = p["arrays"]
            for dt, row in zip(a["date"].tolist(), a["ohlcv"].tolist()):
                prior = by_date.get(dt)
                if prior is not None and tuple(prior) != tuple(row):
                    conflicts.add(dt)
                by_date[dt] = tuple(row)
        for dt in sorted(conflicts):
            by_date.pop(dt, None)
            self._issue(symbol, "1d", dt, "OVERLAP_CONFLICT", "WARN", "multiple daily partitions",
                        {"action": "date excluded; overlapping partitions disagree"})
        overlaps = [(a["period"], b["period"]) for i, a in enumerate(daily_parts) for b in daily_parts[i + 1:]
                    if a["period"].split("_")[0] <= b["period"].split("_")[1] and b["period"].split("_")[0] <= a["period"].split("_")[1]]
        for a, b in overlaps:
            self._issue(symbol, "1d", f"{a}|{b}", "PARTITION_OVERLAP", "INFO", "daily partitions",
                        {"conflicting_dates": len(conflicts)})
        rows = [{"date": dt, "open": v[0], "high": v[1], "low": v[2], "close": v[3], "volume": v[4], "source": "DAILY"}
                for dt, v in by_date.items()]
        usable = [p for p in minute_parts if p["decision"] in {"ACCEPT", "REVIEW", "ACCEPT_CLOSING_PHASE_V2"}]
        recon: list[dict[str, float]] = []
        derived = 0
        for p in usable:
            day = p["arrays"]["day"]
            if day.size != 5:
                continue
            s = p["session"]
            if s in by_date:
                o = by_date[s]
                recon.append({"session": s, **{k: float(day[i] / o[i] - 1) if o[i] else float("nan")
                                               for i, k in enumerate(("open", "high", "low", "close", "volume"))}})
            elif s not in conflicts:
                rows.append({"date": s, "open": float(day[0]), "high": float(day[1]), "low": float(day[2]),
                             "close": float(day[3]), "volume": float(day[4]), "source": "MINUTE_DERIVED"})
                derived += 1
        d = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "source"])
        d = d.sort_values("date").reset_index(drop=True)
        # quality checks on the composite (recorded, not repaired)
        if len(d) > 1:
            prev = d["close"].shift(1)
            gap = (d["open"] / prev - 1).abs()
            for i in np.nonzero((gap > LARGE_GAP).to_numpy())[0]:
                self._issue(symbol, "1d", str(d["date"].iloc[i]), "CORPORATE_ACTION_UNCERTAIN", "WARN", "composite",
                            {"open_gap": float(gap.iloc[i]), "prev_close": float(prev.iloc[i]),
                             "open": float(d["open"].iloc[i]), "source": d["source"].iloc[i],
                             "note": "large gap; corporate action UNKNOWN (no registered action evidence)"})
            ds = [date.fromisoformat(x) for x in d["date"]]
            for a, b in zip(ds, ds[1:]):
                if (b - a).days > 4:
                    self._issue(symbol, "1d", f"{a}..{b}", "CALENDAR_GAP", "INFO", "composite",
                                {"calendar_days": (b - a).days, "note": "not explained; no holiday calendar used"})
            bad = d[(d["low"] > d[["open", "close"]].min(axis=1)) | (d["high"] < d[["open", "close"]].max(axis=1))]
            for _, r in bad.iterrows():
                self._issue(symbol, "1d", str(r["date"]), "OHLC_INCONSISTENT", "ERROR", "composite", {})
            neg = d[d["volume"] < 0]
            for _, r in neg.iterrows():
                self._issue(symbol, "1d", str(r["date"]), "NEGATIVE_DAILY_VOLUME", "ERROR", "composite", {})
        rec_summary: dict[str, Any] = {"shared_sessions": len(recon)}
        if recon:
            rd = pd.DataFrame(recon)
            for k in ("open", "high", "low", "close", "volume"):
                v = rd[k].abs()
                rec_summary[f"{k}_median_abs_rel_diff"] = float(v.median())
                rec_summary[f"{k}_sessions_over_0.5pct"] = int((v > 0.005).sum())
            for _, r in rd[(rd["close"].abs() > 0.005) | (rd["high"].abs() > 0.005) | (rd["low"].abs() > 0.005)].iterrows():
                self._issue(symbol, "1d", str(r["session"]), "MINUTE_DAILY_PRICE_MISMATCH", "WARN", "reconciliation",
                            {k: float(r[k]) for k in ("open", "high", "low", "close")})
            for _, r in rd[rd["volume"].abs() > 0.05].iterrows():
                self._issue(symbol, "1d", str(r["session"]), "MINUTE_DAILY_VOLUME_MISMATCH", "INFO", "reconciliation",
                            {"volume_rel_diff": float(r["volume"])})
        frames = []
        for p in usable:
            a = p["arrays"]
            if a["start_ns"].size:
                frames.append((a["start_ns"], np.column_stack([a["ohlc"], a["volume"]]), a["flags"]))
        if frames:
            m = self._minute_frame(np.concatenate([f[0] for f in frames]), np.concatenate([f[1] for f in frames]),
                                   np.concatenate([f[2] for f in frames]))
            m = m.sort_values("start_ns").reset_index(drop=True)
        else:
            m = self._minute_frame(np.zeros(0, dtype=np.int64), np.zeros((0, 5)), np.zeros(0, dtype=np.int32))
        decisions: dict[str, int] = {}
        for p in minute_parts:
            decisions[p["decision"]] = decisions.get(p["decision"], 0) + 1
        summary = {"symbol": symbol, "fingerprint": fp, "ingest_version": INGEST_VERSION,
                   "first": str(d["date"].iloc[0]) if len(d) else None, "last": str(d["date"].iloc[-1]) if len(d) else None,
                   "daily_rows": int(len(d)), "official_daily_rows": int((d["source"] == "DAILY").sum()),
                   "minute_derived_rows": derived, "excluded_conflict_dates": sorted(conflicts),
                   "daily_partitions": [{"period": p["period"], "decision": p["decision"], "sha256": p["sha256"]}
                                        for p in daily_parts],
                   "minute_files": len(minute_parts), "minute_decisions": decisions,
                   "minute_sessions_used": len(usable), "minute_bars": int(len(m)),
                   "minute_first": usable[0]["session"] if usable else None,
                   "minute_last": usable[-1]["session"] if usable else None,
                   "reconciliation": rec_summary}
        return SymbolData(symbol, d, m, fp, summary)


def ingest_many(settings: Settings, db: AuditDB, symbols: Iterable[str], *, use_minute: bool = True,
                progress: Callable[[int, int, str], None] | None = None,
                log: Callable[[str], None] = lambda _m: None) -> dict[str, SymbolData]:
    ing = Ingestor(settings, db, log)
    out: dict[str, SymbolData] = {}
    syms = list(symbols)
    for i, s in enumerate(syms):
        out[normalize_symbol(s)] = ing.ingest(s, use_minute=use_minute)
        if progress:
            progress(i + 1, len(syms), s)
    return out


def latest_completed_session(datas: Iterable[SymbolData]) -> str | None:
    lasts = [d.last_session for d in datas if d.last_session]
    return max(lasts) if lasts else None
