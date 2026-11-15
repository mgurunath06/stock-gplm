"""Regression tests for the 2026-11-15 remediation found by ``gplm verify`` (synthetic, deterministic)."""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import numpy as np
import pytest

from stock_gplm.app import strategy_dsl as dsl
from stock_gplm.app import strategy_engine as se
from stock_gplm.app import strategy_evolve as evo
from stock_gplm.app.m56_catalogue import _direction
from stock_gplm.app.research import _tree_to_dsl, write_audit
from stock_gplm.verify import fixtures as fx
from stock_gplm.verify.suite_catalogue import CATALOGUE, LIBRARY, semantic
from stock_gplm.verify.suite_validation import _lane


def test_baseline_applies_the_signal_exit() -> None:
    f = fx.ohlcv(1200, fx.SEED + 5, drift=0.0015, regime_flip=False)
    li = _lane(f)
    seg = se.segments(len(f), 10)["validation"]
    ex = se.Exit(10, 2.5, None, None)
    xm = dsl.signal("close > sma(close, 5)", li.ctx)
    fair = se.baseline(li.market, seg, ex, li.buy, li.sell, False, {}, xmask=xm)
    same = se.simulate(li.market, np.ones(len(f), bool), seg, ex, li.buy, li.sell, False, xm, independent=False,
                       fired=np.arange(*seg))
    assert fair == pytest.approx(np.mean([t["net_return"] for t in same]))
    assert abs(fair - se.baseline(li.market, seg, ex, li.buy, li.sell, False, {})) > 1e-3  # the v1 bias was real


def test_false_gp_advantage_is_gone_and_planted_edge_found() -> None:
    f = fx.planted_edge()
    li = _lane(f)
    seg = (0, int(len(f) * 0.6))
    ev = evo.Evolver(li, seg, seed=55, budget="quick")
    artefact = evo.Genome(["close < open", "er(10) < 0.5951"], "short", 3)  # v1 fitness 11.86
    assert ev.fitness(artefact) <= 0
    out = evo.Evolver(li, seg, seed=55, budget="quick").run()
    assert out["selected"] and all(g.stats["mean_net"] > 0 for g in out["selected"])
    assert any(g.direction == "long" and "falling(close, 3)" in g.entry() for g in out["selected"])
    assert len(out["log"]) == out["evaluated"]


@pytest.mark.parametrize("sid", ["C4-004", "C4-005", "C5-007", "C5-008"])
def test_corrected_catalogue_entries_match_the_literal_formula(sid: str, tmp_path: Path) -> None:
    from stock_gplm.app.m56_catalogue import load_catalogue
    from stock_gplm.app.strategy_library import StrategyLibrary

    s = StrategyLibrary([LIBRARY], tmp_path).load(record=False).strategies[sid]
    ref = {r.reference_id: r for r in load_catalogue(CATALOGUE).references}[s.reference_id]
    frames = [fx.ohlcv(1500, fx.SEED + k) for k in range(3)]
    res = semantic(ref.rule_text, s.doc["entry"], s.defaults(), frames)
    assert res["semantic"] == "IDENTICAL" and res["spec_true"] > 20
    assert "literal correction" in s.doc["notes"]


def test_talib_seeding_and_mfi() -> None:
    f = fx.frame_from([10, 11, 12, 13, 14], [10.5, 11.5, 12.5, 13.5, 14.5], [9.5, 10.5, 11.5, 12.5, 13.5],
                      [10, 11, 12, 13, 14])
    ctx = fx.ctx(f)
    e = dsl.evaluate(dsl.parse("ema(close, 3)"), ctx)
    assert np.isnan(e[:2]).all() and np.allclose(e[2:], [11.0, 12.0, 13.0])  # SMA seed, then alpha 0.5
    g = fx.ohlcv(100)
    tr = dsl.evaluate(dsl.parse("tr"), fx.ctx(g))
    atr = dsl.evaluate(dsl.parse("atr(14)"), fx.ctx(g))
    assert math.isnan(tr[0]) and np.isnan(atr[:14]).all() and atr[14] == pytest.approx(tr[1:15].mean())
    mfi = dsl.evaluate(dsl.parse("mfi(14)"), fx.ctx(g))
    assert np.isnan(mfi[:14]).all() and np.isfinite(mfi[14])
    z = fx.edge_cases()["zero_volume"]
    assert np.isnan(dsl.evaluate(dsl.parse("mfi(14)"), fx.ctx(z))[14:]).all()


