from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_gplm.app import cli
from stock_gplm.app.advanced_discovery import discover_tree_strategies
from stock_gplm.app.compute import resolve
from stock_gplm.app.context import App
from stock_gplm.app.db import AuditDB
from stock_gplm.app.ingest import Ingestor
from stock_gplm.app.lanes import build_lane
from stock_gplm.app.m56 import build_manifest, compile_reference, parse_reference_catalogue, run_m56
from stock_gplm.app.settings import DEFAULT_CONFIG, REPO, load
from stock_gplm.m54.fixture import make_fixture


CATALOGUE = REPO / "config" / "strategies" / "reference-300-strategies.txt"


def test_reference_catalogue_accounts_for_exactly_six_by_fifty(tmp_path: Path) -> None:
    refs = parse_reference_catalogue(CATALOGUE)
    assert len(refs) >= 300  # 300 is a floor; the 301+ addendum is imported, not ignored
    assert [sum(r.category_number == c for r in refs) for c in range(1, 7)] == [50] * 6
    assert len({r.reference_id for r in refs}) == len(refs)
    assert refs[0].reference_id == "REF-C1-001"
    assert refs[299].reference_id == "REF-C6-050"
    manifest = build_manifest(CATALOGUE, tmp_path / "manifest.json")
    assert manifest["strategy_count"] == len(refs)
    assert manifest["reference_is_minimum_not_ceiling"] is True
    assert len(manifest["strategies"]) == len(refs)
    assert all(x["sha256"] and x["required_inputs"] and x["implementations"] for x in manifest["strategies"])


def test_catalogue_parser_refuses_an_omission(tmp_path: Path) -> None:
    text = CATALOGUE.read_text(encoding="utf-8")
    text = text.replace(next(x for x in text.splitlines() if x.startswith("50. **3-Bar Momentum Ignition")), "")
    bad = tmp_path / "bad.txt"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="must contain 300"):
        parse_reference_catalogue(bad)


def test_only_reviewed_declarative_translations_execute() -> None:
    refs = parse_reference_catalogue(CATALOGUE)
    golden = next(r for r in refs if r.name.startswith("Golden Cross"))
    doc, reason = compile_reference(golden, "1d")
    assert doc is not None and doc["contract"] == "stock-gplm.strategy.v3"
    assert doc["provenance"]["reference_id"] == "REF-C1-001"
    assert "reviewed" in reason
    ambiguous = next(r for r in refs if r.name.startswith("Darvas Box"))
    doc2, reason2 = compile_reference(ambiguous, "1d")
    assert doc2 is None and "not coerced" in reason2


def test_manifest_only_cli_registers_300_without_reading_candles(tmp_path: Path) -> None:
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cfg["data"] = {"daily_root": str(tmp_path / "does-not-exist-daily"),
                   "minute_roots": [str(tmp_path / "does-not-exist-minute")],
                   "real_raw_tree": str(tmp_path / "does-not-exist-raw")}
    cp = tmp_path / "config.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    home = tmp_path / "home"
    rc = cli.main(["m56", "--stocks", "RELIANCE", "--manifest-only", "--mode", "reference",
                   "--device", "cpu", "--catalogue", str(CATALOGUE), "--config", str(cp),
                   "--home", str(home), "--quiet"])
    assert rc == 0
    db = AuditDB(home / "gplm.sqlite", readonly=True)
    row = db.one("SELECT strategy_count, catalogue_sha256 FROM m56_catalogues")
    assert row and row["strategy_count"] >= 300 and len(row["catalogue_sha256"]) == 64
    assert db.one("SELECT COUNT(*) n FROM data_files")["n"] == 0
    assert db.verify_chain()["ok"]
    db.close()


