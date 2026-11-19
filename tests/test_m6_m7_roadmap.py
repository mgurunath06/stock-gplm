"""PRD v1 roadmap (M6.0 - M7.2): synthetic, tiny fixtures; one executed case per test function.

Ledger: docs/m6-m7-test-budget.md (cap 10 cases)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from stock_gplm.app import alerts as AL
from stock_gplm.app import contracts as C
from stock_gplm.app import opportunity as OP
from stock_gplm.app import paper as P
from stock_gplm.app import risk as RK
from stock_gplm.app import strategy_dsl as D
from stock_gplm.app.benchmark import BaseRatePredictor, record, run
from stock_gplm.app.costs import contract_note, lane_costs
from stock_gplm.app.db import AuditDB
from stock_gplm.app.horizons import INVALID, MATURE, PENDING, LabelSpec, labels
from stock_gplm.app.hybrid import ControlError, control_state, fit_hybrid, set_control
from stock_gplm.app.indicator_store import lane_rows, store
from stock_gplm.app.session_calendar import SessionCalendar
from stock_gplm.m54.backtest import LaneMarket
from stock_gplm.m54.candles import ist_ns
from stock_gplm.m54.timeframes import build_all, daily_bars, monthly_bars, timeframe_minutes

SCHED = {
    "assumed_order_value_inr": 100000,
    "delivery": {"brokerage": {"pct": 0.0, "cap_inr": 20.0}, "stt_buy_pct": 0.1, "stt_sell_pct": 0.1,
                 "exchange_txn_pct": 0.00297, "sebi_fee_pct": 0.0001, "ipft_pct": 0.0001, "stamp_buy_pct": 0.015,
                 "gst_pct": 18.0, "dp_charge_inr_per_sell": 15.93},
    "intraday": {"brokerage": {"pct": 0.03, "cap_inr": 20.0}, "stt_buy_pct": 0.0, "stt_sell_pct": 0.025,
                 "exchange_txn_pct": 0.00297, "sebi_fee_pct": 0.0001, "ipft_pct": 0.0001, "stamp_buy_pct": 0.003,
                 "gst_pct": 18.0, "dp_charge_inr_per_sell": 0.0},
    "slippage_buckets": [{"name": "liquid", "min_traded_value_inr": 1e8, "bps": 5.0},
                         {"name": "illiquid", "min_traded_value_inr": 0, "bps": 25.0}],
}


def _market(close: np.ndarray, *, intraday: bool = False, sessions: list[str] | None = None, natr: float = 0.01,
            spread: float = 0.005) -> LaneMarket:
    n = len(close)
    sessions = sessions or [str(pd.Timestamp("2025-01-01") + pd.Timedelta(days=i))[:10] for i in range(n)]
    o = np.r_[close[0], close[:-1]]
    sid = np.unique(np.asarray(sessions), return_inverse=True)[1]
    end = np.array([ist_ns(s, "15:30") for s in sessions], dtype=np.int64) + np.arange(n) * (60_000_000_000 if intraday else 0)
    return LaneMarket("15m" if intraday else "1d", np.asarray(sessions), sid, end, o.astype(float),
                      np.maximum(o, close) * (1 + spread), np.minimum(o, close) * (1 - spread), close.astype(float),
                      np.full(n, 1e5), np.full(n, natr), np.zeros(n, bool), np.zeros(n, bool), intraday, 248.0)


def test_cost_schedule_contract_note_and_lane_costs() -> None:
    note = contract_note(SCHED, "delivery", 100, 1000.0, 1010.0)
    b, s = note["buy"], note["sell"]
    # hand-computed: STT 0.1% each side, exchange 0.00297%, SEBI+IPFT 0.0001% each, stamp 0.015% buy,
    # GST 18% on (brokerage + exchange + SEBI + IPFT), DP 15.93 on sell
    assert (b["stt"], b["exchange_txn"], b["stamp_duty"], b["gst"]) == (100.0, 2.97, 15.0, 0.5706)
    assert (s["stt"], s["dp_charge"], s["stamp_duty"]) == (101.0, 15.93, 0.0)
    assert note["net_pnl"] == round(1000 - b["total"] - s["total"], 4) and note["fill_basis"] == "SIMULATED"
    intr = contract_note(SCHED, "intraday", 1000, 1000.0, 1000.0)
    assert intr["buy"]["brokerage"] == 20.0  # 0.03% of 10 lakh = 300, capped at 20
    # legacy flat-bps config unchanged when no schedule
    legacy = {"delivery": {"commission_bps": 3, "slippage_bps": 5, "buy_statutory_bps": 12, "sell_statutory_bps": 10}}
    c0, d0 = lane_costs(legacy, "delivery")
    assert d0["model"] == "flat_bps" and abs(c0.buy - 20e-4) < 1e-12
    # liquidity bucket from the discovery segment only: liquid early, illiquid late -> liquid bucket
    n = 100
    close = np.full(n, 100.0)
    vol = np.r_[np.full(60, 2e6), np.full(40, 10.0)]
    sess = [f"2025-01-{(i % 28) + 1:02d}" if i < 28 else f"2025-0{2 + i // 28}-{(i % 28) + 1:02d}" for i in range(n)]
    c1, d1 = lane_costs({**legacy, "schedule": SCHED}, "delivery", sess, close, vol)
    assert d1["liquidity_bucket"] == "liquid" and d1["slippage_bps"] == 5.0 and c1.buy > c1.sell - 1e-3
    c2, d2 = lane_costs({**legacy, "schedule": SCHED}, "delivery", None, None, None)
    assert "LIQUIDITY_UNKNOWN" in d2["liquidity_bucket"] and d2["slippage_bps"] == 25.0


def test_monthly_bars_and_3m_lane() -> None:
    days = pd.bdate_range("2026-01-01", "2026-03-10")
    daily = pd.DataFrame({"date": [d.date().isoformat() for d in days], "open": 100.0, "high": 101.0, "low": 99.0,
                          "close": np.linspace(100, 110, len(days)), "volume": 1e5})
    cal = SessionCalendar.from_statuses({d.date().isoformat(): ("TRADING_SESSION" if d.weekday() < 5 else "MARKET_CLOSED")
                                         for d in pd.date_range("2026-01-01", "2026-03-31")},
                                        source="test", as_of_date="2026-03-31")
    mo = monthly_bars(daily_bars(daily), cal).frame
    assert list(mo["month_status"]) == ["COMPLETE", "COMPLETE", "PARTIAL"]
    assert list(mo["incomplete"]) == [False, False, True]  # a partial month is never a completed bar
    nocal = monthly_bars(daily_bars(daily), None).frame
    assert nocal["incomplete"].all()
    minute = pd.DataFrame({"start_ist": pd.date_range("2026-03-02 09:15", periods=12, freq="min"),
                           "session": "2026-03-02", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    allb = build_all(daily, minute, timeframes=("3m",), calendar=cal)
    assert "1mo" in allb and len(allb["3m"]) == 4 and timeframe_minutes("3m") == 3


def test_indicator_store_provenance_and_schema_v4(tmp_path) -> None:
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    assert db.one("SELECT value FROM meta WHERE key='schema_version'")["value"] == "4"
    m = _market(np.linspace(100, 110, 5))
    m.blocked[3] = True
    X = np.array([[1.0], [np.nan], [3.0], [4.0], [5.0]])
    defs = {"rsi_14": SimpleNamespace(indicator="RSI", params=(("n", 14),), output="value")}
    lane = SimpleNamespace(symbol="ABC-EQ", timeframe="1d", names=["rsi_14"], X=X, defs=defs, market=m,
                           data_fingerprint="rev1", feature_config_sha="f" * 64)
    rows = lane_rows(lane, range(5))
    assert [r["status"] for r in rows] == ["OK", "UNAVAILABLE", "OK", "QUARANTINED", "OK"]
    assert rows[1]["value"] is None and rows[0]["params"] == {"n": 14}  # unavailable is NULL, never 0
    assert store(db, rows) == {"inserted": 5, "already_present": 0}
    assert store(db, rows) == {"inserted": 0, "already_present": 5}  # idempotent, append-only
    with pytest.raises(Exception):
        db.conn.execute("UPDATE indicator_values SET value=0")


def test_new_dsl_primitives_are_causal() -> None:
    rng = np.random.default_rng(7)
    n = 420
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    o = np.r_[c[0], c[:-1]]
    f = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996, "close": c,
                      "volume": 1e5, "session": [f"d{i:04d}" for i in range(n)], "end_ns": np.arange(n)})
    exprs = ['bos(3, "up")', 'order_block_low(3, "up")', "vcp_contractions(3)", 'trendline(3, "low")',
             'pitchfork(3, "upper")', 'pattern("double_top", 3, 0.04)', 'harmonic("bat", 3, 0.1)',
             "pnf_breakout(1, 3, 2)", "adf_stat(close, 60)", "hmm_bull_prob(200, 50, 100)", "knn_analogue(3, 5, 4, 200)",
             "meta_prob(close > sma(close, 10), 3, 20, 25)"]
    cut = 300
    full = D.Context.from_frame(f, "1d", False)
    part = D.Context.from_frame(f.iloc[:cut], "1d", False)
    finite_somewhere = 0
    for e in exprs:
        a = np.asarray(D.evaluate(D.parse(e), full), float)[:cut]
        b = np.asarray(D.evaluate(D.parse(e), part), float)
        assert np.array_equal(np.nan_to_num(a, nan=-9e9), np.nan_to_num(b, nan=-9e9)), e
        finite_somewhere += int(np.isfinite(a).any())
    assert finite_somewhere == len(exprs)


def test_horizon_labels_outcomes_and_pending() -> None:
    # bar 0 signal; entry open[1]=100; ATR = 1% of entry -> stop 98, target 103
    close = np.array([100, 100, 101, 104, 104, 104, 97, 97, 97, 97], float)
    m = _market(close, spread=0.0)
    m.open[:] = np.r_[100, 100, close[1:-1]]
    lab = labels(m, LabelSpec(horizon=4, stop_atr=2.0, target_atr=3.0, band=0.001), 0.0005, 0.0005)
    assert lab["status"][0] == MATURE and lab["outcome"][0] == "TARGET" and lab["direction"][0] == "UP"
    assert abs(lab["gross"][0] - 0.03) < 1e-9 and lab["mfe"][0] >= 0.03
    assert lab["status"][-1] == PENDING and lab["status"][-3] == PENDING  # exit bar not closed yet
    m.blocked[6] = True
    lab2 = labels(m, LabelSpec(horizon=4), 0.0, 0.0)
    assert lab2["status"][3] == INVALID and lab2["outcome"][3] == "BLOCKED_PATH"
    fin = np.ones(len(close), bool)
    fin[5] = False  # a non-final (partial week) bar keeps the label pending
    assert labels(m, LabelSpec(horizon=4), 0.0, 0.0, fin)["status"][1] == PENDING


def test_benchmark_freeze_before_reveal_and_reproducible(tmp_path) -> None:
    days = [d.date().isoformat() for d in pd.bdate_range("2026-07-01", "2026-08-20")]
    daily = pd.DataFrame({"session": days, "close": np.linspace(100, 120, len(days))})
    daily["end_ns"] = [ist_ns(d, "15:30") for d in days]
    daily["avail_ns"] = daily["end_ns"] + 1
    daily["incomplete"] = False
    intr = pd.DataFrame({"session": ["2026-08-12"] * 2 + ["2026-08-13"] * 3,
                         "close": [10, 11, 12, 11, 13.0],
                         "end_ns": [ist_ns("2026-08-12", t) for t in ("15:15", "15:30")] +
                                   [ist_ns("2026-08-13", t) for t in ("09:30", "09:45", "10:00")]})
    intr["avail_ns"], intr["incomplete"] = intr["end_ns"], False
    week = pd.DataFrame({"session": ["2026-08-07", "2026-08-14"], "close": [110.0, 112.0],
                         "end_ns": [ist_ns("2026-08-07", "15:30"), ist_ns("2026-08-14", "15:30")],
                         "avail_ns": [ist_ns("2026-08-07", "15:30"), ist_ns("2026-08-14", "15:30")],
                         "incomplete": [False, True]})
    frames = {"1d": daily, "15m": intr, "1w": week}
    r1 = run(frames, "2026-08-13", BaseRatePredictor(band=0.001), adaptive=False, band=0.001)
    r2 = run(frames, "2026-08-13", BaseRatePredictor(band=0.001), adaptive=False, band=0.001)
    assert r1["result_sha256"] == r2["result_sha256"]
    rows = r1["rows"]
    seen_freeze = set()
    for r in rows:  # every reveal comes after its own freeze
        key = (r.get("timeframe"), r.get("decision_bar"))
        if r["kind"] == "FREEZE":
            seen_freeze.add(key)
        if r["kind"] == "REVEAL":
            assert key in seen_freeze
    s = r1["summary"]["by_timeframe"]
    assert s["15m"]["frozen"] == 3 and s["15m"]["revealed"] == 3  # first target uses the prior session's last bar
    assert s["1d"]["revealed"] == 1 and s["1w"]["pending"] == 1  # weekly target not final -> pending
    ad = run(frames, "2026-08-13", BaseRatePredictor(band=0.001), adaptive=True, band=0.001)
    assert ad["summary"]["updates"] >= 1 and r1["summary"]["updates"] == 0
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    assert record(db, "X", "2026-08-13", "frozen", {"k": 1}, r1)["stored"] is True
    assert record(db, "X", "2026-08-13", "frozen", {"k": 1}, r2)["reproduced"] is True


def test_hybrid_fallback_and_controls(tmp_path) -> None:
    rng = np.random.default_rng(1)
    n = 1200
    X = rng.normal(size=(n, 2))
    sym = np.where(np.arange(n) % 10 == 0, "THIN", "BIG")
    y = (rng.random(n) < 1 / (1 + np.exp(-(1.5 * X[:, 0] + np.where(sym == "BIG", 0.8, 0))))).astype(float)
    model, rep = fit_hybrid(X, y, sym, {"regime": np.where(X[:, 1] > 0, "UP", "DOWN")}, np.arange(n), ["a", "b"],
                            min_stock_rows=200)
    assert "BIG" in model.stock_offset and "THIN" not in model.stock_offset
    assert rep["brier"]["hybrid_calibrated"] < rep["brier"]["base_rate"]
    out = model.predict(X[:2], {"regime": ["UP", "UP"]}, ["BIG", "THIN"])
    assert out[0]["source"] == "STOCK_RESIDUAL" and out[1]["source"] == "SHARED_FALLBACK"
    assert "below the minimum" in out[1]["disclosure"]
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    set_control(db, "DISABLE_ALERTS", "owner", "feed outage")
    set_control(db, "ENABLE_ALERTS", "owner", "recovered", scope="ABC-EQ")
    assert control_state(db, "XYZ-EQ")["alerts_disabled"] is True
    assert control_state(db, "ABC-EQ")["alerts_disabled"] is False
    with pytest.raises(Exception):
        set_control(db, "ROLLBACK", "owner", "bad model", target="fm-unknown")
    with pytest.raises(ControlError):
        set_control(db, "PAUSE_UPDATES", "owner", "")


def test_paper_ledger_forward_only_idempotent_and_lifecycle_gate(tmp_path) -> None:
    from stock_gplm.app import governance as G

    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    close = np.r_[np.full(10, 100.0), np.linspace(100, 110, 10)]
    m = _market(close)
    sig = np.zeros(len(close), bool)
    sig[[2, 12]] = True
    src = "strategy:S1@1"
    with pytest.raises(ValueError):
        P.process(db, src, "ABC-EQ", m, sig, {"max_hold_bars": 3}, 0.001, 0.001)
    P.enrol(db, src, "owner", int(m.end_ns[10]))  # forward-only: bar 2 predates enrolment
    c1 = P.process(db, src, "ABC-EQ", m, sig, {"max_hold_bars": 3, "stop_atr": 2, "target_atr": 50}, 0.001, 0.001)
    c2 = P.process(db, src, "ABC-EQ", m, sig, {"max_hold_bars": 3, "stop_atr": 2, "target_atr": 50}, 0.001, 0.001)
    assert c1 == {"signals": 1, "entries": 1, "exits": 1, "cancels": 0}
    assert c2 == {"signals": 0, "entries": 0, "exits": 0, "cancels": 0}  # restart-safe
    s = P.summary(db, src)
    assert s["closed"] == 1 and s["evidence_ref"] == f"paper:{src}:1"
    assert db.one("SELECT fill_basis FROM paper_events WHERE kind='ENTRY'")["fill_basis"] == "PAPER"
    db.conn.execute("INSERT INTO strategies VALUES ('S1', 1, 'S1', 'USER', 0, 1, '1d', '{}', 'p', 'h', NULL, NULL, 't', NULL)")
    db.conn.commit()
    for st in ("VALIDATED", "BACKTESTED"):
        G.advance_strategy(db, "S1", 1, st, "owner", ["e"])
    with pytest.raises(G.GovernanceError, match="paper evidence"):
        G.advance_strategy(db, "S1", 1, "PAPER_TESTED", "owner", ["e"], paper_min_trades=5)
    out = G.advance_strategy(db, "S1", 1, "PAPER_TESTED", "owner", ["e"], paper_min_trades=1)
    assert f"paper:{src}:1" in out["history"][-1]["evidence"]


def test_opportunity_rank_alert_lifecycle_risk_and_notify(tmp_path) -> None:
    db = AuditDB(tmp_path / "a.sqlite", tmp_path / "ev", run_id="t")
    fresh = C.Freshness("FRESH", "2026-08-13", "2026-08-13", [])
    dec = ist_ns("2026-08-13", "15:30")

    def inp(sym: str, mean_net: float, n: int, **kw) -> OP.OpportunityInput:
        return OP.OpportunityInput(symbol=sym, timeframe="1d", horizon="d1_7", horizon_bars=5, decision_ns=dec,
                                   close=100.0, atr=2.0, source_kind="STRATEGY", source_id="S1", source_version="1",
                                   signal=True, evidence={"n": n, "mean_net": mean_net, "win_rate": 0.6},
                                   exit={"stop_atr": 1.0, "target_atr": 2.0, "max_hold_bars": 3}, freshness=fresh,
                                   timeframe_forecasts=[{"timeframe": "1w", "direction": "DOWN", "p_up": 0.4}],
                                   governance={"calendar_covers": True, "latest_eligible_bar": True}, **kw)
    a = OP.build(inp("AAA-EQ", 0.01, 300))
    b = OP.build(inp("BBB-EQ", 0.01, 40))
    weak = OP.build(inp("CCC-EQ", 0.02, 5))
    neg = OP.build(inp("DDD-EQ", -0.01, 300))
    assert a["action"] == "BUY" and a["stop"] == 98.0 and a["targets"] == [104.0, 106.0] and a["entry_zone"] == [100.0, 100.5]
    assert a["conflicts"] and a["status"] == "SHADOW"  # no approvals -> never LIVE; conflict explained, not suppressed
    assert weak["action"] == "NO_TRADE" and any("WEAK_EVIDENCE" in r for r in weak["no_trade_reasons"])
    assert neg["action"] == "NO_TRADE" and "NEGATIVE_EXPECTED_NET" in neg["no_trade_reasons"]
    ranked = OP.rank([b, weak, a, neg])
    assert [r["symbol"] for r in ranked[:2]] == ["AAA-EQ", "BBB-EQ"] and ranked[0]["rank"] == 1
    assert OP.store(db, ranked)["stored"] == 4 and OP.store(db, ranked)["stored"] == 0
    # risk: missing inputs are reported, never assumed zero
    assert RK.check(None, a)["missing"] == ["RISK_INPUT_MISSING:file"]
    rk = {"capital_inr": 100000, "holdings": [{"symbol": "AAA-EQ", "qty": 100, "avg_price": 100}],
          "limits": {"max_position_pct": 5, "max_sector_pct": 30}}
    chk = RK.check(rk, a)
    assert any("RISK_POSITION_LIMIT" in x for x in chk["blockers"]) and "RISK_INPUT_MISSING:sector(AAA-EQ)" in chk["missing"]
    # alert lifecycle: create -> ARMED -> TRIGGERED -> EXIT (target); duplicate suppressed; catch-up not current
    now = dec + 10 * 60_000_000_000
    al = AL.create(db, a, now)
    assert al["state"] == "ARMED" and al["mode"] == "SHADOW"
    dup = dict(a, natural_key=a["natural_key"] + "-x", opportunity_id="op-x")
    assert AL.create(db, dup, now)["state"] == "SUPPRESSED"
    nxt = [{"end_ns": dec + k * 86_400_000_000_000, "open": 100.2, "high": hi, "low": 99.5, "close": 100.4, "session": "s"}
           for k, hi in ((1, 100.6), (2, 101.0), (3, 104.5))]
    assert AL.advance(db, al["alert_id"], nxt, dec + 10 * 86_400_000_000_000) == ["TRIGGERED", "EXIT"]  # catch-up
    st = AL.status(db, al["alert_id"])
    assert st["state"] == "EXIT" and st["history"][-1]["reason"] == "TARGET_1" and st["history"][-1]["current"] == 0
    out = tmp_path / "outbox.jsonl"
    assert AL.notify(db, out) == {}  # SHADOW alerts are not notified by default
    live = dict(OP.build(inp("EEE-EQ", 0.01, 300)), status="LIVE")
    AL.create(db, live, dec + 60_000_000_000)
    assert AL.notify(db, out) == {"DELIVERED": 1} and AL.notify(db, out) == {}  # once per state and channel
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["state"] == "ARMED"
