"""Synthetic, reproducible tests for the GPLM research application (stock_gplm.app).

All data here is SYNTHETIC (stock_gplm.m54.fixture writes exact ShoonyaDailyV1 / ShoonyaMinuteV1
triplets). Real-data acceptance is recorded separately in docs/m55-acceptance.md.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from stock_gplm.app import cli
from stock_gplm.app.compute import (DeviceUnavailable, forward_matrix, resolve, score_masks, threshold_masks,
                                    torch_status, train_network)
from stock_gplm.app.db import AuditDB, LockBusy, ProcessLock
from stock_gplm.app.ingest import Ingestor
from stock_gplm.app.lanes import assert_causal, build_lane
from stock_gplm.app.library import Library, StrategyError, validate
from stock_gplm.app.minute_adapter import adapt_file, regime_policy
from stock_gplm.app.rolling import LabelGuard, LaneReplay, LookaheadError
from stock_gplm.app.settings import DEFAULT_CONFIG, load
from stock_gplm.m54.candles import IST
from stock_gplm.m54.fixture import make_fixture

TORCH_GPU = bool(torch_status().get("usable"))


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict:
    base = tmp_path_factory.mktemp("gplm")
    raw = base / "raw"
    make_fixture(raw, ("AAA", "BBB"), start="2025-01-01", end="2026-03-31", minute_from="2025-12-01",
                 cutoff="2026-03-02", seed=11)
    # Drop the last daily partition so the final month exists only as minute data (minute-derived days).
    for sym in ("AAA-EQ", "BBB-EQ"):
        for p in (raw / "daily" / "NSE" / sym).glob("2026-03-02_*"):
            p.unlink()
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cfg["home"] = str(base / "home")
    cfg["data"].update(daily_root=str(raw / "daily" / "NSE"), minute_roots=[str(raw / "minute" / "NSE")],
                       real_raw_tree=str(raw))
    cfg["rolling"].update(start="2026-03-09", min_train_rows=60, mlp_epochs_full=20, mlp_epochs_update=5,
                          rediscover_every=5, calibration_min_outcomes=10, champion_min_outcomes=8, champion_window=30)
    cfg["models"]["logit"]["epochs"] = 60
    cfg["horizons"].update({"1d": [1, 5, 10], "15m": [4]})
    cfg["timeframes"]["intraday_min_sessions"] = 40
    cfg["prediction"]["min_rolling_outcomes"] = 5
    cfg["discovery"]["engine_config"] = str(DEFAULT_CONFIG.parent.parent / "m54" / "unified-default.json")
    cfg_path = base / "gplm-test.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return {"base": base, "raw": raw, "config": cfg_path, "raw_hash": _tree_hash(raw)}


def _settings(world: dict, home: Path | None = None):
    s = load(world["config"], home)
    s.ensure()
    return s


# ----------------------------------------------------------------------------- ingestion
def test_ingest_incremental_and_minute_derived(world: dict, tmp_path: Path) -> None:
    s = _settings(world, tmp_path / "h")
    db = AuditDB(s.db_path, s.events_dir)
    db.start_run("t", "t", {}, device=None, code_sha="x", config_sha="y")
    ing = Ingestor(s, db)
    d = ing.ingest("AAA")
    assert d.changed_files > 0
    assert d.summary["minute_derived_rows"] > 10
    assert set(d.daily["source"]) == {"DAILY", "MINUTE_DERIVED"}
    assert d.daily["date"].is_monotonic_increasing and d.daily["date"].is_unique
    rec = d.summary["reconciliation"]
    assert rec["shared_sessions"] > 40 and rec["close_median_abs_rel_diff"] < 1e-3
    again = ing.ingest("AAA")
    assert again.changed_files == 0 and again.fingerprint == d.fingerprint
    assert _tree_hash(world["raw"]) == world["raw_hash"], "raw files must never change"
    assert db.verify_chain()["ok"]


def _write_minute(dirpath: Path, symbol: str, session: str, reset_at: str | None, negative_at: str | None) -> Path:
    d = datetime.fromisoformat(f"{session}T09:14").replace(tzinfo=IST)
    recs, cum = [], 0
    for j in range(376):
        ts = d + timedelta(minutes=j)
        clock = ts.strftime("%H:%M")
        vol = 1000 + j
        if reset_at and clock == reset_at:
            vol = -cum + 50  # counter restarts
        if negative_at and clock == negative_at:
            vol = -500
        cum += vol
        px = 100 + j * 0.01
        recs.append({"stat": "Ok", "time": ts.strftime("%d-%m-%Y %H:%M:%S"), "ssboe": str(int(ts.timestamp())),
                     "into": f"{px:.2f}", "inth": f"{px + 0.05:.2f}", "intl": f"{px - 0.05:.2f}", "intc": f"{px:.2f}",
                     "intvwap": f"{px:.2f}", "intv": str(vol), "intoi": "0", "v": str(max(cum, 0)), "oi": "0"})
    for i in range(1, len(recs)):  # keep intv == diff(v) exactly, as the provider does
        recs[i]["intv"] = str(int(recs[i]["v"]) - int(recs[i - 1]["v"]))
    recs.reverse()
    payload = gzip.compress(json.dumps(recs).encode(), mtime=0)
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / f"{session}.json.gz"
    p.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (dirpath / f"{session}.meta.json").write_text(json.dumps({
        "source": "shoonya", "symbol": symbol, "token": "1", "session_date": session, "session_type": "REGULAR",
        "quality_status": "UNVALIDATED_RAW", "expected_start": "09:15", "expected_end": "15:30",
        "sha256_compressed": digest, "records": len(recs), "fetched_at_utc": "2000-01-01T00:00:00+00:00"}))
    Path(f"{p}.done").write_text(json.dumps({"status": "COMPLETE", "file": p.name, "bytes": len(payload),
                                             "records": len(recs), "sha256": digest, "quality_status": "UNVALIDATED_RAW",
                                             "published_at_utc": "2000-01-01T00:00:00+00:00"}))
    return p


def test_closing_phase_counter_reset_accepted_and_volume_withheld(tmp_path: Path) -> None:
    p = _write_minute(tmp_path / "X-EQ", "X-EQ", "2026-09-01", reset_at="15:20", negative_at=None)
    r = adapt_file(p, "X-EQ", "2026-09-01", regime_policy())
    assert r.decision == "ACCEPT_CLOSING_PHASE_V2"
    assert r.regular_bars == 375 and len(r.volume) == 375
    clocks = [datetime.fromtimestamp(int(t) // 10**9, IST).strftime("%H:%M")
              for t in r.start_ns]
    withheld = [c for c, v in zip(clocks, r.volume) if np.isnan(v)]
    assert withheld and min(withheld) == "15:20" and all(c >= "15:20" for c in withheld)
    assert not np.isnan(r.ohlc).any(), "prices are always kept"


def test_negative_volume_before_closing_phase_is_quarantined(tmp_path: Path) -> None:
    p = _write_minute(tmp_path / "X-EQ", "X-EQ", "2026-09-02", reset_at=None, negative_at="11:00")
    r = adapt_file(p, "X-EQ", "2026-09-02", regime_policy())
    assert r.decision == "QUARANTINE"
    assert "NEGATIVE_INTERVAL_VOLUME_BEFORE_CLOSING_PHASE" in r.issues


# ----------------------------------------------------------------------------- lanes / causality
def test_features_are_causal_daily_and_intraday(world: dict, tmp_path: Path) -> None:
    s = _settings(world, tmp_path / "h")
    db = AuditDB(s.db_path, s.events_dir)
    d = Ingestor(s, db).ingest("AAA")
    for tf in ("1d", "15m"):
        res = assert_causal(d, tf, s.section("costs"), "2026-02-20")
        assert res["identical"], res


# ----------------------------------------------------------------------------- compute
def test_score_masks_matches_naive_loop() -> None:
    rng = np.random.default_rng(1)
    T, C = 400, 30
    R = rng.standard_normal((T, 3)) * 0.01
    V = rng.random((T, 3)) > 0.1
    M = rng.random((C, T)) < 0.2
    sc = score_masks(resolve("cpu"), M, R, V)
    for c in (0, 7, 29):
        for j in range(3):
            sel = M[c] & V[:, j]
            assert sc["n"][c, j] == sel.sum()
            assert np.isclose(sc["mean"][c, j], R[sel, j].mean())
            assert np.isclose(sc["std"][c, j], R[sel, j].std(ddof=1))
            assert np.isclose(sc["win"][c, j], (R[sel, j] > 0).mean())


def test_training_reduces_loss_and_learns_signal() -> None:
    rng = np.random.default_rng(2)
    X = rng.standard_normal((2000, 10))
    y = (X[:, 0] - 0.5 * X[:, 1] + 0.3 * rng.standard_normal(2000) > 0).astype(float)
    hist: list[tuple[int, float, float]] = []
    for hidden in ((), (16, 8)):
        hist.clear()
        train_network(resolve("cpu"), X, y, np.ones(2000), hidden, 150, 0.03, 1e-3, 5,
                      on_epoch=lambda e, l, a: hist.append((e, l, a)), record_every=10)
        assert hist[-1][1] < hist[0][1] and hist[-1][2] > 0.85


def test_device_gpu_refused_without_cuda() -> None:
    if TORCH_GPU:
        pytest.skip("CUDA is usable here")
    with pytest.raises(DeviceUnavailable):
        resolve("gpu")
    assert resolve("auto").name == "cpu"


@pytest.mark.skipif(not TORCH_GPU, reason="needs a CUDA build of PyTorch and a GPU")
def test_gpu_matches_cpu() -> None:  # pragma: no cover - runs on the owner's GPU machine
    rng = np.random.default_rng(3)
    X = rng.standard_normal((3000, 20))
    R = rng.standard_normal((3000, 2)) * 0.01
    V = rng.random((3000, 2)) > 0.05
    f = np.arange(20)
    thr = np.zeros(20)
    gt = np.ones(20, dtype=bool)
    cpu, gpu = resolve("cpu"), resolve("gpu")
    a = score_masks(cpu, threshold_masks(cpu, X, f, thr, gt), R, V)
    b = score_masks(gpu, threshold_masks(gpu, X, f, thr, gt), R, V)
    assert np.allclose(a["n"], b["n"]) and np.allclose(a["mean"], b["mean"], atol=1e-6)


# ----------------------------------------------------------------------------- rule search
def test_rule_search_finds_a_planted_signal(world: dict, tmp_path: Path) -> None:
    from stock_gplm.app.candidates import search

    s = _settings(world, tmp_path / "h")
    db = AuditDB(s.db_path, s.events_dir)
    d = Ingestor(s, db).ingest("AAA")
    lane = build_lane(d, "1d", s.section("costs"))
    m = lane.market
    R, V = forward_matrix(m.open, m.close, [5], m.session_id, False, 0.0)
    planted = np.where(V[:, 0], np.where(R[:, 0] > 0.01, 1.0, 0.0), np.nan)  # deliberately "knows" the future
    lane.names.append("PLANT().signal")
    lane.X = np.column_stack([lane.X, planted])
    cfg = {**s.section("candidates"), "min_trades": 5}
    res = search(resolve("cpu"), lane, len(m) - 1, [5], cfg)
    assert res["tests"] > 100
    assert any("PLANT().signal" in r["text"] and r["tier"] == "FDR_SELECTED" for r in res["rules"])


# ----------------------------------------------------------------------------- rolling integrity
def test_label_guard_refuses_lookahead(world: dict, tmp_path: Path) -> None:
    s = _settings(world, tmp_path / "h")
    db = AuditDB(s.db_path, s.events_dir)
    lane = build_lane(Ingestor(s, db).ingest("AAA"), "1d", s.section("costs"))
    g = LabelGuard(lane)
    g.labels(np.array([10, 20]), 5, 25)
    with pytest.raises(LookaheadError):
        g.labels(np.array([10, 21]), 5, 25)


def _replay(world: dict, home: Path, sym: str = "AAA", max_steps: int | None = None, tf: str = "1d") -> tuple:
    s = _settings(world, home)
    db = AuditDB(s.db_path, s.events_dir)
    db.start_run("replay", "t", {}, device="cpu", code_sha="x", config_sha="y")
    from stock_gplm.app.lanes import all_bars
    from stock_gplm.app.progress import Progress

    d = Ingestor(s, db).ingest(sym)
    bars = all_bars(d)
    lane = build_lane(d, tf, s.section("costs"), bars=bars)
    rp = LaneReplay(db, lane, s.section("horizons")[tf], dict(s.raw), resolve("cpu"), Progress(None, quiet=True),
                    s.models_dir, Library(s.strategies_dir, db), bars, max_steps=max_steps)
    return rp.run(), db, lane


def test_replay_predictions_precede_outcomes_and_resume_is_exact(world: dict, tmp_path: Path) -> None:
    full, db_full, lane = _replay(world, tmp_path / "full")
    assert full["steps_done"] == full["steps_total"] > 10
    rows = db_full.q("SELECT p.decision_ns, p.horizon_bars, e.outcome_ns FROM predictions p JOIN evaluations e "
                     "ON e.prediction_id=p.prediction_id WHERE e.outcome_ns IS NOT NULL")
    assert rows and all(r["outcome_ns"] > r["decision_ns"] for r in rows)
    live = db_full.q("SELECT * FROM predictions WHERE mode='LIVE'")
    assert live and all(r["decision_ns"] == int(lane.market.end_ns[-1]) for r in live)
    assert not db_full.q("SELECT 1 FROM predictions p JOIN evaluations e ON e.prediction_id=p.prediction_id WHERE p.mode='LIVE'")
    # interrupted run (two chunks) gives the identical predictions
    _replay(world, tmp_path / "chunked", max_steps=4)
    second, db_c, _ = _replay(world, tmp_path / "chunked")
    a = {(r["natural_key"].split("|", 1)[1], round(r["prob_raw"] or -1, 9))
         for r in db_full.q("SELECT natural_key, prob_raw FROM predictions")}
    b = {(r["natural_key"].split("|", 1)[1], round(r["prob_raw"] or -1, 9))
         for r in db_c.q("SELECT natural_key, prob_raw FROM predictions")}
    assert a == b
    # idempotent: nothing new on a repeat
    again, db_r, _ = _replay(world, tmp_path / "full")
    assert again["predictions_new"] == 0 and again["evaluations_new"] == 0 and again["fits"] == 0


def test_intraday_replay_runs_on_minute_lane(world: dict, tmp_path: Path) -> None:
    out, db, lane = _replay(world, tmp_path / "intra", tf="15m")
    assert lane.intraday and out["steps_done"] > 5
    n = db.one("SELECT COUNT(*) n FROM predictions WHERE timeframe='15m'")["n"]
    assert n > 100
    bad = db.q("SELECT p.decision_label, e.outcome_label FROM predictions p JOIN evaluations e ON "
               "e.prediction_id=p.prediction_id WHERE p.source_kind='MODEL' AND substr(p.decision_label,1,10) != "
               "substr(e.outcome_label,1,10)")
    assert not bad, "intraday model outcomes must finish inside their session"


# ----------------------------------------------------------------------------- audit store
def test_predictions_are_append_only_and_chain_detects_tampering(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev")
    db.start_run("t", "t", {}, device=None, code_sha="x", config_sha="y")
    assert db.add_prediction({"natural_key": "k1", "mode": "REPLAY", "symbol": "A", "timeframe": "1d", "lane_key": "A|1d",
                              "source_kind": "MODEL", "source_id": "logit", "horizon_bars": 1, "decision_label": "d",
                              "decision_ns": 1, "direction": "LONG", "actionable": 0})
    assert not db.add_prediction({"natural_key": "k1", "mode": "REPLAY", "symbol": "A", "timeframe": "1d",
                                  "lane_key": "A|1d", "source_kind": "MODEL", "source_id": "logit", "horizon_bars": 1,
                                  "decision_label": "d", "decision_ns": 1, "direction": "LONG", "actionable": 0})
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE predictions SET direction='SHORT'")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("DELETE FROM events")
    db.event("X", {"a": 1})
    assert db.verify_chain()["ok"]
    db.conn.execute("DROP TRIGGER events_no_update")
    db.conn.execute("UPDATE events SET payload_json='{\"a\":2}' WHERE kind='X'")
    db.conn.commit()
    assert not db.verify_chain()["ok"]


def test_process_lock_blocks_second_writer(tmp_path: Path) -> None:
    a = ProcessLock(tmp_path / "l").acquire("one")
    with pytest.raises(LockBusy):
        ProcessLock(tmp_path / "l").acquire("two")
    a.release()
    ProcessLock(tmp_path / "l").acquire("three").release()


# ----------------------------------------------------------------------------- library
def test_library_validation_versions_and_integrity(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev")
    lib = Library(tmp_path / "lib", db)
    doc = {"name": "t", "timeframe": "1d", "entry": {"logic": "AND", "conditions": [
        {"feature": "RSI(period=14).value", "op": "lt", "threshold": 30}]}, "exit": {"max_hold_bars": 5, "stop_atr": 2}}
    for bad in ({**doc, "code": "import os"}, {**doc, "entry": {"conditions": [{"feature": "__import__('os')", "op": "lt",
                                                                                 "threshold": 1}]}},
                {**doc, "entry": {"conditions": [{"feature": "RSI(period=14).value", "op": "exec", "threshold": 1}]}},
                {**doc, "exit": {"max_hold_bars": 0}}):
        with pytest.raises(StrategyError):
            validate(bad)
    r1 = lib.add(doc, immutable=True, origin="test", active=True)
    assert lib.add(doc, immutable=True, origin="test", active=True)["created"] is False
    r2 = lib.copy_for_edit(r1["strategy_id"], {"exit": {"max_hold_bars": 7, "stop_atr": 2}})
    assert r2["version"] == 2 and Path(r1["path"]).is_file() and Path(r2["path"]).is_file()
    assert lib.get(r1["strategy_id"], 1)["exit"]["max_hold_bars"] == 5
    assert lib.get(r1["strategy_id"])["exit"]["max_hold_bars"] == 7
    p = Path(r2["path"])
    p.write_text(p.read_text().replace('"max_hold_bars": 7', '"max_hold_bars": 9'))
    with pytest.raises(StrategyError):
        lib.get(r1["strategy_id"], 2)


# ----------------------------------------------------------------------------- AI governance
def test_ai_disabled_remote_refused_and_mock_validated(tmp_path: Path) -> None:
    from stock_gplm.app.ai import AIRefused, generate, mock_transport

    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev")
    db.start_run("ai", "t", {}, device=None, code_sha="x", config_sha="y")
    lib = Library(tmp_path / "lib", db)
    cat = [{"feature": "RSI(period=14).value", "family": "momentum", "kind": "bounded"}]
    cfg = json.loads(DEFAULT_CONFIG.read_text())["ai"]
    with pytest.raises(AIRefused):
        generate(db, lib, cfg, "ollama", "AAA-EQ", "1d", cat, allow_remote=False)
    cfg["enabled"] = True
    with pytest.raises(AIRefused):
        generate(db, lib, cfg, "anthropic", "AAA-EQ", "1d", cat, allow_remote=True, environ={"ANTHROPIC_API_KEY": "sk-x"})
    reply = {"message": {"content": json.dumps({"strategies": [
        {"name": "ok", "timeframe": "1d", "entry": {"logic": "AND", "conditions": [
            {"feature": "RSI(period=14).value", "op": "lt", "threshold": 25}]}, "exit": {"max_hold_bars": 5}},
        {"name": "bad", "timeframe": "1d", "entry": {"logic": "AND", "conditions": [
            {"feature": "os.system('x')", "op": "lt", "threshold": 1}]}, "exit": {"max_hold_bars": 5}}]})},
        "prompt_eval_count": 120, "eval_count": 80}
    res = generate(db, lib, cfg, "ollama", "AAA-EQ", "1d", cat, allow_remote=False, transport=mock_transport(reply))
    assert len(res["accepted"]) == 1 and len(res["rejected"]) == 1
    st = db.one("SELECT active, origin FROM strategies")
    assert st["active"] == 0 and st["origin"] == "ai:ollama"
    call = db.one("SELECT * FROM api_calls")
    assert call["input_tokens"] == 120 and call["cost_usd"] == 0.0 and call["success"] == 1
    assert "sk-" not in json.dumps(db.q("SELECT payload_json FROM events"))


# ----------------------------------------------------------------------------- CLI, daily, dashboard
def test_daily_cli_twice_is_idempotent_and_dashboard_serves(world: dict, tmp_path: Path) -> None:
    home = tmp_path / "home"
    args = ["--config", str(world["config"]), "--home", str(home), "--quiet"]
    assert cli.main(["daily", "--stocks", "AAA,BBB", "--device", "cpu", *args]) == 0
    db = AuditDB(home / "gplm.sqlite", readonly=True)
    n1 = db.one("SELECT COUNT(*) n FROM predictions")["n"]
    e1 = db.one("SELECT COUNT(*) n FROM evaluations")["n"]
    f1 = db.one("SELECT COUNT(*) n FROM training_history")["n"]
    live = db.q("SELECT DISTINCT symbol FROM predictions WHERE mode='LIVE'")
    assert n1 > 0 and e1 > 0 and len(live) == 2
    assert db.one("SELECT COUNT(*) n FROM api_calls")["n"] == 0, "API-disabled run makes no external calls"
    db.close()
    assert cli.main(["daily", "--stocks", "AAA,BBB", "--device", "cpu", *args]) == 0
    db = AuditDB(home / "gplm.sqlite", readonly=True)
    assert db.one("SELECT COUNT(*) n FROM predictions")["n"] == n1
    assert db.one("SELECT COUNT(*) n FROM evaluations")["n"] == e1
    assert db.one("SELECT COUNT(*) n FROM training_history")["n"] == f1
    assert db.verify_chain()["ok"]
    from stock_gplm.app.report import trace_prediction

    pid = db.one("SELECT p.prediction_id FROM predictions p JOIN evaluations e ON e.prediction_id=p.prediction_id "
                 "WHERE p.source_kind='MODEL' LIMIT 1")["prediction_id"]
    tr = trace_prediction(db, pid)
    assert tr["model_artifact"]["artifact_sha256_ok"] is True
    assert tr["input_data"]["source_files_up_to_decision"] and tr["evaluation"]["outcome_ns"] > tr["prediction"]["decision_ns"]
    assert tr["prediction_run"]["status"] == "COMPLETED" and tr["config_snapshot"]
    db.close()
    assert (home / "reports" / "latest" / "report.html").is_file()
    # dashboard on an ephemeral port
    from http.server import ThreadingHTTPServer

    from stock_gplm.app.dashboard import Handler

    s = load(world["config"], home)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"settings": s}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        for path in ("/", "/api/summary", "/api/stock?symbol=AAA-EQ", f"/api/trace?id={pid}", "/api/strategies",
                     "/export/predictions.csv", "/api/issues"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=20) as r:
                assert r.status == 200
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/summary", timeout=20) as r:
            body = json.loads(r.read())
        assert body["symbols"] == ["AAA-EQ", "BBB-EQ"] and body["forecasts"]
    finally:
        srv.shutdown()


def test_interrupted_daily_resumes_without_duplicates(world: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    args = ["--config", str(world["config"]), "--home", str(home), "--quiet", "--no-intraday"]
    calls = {"n": 0}
    orig = LaneReplay._signal_predictions

    def boom(self, *a, **k):  # crash in the middle of the 6th step
        calls["n"] += 1
        if calls["n"] == 6:
            raise RuntimeError("simulated crash")
        return orig(self, *a, **k)

    monkeypatch.setattr(LaneReplay, "_signal_predictions", boom)
    assert cli.main(["replay", "--stocks", "AAA", "--device", "cpu", *args]) == 0  # lane failure is reported, run finishes
    monkeypatch.setattr(LaneReplay, "_signal_predictions", orig)
    db = AuditDB(home / "gplm.sqlite", readonly=True)
    assert db.q("SELECT 1 FROM events WHERE kind='LANE_FAILED'")
    ck = db.one("SELECT payload_json FROM checkpoints WHERE key LIKE 'replay:%'")
    assert ck, "completed steps were checkpointed before the crash"
    db.close()
    assert cli.main(["replay", "--stocks", "AAA", "--device", "cpu", *args]) == 0
    fresh = tmp_path / "fresh"
    assert cli.main(["replay", "--stocks", "AAA", "--device", "cpu", "--config", str(world["config"]), "--home",
                     str(fresh), "--quiet", "--no-intraday"]) == 0
    a = AuditDB(home / "gplm.sqlite", readonly=True)
    b = AuditDB(fresh / "gplm.sqlite", readonly=True)
    ka = {r["natural_key"] for r in a.q("SELECT natural_key FROM predictions")}
    kb = {r["natural_key"] for r in b.q("SELECT natural_key FROM predictions")}
    assert ka == kb, "a crashed-then-resumed run equals an uninterrupted run"


def test_cli_strategies_and_audit_commands(world: dict, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    home = tmp_path / "home"
    args = ["--config", str(world["config"]), "--home", str(home), "--quiet"]
    assert cli.main(["strategies", "seed", *args]) == 0
    assert cli.main(["strategies", "list", *args]) == 0
    out = capsys.readouterr().out
    sid = next(line.split()[0] for line in out.splitlines() if line.strip().startswith("st-"))
    assert cli.main(["strategies", "export", "--id", sid, *args]) == 0
    assert json.loads(capsys.readouterr().out)["strategy_id"] == sid
    assert cli.main(["strategies", "edit", sid, "--set", "exit.max_hold_bars=3", *args]) == 0
    assert cli.main(["audit", "--verify", *args]) == 0
    assert cli.main(["audit", *args]) == 0
    assert cli.main(["doctor", *args]) in (0, 1)
    shutil.rmtree(home / "events", ignore_errors=True)


def test_daily_incremental_new_session_processes_only_new_work(world: dict, tmp_path: Path) -> None:
    """A new published minute session is ingested alone and advances each lane by exactly one step."""
    raw = tmp_path / "raw"
    shutil.copytree(world["raw"], raw)
    sess = sorted(p.name[:10] for p in (raw / "minute" / "NSE" / "AAA-EQ").glob("*.json.gz"))
    held = sess[-1]
    stash = tmp_path / "stash"
    stash.mkdir()
    for p in (raw / "minute" / "NSE" / "AAA-EQ").glob(f"{held}*"):
        shutil.move(str(p), stash / p.name)
    cfg = json.loads(Path(world["config"]).read_text())
    cfg["data"].update(daily_root=str(raw / "daily" / "NSE"), minute_roots=[str(raw / "minute" / "NSE")],
                       real_raw_tree=str(raw))
    cp = tmp_path / "cfg.json"
    cp.write_text(json.dumps(cfg))
    home = tmp_path / "home"
    args = ["--config", str(cp), "--home", str(home), "--quiet", "--no-intraday"]
    assert cli.main(["daily", "--stocks", "AAA", "--device", "cpu", *args]) == 0
    db = AuditDB(home / "gplm.sqlite", readonly=True)
    before = {k: db.one(f"SELECT COUNT(*) n FROM {k}")["n"] for k in ("predictions", "evaluations", "data_files")}
    live_before = db.one("SELECT MAX(decision_label) d FROM predictions WHERE mode='LIVE'")["d"]
    db.close()
    for p in stash.iterdir():  # the evening's new file is published
        shutil.move(str(p), raw / "minute" / "NSE" / "AAA-EQ" / p.name)
    assert cli.main(["daily", "--stocks", "AAA", "--device", "cpu", *args, "--json"]) == 0
    db = AuditDB(home / "gplm.sqlite", readonly=True)
    after = {k: db.one(f"SELECT COUNT(*) n FROM {k}")["n"] for k in ("predictions", "evaluations", "data_files")}
    assert after["data_files"] == before["data_files"] + 1
    assert after["predictions"] > before["predictions"] and after["evaluations"] > before["evaluations"]
    assert db.one("SELECT MAX(decision_label) d FROM predictions WHERE mode='LIVE'")["d"] == held > live_before
    changed = db.q("SELECT payload_json FROM events WHERE kind IN ('DATA_FILE_NEW','DATA_FILE_CHANGED') AND run_id="
                   "(SELECT run_id FROM runs ORDER BY started_utc DESC LIMIT 1)")
    assert len(changed) == 1 and held in changed[0]["payload_json"]
    rp = db.one("SELECT payload_json FROM events WHERE kind='REPLAY_LANE_DONE' ORDER BY seq DESC LIMIT 1")
    assert json.loads(rp["payload_json"])["steps_done"] == 1
