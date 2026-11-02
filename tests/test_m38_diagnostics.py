"""Synthetic M3.8 tests: diagnostic arithmetic, audit classification, replay and holdout isolation.
They prove contracts only; nothing here says anything about real market behaviour."""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from stock_gplm.m38 import analysis as A
from stock_gplm.m38.run import build_diagnostics
from test_m37_exploratory import _run, setup, upstream  # noqa: F401  (fixtures)


def _prices(closes, volumes, symbol="X-EQ"):
    days = pd.bdate_range("2024-01-01", periods=len(closes)).strftime("%Y-%m-%d")
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"symbol": symbol, "trading_date": days, "open": closes, "high": closes * 1.01,
                         "low": closes * 0.99, "close": closes, "volume": volumes})


def test_unadjusted_split_is_quarantined_and_a_crash_is_not_called_one():
    split = _prices([100.0] * 30 + [20.0] * 30, [1000] * 30 + [5000] * 30, "SPLIT-EQ")
    crash = _prices([100.0] * 30 + [70.0] + [90.0] * 29, [1000] * 60, "CRASH-EQ")
    events = A.classify_large_moves(pd.concat([split, crash]))
    split_events = events[events["symbol"] == "SPLIT-EQ"]
    assert split_events["classification"].tolist() == ["PROBABLE_UNADJUSTED_CORPORATE_ACTION"]
    assert split_events["nearest_factor"].tolist() == ["split_1:5"]
    # The drop and the rebound are both large moves; neither matches a persistent factor step.
    assert set(events.loc[events["symbol"] == "CRASH-EQ", "classification"]) == {"UNEXPLAINED_LARGE_MOVE"}


def test_stale_runs_volume_spikes_and_ohlc_inconsistency_are_counted():
    prices = _prices([10.0] * 6 + list(np.linspace(10.1, 12, 74)), [100] * 79 + [100_000])
    prices.loc[3, "high"] = 5.0
    row = A.stale_and_volume(prices).iloc[0]
    assert row["stale_runs"] == 1 and row["stale_days"] == 6
    assert row["volume_spikes"] == 1 and row["ohlc_inconsistent"] == 1


def test_turnover_cost_and_decomposition_identities():
    days = [f"2024-01-{d:02d}" for d in range(1, 9)]
    positions = {d: i for i, d in enumerate(days)}
    rows = []
    for i, d in enumerate(days):
        for s in range(10):
            # Symbol 9 always scores highest and returns 2%; others 0%, so the benchmark is 0.2%.
            rows.append({"symbol": f"S{s}", "as_of_date": d, "gross_return_at_max_offset": 0.02 if s == 9 else 0.0,
                         "benchmark_return": 0.002, "target": int(s == 9)})
    frame = pd.DataFrame(rows)
    score = frame["symbol"].str[1:].astype(float).to_numpy()
    summary, periods = A.top_portfolio(frame, score, top_fraction=0.1, step=2, positions=positions, round_trip=0.005)
    assert summary["periods"] == 4 and summary["turnover_mean"] == 0.0
    assert summary["gross_excess_mean"] == pytest.approx(0.018)
    assert summary["net_excess_full_cost_mean"] == pytest.approx(0.013)
    # Only the first period trades; later periods hold the same name.
    assert summary["net_excess_turnover_cost_mean"] == pytest.approx(0.018 - 0.005 / 4)
    assert summary["benchmark_minus_bottom_mean"] == pytest.approx(0.002)


def test_label_audit_separates_median_beating_from_beating_the_mean_after_costs():
    returns = [0.0, 0.001, 0.002, 0.003, 0.2]
    frame = pd.DataFrame({"as_of_date": "d", "gross_return_at_max_offset": returns,
                          "benchmark_return": np.mean(returns), "target": [0, 0, 0, 1, 1]})
    audit = A.label_audit(frame, round_trip=0.005)
    assert audit["positive_rate"] == 0.4 and audit["share_beating_mean_after_cost"] == 0.2
    assert audit["positives_not_beating_mean_after_cost"] == 0.5
    assert audit["mean_minus_median_per_date"] > 0


