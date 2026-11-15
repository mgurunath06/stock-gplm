"""Synthetic tests for the unified multi-timeframe discovery engine (no real market data is read)."""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_gplm.m54 import candles, timeframes as tfm
from stock_gplm.m54.backtest import Costs, ExitPolicy, LaneMarket, metrics, simulate
from stock_gplm.m54.candles import AccessPolicy, ist_ns, load_daily, load_minute, resolve_policy
from stock_gplm.m54.fixture import make_fixture
from stock_gplm.m54.hypotheses import Condition, Hypothesis
from stock_gplm.m54.recency import Scheme, schemes, select, weights
from stock_gplm.m54.unified import Lane, UnifiedRun, _row, load_settings

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "m54" / "unified-default.json"
SESSION_POLICY = REPO / "config" / "m3" / "minute-session-policy.json"
FREE = Costs(0.0, 0.0)


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> dict:
    out = tmp_path_factory.mktemp("fixture")
    info = make_fixture(out, ["RELIANCE"], start="2020-01-01", end="2021-06-30", minute_from="2021-01-01",
                        cutoff="2021-05-03", seed=11)
    return info


def _settings(fx: dict, budget: str = "quick") -> dict:
    s = load_settings(CONFIG, budget, {"daily_root": fx["daily_root"], "minute_roots": [fx["minute_root"]]})
    s["budget"].update({"max_single_candidates": 120, "max_interactions": 8, "cross_tf_max": 20, "checkpoint_every": 100,
                        "auto_timeframes": ["15m", "60m", "1d"]})
    return s


def _policy(fx: dict) -> AccessPolicy:
    return resolve_policy({}, Path(fx["daily_root"]), [Path(fx["minute_root"])], REPO / "nonexistent-raw", None, None)


# ----------------------------------------------------------------------------- data access policy

def test_synthetic_policy_requires_marker_everywhere(fx: dict, tmp_path: Path) -> None:
    p = _policy(fx)
    assert p.basis == "SYNTHETIC_FIXTURE" and p.cutoff == "2021-05-03"
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ValueError, match="every data root"):
        resolve_policy({}, Path(fx["daily_root"]), [bare], REPO / "nonexistent-raw", None, None)
    with pytest.raises(ValueError, match="real raw tree"):
        resolve_policy({}, Path(fx["daily_root"]), [Path(fx["minute_root"])], Path(fx["daily_root"]).parents[1], None, None)


def test_excluded_partitions_are_never_opened(fx: dict, tmp_path: Path) -> None:
    root = tmp_path / "copy"
    shutil.copytree(Path(fx["daily_root"]).parents[1], root)
    daily, minute = root / "daily" / "NSE", root / "minute" / "NSE"
    policy = _policy({"daily_root": str(daily), "minute_root": str(minute)})
    locked = []
    for path in list((daily / "RELIANCE-EQ").glob("*.json.gz")) + list((minute / "RELIANCE-EQ").glob("*.json.gz")):
        end = path.name.removesuffix(".json.gz").split("_")[-1]
        if end >= policy.cutoff:
            path.write_bytes(b"corrupt")  # opening it would reject the partition
            os.chmod(path, 0)  # and reading it would raise
            locked.append(path)
    assert locked
    try:
        d = load_daily(daily, "RELIANCE-EQ", policy)
        m = load_minute([minute], "RELIANCE-EQ", policy, candles.SessionPolicy.load(SESSION_POLICY))
    finally:
        for path in locked:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    assert d.status == "ACCEPTED" and (d.bars["date"] < policy.cutoff).all()
    excluded = [p for p in d.partitions + m.partitions if not p["permitted"]]
    assert excluded and all(p["disposition"].startswith("EXCLUDED_HOLDOUT") for p in excluded)
    assert (m.bars["session"] < policy.cutoff).all() and len(m.bars) > 0


def test_start_end_only_narrow(fx: dict) -> None:
    p = AccessPolicy("SYNTHETIC_FIXTURE", "2021-05-03", {}, start="2021-02-01", end="2021-03-31")
    d = load_daily(Path(fx["daily_root"]), "RELIANCE-EQ", p)
    assert d.bars["date"].min() >= "2021-02-01" and d.bars["date"].max() <= "2021-03-31"
    assert p.partition("2021-05-03", "2021-05-03")[0] is False


