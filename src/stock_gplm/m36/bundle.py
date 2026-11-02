"""Build a manifest-bound reference bundle for one M3.1 run from the evidence store.

Each component is built only from registered sources whose adapters accept every
row. A component that is missing, rejected or in conflict with the observed candles
is reported as a gap with counts and is not written. Components that validate are
written in the M2.1 contracts and re-imported with the M2.1 importers before the
bundle is published, so M3.2 can consume them unchanged.

The bundle never infers sessions from candles, listings from a current list, or
ratios that NSE did not state. Point-in-time membership stays UNKNOWN until a
validated delisting history exists; no V1 adapter accepts one.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

from stock_gplm.m2.models import HistoricalAlias, Instrument, InstrumentInventory, Provenance
from stock_gplm.m2.reference_data import import_corporate_actions_v1, import_nse_calendar_v1

from . import adapters
from .store import SourceRecord, load_store

BUNDLE_CONTRACT = "stock-gplm.m36-reference-bundle.v1"
M31_USED = ("observed-sessions.csv", "symbol-coverage.csv", "ohlcv-flags.csv")
SOURCE_NAME = "National Stock Exchange of India; manual browser downloads normalised by stock-gplm M3.6"
CLASSES_FILE = "instrument-classes.csv"
COMPONENT_NAMES = ("calendar", "corporate_actions", "instrument_classification", "alias_history",
                   "point_in_time_membership")
WHERE_TO_GET = {
    "calendar": [
        "https://www.nseindia.com/resources/exchange-communication-holidays "
        "(trading holidays and the weekly trading schedule; save the page as nse-weekly-schedule)",
        "NSE circulars announcing each year's trading holidays and every special or Muhurat session "
        "(register each as nse-calendar-source, then transcribe them as nse-calendar-transcription)",
    ],
    "corporate_actions": [
        "https://www.nseindia.com/companies-listing/corporate-filings-actions "
        "(Equity, all symbols, date range covering the M3.1 span, CSV download)",
    ],
    "instrument_classification": [
        "https://betanseapi.nseindia.com/static/market-data/securities-available-for-trading "
        "(the equity list EQUITY_L.csv and the ETF list)",
    ],
    "alias_history": [
        "https://betanseapi.nseindia.com/static/market-data/securities-available-for-trading or "
        "https://www.nseindia.in/all-reports (the symbol-change list)",
    ],
    "point_in_time_membership": [
        "https://betanseapi.nseindia.com/static/market-data/securities-available-for-trading (EQUITY_L.csv listing dates)",
        "An NSE list of delisted and suspended securities covering the M3.1 span; no V1 adapter exists, "
        "so register the file and share a sanitised sample to add one",
    ],
}


@dataclass
class Component:
    name: str
    status: str = "BLOCKED"
    gaps: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def gap(self, code: str, count: int, detail: str, **extra: Any) -> None:
        self.gaps.append({"component": self.name, "code": code, "count": count, "detail": detail, **extra})

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "gaps": self.gaps, "report": self.report, "sources": self.sources,
                "where_to_get": WHERE_TO_GET[self.name] if self.gaps else []}


@dataclass(frozen=True)
class M31View:
    run_id: str
    sessions: tuple[str, ...]
    symbols: dict[str, tuple[str, str]]
    large_moves: tuple[tuple[str, str], ...]
    hashes: dict[str, str]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_m31(run: Path) -> M31View:
    """Read only the M3.1 outputs the bundle needs, after checking them against the manifest."""
    run = Path(run)
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contract") != "stock-gplm.m3-readiness-run.v1":
        raise ValueError("input is not an M3.1 readiness run")
    hashes = {}
    for name in M31_USED:
        expected = manifest.get("outputs", {}).get(name)
        if expected is None or _sha(run / name) != expected:
            raise ValueError(f"M3.1 output does not match its manifest: {name}")
        hashes[name] = expected
    with (run / "observed-sessions.csv").open(encoding="utf-8", newline="") as stream:
        sessions = tuple(row["trading_date"] for row in csv.DictReader(stream))
    with (run / "symbol-coverage.csv").open(encoding="utf-8", newline="") as stream:
        symbols = {row["symbol"]: (row["first_observed_date"], row["last_observed_date"])
                   for row in csv.DictReader(stream) if int(row["observed_candles"]) > 0}
    with (run / "ohlcv-flags.csv").open(encoding="utf-8", newline="") as stream:
        moves = tuple((row["symbol"], row["trading_date"]) for row in csv.DictReader(stream)
                      if row["code"] == "LARGE_CLOSE_MOVE")
    if not sessions or not symbols:
        raise ValueError("M3.1 run has no observed sessions or symbols")
    return M31View(manifest["run_id"], sessions, symbols, moves, hashes)


def _days(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def _ranges(days: Iterable[date]) -> list[list[str]]:
    """Compress dates into inclusive [start, end] ranges for reporting."""
    out: list[list[date]] = []
    for day in sorted(days):
        if out and day == out[-1][1] + timedelta(days=1):
            out[-1][1] = day
        else:
            out.append([day, day])
    return [[a.isoformat(), b.isoformat()] for a, b in out]


def _merged(intervals: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
    merged: list[list[date]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _in_range(record: SourceRecord, day: str) -> bool:
    return record.applicable_start <= day <= record.applicable_end


@dataclass
class _Parsed:
    record: SourceRecord
    parsed: adapters.Parsed


def _parse_all(component: Component, records: list[SourceRecord], kind: str,
               rejected_rows: list[dict[str, Any]], date_key: str | None = None) -> list[_Parsed]:
    """Parse each source; a file with any rejected row or layout mismatch is not used."""
    usable = []
    for record in records:
        component.sources.append(record.sha256)
        try:
            parsed = adapters.parse(kind, record.path, list(record.acquisition.get("derived_from") or []))
        except adapters.LayoutError as exc:
            component.gap("LAYOUT_NOT_SUPPORTED", 1, str(exc), source=record.sha256, observed_header=exc.observed)
            continue
        except ValueError as exc:
            component.gap("SOURCE_UNREADABLE", 1, str(exc), source=record.sha256)
            continue
        if date_key is not None:
            for item in list(parsed.records):
                if not _in_range(record, str(item[date_key])):
                    parsed.records.remove(item)
                    parsed.rejected.append({"reason": f"{date_key} {item[date_key]} outside the file's "
                                                      "declared applicable range", "row": item})
        for item in parsed.rejected:
            rejected_rows.append({"component": kind, "source": record.sha256, **item})
        if parsed.rejected:
            component.gap("SOURCE_ROWS_REJECTED", len(parsed.rejected),
                          f"{record.acquisition['file_name']}: rows rejected; the file is not used",
                          source=record.sha256)
            continue
        usable.append(_Parsed(record, parsed))
    return usable


def _latest(records: list[SourceRecord]) -> list[SourceRecord]:
    """The most recently downloaded file only; older snapshots of a current list add nothing."""
    if not records:
        return []
    return [max(records, key=lambda r: (r.acquisition["downloaded_at_utc"], r.sha256))]


# --------------------------------------------------------------------------- calendar

def build_calendar(records: list[SourceRecord], view: M31View, rejected_rows: list[dict[str, Any]]
                   ) -> tuple[Component, list[tuple[str, str, str]] | None]:
    component = Component("calendar")
    start, end = date.fromisoformat(view.sessions[0]), date.fromisoformat(view.sessions[-1])
    span = list(_days(start, end))
    component.report = {"span": [start.isoformat(), end.isoformat()], "span_dates": len(span)}
    weekly = [r for r in records if r.component == "nse-weekly-schedule"
              and r.applicable_start <= start.isoformat() and r.applicable_end >= end.isoformat()]
    component.sources += [r.sha256 for r in weekly]
    if not weekly:
        component.gap("WEEKLY_SCHEDULE_SOURCE_MISSING", len(span),
                      "no registered NSE weekly-schedule document covers the span; weekend status stays UNKNOWN")
    by_scope: dict[str, dict[str, tuple[str, str]]] = {}
    for scope, status in (("TRADING_HOLIDAYS", "MARKET_CLOSED"), ("SPECIAL_SESSIONS", "SPECIAL_SESSION")):
        candidates = [r for r in records if r.component == "nse-calendar-transcription" and r.scope == scope]
        usable = _parse_all(component, candidates, "nse-calendar-transcription", rejected_rows, "date")
        if scope == "SPECIAL_SESSIONS":
            # Special sessions are announced ad hoc, so a transcription cannot vouch for dates after it was made.
            for item in list(usable):
                if item.record.applicable_end > item.record.acquisition["downloaded_at_utc"][:10]:
                    component.gap("SPECIAL_SESSION_COVERAGE_CLAIMED_IN_FUTURE", 1,
                                  "a special-session transcription claims dates after it was made; not used",
                                  source=item.record.sha256)
                    usable.remove(item)
        dates: dict[str, tuple[str, str]] = {}
        for item in usable:
            for row in item.parsed.records:
                if row["status"] != status:
                    component.gap("TRANSCRIPTION_STATUS_SCOPE_MISMATCH", 1,
                                  f"{row['date']} has {row['status']} in a {scope} transcription",
                                  source=item.record.sha256)
                    continue
                label = f"{row['label']} [source {row['source_sha256'][:12]}]"
                if row["date"] in dates and dates[row["date"]][0] != status:
                    component.gap("TRANSCRIPTION_CONFLICT", 1, f"{row['date']} differs between files")
                dates[row["date"]] = (status, label)
        covered = _merged((date.fromisoformat(i.record.applicable_start), date.fromisoformat(i.record.applicable_end))
                          for i in usable)
        missing = [d for d in span if not any(a <= d <= b for a, b in covered)]
        if missing:
            component.gap(f"{scope}_COVERAGE_MISSING", len(missing),
                          f"span dates with no validated {scope.lower().replace('_', ' ')} transcription",
                          ranges=_ranges(missing))
        by_scope[scope] = dates
        component.report[f"{scope.lower()}_in_span"] = sum(start.isoformat() <= d <= end.isoformat() for d in dates)
    if component.gaps:
        return component, None
    rows: list[tuple[str, str, str]] = []
    for day in span:
        key = day.isoformat()
        if key in by_scope["SPECIAL_SESSIONS"]:
            rows.append((key, "SPECIAL_SESSION", by_scope["SPECIAL_SESSIONS"][key][1]))
        elif key in by_scope["TRADING_HOLIDAYS"]:
            rows.append((key, "MARKET_CLOSED", by_scope["TRADING_HOLIDAYS"][key][1]))
        elif day.weekday() >= 5:
            rows.append((key, "MARKET_CLOSED", f"WEEKLY_CLOSURE [source {weekly[0].sha256[:12]}]"))
        else:
            rows.append((key, "TRADING_SESSION", f"REGULAR_WEEKDAY [source {weekly[0].sha256[:12]}]"))
    # The calendar is checked against candles, never derived from them.
    sessions = {d for d, s, _ in rows if s in {"TRADING_SESSION", "SPECIAL_SESSION"}}
    observed = set(view.sessions)
    on_closed = sorted(observed - sessions)
    without = sorted(sessions - observed)
    component.report |= {"calendar_sessions": len(sessions), "observed_sessions": len(observed),
                         "special_sessions": sum(s == "SPECIAL_SESSION" for _, s, _ in rows)}
    if on_closed:
        component.gap("CANDLES_ON_CLOSED_DAY", len(on_closed),
                      "candles exist on dates the calendar marks closed", dates=on_closed)
    if without:
        component.gap("CALENDAR_SESSION_WITHOUT_ANY_CANDLE", len(without),
                      "calendar sessions with no candle for any symbol; explain them before use", dates=without)
    if component.gaps:
        return component, None
    component.status = "VALIDATED"
    return component, rows


# --------------------------------------------------------------------------- symbol changes and aliases

@dataclass(frozen=True)
class Change:
    old: str
    new: str
    effective: str


def _alias_chain(symbol: str, changes: list[Change], before: str | None) -> list[tuple[str, str | None, str]]:
    """Earlier names of `symbol`, newest first, as (alias, valid_from, valid_to)."""
    into = [c for c in changes if c.new == symbol and (before is None or c.effective < before)]
    if not into:
        return []
    latest = max(into, key=lambda c: c.effective)
    valid_to = (date.fromisoformat(latest.effective) - timedelta(days=1)).isoformat()
    earlier = _alias_chain(latest.old, changes, latest.effective)
    valid_from = earlier[0][2] if earlier else None
    if valid_from is not None:
        valid_from = (date.fromisoformat(valid_from) + timedelta(days=1)).isoformat()
    return [(latest.old, valid_from, valid_to), *earlier]


def build_aliases(records: list[SourceRecord], view: M31View, rejected_rows: list[dict[str, Any]]
                  ) -> tuple[Component, list[Change] | None]:
    component = Component("alias_history")
    candidates = _latest([r for r in records if r.component == "nse-symbol-changes"])
    usable = _parse_all(component, candidates, "nse-symbol-changes", rejected_rows)
    if not candidates:
        component.gap("SYMBOL_CHANGE_SOURCE_MISSING", len(view.symbols),
                      "no registered NSE symbol-change list; alias history stays UNKNOWN for every symbol")
    if not usable:
        return component, None
    changes = [Change(r["old_symbol"], r["new_symbol"], r["effective_date"]) for r in usable[0].parsed.records]
    last = view.sessions[-1]
    # A change before the first candle means the old name was later reused, which is not a conflict.
    renamed_away = sorted(s for s in view.symbols if any(
        c.old + "-EQ" == s and view.symbols[s][0] < c.effective <= view.symbols[s][1] for c in changes))
    with_aliases = sum(bool(_alias_chain(s[:-3], changes, None)) for s in view.symbols)
    component.report = {"changes": len(changes), "symbols_with_earlier_names": with_aliases,
                        "changes_effective_in_span": sum(view.sessions[0] <= c.effective <= last for c in changes)}
    if renamed_away:
        component.gap("CANDLES_UNDER_A_RETIRED_SYMBOL", len(renamed_away),
                      "M3.1 symbols that NSE renamed on or before their last candle", symbols=renamed_away)
        return component, None
    component.status = "VALIDATED"
    return component, changes


# --------------------------------------------------------------------------- corporate actions

def _canonical(symbol: str, ex_date: str, changes: list[Change] | None) -> str:
    """Follow later symbol changes so an action filed under an old name lands on today's name."""
    current, when = symbol, ex_date
    for _ in range(32):
        later = [c for c in (changes or []) if c.old == current and c.effective > when]
        if not later:
            return current
        step = min(later, key=lambda c: c.effective)
        current, when = step.new, step.effective
    raise ValueError(f"symbol-change chain for {symbol} does not terminate")


