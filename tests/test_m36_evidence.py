"""Synthetic M3.6 tests. Every NSE-shaped file here is invented to exercise the adapter
contracts; nothing proves compatibility with real NSE downloads or says anything about
real holidays, corporate actions or listings."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_gplm.m36 import adapters
from stock_gplm.m36.bundle import build_bundle, load_bundle
from stock_gplm.m36.cli import main as evidence_main
from stock_gplm.m36.store import WEEKLY_RULE, load_store, register_source

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
URL = "https://www.nseindia.com/synthetic-test-page"
HOLIDAY = "2024-01-26"          # synthetic weekday closure
SPECIAL = "2024-01-20"          # synthetic Saturday session
SPLIT_DAY = "2024-02-15"
SYMBOLS = ("ALPHA-EQ", "BETA-EQ", "DELTA-EQ", "GAMMA-EQ")


def _sessions() -> list[str]:
    out, day = [], date(2024, 1, 1)
    while day <= date(2024, 3, 29):
        key = day.isoformat()
        if (day.weekday() < 5 and key != HOLIDAY) or key == SPECIAL:
            out.append(key)
        day += timedelta(days=1)
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _m31(root: Path, sessions: list[str] | None = None) -> Path:
    """A minimal manifest-bound M3.1 run with a flagged split on ALPHA-EQ."""
    sessions = sessions or _sessions()
    run = root / "m31"
    run.mkdir(parents=True)
    with (run / "research-dataset.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["symbol", "trading_date", "source_timestamp_utc", "open", "high", "low", "close",
                         "volume", "source_path", "source_sha256"])
        for n, symbol in enumerate(SYMBOLS):
            for i, day in enumerate(sessions):
                close = 100 + n + i * 0.1
                if symbol == "ALPHA-EQ" and day >= SPLIT_DAY:
                    close /= 5
                writer.writerow([symbol, day, f"{day}T10:00:00Z", f"{close:.2f}", f"{close + 1:.2f}",
                                 f"{close - 1:.2f}", f"{close:.2f}", 1000, f"{symbol}/seg.json.gz", "0" * 64])
    with (run / "observed-sessions.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["trading_date", "weekday", "symbols_observed"])
        writer.writerows([d, "x", len(SYMBOLS)] for d in sessions)
    with (run / "symbol-coverage.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["symbol", "observed_candles", "first_observed_date", "last_observed_date",
                         "non_accepted_segments", "large_close_moves"])
        writer.writerows([s, len(sessions), sessions[0], sessions[-1], 0, int(s == "ALPHA-EQ")] for s in SYMBOLS)
    (run / "ohlcv-flags.csv").write_text(f"symbol,trading_date,code,value\nALPHA-EQ,{SPLIT_DAY},LARGE_CLOSE_MOVE,-0.8\n")
    (run / "inventory.json").write_text(json.dumps({"raw_root": str(root / "raw"), "issues": []}))
    (run / "coverage-quality.json").write_text(json.dumps({"raw_candle_acceptance": "ACCEPT"}))
    outputs = {p.name: _sha(p) for p in sorted(run.iterdir())}
    (run / "run-manifest.json").write_text(json.dumps({
        "contract": "stock-gplm.m3-readiness-run.v1", "run_id": "synthetic-m31", "outputs": outputs,
        "raw_inputs_mutated": False}))
    return run


def _file(root: Path, name: str, text: str) -> Path:
    path = root / "downloads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _register(root: Path, component: str, name: str, text: str, scope: str, start: str = "2023-01-01",
              end: str = "2024-12-31", **extra) -> str:
    meta = {"scope": scope, "source_url": URL, "downloaded_at_utc": "2026-11-01T09:00:00Z",
            "applicable_start": start, "applicable_end": end, **extra}
    return register_source(_file(root, name, text), store_root=root / "store", component=component,
                           metadata=meta, now=NOW).sha256


CA_HEADER = ",".join(adapters.CORPORATE_ACTIONS_HEADER)
CA_ROWS = (
    f'ALPHA,Alpha Ltd,EQ,"Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",10,'
    f"15-Feb-2024,15-Feb-2024,-,-\n"
    "BETA,Beta Ltd,EQ,Annual General Meeting/Dividend - Rs 2.50 Per Share,1,10-Jan-2024,10-Jan-2024,-,-\n"
    "OLDDELTA,Delta Ltd,EQ,Bonus 1:1,10,05-Jan-2023,06-Jan-2023,-,-\n"
    "BETA,Beta Ltd,EQ,Rights 1:5 @ Premium Rs 100/-,1,20-Mar-2024,20-Mar-2024,-,-\n"
    "GONE,Gone Ltd,EQ,Bonus 1:2,10,01-Feb-2024,02-Feb-2024,-,-\n"
)


def _full_store(root: Path) -> None:
    """Register every component a bundle can validate (delisting history excepted)."""
    _register(root, "nse-weekly-schedule", "timings.html", "<html>synthetic</html>", "WEEKLY_SCHEDULE",
              declared_rule=WEEKLY_RULE)
    holidays = _register(root, "nse-calendar-source", "holidays.pdf", "%PDF synthetic holidays", "TRADING_HOLIDAYS")
    special = _register(root, "nse-calendar-source", "special.pdf", "%PDF synthetic special", "SPECIAL_SESSIONS",
                        end="2026-10-31")
    _register(root, "nse-calendar-transcription", "holidays.csv",
              f"date,status,label,source_sha256\n{HOLIDAY},MARKET_CLOSED,Synthetic holiday,{holidays}\n",
              "TRADING_HOLIDAYS", derived_from=[holidays])
    _register(root, "nse-calendar-transcription", "special.csv",
              f"date,status,label,source_sha256\n{SPECIAL},SPECIAL_SESSION,Synthetic Saturday session,{special}\n",
              "SPECIAL_SESSIONS", end="2026-10-31", derived_from=[special])
    _register(root, "nse-corporate-actions", "CF-CA.csv", CA_HEADER + "\n" + CA_ROWS, "ALL_EQUITY_SYMBOLS")
    _register(root, "nse-equity-list", "EQUITY_L.csv", ",".join(adapters.EQUITY_LIST_HEADER).replace(",", ", ") + "\n"
              "ALPHA,Alpha Ltd,EQ,01-Jan-2010,10,1,INE000A01010,2\n"
              "BETA,Beta Ltd,EQ,01-Jan-2011,10,1,INE000B01010,1\n"
              "DELTA,Delta Ltd,EQ,01-Jan-2012,10,1,INE000D01010,10\n", "ALL_LISTED_EQUITIES")
    _register(root, "nse-etf-list", "eq_etfseclist.csv", ",".join(adapters.ETF_LIST_HEADER) + "\n"
              "GAMMA,Nifty 50,Gamma ETF,01-Jan-2015,1,INF000G01010,10\n", "ALL_LISTED_ETFS")
    _register(root, "nse-symbol-changes", "symbolchange.csv", ",".join(adapters.SYMBOL_CHANGES_HEADER) + "\n"
              "OLDDELTA,Delta Ltd,01-Jun-2023,DELTA\n", "ALL_SYMBOL_CHANGES")


# --------------------------------------------------------------------------- store

def test_registration_copies_bytes_read_only_with_provenance(tmp_path):
    digest = _register(tmp_path, "nse-equity-list", "EQUITY_L.csv", "x\n", "ALL_LISTED_EQUITIES")
    records, errors = load_store(tmp_path / "store")
    assert errors == [] and [r.sha256 for r in records] == [digest]
    record = records[0]
    assert record.path.read_text() == "x\n"
    assert not record.path.stat().st_mode & stat.S_IWUSR
    assert record.acquisition["source_url"] == URL
    assert record.acquisition["acquisition_method"] == "MANUAL_BROWSER_DOWNLOAD"
    assert "nse-terms-of-use" in record.acquisition["access_terms"]["terms_of_use"]
    # Identical registration is idempotent; a conflicting one is refused, never overwritten.
    assert _register(tmp_path, "nse-equity-list", "EQUITY_L.csv", "x\n", "ALL_LISTED_EQUITIES") == digest
    with pytest.raises(FileExistsError):
        _register(tmp_path, "nse-equity-list", "EQUITY_L.csv", "x\n", "ALL_LISTED_EQUITIES", notes="changed")


@pytest.mark.parametrize("change, message", [
    ({"source_url": "https://example.com/EQUITY_L.csv"}, "official NSE host"),
    ({"source_url": "http://www.nseindia.com/x"}, "official NSE host"),
    ({"downloaded_at_utc": "2026-11-03T00:00:00Z"}, "future"),
    ({"downloaded_at_utc": "2026-11-01T09:00:00"}, "timezone"),
    ({"scope": "EVERYTHING"}, "scope"),
    ({"applicable_start": "2025-01-01", "applicable_end": "2024-01-01"}, "after"),
])
def test_registration_refuses_unverifiable_metadata(tmp_path, change, message):
    meta = {"scope": "ALL_LISTED_EQUITIES", "source_url": URL, "downloaded_at_utc": "2026-11-01T09:00:00Z",
            "applicable_start": "2024-01-01", "applicable_end": "2024-12-31", **change}
    with pytest.raises(ValueError, match=message):
        register_source(_file(tmp_path, "f.csv", "x"), store_root=tmp_path / "store",
                        component="nse-equity-list", metadata=meta, now=NOW)


def test_transcription_must_cite_registered_sources_and_store_stays_outside_raw(tmp_path):
    with pytest.raises(ValueError, match="not a registered nse-calendar-source"):
        _register(tmp_path, "nse-calendar-transcription", "t.csv", "x", "TRADING_HOLIDAYS", derived_from=["a" * 64])
    with pytest.raises(ValueError, match="raw tree"):
        register_source(_file(tmp_path, "f.csv", "x"), store_root=tmp_path / "raw" / "store",
                        component="nse-equity-list", protected_roots=(tmp_path / "raw",), now=NOW,
                        metadata={"scope": "ALL_LISTED_EQUITIES", "source_url": URL,
                                  "downloaded_at_utc": "2026-11-01T09:00:00Z",
                                  "applicable_start": "2024-01-01", "applicable_end": "2024-12-31"})


def test_tampered_store_bytes_are_reported_not_skipped(tmp_path):
    _register(tmp_path, "nse-equity-list", "EQUITY_L.csv", "x\n", "ALL_LISTED_EQUITIES")
    record = load_store(tmp_path / "store")[0][0]
    os.chmod(record.path.parent, 0o755)
    os.chmod(record.path, 0o644)
    record.path.write_text("tampered\n")
    records, errors = load_store(tmp_path / "store")
    assert records == [] and "do not match" in errors[0]


# --------------------------------------------------------------------------- adapters

@pytest.mark.parametrize("purpose, kind, status, factor", [
    ("Bonus 1:1", "BONUS", "UNADJUSTED", "1/2"),
    ("Bonus 3:2", "BONUS", "UNADJUSTED", "2/5"),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", "SPLIT", "UNADJUSTED", "1/10"),
    ("Consolidation of shares From Rs 1/- To Rs 10/-", "CONSOLIDATION", "UNADJUSTED", "10"),
    ("Bonus issue", "BONUS", "UNKNOWN", None),
    ("Face Value Split (Sub-Division)", "SPLIT", "UNKNOWN", None),
    ("Rights 1:5 @ Premium Rs 100/-", "RIGHTS", "POLICY_REQUIRED", None),
    ("Scheme of Arrangement", "SCHEME_OF_ARRANGEMENT", "POLICY_REQUIRED", None),
    ("Demerger", "DEMERGER", "POLICY_REQUIRED", None),
    ("Interim Dividend - Rs 5 Per Share", "DIVIDEND", "NOT_APPLICABLE", None),
    ("Something new", "UNRECOGNIZED", "UNKNOWN", None),
])
def test_purpose_parsing_uses_only_stated_ratios(purpose, kind, status, factor):
    actions = adapters.parse_purpose(purpose)
    assert actions[0].action_type == kind and actions[0].adjustment_status == status
    assert actions[0].details.get("price_adjustment_factor") == factor


def test_combined_purpose_keeps_every_component():
    kinds = {a.action_type for a in adapters.parse_purpose("Annual General Meeting/Dividend - Rs 2 Per Share")}
    assert kinds == {"MEETING", "DIVIDEND"}


def test_layout_mismatch_is_refused_with_the_observed_header():
    with pytest.raises(adapters.LayoutError) as error:
        adapters.parse_corporate_actions(b"SYMBOL,PURPOSE,EX DATE\nA,Bonus 1:1,01-Jan-2024\n")
    assert error.value.observed == ["SYMBOL", "PURPOSE", "EX DATE"]


def test_bad_rows_are_returned_as_rejections_not_dropped():
    raw = (CA_HEADER + "\nA,A Ltd,EQ,Bonus 1:1,10,2024-01-05,-,-,-\nB,B Ltd,EQ,Bonus 1:1,10,-,-,-,-\n"
           "C,C Ltd,EQ,Bonus 1:1,10,05-Jan-2024,-,-,-\n").encode()
    parsed = adapters.parse_corporate_actions(raw)
    assert [r["symbol"] for r in parsed.records] == ["C"]
    assert [r["line"] for r in parsed.rejected] == [2, 3]


# --------------------------------------------------------------------------- bundle

def test_empty_store_reports_every_gap_with_counts_and_writes_no_component(tmp_path):
    m31 = _m31(tmp_path)
    bundle = build_bundle(store_root=tmp_path / "store", readiness_run=m31, output_root=tmp_path / "out")
    manifest = json.loads((bundle / "bundle-manifest.json").read_text())
    assert set(manifest["components"].values()) == {"BLOCKED"}
    assert manifest["network_used"] is False
    assert not {"nse-calendar.csv", "corporate-actions.json", "inventory.json", "instrument-classes.csv"} & set(
        manifest["outputs"])
    report = json.loads((bundle / "coverage-report.json").read_text())
    codes = {g["code"]: g["count"] for c in report["components"].values() for g in c["gaps"]}
    span = (date(2024, 3, 29) - date(2024, 1, 1)).days + 1
    assert codes["WEEKLY_SCHEDULE_SOURCE_MISSING"] == span
    assert codes["TRADING_HOLIDAYS_COVERAGE_MISSING"] == span
    assert codes["CORPORATE_ACTION_COVERAGE_MISSING"] == len(SYMBOLS)
    assert codes["SYMBOL_CHANGE_SOURCE_MISSING"] == len(SYMBOLS)
    assert codes["DELISTING_HISTORY_SOURCE_MISSING"] == len(SYMBOLS)
    assert "corporate-filings-actions" in (bundle / "evidence-gaps.md").read_text()
    # Same evidence and M3.1 run give the same bundle identity; a rebuild is refused, not overwritten.
    with pytest.raises(FileExistsError):
        build_bundle(store_root=tmp_path / "store", readiness_run=m31, output_root=tmp_path / "out")
    with pytest.raises(ValueError, match="outside"):
        build_bundle(store_root=tmp_path / "store", readiness_run=m31, output_root=tmp_path / "store" / "x")


def test_full_evidence_validates_every_component_but_membership(tmp_path):
    m31 = _m31(tmp_path)
    _full_store(tmp_path)
    bundle = build_bundle(store_root=tmp_path / "store", readiness_run=m31, output_root=tmp_path / "out")
    manifest = json.loads((bundle / "bundle-manifest.json").read_text())
    assert manifest["components"] == {
        "calendar": "VALIDATED", "corporate_actions": "VALIDATED", "instrument_classification": "VALIDATED",
        "alias_history": "VALIDATED", "point_in_time_membership": "BLOCKED"}
    report = json.loads((bundle / "coverage-report.json").read_text())["components"]
    assert [g["code"] for g in report["point_in_time_membership"]["gaps"]] == ["DELISTING_HISTORY_SOURCE_MISSING"]
    with (bundle / "nse-calendar.csv").open() as stream:
        days = {r["date"]: r["status"] for r in csv.DictReader(stream)}
    assert days[HOLIDAY] == "MARKET_CLOSED" and days[SPECIAL] == "SPECIAL_SESSION"
    assert days["2024-01-21"] == "MARKET_CLOSED" and days["2024-01-22"] == "TRADING_SESSION"
    catalogs = {c["symbol"]: c for c in json.loads((bundle / "corporate-actions.json").read_text())["catalogs"]}
    split = catalogs["ALPHA-EQ"]["actions"][0]
    assert split["action_type"] == "SPLIT" and split["details"]["price_adjustment_factor"] == "1/5"
    # An action filed under the pre-change symbol lands on today's symbol and records the original.
    assert catalogs["DELTA-EQ"]["actions"][0]["details"]["reported_symbol"] == "OLDDELTA"
    assert catalogs["GAMMA-EQ"]["actions"] == [] and catalogs["GAMMA-EQ"]["coverage_status"] == "COMPLETE"
    actions = report["corporate_actions"]["report"]
    assert actions["large_close_moves_explained"] == 1 and actions["actions_outside_universe"] == 1
    assert "GONE" in (bundle / "corporate-actions-outside-universe.csv").read_text()
    assert "RIGHTS" in (bundle / "corporate-actions-unresolved.csv").read_text()
    classes = (bundle / "instrument-classes.csv").read_text()
    assert "GAMMA-EQ,ETF" in classes and "ALPHA-EQ,EQUITY" in classes
    inventory = json.loads((bundle / "inventory.json").read_text())
    delta = next(i for i in inventory["instruments"] if i["nse_symbol"] == "DELTA-EQ")
    assert delta["aliases"][0]["symbol"] == "OLDDELTA-EQ" and delta["aliases"][0]["valid_to"] == "2023-05-31"
    assert delta["membership_verified_through"] is None and delta["listing_date"] == "2012-01-01"


def test_calendar_is_checked_against_candles_never_derived_from_them(tmp_path):
    sessions = _sessions() + []
    sessions.insert(sessions.index("2024-01-25") + 1, HOLIDAY)
    m31 = _m31(tmp_path, sorted(sessions))
    _full_store(tmp_path)
    bundle = build_bundle(store_root=tmp_path / "store", readiness_run=m31, output_root=tmp_path / "out")
    calendar = json.loads((bundle / "coverage-report.json").read_text())["components"]["calendar"]
    assert calendar["status"] == "BLOCKED"
    assert calendar["gaps"][0]["code"] == "CANDLES_ON_CLOSED_DAY" and calendar["gaps"][0]["dates"] == [HOLIDAY]
    assert "nse-calendar.csv" not in json.loads((bundle / "bundle-manifest.json").read_text())["outputs"]


def test_rejected_rows_block_the_component_and_are_recorded(tmp_path):
    m31 = _m31(tmp_path)
    _register(tmp_path, "nse-corporate-actions", "CF-CA.csv", CA_HEADER + "\nA,A Ltd,EQ,Bonus 1:1,10,bad,-,-,-\n",
              "ALL_EQUITY_SYMBOLS")
    bundle = build_bundle(store_root=tmp_path / "store", readiness_run=m31, output_root=tmp_path / "out")
    gaps = json.loads((bundle / "coverage-report.json").read_text())["components"]["corporate_actions"]["gaps"]
    assert [g["code"] for g in gaps] == ["SOURCE_ROWS_REJECTED", "CORPORATE_ACTION_COVERAGE_MISSING"]
    assert "not DD-Mon-YYYY" in (bundle / "rejected-rows.jsonl").read_text()


def test_special_sessions_cannot_be_vouched_for_beyond_the_transcription(tmp_path):
    m31 = _m31(tmp_path)
    source = _register(tmp_path, "nse-calendar-source", "s.pdf", "%PDF", "SPECIAL_SESSIONS", end="2027-12-31")
    _register(tmp_path, "nse-calendar-transcription", "s.csv", "date,status,label,source_sha256\n",
              "SPECIAL_SESSIONS", end="2027-12-31", derived_from=[source])
    bundle = build_bundle(store_root=tmp_path / "store", readiness_run=m31, output_root=tmp_path / "out")
    codes = [g["code"] for g in json.loads((bundle / "coverage-report.json").read_text())
             ["components"]["calendar"]["gaps"]]
    assert "SPECIAL_SESSION_COVERAGE_CLAIMED_IN_FUTURE" in codes and "SPECIAL_SESSIONS_COVERAGE_MISSING" in codes


def test_bundle_load_verifies_binding_and_hashes(tmp_path):
    m31 = _m31(tmp_path)
    _full_store(tmp_path)
    bundle = build_bundle(store_root=tmp_path / "store", readiness_run=m31, output_root=tmp_path / "out")
    loaded = load_bundle(bundle, "synthetic-m31")
    assert loaded.calendar and loaded.corporate_actions and loaded.instrument_classes["GAMMA-EQ"] == "ETF"
    with pytest.raises(ValueError, match="different M3.1 run"):
        load_bundle(bundle, "another-run")
    (bundle / "instrument-classes.csv").write_text("symbol,instrument_class\nGAMMA-EQ,EQUITY\n")
    with pytest.raises(ValueError, match="does not match its manifest"):
        load_bundle(bundle, "synthetic-m31")


def test_m32_consumes_only_validated_components_and_keeps_membership_blocked(tmp_path, monkeypatch, capsys):
    from stock_gplm.m3 import research_cli

    m31 = _m31(tmp_path)
    _full_store(tmp_path)
    assert evidence_main(["--store", str(tmp_path / "store"), "build", "--readiness-run", str(m31),
                          "--output-root", str(tmp_path / "bundles")]) == 1
    bundle = json.loads(capsys.readouterr().out)["bundle"]
    monkeypatch.setattr("sys.argv", ["research", "--readiness-run", str(m31), "--output-root",
                                     str(tmp_path / "m32"), "--reference-bundle", bundle])
    assert research_cli.main() == 0
    run = next((tmp_path / "m32").iterdir())
    gates = json.loads((run / "quality-gates.json").read_text())["gates"]
    assert {k: v["status"] for k, v in gates.items()} == {
        "raw_candle_acceptance": "PASS", "authoritative_calendar": "PASS", "point_in_time_membership": "BLOCKED",
        "alias_history": "PASS", "corporate_action_coverage": "PASS", "instrument_classification": "PASS",
        "minute_data": "BLOCKED"}
    axis = (run / "session-axis.csv").read_text().splitlines()[1:]
    assert axis == _sessions() and SPECIAL in axis and HOLIDAY not in axis
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["evidence"]["m36_bundle"] == Path(bundle).name


def test_cli_register_and_list_round_trip(tmp_path, capsys):
    path = _file(tmp_path, "EQUITY_L.csv", "x\n")
    store = str(tmp_path / "store")
    assert evidence_main(["--store", store, "register", str(path), "--component", "nse-equity-list",
                          "--scope", "ALL_LISTED_EQUITIES", "--source-url", URL,
                          "--downloaded-at", "2026-11-01T09:00:00+05:30", "--applicable-start", "2026-11-01",
                          "--applicable-end", "2026-11-01"]) == 0
    assert json.loads(capsys.readouterr().out)["downloaded_at_utc"] == "2026-11-01T03:30:00Z"
    assert evidence_main(["--store", store, "list"]) == 0
    assert len(json.loads(capsys.readouterr().out)["sources"]) == 1
    assert evidence_main(["--store", store, "register", str(path), "--component", "nse-equity-list",
                          "--scope", "ALL_LISTED_EQUITIES", "--source-url", "https://example.org/x",
                          "--downloaded-at", "2026-11-01T09:00:00Z", "--applicable-start", "2026-11-01",
                          "--applicable-end", "2026-11-01"]) == 3