def test_minute_duplicates_identical_and_conflicting(fx: dict, tmp_path: Path) -> None:
    src = Path(fx["minute_root"]) / "RELIANCE-EQ"
    sess = sorted(p.name.removesuffix(".json.gz") for p in src.glob("*.json.gz"))[:2]
    a, b = tmp_path / "a" / "RELIANCE-EQ", tmp_path / "b" / "RELIANCE-EQ"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    for s in sess:
        for suffix in (".json.gz", ".json.gz.done", ".meta.json"):
            shutil.copy(src / f"{s}{suffix}", a / f"{s}{suffix}")
            shutil.copy(src / f"{s}{suffix}", b / f"{s}{suffix}")
    done = json.loads((b / f"{sess[1]}.json.gz.done").read_text())
    done["sha256"] = "0" * 64  # a differing copy
    (b / f"{sess[1]}.json.gz.done").write_text(json.dumps(done))
    policy = AccessPolicy("SYNTHETIC_FIXTURE", "2030-01-01", {})
    m = load_minute([tmp_path / "a", tmp_path / "b"], "RELIANCE-EQ", policy, candles.SessionPolicy.load(SESSION_POLICY))
    disp = {(Path(p["file"]).parent.parent.name, p["session"]): p["disposition"] for p in m.partitions}
    assert disp[("b", sess[0])].startswith("DUPLICATE_OF")
    assert disp[("a", sess[1])].startswith("CONFLICTING_DUPLICATE") and disp[("b", sess[1])].startswith("CONFLICTING")
    assert set(m.bars["session"]) == {sess[0]}


# ----------------------------------------------------------------------------- multi-timeframe bars

def _minutes(session: str, n: int = 375, skip: tuple[int, ...] = ()) -> pd.DataFrame:
    t0 = pd.Timestamp(f"{session} 09:15")
    rows = [{"session": session, "start_ist": t0 + pd.Timedelta(minutes=int(i)), "open": 100 + i, "high": 101 + i,
             "low": 99 + i, "close": 100.5 + i, "volume": 10.0} for i in range(n) if i not in skip]
    return pd.DataFrame(rows)


def test_resample_aggregates_and_flags_incomplete() -> None:
    one = tfm.minute_frame(_minutes("2021-01-04", skip=(20,)))
    b15 = tfm.resample_intraday(one, "15m").frame
    assert len(b15) == 25
    first = b15.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"], first["volume"]) == (100, 115, 99, 114.5, 150)
    assert bool(b15.iloc[1]["incomplete"]) and not bool(b15.iloc[0]["incomplete"])  # minute 20 missing; never filled
    assert b15.iloc[0]["end_ns"] == ist_ns("2021-01-04", "09:30")
    b60 = tfm.resample_intraday(one, "60m").frame
    assert len(b60) == 7 and b60.iloc[-1]["end_ns"] == ist_ns("2021-01-04", "15:30")
    assert not bool(b60.iloc[-1]["incomplete"])  # the 15-minute final bucket is complete by definition


def test_four_hour_and_configurable_session_anchored_bars() -> None:
    one = tfm.minute_frame(_minutes("2021-01-04"))
    b4h = tfm.resample_intraday(one, "4h").frame
    assert len(b4h) == 2
    assert list(b4h["end_ns"]) == [ist_ns("2021-01-04", "13:15"), ist_ns("2021-01-04", "15:30")]
    assert list(b4h["volume"]) == [2400.0, 1350.0]
    assert not b4h["incomplete"].any()
    b2h = tfm.resample_intraday(one, "2h").frame
    assert len(b2h) == 4 and b2h.iloc[-1]["end_ns"] == ist_ns("2021-01-04", "15:30")
    assert tfm.timeframe_minutes("4h") == 240 and tfm.timeframe_minutes("90m") == 90
    with pytest.raises(ValueError):
        tfm.timeframe_minutes("8h")