def build_corporate_actions(records: list[SourceRecord], view: M31View, changes: list[Change] | None,
                            rejected_rows: list[dict[str, Any]]
                            ) -> tuple[Component, dict[str, Any] | None, dict[str, list[dict[str, Any]]]]:
    component = Component("corporate_actions")
    side: dict[str, list[dict[str, Any]]] = {"unresolved": [], "out_of_universe": [], "unexplained_moves": []}
    files = [r for r in records if r.component == "nse-corporate-actions"]
    partial = [r for r in files if r.scope != "ALL_EQUITY_SYMBOLS"]
    for record in partial:
        component.sources.append(record.sha256)
        component.gap("PARTIAL_SCOPE_SOURCE_NOT_USED", 1,
                      "a selected-symbols export cannot establish complete coverage", source=record.sha256)
    usable = _parse_all(component, [r for r in files if r.scope == "ALL_EQUITY_SYMBOLS"],
                        "nse-corporate-actions", rejected_rows, "ex_date")
    start, end = view.sessions[0], view.sessions[-1]
    covered = _merged((date.fromisoformat(i.record.applicable_start), date.fromisoformat(i.record.applicable_end))
                      for i in usable)
    window = next(((a, b) for a, b in covered if a.isoformat() <= start and b.isoformat() >= end), None)
    if window is None:
        missing = [d for d in _days(date.fromisoformat(start), date.fromisoformat(end))
                   if not any(a <= d <= b for a, b in covered)]
        component.gap("CORPORATE_ACTION_COVERAGE_MISSING", len(view.symbols),
                      f"symbols without complete corporate-action coverage; {len(missing)} span dates uncovered",
                      ranges=_ranges(missing))
        return component, None, side
    actions: dict[str, dict[tuple[str, str, str, str], dict[str, Any]]] = {s: {} for s in view.symbols}
    non_eq = 0
    for item in usable:
        for row in item.parsed.records:
            if row["series"] != "EQ":
                non_eq += 1
                continue
            canonical = _canonical(str(row["symbol"]), str(row["ex_date"]), changes) + "-EQ"
            details = dict(row["details"])  # type: ignore[arg-type]
            if canonical != f"{row['symbol']}-EQ":
                details["reported_symbol"] = str(row["symbol"])
            if canonical not in actions:
                side["out_of_universe"].append({"symbol": row["symbol"], "ex_date": row["ex_date"],
                                                "purpose": row["purpose"], "action_type": row["action_type"]})
                continue
            key = (str(row["ex_date"]), str(row["action_type"]), str(row["purpose"]), str(row["symbol"]))
            actions[canonical][key] = {"action_type": row["action_type"], "effective_date": row["ex_date"],
                                       "adjustment_status": row["adjustment_status"],
                                       "details": {k: str(v) for k, v in details.items()}}
    catalogs = []
    for symbol in sorted(actions):
        ordered = [actions[symbol][k] for k in sorted(actions[symbol])]
        catalogs.append({"symbol": symbol, "coverage_status": "COMPLETE", "coverage_start": window[0].isoformat(),
                         "coverage_end": window[1].isoformat(), "actions": ordered})
        for action in ordered:
            if action["adjustment_status"] in {"UNKNOWN", "POLICY_REQUIRED"}:
                side["unresolved"].append({"symbol": symbol, **{k: action[k] for k in (
                    "effective_date", "action_type", "adjustment_status")},
                    "purpose": action["details"].get("purpose", "")})
    # Cross-check flagged large close moves against price-affecting actions (report only).
    position = {d: i for i, d in enumerate(view.sessions)}
    breaking = {c["symbol"]: {a["effective_date"] for a in c["actions"] if a["adjustment_status"] != "NOT_APPLICABLE"}
                for c in catalogs}
    explained = 0
    for symbol, day in view.large_moves:
        # An ex-date on a closed day shows up at the next session, so (previous session, day] counts.
        prior = view.sessions[position[day] - 1] if position.get(day, 0) > 0 else ""
        if any(prior < ex <= day for ex in breaking.get(symbol, ())):
            explained += 1
        else:
            side["unexplained_moves"].append({"symbol": symbol, "trading_date": day})
    stated = [a for c in catalogs for a in c["actions"] if "price_adjustment_factor" in a["details"]]
    by_type: dict[str, int] = {}
    for c in catalogs:
        for a in c["actions"]:
            by_type[f"{a['action_type']}:{a['adjustment_status']}"] = by_type.get(
                f"{a['action_type']}:{a['adjustment_status']}", 0) + 1
    component.report = {
        "coverage_window": [window[0].isoformat(), window[1].isoformat()],
        "symbols_with_complete_coverage": len(catalogs),
        "actions_in_universe": sum(len(c["actions"]) for c in catalogs),
        "actions_by_type_and_status": dict(sorted(by_type.items())),
        "actions_with_stated_ratio": len(stated),
        "unresolved_actions_kept_as_series_breaks": len(side["unresolved"]),
        "actions_outside_universe": len(side["out_of_universe"]),
        "non_eq_series_rows": non_eq,
        "large_close_moves": len(view.large_moves), "large_close_moves_explained": explained,
        "large_close_moves_unexplained": len(view.large_moves) - explained,
        "factor_range_of_stated_ratios": (
            [str(min(Fraction(a["details"]["price_adjustment_factor"]) for a in stated)),
             str(max(Fraction(a["details"]["price_adjustment_factor"]) for a in stated))] if stated else None),
    }
    component.status = "VALIDATED"
    payload = {"catalogs": catalogs, "window": [window[0].isoformat(), window[1].isoformat()]}
    return component, payload, side