def test_reference_run_attempts_every_strategy_and_persists_explicit_coverage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "raw"
    make_fixture(raw, ("AAA",), start="2022-01-01", end="2026-03-31", minute_from="2026-03-01",
                 cutoff="2026-03-02", seed=77)
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cfg["data"].update(daily_root=str(raw / "daily" / "NSE"), minute_roots=[str(raw / "minute" / "NSE")],
                       real_raw_tree=str(raw))
    cfg["m56"].update(min_oos_trades=5, walk_forward_folds=3)
    cp = tmp_path / "config.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    home = tmp_path / "home"
    assert cli.main(["m56", "--stocks", "AAA", "--timeframes", "1d", "--mode", "reference",
                     "--budget", "quick", "--device", "cpu", "--catalogue", str(CATALOGUE),
                     "--config", str(cp), "--home", str(home), "--quiet"]) == 0
    db = AuditDB(home / "gplm.sqlite", readonly=True)
    assert db.one("SELECT COUNT(*) n FROM m56_coverage")["n"] == len(parse_reference_catalogue(CATALOGUE))
    statuses = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM m56_coverage GROUP BY status")}
    assert statuses["EVALUATED_VALIDATION"] > 0
    assert statuses["BLOCKED_IMPLEMENTATION"] > 0
    assert statuses["BLOCKED_MISSING_DATA"] > 0
    assert db.one("SELECT COUNT(*) n FROM m56_results")["n"] > 0
    assert db.verify_chain()["ok"]
    db.close()
    s = load(cp, tmp_path / "advanced-home")
    s.ensure()
    adb = AuditDB(s.db_path, s.events_dir)
    data = Ingestor(s, adb).ingest("AAA")
    lane = build_lane(data, "1d", s.section("costs"), cache_dir=s.cache_dir / "features")
    assert lane is not None
    advanced = discover_tree_strategies(lane, {**cfg["m56"], "fdr_q": 1.0, "tree_max_candidates": 8})
    assert advanced["status"] == "COMPLETED"
    assert advanced["method"] == "gradient_boosting_plus_decision_tree"
    assert advanced["selected_features"]
    assert all(c["document"]["contract"] == "stock-gplm.strategy.v3" for c in advanced["candidates"])
    adb.close()
    import stock_gplm.app.discover as discovery_module

    monkeypatch.setattr(discovery_module, "discover", lambda *_a, **_k: {
        "symbols": ["AAA-EQ"], "results": [{"symbol": "AAA-EQ", "engine": {"run_id": "synthetic",
        "strategies": []}, "screens": []}], "device": "cpu", "device_stats": {}, "budget": "quick"})
    app = App(cp, tmp_path / "discovery-home", quiet=True)
    app.start("m56", "test", {}, device="cpu")
    try:
        integrated = run_m56(app, ["AAA-EQ"], ["1d"], resolve("cpu"), catalogue=CATALOGUE,
                             mode="discovery", budget="quick")
        app.finish("COMPLETED", {"test": True})
    finally:
        app.close()
    assert integrated["advanced_discovery"][0]["status"] == "COMPLETED"
    idb = AuditDB(tmp_path / "discovery-home" / "gplm.sqlite", readonly=True)
    assert idb.one("SELECT COUNT(*) n FROM m56_method_runs")["n"] >= 2
    idb.close()


def test_window_data_bounds_rows_and_changes_fingerprint() -> None:
    import pandas as pd

    from stock_gplm.app.ingest import SymbolData, window_data

    daily = pd.DataFrame({"date": ["2025-10-17", "2025-10-20", "2025-10-21", "2026-01-02"], "close": [1, 2, 3, 4]})
    minute = pd.DataFrame({"session": ["2025-10-20", "2026-01-02"], "close": [1, 2]})
    d = SymbolData("AAA-EQ", daily, minute, "fp")
    assert window_data(d) is d
    w = window_data(d, end="2025-10-20")
    assert list(w.daily["date"]) == ["2025-10-17", "2025-10-20"]
    assert list(w.minute["session"]) == ["2025-10-20"]
    assert w.fingerprint != d.fingerprint and w.summary["window"]["end"] == "2025-10-20"
    assert w.summary["minute_last"] == "2025-10-20" and w.summary["minute_sessions_used"] == 1


def _windowed_run(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch, distort: bool,
                  end: str | None = None) -> list[tuple]:
    import stock_gplm.app.m56 as m56mod

    raw = tmp_path / "raw"
    if not raw.exists():
        make_fixture(raw, ("AAA",), start="2021-01-01", end="2026-03-31", minute_from="2026-03-01",
                     cutoff="2026-03-02", seed=91)
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cfg["data"].update(daily_root=str(raw / "daily" / "NSE"), minute_roots=[str(raw / "minute" / "NSE")],
                       real_raw_tree=str(raw))
    cfg["m56"].update(min_oos_trades=1, walk_forward_folds=3, fdr_q=1.0, max_drawdown=1.0,
                      development_end="2025-06-30")
    cp = tmp_path / f"{name}.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    real = m56mod.ingest_many

    def fake(*a, **k):  # identical before the development end; wildly different after it
        out = real(*a, **k)
        if distort:
            for d in out.values():
                late = d.daily["date"].astype(str) > "2025-06-30"
                for col in ("open", "high", "low", "close"):
                    d.daily.loc[late, col] = d.daily.loc[late, col] * 7.0
        return out

    monkeypatch.setattr(m56mod, "ingest_many", fake)
    app = App(cp, tmp_path / name, quiet=True)
    app.start("m56", "test", {}, device="cpu")
    try:
        res = run_m56(app, ["AAA-EQ"], ["1d"], resolve("cpu"), catalogue=CATALOGUE, mode="reference", end=end)
        app.finish("COMPLETED", {})
    finally:
        app.close()
    assert res["window"]["end"] == "2025-06-30"
    db = AuditDB(tmp_path / name / "gplm.sqlite", readonly=True)
    rows = [(r["strategy_id"], r["regime"], r["evidence_stage"], r["classification"], r["metrics_json"])
            for r in db.q("SELECT * FROM m56_results ORDER BY strategy_id, regime, evidence_stage")]
    db.close()
    return rows


def test_rows_after_development_end_cannot_change_any_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clean = _windowed_run(tmp_path, "clean", monkeypatch, distort=False)
    distorted = _windowed_run(tmp_path, "distorted", monkeypatch, distort=True)
    assert clean and clean == distorted


def test_end_after_development_end_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="locked holdout stays closed"):
        _windowed_run(tmp_path, "late", monkeypatch, distort=False, end="2026-01-31")