def test_seed_skips_leading_gaps() -> None:
    x = np.array([np.nan, 1.0, np.nan, 2.0, 3.0, 4.0, 5.0])
    out = dsl.ema(x, 3)
    assert np.isnan(out[:5]).all() and out[5] == pytest.approx(3.0) and out[6] == pytest.approx(4.0)


@pytest.mark.parametrize("name,text,want", [
    ("Swing EMA Crossover (9/21 EMA)", "Short-term momentum ignition.", "long"),
    ("Guppy Multiple Moving Average (GMMA) Expansion", "Traders (short EMAs) pulling away", "long"),
    ("F&O Short Covering Rally (Price Up + OI Down)", "Trapped short sellers forced to buy back", "long"),
    ("Evening Star (3-Bar Top)", "Mirror reversal at the top", "short"),
    ("Wyckoff Upthrust After Distribution (Short)", "False breakout", "short"),
    ("ATR Stretch from 21-EMA Fade", "x (Short/Profit-taking rule)", "short"),
])
def test_direction_markers(name: str, text: str, want: str) -> None:
    assert _direction(f"{name} {text}", name) == want


def test_tree_definition_resolved_against_run_dir(tmp_path: Path) -> None:
    rel = Path("strategies") / "original" / "X-EQ" / "1d" / "ml-0123456789abcdef.json"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(json.dumps({"entry": {"conditions": [
        {"feature": "RSI(period=14).value", "op": "lt", "threshold": 30.0}]}, "exit": {"max_hold_bars": 5}}))
    row = {"definition_file": str(rel), "strategy_id": "ml-0123456789abcdef"}
    assert _tree_to_dsl(row, "X-EQ", "1d", "fp", "run") is None or Path.cwd() == tmp_path
    doc = _tree_to_dsl(row, "X-EQ", "1d", "fp", "run", tmp_path)
    assert doc is not None and doc["entry"] == 'feature("RSI(period=14).value") < 30.0'


def test_write_audit_is_atomic_and_indexed(tmp_path: Path) -> None:
    lane = {"symbol": "X-EQ", "timeframe": "1d", "data_fingerprint": "fp", "run_id": "r"}
    audit = {"trades": [["S1", "abc", "long", "{}", "validation", "2024-01-01", "2024-01-02", "2024-01-05", 100.0,
                         101.0, "TIME", 0.01, 0.006, 4, 0.02, -0.01]],
             "ml_log": [{"method": "evolve", "seq": 1, "entry": "rsi(close, 2) < 10", "fitness": 1.5}]}
    files = write_audit(tmp_path, "lane1", lane, audit, {"trades": "validation_final", "ml_log": True})
    assert set(files) == {"trades/X-EQ/1d.csv.gz", "ml/X-EQ/1d.jsonl.gz"}
    text = gzip.open(tmp_path / "audit" / "trades" / "X-EQ" / "1d.csv.gz", "rt").read().splitlines()
    assert text[0].startswith("strategy_id,") and text[1].startswith("S1,")
    idx = (tmp_path / "audit" / "index.jsonl").read_text().splitlines()
    assert len(idx) == 1 and json.loads(idx[0])["files"]["trades/X-EQ/1d.csv.gz"]["rows"] == 1
    assert not list((tmp_path / "audit").rglob(".*.tmp"))
    assert write_audit(tmp_path, "lane2", lane, audit, {"trades": "none", "ml_log": False}) == {}