# --------------------------------------------------------------------------- listings and classes

def build_listings(records: list[SourceRecord], view: M31View, rejected_rows: list[dict[str, Any]]
                   ) -> tuple[Component, Component, dict[str, dict[str, Any]], dict[str, tuple[str, str]] | None]:
    classes = Component("instrument_classification")
    membership = Component("point_in_time_membership")
    equity = _parse_all(classes, _latest([r for r in records if r.component == "nse-equity-list"]),
                        "nse-equity-list", rejected_rows)
    etf = _parse_all(classes, _latest([r for r in records if r.component == "nse-etf-list"]),
                     "nse-etf-list", rejected_rows)
    listing: dict[str, dict[str, Any]] = {}
    labelled: dict[str, set[str]] = {}
    for parsed, cls in ((equity, "EQUITY"), (etf, "ETF")):
        for item in parsed:
            for row in item.parsed.records:
                if cls == "EQUITY" and row["series"] != "EQ":
                    continue
                symbol = f"{row['symbol']}-EQ"
                labelled.setdefault(symbol, set()).add(cls)
                listing[symbol] = {"listing_date": row["listing_date"], "isin": row["isin"],
                                   "source": item.record}
    for kind, parsed in (("nse-equity-list", equity), ("nse-etf-list", etf)):
        if not any(r.component == kind for r in records):
            classes.gap(f"{kind.upper().replace('-', '_')}_SOURCE_MISSING", len(view.symbols),
                        f"no registered {kind}; ETF/equity separation is not established")
    result: dict[str, tuple[str, str]] | None = None
    if equity and etf:
        result = {}
        source = {"EQUITY": equity[0].record.sha256, "ETF": etf[0].record.sha256}
        conflicts = []
        for symbol in sorted(view.symbols):
            found = labelled.get(symbol, set())
            if len(found) == 1:
                cls = next(iter(found))
                result[symbol] = (cls, source[cls])
            else:
                result[symbol] = ("UNKNOWN", "")
                if len(found) > 1:
                    conflicts.append(symbol)
        counts: dict[str, int] = {}
        for cls, _ in result.values():
            counts[cls] = counts.get(cls, 0) + 1
        classes.report = {"class_counts": dict(sorted(counts.items())), "listed_in_both": conflicts}
        classes.status = "VALIDATED"
    membership.sources = [item.record.sha256 for item in equity + etf]
    with_listing = sorted(s for s in view.symbols if s in listing)
    before = sorted(s for s in with_listing if listing[s]["listing_date"] > view.symbols[s][0])
    membership.report = {"symbols_with_exchange_listing_date": len(with_listing),
                         "symbols_without_listing_date": len(view.symbols) - len(with_listing),
                         "symbols_with_candles_before_listing_date": len(before)}
    if before:
        membership.gap("CANDLES_BEFORE_EXCHANGE_LISTING_DATE", len(before),
                       "candles precede the NSE listing date (migration or relisting needs evidence)",
                       symbols=before)
    delisted = [r for r in records if r.component == "nse-delisted-securities"]
    membership.sources += [r.sha256 for r in delisted]
    if delisted:
        membership.gap("DELISTING_HISTORY_LAYOUT_UNCONFIRMED", len(view.symbols),
                       "a delisting source is registered but no V1 adapter has been validated against it; "
                       "membership stays UNKNOWN")
    else:
        membership.gap("DELISTING_HISTORY_SOURCE_MISSING", len(view.symbols),
                       "without delisting and suspension history, a current list cannot prove historical "
                       "membership; membership stays UNKNOWN and delisted securities absent from M3.1 "
                       "cannot be counted")
    if not with_listing:
        membership.gap("LISTING_DATE_SOURCE_MISSING", len(view.symbols), "no validated NSE listing dates")
    return classes, membership, listing, result