def test_htf_alignment_is_causal() -> None:
    daily = pd.DataFrame({"date": ["2021-01-04", "2021-01-05"], "open": [1.0, 2], "high": [1.0, 2], "low": [1.0, 2],
                          "close": [1.0, 2], "volume": [1.0, 1]})
    d = tfm.daily_bars(daily).frame
    one = tfm.minute_frame(pd.concat([_minutes("2021-01-04", 30), _minutes("2021-01-05", 30)], ignore_index=True)).frame
    vals = tfm.align(one["end_ns"].to_numpy(), d["avail_ns"].to_numpy(), d["close"].to_numpy())
    assert np.isnan(vals[:30]).all()  # session 1 cannot see its own (or any) daily close
    assert (vals[30:] == 1.0).all()  # session 2 sees only session 1's close, never its own
    dd = tfm.daily_bars(pd.DataFrame({"date": [f"2021-01-{x:02d}" for x in (4, 5, 6, 7, 8, 11, 12)],
                                      **{c: np.arange(1.0, 8.0) for c in ("open", "high", "low", "close", "volume")}}))
    w = tfm.weekly_bars(dd).frame
    wv = tfm.align(dd.frame["end_ns"].to_numpy(), w["avail_ns"].to_numpy(), w["close"].to_numpy())
    assert np.isnan(wv[:4]).all() and wv[4] == 5.0 and wv[5] == 5.0 and wv[6] == 5.0  # open final week never read
    short = tfm.daily_bars(pd.DataFrame({"date": ["2021-01-04", "2021-01-07", "2021-01-11"],  # week 1 ends Thursday
                                         **{c: [1.0, 2.0, 3.0] for c in ("open", "high", "low", "close", "volume")}}))
    ws = tfm.weekly_bars(short).frame
    sv = tfm.align(short.frame["end_ns"].to_numpy(), ws["avail_ns"].to_numpy(), ws["close"].to_numpy())
    assert np.isnan(sv[1]) and sv[2] == 2.0  # a Thursday-ending week is read only after Friday 15:30


def test_features_unchanged_when_future_bars_removed(fx: dict) -> None:
    s = _settings(fx)
    run = UnifiedRun(s, "RELIANCE", Path(fx["daily_root"]).parents[2] / "out-trunc")
    run.load(create=False)
    lane = Lane(run, "60m", run.bars["60m"], {"1d": run.bars["1d"]})
    lane.run.output_root = Path(fx["daily_root"]).parents[2] / "out-trunc"
    lane.build_features()
    full = lane.features
    k = len(lane.frame) // 2
    cut_ns = int(lane.frame["end_ns"].iloc[k])
    short = UnifiedRun(s, "RELIANCE", Path(fx["daily_root"]).parents[2] / "out-trunc2")
    short.load(create=False)
    bars60 = tfm.Bars("60m", run.bars["60m"].frame.iloc[: k + 1].copy(), "trunc")
    d = run.bars["1d"].frame
    bars1d = tfm.Bars("1d", d[d["avail_ns"] <= cut_ns].copy(), "trunc")
    lane2 = Lane(short, "60m", bars60, {"1d": bars1d})
    lane2.build_features()
    checked = 0
    for key, v in lane2.features.items():
        a, b = full[key][: k + 1], v
        both = np.isfinite(a) & np.isfinite(b)
        assert np.allclose(a[both], b[both], rtol=1e-9, atol=1e-12), key
        assert (np.isfinite(a) == np.isfinite(b)).all() or key.startswith(("KAMA", "PSAR")), key
        checked += 1
    assert checked > 500


# ----------------------------------------------------------------------------- backtest

def _market(o, h, lo, c, *, sessions=None, intraday=False, natr=0.01, blocked=None) -> LaneMarket:
    n = len(o)
    sessions = sessions or [f"2021-01-{i + 1:02d}" for i in range(n)]
    sid = tfm.session_ids(sessions)
    return LaneMarket("15m" if intraday else "1d", np.array(sessions), sid, np.arange(n, dtype=np.int64) * 60_000_000_000,
                      np.array(o, float), np.array(h, float), np.array(lo, float), np.array(c, float), np.ones(n),
                      np.full(n, natr), np.array(blocked or [False] * n), np.zeros(n, bool), intraday, 248.0)