def test_ic_t_statistic_uses_non_overlapping_dates():
    dates = [f"d{i:03d}" for i in range(100)]
    positions = {d: i for i, d in enumerate(dates)}
    ic = pd.Series(np.where(np.arange(100) % 2 == 0, 0.1, -0.1), index=dates)
    summary = A.ic_summary(ic, positions, step=2)
    assert summary["independent_dates"] == 50 and summary["independent_mean_ic"] == pytest.approx(0.1)


def test_events_in_label_window_respect_the_horizon():
    positions = {f"d{i}": i for i in range(10)}
    frame = pd.DataFrame({"symbol": ["A", "A", "A", "B"], "as_of_date": ["d0", "d3", "d5", "d0"]})
    events = pd.DataFrame({"symbol": ["A"], "trading_date": ["d5"]})
    assert A.rows_with_event_in_window(frame, events, positions, 2).tolist() == [False, True, False, False]


def test_diagnostics_replay_counted_trials_without_touching_ledger_or_holdout(setup):  # noqa: F811
    m31, m32, policy, etf, root = setup
    ledger = root / "ledger.jsonl"
    experiment = _run(setup, ledger=ledger)
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    run = build_diagnostics(m31_run=m31, m32_run=m32, experiment=experiment, trial_ledger=ledger,
                            output_root=root / "m38")
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["replay_verified"] is True and manifest["new_model_trials"] == 0
    assert manifest["trial_ledger_sha256_unchanged"] == before == hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert manifest["locked_holdout_accessed"] is False and manifest["prices_modified"] is False
    decomposition = json.loads((run / "decomposition.json").read_text())
    trained = json.loads((experiment / "run-manifest.json").read_text())["trained_horizons"]
    assert set(decomposition) == set(trained)
    hypotheses = pd.read_csv(run / "hypothesis-ledger.csv")
    assert len(hypotheses) == manifest["hypothesis_tests"] and "p_bonferroni" in hypotheses
    # Poisoning every holdout label leaves every diagnostic unchanged.
    holdout = json.loads((m32 / "split-plan.json").read_text())["plans"][0]["locked_holdout"]["first_as_of"]
    labels = pd.read_csv(m32 / "labels.csv", dtype=str, keep_default_na=False)
    mask = (labels["as_of_date"] >= holdout) & (labels["status"] == "COMPLETE")
    labels.loc[mask, "gross_return_at_max_offset"] = "9.99"
    labels.to_csv(m32 / "labels.csv", index=False, lineterminator="\n")
    m32_manifest = json.loads((m32 / "run-manifest.json").read_text())
    m32_manifest["outputs"]["labels.csv"] = hashlib.sha256((m32 / "labels.csv").read_bytes()).hexdigest()
    (m32 / "run-manifest.json").write_text(json.dumps(m32_manifest))
    again = build_diagnostics(m31_run=m31, m32_run=m32, experiment=experiment, trial_ledger=ledger,
                              output_root=root / "m38b")
    for name in ("decomposition.json", "feature-evaluation.csv", "hypothesis-ledger.csv", "ohlcv-audit.json"):
        assert (run / name).read_bytes() == (again / name).read_bytes(), name


def test_small_samples_use_student_t_and_cannot_be_significant():
    dates = [f"d{i:02d}" for i in range(6)]
    ic = pd.Series([0.05, 0.06, 0.04, 0.05, 0.07, 0.05], index=dates)
    summary = A.ic_summary(ic, {d: i for i, d in enumerate(dates)}, step=1)
    assert summary["independent_dates"] == 6 and summary["enough_independent_dates"] is False
    # With 5 degrees of freedom the p-value is far larger than a normal approximation would give.
    assert summary["p_student"] > 2 * (1 - 0.5 * (1 + __import__("math").erf(summary["t"] / 2 ** 0.5)))