def build_inventory(view: M31View, listing: dict[str, dict[str, Any]], changes: list[Change] | None,
                    as_of: str) -> InstrumentInventory:
    """Identities from M3.1 enriched with NSE listing dates, ISINs and aliases; membership bounds stay unset."""
    base = Provenance(source=f"M3.1 readiness run {view.run_id}", as_of_date=view.sessions[-1],
                      reference="symbol-coverage.csv")
    instruments = []
    for symbol in sorted(view.symbols):
        provenance = {"nse_symbol": base}
        fields: dict[str, Any] = {}
        entry = listing.get(symbol)
        if entry is not None:
            record: SourceRecord = entry["source"]
            nse = Provenance(source=SOURCE_NAME, as_of_date=record.acquisition["downloaded_at_utc"][:10],
                             reference=f"{record.acquisition['source_url']} sha256:{record.sha256}")
            fields |= {"listing_date": entry["listing_date"], "isin": entry["isin"]}
            provenance |= {"listing_date": nse, "isin": nse}
        aliases: tuple[HistoricalAlias, ...] = ()
        if changes is not None:
            alias_source = Provenance(source=SOURCE_NAME, as_of_date=as_of, reference="nse-symbol-changes")
            aliases = tuple(HistoricalAlias(f"{old}-EQ", start, end, alias_source)
                            for old, start, end in _alias_chain(symbol[:-3], changes, None))
            fields["alias_history_status"] = "COMPLETE"
            provenance["alias_history_status"] = alias_source
        instruments.append(Instrument(nse_symbol=symbol, aliases=aliases, field_provenance=provenance, **fields))
    return InstrumentInventory(as_of_date=as_of, instruments=tuple(instruments))