def test_stop_target_gap_and_both_in_bar() -> None:
    o = [100, 100, 100, 95, 100, 100]
    m = _market(o, [101, 101, 101, 96, 103, 101], [99, 99, 99, 94, 97, 99], [100, 100, 100, 95, 100, 100], natr=0.02)
    mask = np.zeros(6, bool)
    mask[1] = True
    tr = simulate(m, mask, (0, 6), ExitPolicy(3, stop_atr=1.0), FREE)
    assert tr[0]["exit_reason"] == "STOP_GAP" and tr[0]["exit_price"] == 95  # gap through the stop fills at the open
    mask = np.zeros(6, bool)
    mask[3] = True  # entry open 100 at bar 4: stop 98 and target 102 both touched -> stop first
    tr = simulate(m, mask, (0, 6), ExitPolicy(1, stop_atr=1.0, target_atr=1.0), FREE)
    assert tr[0]["exit_reason"] == "STOP" and tr[0]["exit_price"] == pytest.approx(98)


def test_time_exit_purging_and_blocked_paths() -> None:
    m = _market([100, 101, 102, 103, 104], [105] * 5, [95] * 5, [100, 101, 102, 103, 104])
    mask = np.ones(5, bool)
    tr = simulate(m, mask, (0, 5), ExitPolicy(2), FREE)
    assert [t["t"] for t in tr] == [0, 2 - 0 + 1 - 1] or tr[0]["exit_price"] == 103
    assert all(t["exit_index"] <= 4 for t in tr)  # every trade finishes inside the segment
    mb = _market([100] * 5, [101] * 5, [99] * 5, [100] * 5, blocked=[False, False, True, False, False])
    assert simulate(mb, np.array([True, False, False, False, False]), (0, 5), ExitPolicy(2), FREE) == []


def test_intraday_never_enters_overnight_and_is_flat_by_close() -> None:
    sessions = ["2021-01-04"] * 3 + ["2021-01-05"] * 3
    m = _market([100, 101, 102, 110, 111, 112], [105] * 3 + [115] * 3, [95] * 3 + [105] * 3,
                [100, 101, 102, 110, 111, 112], sessions=sessions, intraday=True)
    mask = np.array([False, True, True, False, False, False])
    tr = simulate(m, mask, (0, 6), ExitPolicy(10), FREE)
    assert len(tr) == 1 and tr[0]["exit_reason"] == "SESSION_CLOSE" and tr[0]["exit_index"] == 2


def test_intraday_statistics_cluster_by_session() -> None:
    sessions = sum(([f"2021-01-{d:02d}"] * 4 for d in range(4, 9)), [])
    n = len(sessions)
    rng = np.random.default_rng(0)
    c = 100 + rng.standard_normal(n).cumsum() * 0.1
    m = _market(c, c + 0.5, c - 0.5, c, sessions=sessions, intraday=True)
    tr = simulate(m, np.ones(n, bool), (0, n), ExitPolicy(1), FREE)
    met = metrics(tr, m, (0, n), 0.0)
    assert met["trades"] > met["effective_n"] and met["effective_n"] == len({t["session_id"] for t in tr})
    assert met["independence_unit"] == "session"


# ----------------------------------------------------------------------------- recency

def test_recency_weights_use_sessions_and_select_ties_uniform() -> None:
    sc = Scheme("exp_hl10", 10.0)
    w = weights(sc, [0, 10, 20], 20)
    assert w == pytest.approx([0.25, 0.5, 1.0])
    wr = weights(Scheme("r", None, True, 0.5), [1, 2], 2, ["up", "down"], "up")
    assert list(wr) == [1.0, 0.5]
    assert [s.name for s in schemes({"half_lives_sessions": [20], "regime_conditional": True})] == \
        ["uniform", "exp_hl20", "regime_match", "exp_hl20_regime"]
    name, _ = select({"uniform": [0.01, 0.02], "exp_hl20": [0.01, 0.02]})
    assert name == "uniform"
    name, table = select({"uniform": [0.0], "exp_hl20": [0.03]})
    assert name == "exp_hl20" and table["exp_hl20"]["mean_validation_net"] == 0.03


# ----------------------------------------------------------------------------- engine end to end

def test_planted_effect_is_detected(fx: dict, tmp_path: Path) -> None:
    run = UnifiedRun(_settings(fx), "RELIANCE", tmp_path)
    run.load(create=False)
    run.output_root = tmp_path
    lane = Lane(run, "1d", run.bars["1d"], {})
    lane.build_features()
    h = Hypothesis((Condition("STATS(period=10).streak", "lt", -2.5, "level"),))
    whole = {"discovery": (0, len(lane.frame))}
    ctx = {**lane.ctx(), "segs": whole}
    r = _row(ctx, h, ExitPolicy(1), "discovery")
    assert r["excess_mean"] > 0.003 and r["t_stat"] > 0  # ~45 events: magnitude recovered, not significance


