"""Synthetic, deterministic M5 fixtures. Nothing here is market evidence."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from stock_gplm.m5.evidence import ReferenceEvidence, load_reference_evidence
from stock_gplm.m5.models import canonical_hash

ROOT = Path(__file__).parents[1]
START = date(2024, 1, 1)
END = date(2024, 9, 30)
HOLDOUT = "2024-10-01"
CLOSED = {"2024-01-26", "2024-03-25", "2024-08-15"}  # synthetic closures, not the real NSE calendar


def sessions(start: date = START, end: date = END) -> list[str]:
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor.isoformat() not in CLOSED:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def price_path(days: list[str], phase: float = 0.0) -> list[tuple[str, str, str, str, str, int]]:
    rows = []
    previous = 100.0
    for i, day in enumerate(days):
        close = round(100 + 12 * math.sin(i / 7 + phase) + 0.04 * i, 2)
        opening = round(previous * (1 + 0.003 * math.cos(i + phase)), 2)
        high = round(max(opening, close) + 0.6, 2)
        low = round(min(opening, close) - 0.6, 2)
        rows.append((day, f"{opening:.2f}", f"{high:.2f}", f"{low:.2f}", f"{close:.2f}", 1000 + 37 * (i % 9)))
        previous = close
    return rows


def _encode_rows(rows: list[tuple[str, str, str, str, str, int]]) -> bytes:
    records = []
    for day, o, h, low, c, v in sorted(rows, reverse=True):
        when = date.fromisoformat(day)
        stamp = int(datetime(when.year, when.month, when.day, tzinfo=timezone.utc).timestamp())
        records.append(json.dumps({"time": when.strftime("%d-%b-%Y").upper(), "into": o, "inth": h, "intl": low,
                                   "intc": c, "ssboe": str(stamp), "intv": f"{v}.00"}))
    return gzip.compress(json.dumps(records).encode(), mtime=0)


def write_segment(raw_root: Path, symbol: str, start: str, end: str, rows: list[tuple], *,
                  done: bool = True, meta_overrides: dict[str, Any] | None = None) -> Path:
    directory = raw_root / symbol
    directory.mkdir(parents=True, exist_ok=True)
    payload = _encode_rows(rows)
    digest = hashlib.sha256(payload).hexdigest()
    stem = f"{start}_{end}"
    (directory / f"{stem}.json.gz").write_bytes(payload)
    meta = {"source": "shoonya", "symbol": symbol, "from": start, "to": end, "quality_status": "UNVALIDATED_RAW",
            "sha256_compressed": digest, "records": len(rows), "fetched_at_utc": "2024-10-02T00:00:00+00:00"}
    meta.update(meta_overrides or {})
    (directory / f"{stem}.meta.json").write_text(json.dumps(meta))
    if done:
        (directory / f"{stem}.json.gz.done").write_text(json.dumps({
            "status": "COMPLETE", "file": f"{stem}.json.gz", "bytes": len(payload), "records": len(rows),
            "sha256": digest, "quality_status": "UNVALIDATED_RAW", "published_at_utc": "2024-10-02T00:00:00+00:00"}))
    return directory / f"{stem}.json.gz"


def write_symbol(raw_root: Path, symbol: str, phase: float = 0.0, *, drop: set[str] = frozenset(),
                 extra_days: list[str] | None = None) -> list[tuple]:
    """Two published windows (H1 and H2 2024), the second straddling the synthetic holdout."""
    days = sorted({*sessions(START, date(2024, 12, 31)), *(extra_days or [])})
    rows = [r for r in price_path(days, phase) if r[0] not in drop]
    write_segment(raw_root, symbol, "2024-01-01", "2024-06-30", [r for r in rows if r[0] <= "2024-06-30"])
    write_segment(raw_root, symbol, "2024-07-01", "2024-12-31", [r for r in rows if r[0] >= "2024-07-01"])
    return rows


def _manifest(directory: Path, name: str, contract: str, payload: dict, coverage: str = "COMPLETE",
              start: str = "2023-01-01", end: str = "2024-12-31") -> dict[str, str]:
    raw = json.dumps(payload).encode()
    (directory / f"{name}.json").write_bytes(raw)
    (directory / f"{name}.manifest.json").write_text(json.dumps({
        "schema_version": 1, "contract": contract, "payload_file": f"{name}.json",
        "sha256": hashlib.sha256(raw).hexdigest(), "source": "synthetic-test-fixture",
        "source_reference": "synthetic://not-market-evidence", "publication_date": "2024-10-01",
        "acquired_at_utc": "2024-10-02T00:00:00Z", "effective_start": start, "effective_end": end,
        "coverage_status": coverage}))
    return {"payload": f"{name}.json", "manifest": f"{name}.manifest.json"}


def calendar_registration(start: date = date(2023, 12, 1), end: date = date(2024, 12, 31),
                          closed: set[str] = CLOSED) -> dict[str, Any]:
    manifest = {
        "document_file": "nse-holidays.pdf", "document_sha256": "a" * 64, "document_media_type": "application/pdf",
        "records_file": "calendar.csv", "records_sha256": "b" * 64,
        "source_reference": "https://www.nseindia.com/resources/exchange-communication-holidays",
        "license_reference": "synthetic fixture", "publication_date": "2023-12-01",
        "acquired_at_utc": "2023-12-02T00:00:00Z", "effective_start": start.isoformat(),
        "effective_end": end.isoformat(), "exchange": "NSE", "market": "EQUITY", "timezone_name": "Asia/Kolkata",
        "acquisition_method": "MANUAL_BROWSER_DOWNLOAD", "usage_basis": "USER_CONFIRMED_AUTHORIZED_RESEARCH_USE",
        "coverage_status": "COMPLETE", "contract": "stock-gplm.m25r-calendar-registration-manifest.v1",
        "schema_version": 1,
    }
    rows = []
    cursor = start
    while cursor <= end:
        is_open = cursor.weekday() < 5 and cursor.isoformat() not in closed
        rows.append({"exchange": "NSE", "date": cursor.isoformat(),
                     "status": "TRADING_SESSION" if is_open else "MARKET_CLOSED",
                     "session_open": "09:15" if is_open else None, "session_close": "15:30" if is_open else None,
                     "label": None})
        cursor += timedelta(days=1)
    binding = {"schema_version": 1, "contract": "stock-gplm.m25r-registered-calendar.v1",
               "manifest": manifest, "sessions": rows}
    return {**binding, "registration_id": canonical_hash(binding)}


def write_master(path: Path, symbols: tuple[str, ...]) -> Path:
    """Synthetic M2.2-shaped Shoonya master (tokens 1000, 1001, ...); not the real master."""
    rows = "".join(f"NSE,{1000 + i},1,{s.rsplit('-', 1)[0]},{s},EQ,0.05,\n" for i, s in enumerate(symbols))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("NSE_symbols.txt", "Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,\n" + rows)
    return path


def price_basis_registration(symbols: tuple[str, ...], basis: str = "RAW_UNADJUSTED",
                             start: str = "2023-01-01", end: str = "2024-12-31") -> dict[str, Any]:
    """Synthetic reviewed price-basis registration; real Shoonya data has none (basis stays unverified)."""
    value = {"contract": "stock-gplm.m51-provider-price-basis.v1", "schema_version": 1, "provider": "shoonya",
             "exchange": "NSE", "basis": basis, "symbols": list(symbols), "effective_start": start,
             "effective_end": end, "source_reference": "synthetic://not-market-evidence",
             "document_sha256": "c" * 64, "acquisition_method": "MANUAL_REVIEW",
             "reviewed_at_utc": "2024-10-02T00:00:00Z", "review_note": "synthetic test fixture"}
    return value | {"registration_id": canonical_hash(value)}


def event_record(ex_date: str, available: str, *, kind: str = "STOCK_SPLIT", new: int | None = 2,
                 base: int | None = 1, symbol: str = "ALPHA-EQ", event_id: str | None = None,
                 **extra: Any) -> dict[str, Any]:
    """One M3.9-B ``CorporateActionEvent.to_dict`` record for the adjustment-events file."""
    record = {"event_id": event_id or f"{kind.lower()}-{ex_date}", "symbol": symbol, "event_type": kind,
              "ex_date": ex_date, "event_date": None, "record_date": None, "announcement_date": None,
              "source": {"source": "synthetic-fixture", "source_identifier": f"evt-{ex_date}",
                         "licence_status": "APPROVED_FOR_DERIVATION", "available_at": available,
                         "retrieved_at": "2024-10-02T00:00:00Z"},
              "validation_state": "VALIDATED", "validation_reason": None, "new_shares": new, "base_shares": base,
              "cash_amount_inr": None, "reference_price_inr": None, "subscription_price_inr": None,
              "stated_price_factor": None}
    return record | extra


def build_world(tmp_path: Path, symbols: tuple[str, ...] = ("ALPHA-EQ", "BETA-EQ"), *,
                listing: bool = True, calendar: bool = True, aliases: str | None = "COMPLETE",
                corporate_actions: bool = True, ca_actions: list[dict] | None = None,
                alias_records: list[dict] | None = None,
                provider_basis: str | None = "RAW_UNADJUSTED",
                events: list[dict] | None = None) -> tuple[Path, ReferenceEvidence]:
    raw_root = tmp_path / "data" / "raw" / "shoonya" / "NSE"
    raw_root.mkdir(parents=True)
    for index, symbol in enumerate(symbols):
        write_symbol(raw_root, symbol, phase=index * 1.3)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    m32 = evidence_dir / "m32-run"
    m32.mkdir()
    plan = {"schema_version": 1, "plans": [
        {"horizon": "1-7d", "locked_holdout": {"first_as_of": "2024-11-01", "last_as_of": "2024-12-31"}},
        {"horizon": "6-12m", "locked_holdout": {"first_as_of": HOLDOUT, "last_as_of": "2024-12-31"}}]}
    raw_plan = json.dumps(plan).encode()
    (m32 / "split-plan.json").write_bytes(raw_plan)
    (m32 / "run-manifest.json").write_text(json.dumps({
        "contract": "stock-gplm.m32-research-run.v1", "run_id": "synthetic-m32",
        "outputs": {"split-plan.json": hashlib.sha256(raw_plan).hexdigest()}}))
    paths: dict[str, Any] = {"contract": "stock-gplm.m5-evidence-paths.v1", "m32_run": "m32-run",
                             "etf_heuristic": str(ROOT / "config" / "m37" / "etf-symbol-heuristic.json")}
    if calendar:
        (evidence_dir / "calendar-registration.json").write_text(json.dumps(calendar_registration()))
        paths["calendar_registration"] = "calendar-registration.json"
    if listing:
        paths["listing_history"] = _manifest(evidence_dir, "listing", "stock-gplm.listing-history.v1", {"records": [
            {"canonical_symbol": s, "listing_date": "2023-01-02", "delisting_date": None,
             "membership_verified_through": "2024-12-31"} for s in symbols]})
    if aliases is not None:
        paths["symbol_mappings"] = _manifest(evidence_dir, "aliases", "stock-gplm.historical-symbol-mappings.v1",
                                             {"records": alias_records or []}, coverage=aliases)
    if corporate_actions:
        paths["corporate_actions"] = _manifest(evidence_dir, "ca", "stock-gplm.corporate-actions.v1", {"catalogs": [
            {"symbol": s, "coverage_status": "COMPLETE", "coverage_start": "2023-01-01", "coverage_end": "2024-12-31",
             "actions": (ca_actions or []) if s == symbols[0] else []} for s in symbols]})
    paths["instrument_master"] = write_master(evidence_dir / "NSE_symbols.txt.zip", symbols).name
    if provider_basis is not None:
        (evidence_dir / "price-basis.json").write_text(json.dumps(price_basis_registration(symbols, provider_basis)))
        paths["provider_price_basis"] = "price-basis.json"
    if events is not None:
        (evidence_dir / "events.json").write_text(json.dumps({
            "contract": "stock-gplm.m51-adjustment-events.v1", "schema_version": 1, "events": events}))
        paths["adjustment_events"] = "events.json"
    config = evidence_dir / "evidence-paths.json"
    config.write_text(json.dumps(paths))
    return raw_root, load_reference_evidence(config)


def strategy_config(**overrides: Any) -> dict[str, Any]:
    config = {
        "schema_version": 1,
        "strategy": {"id": "fixture-cross", "name": "Fixture crossover", "description": "Synthetic test strategy.",
                     "enabled": True, "timeframe": "1d"},
        "universe": {"exchange": "NSE", "instrument_type": "EQ", "symbols": []},
        "indicators": [{"id": "fast", "type": "SMA", "period": 5, "field": "close"},
                       {"id": "slow", "type": "SMA", "period": 12, "field": "close"}],
        "entry": {"operator": "CROSS_ABOVE", "left": {"indicator": "fast"}, "right": {"indicator": "slow"}},
        "exit": {"condition": {"operator": "CROSS_BELOW", "left": {"indicator": "fast"}, "right": {"indicator": "slow"}},
                 "max_holding_sessions": 15},
        "risk": {"stop_loss": {"type": "percent", "value": 6}, "take_profit": {"type": "percent", "value": 9}},
        "position_sizing": {"type": "fixed_fraction", "value": 0.3},
        "trading_costs": {"profile": "fixture", "commission_bps": 5, "slippage_bps": 10},
    }
    config.update(overrides)
    return config


def settings_dict(**overrides: Any) -> dict[str, Any]:
    value = {"contract": "stock-gplm.m5-backtest-settings.v1", "evaluation_start": "2024-02-01",
             "evaluation_end": "2024-09-30", "initial_capital": 1_000_000.0, "max_open_positions": 5,
             "max_position_fraction": 0.5, "max_gross_exposure": 1.0, "buy_statutory_bps": 12.41,
             "sell_statutory_bps": 10.91, "cost_basis": "synthetic test assumption"}
    value.update(overrides)
    return value


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@pytest.fixture
def waive_strict_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST-ONLY: waive the M5.1.1 strict authority gates for synthetic fixtures.

    These fixtures are synthetic by design, so the real policy keeps every strict
    request BLOCKED. Modules that exercise the strict run, replay, ranking and
    accounting machinery opt in explicitly. Only in-process patching can do this,
    which is out of scope for the seal threat model. ``tests/test_m511_authority.py``
    runs without the waiver and proves the gates hold.
    """
    import stock_gplm.m5.adapter as adapter
    import stock_gplm.m5.runner as runner
    monkeypatch.setattr(adapter, "strict_authority_failures", lambda evidence, request=None: [])
    monkeypatch.setattr(runner, "_strict_cost_profile", lambda datasets, mode, strategy, settings: None)
