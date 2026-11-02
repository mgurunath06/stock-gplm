"""Synthetic M3.5 tests. They prove the gating, leakage and artefact contracts only;
nothing here says anything about real market behaviour or collector compatibility."""
from __future__ import annotations

import csv
import hashlib
import json
import random
import shutil
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_gplm.m3.research import build_research_run
from stock_gplm.m3.splits import LeakageError, Partition
from stock_gplm.m35.data import (
    build_panel, horizon_frame, load_inputs, raw_acceptance_scoped, select, static_universe,
)
from stock_gplm.m35.policy import evaluate_daily_gates, load_policy
from stock_gplm.m35.run import HOLDOUT_LEDGER, TRIAL_LEDGER, TrialBudgetExceeded, build_training_run

SESSIONS = 680
SYMBOLS = 24
REPO = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sessions() -> list[str]:
    day, out = date(2022, 1, 3), []
    while len(out) < SESSIONS:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _write_m31(root: Path) -> Path:
    """A minimal manifest-bound M3.1 run with one non-accepted segment on a short symbol."""
    run = root / "m31"
    run.mkdir(parents=True)
    rng = random.Random(11)
    sessions = _sessions()
    names = [f"SYN{i:02d}-EQ" for i in range(SYMBOLS)] + ["BADSEG-EQ"]
    with (run / "research-dataset.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["symbol", "trading_date", "source_timestamp_utc", "open", "high", "low", "close",
                         "volume", "source_path", "source_sha256"])
        for name in names:
            close, drift = 100.0 + rng.random() * 50, rng.uniform(-0.0004, 0.0008)
            days = sessions if name != "BADSEG-EQ" else sessions[:100]
            for day in days:
                open_ = close * (1 + rng.gauss(0, 0.004))
                close = max(5.0, open_ * (1 + drift + rng.gauss(0, 0.015)))
                high, low = max(open_, close) * 1.006, min(open_, close) * 0.994
                writer.writerow([name, day, f"{day}T00:00:00Z", f"{open_:.2f}", f"{high:.2f}", f"{low:.2f}",
                                 f"{close:.2f}", rng.randint(200_000, 900_000), f"{name}/seg.json.gz", "0" * 64])
    with (run / "observed-sessions.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["trading_date", "weekday", "symbols_observed"])
        for day in sessions:
            writer.writerow([day, "Mon", len(names)])
    with (run / "symbol-coverage.csv").open("w", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["symbol", "observed_candles", "first_observed_date", "last_observed_date",
                         "non_accepted_segments", "large_close_moves"])
        for name in names:
            count = SESSIONS if name != "BADSEG-EQ" else 100
            writer.writerow([name, count, sessions[0], sessions[count - 1], int(name == "BADSEG-EQ"), 0])
    (run / "ohlcv-flags.csv").write_text("symbol,trading_date,code,value\n")
    (run / "inventory.json").write_text(json.dumps({
        "raw_root": str(root / "raw"), "segments": [],
        "issues": [{"code": "SEGMENT_NOT_ACCEPTED", "path": "BADSEG-EQ/seg.json.gz"}],
    }))
    (run / "coverage-quality.json").write_text(json.dumps({"raw_candle_acceptance": "REVIEW"}))
    outputs = {p.name: _sha(p) for p in sorted(run.iterdir())}
    (run / "run-manifest.json").write_text(json.dumps({
        "contract": "stock-gplm.m3-readiness-run.v1", "run_id": "synthetic-m31",
        "outputs": outputs, "raw_inputs_mutated": False,
    }))
    return run


def _policy(root: Path, **changes) -> Path:
    raw = json.loads((REPO / "config/m35/daily-training-policy.json").read_text())
    raw["horizons"] = ["1-7d"]
    raw["universe"] |= {"minimum_median_turnover_inr": 1_000_000, "minimum_volatility_20": 0.0}
    raw["models"]["lightgbm"] |= {"n_estimators": 40, "num_threads": 2, "min_child_samples": 50}
    raw["models"]["logistic"]["max_iter"] = 100
    for key, value in changes.items():
        raw[key] = value
    path = root / f"policy-{hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:8]}.json"
    path.write_text(json.dumps(raw))
    return path


def _pass_evidence_gates(m32: Path) -> None:
    """Test-only stand-in for supplied calendar, listing, alias and corporate-action evidence."""
    path = m32 / "quality-gates.json"
    gates = json.loads(path.read_text())
    for name in ("authoritative_calendar", "point_in_time_membership", "alias_history", "corporate_action_coverage"):
        gates["gates"][name] = {"status": "PASS"}
    path.write_text(json.dumps(gates))
    manifest = json.loads((m32 / "run-manifest.json").read_text())
    manifest["outputs"]["quality-gates.json"] = _sha(path)
    (m32 / "run-manifest.json").write_text(json.dumps(manifest))


@pytest.fixture(scope="module")
def upstream(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("m35")
    m31 = _write_m31(root)
    return m31, build_research_run(readiness_run=m31, output_root=root / "m32")


@pytest.fixture
def runs(upstream, tmp_path) -> tuple[Path, Path, Path]:
    """Private copies, so tests that forge gates or tamper files stay isolated."""
    m31, m32 = shutil.copytree(upstream[0], tmp_path / "m31"), shutil.copytree(upstream[1], tmp_path / "m32")
    return m31, m32, tmp_path


def test_blocked_evidence_runs_only_exploratory_baselines_and_never_scores_holdout(runs):
    m31, m32, root = runs
    run = build_training_run(m31_run=m31, m32_run=m32, policy_path=_policy(root), output_root=root / "out")
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["training_status"] == {"1-7d": "BLOCKED_EXPLORATORY_BASELINES_ONLY"}
    gates = json.loads((run / "gates.json").read_text())
    assert gates["gates"]["raw_candle_acceptance"]["status"] == "PASS_SCOPED_TO_ELIGIBLE_UNIVERSE"
    assert gates["gates"]["minute_data"]["status"] == "NOT_REQUIRED_FOR_DAILY_ONLY_TRAINING"
    assert set(gates["horizons"]["1-7d"]["blockers"]) == {
        "authoritative_calendar", "point_in_time_membership", "alias_history", "corporate_action_coverage"}
    metrics = json.loads((run / "metrics.json").read_text())["1-7d"]
    assert set(metrics["models"]) == {"heuristic_momentum", "logistic"}
    assert all(m["locked_holdout"] == "NOT_EVALUATED_EXPLORATORY_MODE" for m in metrics["models"].values())
    assert not list(run.rglob("lightgbm*"))
    assert not (root / "out" / HOLDOUT_LEDGER).exists()
    ranking = pd.read_csv(run / "ranking-1-7d.csv")
    assert (ranking["price_date"] == _sessions()[-1]).all()
    assert ranking["status"].str.startswith("EXPLORATORY_BASELINE_GATES_BLOCKED").all()
    assert ranking["probability_outperform_median"].between(0, 1).all()
    assert set(ranking["probability_support"]) <= {"SUPPORTED", "NOT_SUPPORTED_BY_WALK_FORWARD_EVIDENCE"}
    thin = ranking["walk_forward_rows_in_bin"] < 1000
    assert (ranking.loc[thin, "probability_support"] == "NOT_SUPPORTED_BY_WALK_FORWARD_EVIDENCE").all()
    assert json.loads((run / "models/1-7d/logistic.calibration.json").read_text())["method"] == "platt"
    assert "BADSEG-EQ" not in set(ranking["symbol"])
    assert manifest["orders_placed"] is False and manifest["remote_calls"] is False


def test_passing_daily_gates_train_lightgbm_and_consume_the_holdout_once(runs):
    m31, m32, root = runs
    _pass_evidence_gates(m32)
    out = root / "out"
    run = build_training_run(m31_run=m31, m32_run=m32, policy_path=_policy(root), output_root=out)
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["training_status"] == {"1-7d": "MODEL_TRAINED"}
    assert (run / "models/1-7d/lightgbm.txt").read_text().startswith("tree")
    lgbm = json.loads((run / "metrics.json").read_text())["1-7d"]["models"]["lightgbm"]
    assert isinstance(lgbm["locked_holdout"], dict) and lgbm["locked_holdout"]["rows"] > 0
    assert pd.read_csv(run / "ranking-1-7d.csv")["model"].eq("lightgbm").all()
    # A second policy on the same M3.2 run may not look at the holdout again.
    again = build_training_run(m31_run=m31, m32_run=m32, policy_path=_policy(root, seed=8), output_root=out)
    second = json.loads((again / "metrics.json").read_text())["1-7d"]["models"]["lightgbm"]
    assert second["locked_holdout"] == f"ALREADY_CONSUMED_BY_RUN {run.name}"
    assert len((out / HOLDOUT_LEDGER).read_text().splitlines()) == 1


def test_trial_budget_is_reserved_before_fitting_and_enforced(runs):
    m31, m32, root = runs
    out = root / "out"
    tight = json.loads(_policy(root).read_text())
    tight["evaluation"]["trial_budget_per_horizon"] = 3
    path = root / "tight.json"
    path.write_text(json.dumps(tight))
    build_training_run(m31_run=m31, m32_run=m32, policy_path=path, output_root=out)
    tight["seed"] = 9
    path.write_text(json.dumps(tight))
    with pytest.raises(TrialBudgetExceeded):
        build_training_run(m31_run=m31, m32_run=m32, policy_path=path, output_root=out)
    assert len((out / TRIAL_LEDGER).read_text().splitlines()) == 2


def test_rerun_is_refused_and_a_fresh_root_reproduces_identical_bytes(runs):
    m31, m32, root = runs
    policy = _policy(root)
    first = build_training_run(m31_run=m31, m32_run=m32, policy_path=policy, output_root=root / "a")
    with pytest.raises(FileExistsError):
        build_training_run(m31_run=m31, m32_run=m32, policy_path=policy, output_root=root / "a")
    second = build_training_run(m31_run=m31, m32_run=m32, policy_path=policy, output_root=root / "b")
    assert first.name == second.name
    assert (first / "run-manifest.json").read_bytes() == (second / "run-manifest.json").read_bytes()


def test_tampered_upstream_output_and_unbound_runs_are_refused(runs):
    m31, m32, root = runs
    with (m32 / "labels.csv").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="does not match its manifest"):
        build_training_run(m31_run=m31, m32_run=m32, policy_path=_policy(root), output_root=root / "out")


def test_output_inside_upstream_or_raw_tree_is_refused(runs):
    m31, m32, root = runs
    with pytest.raises(ValueError, match="outside"):
        build_training_run(m31_run=m31, m32_run=m32, policy_path=_policy(root), output_root=m32 / "nested")
    raw_root = Path(json.loads((m31 / "inventory.json").read_text())["raw_root"])
    with pytest.raises(ValueError, match="outside"):
        build_training_run(m31_run=m31, m32_run=m32, policy_path=_policy(root), output_root=raw_root / "x")


def test_panel_rows_do_not_change_when_later_candles_are_removed(runs):
    m31, m32, root = runs
    policy = load_policy(_policy(root))
    inputs = load_inputs(m31, m32, policy.horizons)
    eligible = set(static_universe(inputs.coverage, policy).query("static_status == 'ELIGIBLE'")["symbol"])
    full = build_panel(inputs, policy, eligible)
    cut = inputs.axis.sessions[400]
    inputs.prices = inputs.prices[inputs.prices["as_of_date"] <= cut]
    inputs.features = inputs.features[inputs.features["as_of_date"] <= cut].reset_index(drop=True)
    short = build_panel(inputs, policy, eligible)
    columns = [c for c in short.columns if c not in {"flags_cum"}]
    left = full[full["as_of_date"] <= cut][columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(left, short[columns].reset_index(drop=True))


def test_target_is_benchmark_relative_and_only_complete_labels_enter(runs):
    m31, m32, root = runs
    policy = load_policy(_policy(root))
    inputs = load_inputs(m31, m32, policy.horizons)
    eligible = set(static_universe(inputs.coverage, policy).query("static_status == 'ELIGIBLE'")["symbol"])
    frame = horizon_frame(build_panel(inputs, policy, eligible), inputs.labels["1-7d"], "1-7d")
    assert (frame["status"] == "COMPLETE").all()
    day = frame[frame["as_of_date"] == frame["as_of_date"].iloc[0]]
    median = day["gross_return_at_max_offset"].median()
    assert ((day["gross_return_at_max_offset"] > median).astype("int8") == day["target"]).all()
    late = frame["as_of_date"].max()
    with pytest.raises(LeakageError):
        select(frame, Partition(frame["as_of_date"].min(), late), next_first=late, axis=inputs.axis, embargo=5)


def test_gate_scoping_cannot_pass_reference_evidence_or_hide_other_issues(runs):
    m31, m32, root = runs
    policy = load_policy(_policy(root))
    inputs = load_inputs(m31, m32, policy.horizons)
    assert raw_acceptance_scoped(inputs, {"SYN00-EQ"})
    assert not raw_acceptance_scoped(inputs, {"BADSEG-EQ"})
    inputs.m31_issues.append({"code": "SOMETHING_ELSE", "path": "X/y"})
    assert not raw_acceptance_scoped(inputs, {"SYN00-EQ"})
    blocked = {name: {"status": "BLOCKED", "blocker": "x"} for name in (
        "raw_candle_acceptance", "authoritative_calendar", "point_in_time_membership",
        "alias_history", "corporate_action_coverage", "minute_data")}
    report = evaluate_daily_gates(blocked, scoped_raw_acceptance=True, folds={"1-7d": 2}, horizons=("1-7d",))
    assert report["horizons"]["1-7d"]["mode"] == "EXPLORATORY_BASELINE_ONLY"
    assert "FEWER_THAN_3_WALK_FORWARD_FOLDS" in report["horizons"]["1-7d"]["blockers"]


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p["exploratory_models"].append("lightgbm"), "LightGBM may not run"),
    (lambda p: p.__setitem__("horizons", ["intraday"]), "not a daily"),
    (lambda p: p["costs"]["buy_bps"].__setitem__("stt", -1), "nonnegative"),
    (lambda p: p["evaluation"].__setitem__("trial_budget_per_horizon", 0), "trial budget"),
])
def test_invalid_policies_are_refused(tmp_path, mutate, message):
    raw = json.loads((REPO / "config/m35/daily-training-policy.json").read_text())
    mutate(raw)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=message):
        load_policy(path)


def test_round_trip_cost_matches_the_policy_components():
    policy = load_policy(REPO / "config/m35/daily-training-policy.json")
    costs = policy.raw["costs"]
    expected = (sum(costs["buy_bps"].values()) + sum(costs["sell_bps"].values())) / 1e4
    assert policy.round_trip_cost() == pytest.approx(expected)
    assert 0.004 < policy.round_trip_cost() < 0.01