def test_end_to_end_resume_report_and_library(fx: dict, tmp_path: Path) -> None:
    s = _settings(fx)
    out = tmp_path / "out"
    first = UnifiedRun(s, "RELIANCE", out, workers=2, log=lambda _m: None).execute()
    assert first["state"] == "COMPLETED" and not first["reused"]
    d = Path(first["dir"])
    report = (d / "report.md").read_text()
    for section in ("## A.", "## B.", "## C.", "## D.", "## P.", "## E–N.", "## O.", "## Q.", "## R.", "SYNTHETIC FIXTURE"):
        assert section in report
    ranked = json.loads((d / "stages" / "rank.json").read_text())["payload"]
    data = json.loads((d / "stages" / "data.json").read_text())["payload"]
    assert data["lanes"]["15m"]["decision"] == "RUN" and data["lanes"]["1d"]["decision"] == "RUN"
    assert data["coverage"]["1m"]["permitted"] > 40 and data["coverage"]["1m"]["excluded"]["EXCLUDED_HOLDOUT"] > 0
    cross = json.loads((d / "stages" / "15m" / "cross_tf.json").read_text())["payload"]
    exits = json.loads((d / "stages" / "1d" / "exits.json").read_text())["payload"]
    assert cross["tests"] > 0 and exits["tests"] > 0
    assert ranked["total_discovery_tests"] > 0 and ranked["verdict"]
    sdoc = json.loads((d / "strategies.json").read_text())
    assert all(x["contract"] == "stock-gplm.m54-strategy.v2" and x["editable"] is False for x in sdoc["strategies"])
    lib = json.loads((d / "run-manifest.json").read_text())["library"]
    assert lib and all(not os.access(p, os.W_OK) or os.getuid() == 0 for p in lib)
    second = UnifiedRun(s, "RELIANCE", out, workers=2, log=lambda _m: None).execute()
    assert second["reused"] and second["run_id"] == first["run_id"]
    from stock_gplm.m54.unified_bulk import library_export, library_list
    items = library_list(out, "RELIANCE")
    assert items
    res = library_export(out, items[0]["strategy_id"], "my-copy", tmp_path / "user")
    doc = json.loads(Path(res["exported"]).read_text())
    assert doc["editable"] is True and doc["derived_from"]["strategy_id"] == items[0]["strategy_id"]
    with pytest.raises(SystemExit):
        library_export(out, items[0]["strategy_id"], "my-copy", tmp_path / "user")


def test_stage_checkpoints_resume_after_interruption(fx: dict, tmp_path: Path) -> None:
    s = _settings(fx)
    out = tmp_path / "out"
    run = UnifiedRun(s, "RELIANCE", out, log=lambda _m: None)
    run.load()
    (run.dir / "STOP").write_text("x")
    assert UnifiedRun(s, "RELIANCE", out, log=lambda _m: None).execute()["state"] == "STOPPED"
    (run.dir / "STOP").unlink()
    r2 = UnifiedRun(s, "RELIANCE", out, log=lambda _m: None)
    res = r2.execute()
    assert res["state"] == "COMPLETED"
    (Path(res["dir"]) / "run-manifest.json").unlink()  # lost outputs: every stage is reused, outputs rebuilt
    res3 = UnifiedRun(s, "RELIANCE", out, log=lambda _m: None).execute()
    assert res3["state"] == "COMPLETED" and res3["stages_computed"] == 0 and res3["stages_reused"] > 10
    assert (Path(res["dir"]) / "run-manifest.json").exists()


def test_output_root_inside_raw_is_refused(fx: dict) -> None:
    with pytest.raises(ValueError):
        UnifiedRun(_settings(fx), "RELIANCE", Path(fx["daily_root"]) / "x")


def test_legacy_cli_stays_on_daily_engine() -> None:
    from stock_gplm.m54.cli import _unified, build_parser
    legacy = build_parser().parse_args(["discover", "--stock", "RELIANCE", "--budget", "quick"])
    new = build_parser().parse_args(["discover", "--stock", "RELIANCE", "--data", "all-available", "--timeframes", "auto"])
    assert not _unified(legacy) and _unified(new)