# --------------------------------------------------------------------------- bundle

def _manifest(contract: str, payload: str, digest: str, sources: list[SourceRecord], start: str, end: str
              ) -> dict[str, Any]:
    latest = max(r.acquisition["downloaded_at_utc"] for r in sources)
    return {"schema_version": 1, "contract": contract, "payload_file": payload, "sha256": digest,
            "source": SOURCE_NAME, "source_reference": "m36-sources:" + ",".join(sorted(r.sha256 for r in sources)),
            "publication_date": latest[:10], "acquired_at_utc": latest, "effective_start": start,
            "effective_end": end, "coverage_status": "COMPLETE"}


def _write(path: Path, data: bytes | str) -> None:
    with path.open("xb") as stream:
        stream.write(data.encode() if isinstance(data, str) else data)


def _json(path: Path, value: Any) -> None:
    _write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _csv(rows: list[dict[str, Any]], columns: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _gaps_markdown(bundle_id: str, view: M31View, components: dict[str, Component], store_errors: list[str]) -> str:
    lines = [f"# M3.6 reference evidence gaps — bundle `{bundle_id[:12]}`", "",
             f"M3.1 run `{view.run_id[:12]}`: {len(view.symbols)} symbols, {len(view.sessions)} observed sessions, "
             f"{view.sessions[0]} to {view.sessions[-1]}.", "",
             "| Component | Status | Gaps |", "| --- | --- | --- |"]
    for name in COMPONENT_NAMES:
        c = components[name]
        lines.append(f"| `{name}` | {c.status} | {len(c.gaps)} |")
    if store_errors:
        lines += ["", "## Evidence-store integrity errors", ""] + [f"- {e}" for e in store_errors]
    for name in COMPONENT_NAMES:
        c = components[name]
        if not c.gaps:
            continue
        lines += ["", f"## `{name}`", "", "| Gap | Count | Detail |", "| --- | --- | --- |"]
        lines += [f"| `{g['code']}` | {g['count']} | {g['detail']} |" for g in c.gaps]
        lines += ["", "Official sources to download manually:", ""] + [f"- {w}" for w in WHERE_TO_GET[name]]
    return "\n".join(lines) + "\n"


def build_bundle(*, store_root: Path, readiness_run: Path, output_root: Path,
                 protected_roots: tuple[Path, ...] = ()) -> Path:
    """Write a bundle (validated components plus a full gap report) and return its directory."""
    output_root = Path(output_root).resolve()
    for root in (*protected_roots, Path(store_root), Path(readiness_run)):
        root = Path(root).resolve()
        if output_root == root or root in output_root.parents:
            raise ValueError(f"bundle output must be outside {root}")
    view = load_m31(readiness_run)
    records, store_errors = load_store(store_root)
    binding = {
        "contract": BUNDLE_CONTRACT, "adapter_version": adapters.ADAPTER_VERSION, "m31_run_id": view.run_id,
        "m31_outputs": view.hashes, "store_errors": store_errors,
        "sources": sorted(({k: v for k, v in r.acquisition.items() if k != "registered_at_utc"}
                           for r in records), key=lambda a: (a["component"], a["sha256"])),
    }
    bundle_id = _hash(binding)
    final = output_root / bundle_id
    if final.exists():
        raise FileExistsError(f"reference bundle already exists: {bundle_id}")
    rejected: list[dict[str, Any]] = []
    calendar, calendar_rows = build_calendar(records, view, rejected)
    aliases, changes = build_aliases(records, view, rejected)
    actions, action_payload, side = build_corporate_actions(records, view, changes, rejected)
    classes, membership, listing, class_map = build_listings(records, view, rejected)
    components = {c.name: c for c in (calendar, actions, classes, aliases, membership)}
    by_sha = {r.sha256: r for r in records}
    output_root.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f".{bundle_id}.partial-", dir=output_root))
    try:
        if calendar_rows is not None:
            text = _csv([dict(zip(("date", "status", "label"), r)) for r in calendar_rows], ["date", "status", "label"])
            _write(partial / "nse-calendar.csv", text)
            sources = [by_sha[s] for s in calendar.sources]
            for s in list(sources):
                sources += [by_sha[d] for d in s.acquisition.get("derived_from") or []]
            _json(partial / "nse-calendar.csv.manifest.json", _manifest(
                "stock-gplm.nse-calendar.v1", "nse-calendar.csv", _sha(partial / "nse-calendar.csv"),
                sources, calendar_rows[0][0], calendar_rows[-1][0]))
            import_nse_calendar_v1(partial / "nse-calendar.csv", partial / "nse-calendar.csv.manifest.json")
        if action_payload is not None:
            _json(partial / "corporate-actions.json", {"catalogs": action_payload["catalogs"]})
            _json(partial / "corporate-actions.json.manifest.json", _manifest(
                "stock-gplm.corporate-actions.v1", "corporate-actions.json", _sha(partial / "corporate-actions.json"),
                [by_sha[s] for s in actions.sources], *action_payload["window"]))
            import_corporate_actions_v1(partial / "corporate-actions.json",
                                        partial / "corporate-actions.json.manifest.json")
        if listing or changes is not None:
            used = [r for r in records if r.sha256 in set(aliases.sources) | set(membership.sources)]
            as_of = max((r.acquisition["downloaded_at_utc"][:10] for r in used), default=view.sessions[-1])
            inventory = build_inventory(view, listing, changes, as_of)
            _json(partial / "inventory.json", inventory.to_dict())
            InstrumentInventory.load(partial / "inventory.json")
        if class_map is not None:
            _write(partial / CLASSES_FILE, _csv(
                [{"symbol": s, "instrument_class": c, "source_sha256": src} for s, (c, src) in sorted(class_map.items())],
                ["symbol", "instrument_class", "source_sha256"]))
        _write(partial / "corporate-actions-unresolved.csv", _csv(
            side["unresolved"], ["symbol", "effective_date", "action_type", "adjustment_status", "purpose"]))
        _write(partial / "corporate-actions-outside-universe.csv", _csv(
            side["out_of_universe"], ["symbol", "ex_date", "action_type", "purpose"]))
        _write(partial / "large-moves-unexplained.csv", _csv(side["unexplained_moves"], ["symbol", "trading_date"]))
        _write(partial / "rejected-rows.jsonl", "".join(json.dumps(r, sort_keys=True, default=str) + "\n"
                                                        for r in rejected))
        report = {
            "schema_version": 1, "contract": BUNDLE_CONTRACT, "bundle_id": bundle_id, "m31_run_id": view.run_id,
            "span": [view.sessions[0], view.sessions[-1]], "symbols": len(view.symbols),
            "observed_sessions": len(view.sessions), "store_errors": store_errors,
            "sources": [r.summary() for r in sorted(records, key=lambda r: (r.component, r.sha256))],
            "components": {name: components[name].to_dict() for name in COMPONENT_NAMES},
            "gap_count": sum(len(c.gaps) for c in components.values()) + len(store_errors),
        }
        _json(partial / "coverage-report.json", report)
        _write(partial / "evidence-gaps.md", _gaps_markdown(bundle_id, view, components, store_errors))
        outputs = {p.name: _sha(p) for p in sorted(partial.iterdir()) if p.is_file()}
        _json(partial / "bundle-manifest.json", {
            **binding, "bundle_id": bundle_id,
            "components": {name: components[name].status for name in COMPONENT_NAMES},
            "outputs": outputs, "raw_inputs_read": False, "network_used": False,
        })
        os.rename(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final


@dataclass(frozen=True)
class LoadedBundle:
    bundle_id: str
    components: dict[str, str]
    calendar: tuple[Path, Path] | None
    corporate_actions: tuple[Path, Path] | None
    inventory: Path | None
    instrument_classes: dict[str, str] | None


def load_bundle(path: Path, readiness_run_id: str) -> LoadedBundle:
    """Verify a bundle's outputs and binding; expose only components that validated."""
    path = Path(path)
    manifest = json.loads((path / "bundle-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contract") != BUNDLE_CONTRACT or manifest.get("bundle_id") != path.name:
        raise ValueError("input is not an M3.6 reference bundle")
    if manifest.get("m31_run_id") != readiness_run_id:
        raise ValueError("reference bundle was built for a different M3.1 run")
    for name, digest in manifest["outputs"].items():
        if _sha(path / name) != digest:
            raise ValueError(f"reference bundle output does not match its manifest: {name}")
    status = manifest["components"]
    classes = None
    if status["instrument_classification"] == "VALIDATED":
        with (path / CLASSES_FILE).open(encoding="utf-8", newline="") as stream:
            classes = {row["symbol"]: row["instrument_class"] for row in csv.DictReader(stream)}

    def pair(name: str, component: str) -> tuple[Path, Path] | None:
        return (path / name, path / f"{name}.manifest.json") if status[component] == "VALIDATED" else None

    return LoadedBundle(
        manifest["bundle_id"], dict(status), pair("nse-calendar.csv", "calendar"),
        pair("corporate-actions.json", "corporate_actions"),
        path / "inventory.json" if "inventory.json" in manifest["outputs"] else None, classes,
    )

