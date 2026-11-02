"""Synthetic M3.7 tests: holdout isolation, shared trial budget, ETF separation and artefacts.
They prove contracts only; nothing here says anything about real market behaviour."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from stock_gplm.m3.research import build_research_run
from stock_gplm.m37.run import TAG, TrialBudgetExceeded, build_exploratory_run, load_policy
from test_m35_training import _write_m31

REPO = Path(__file__).resolve().parents[1]
ETF = "SYN23-EQ"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def upstream(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("m37")
    m31 = _write_m31(root)
    return m31, build_research_run(readiness_run=m31, output_root=root / "m32")


@pytest.fixture
def setup(upstream, tmp_path):
    m31, m32 = shutil.copytree(upstream[0], tmp_path / "m31"), shutil.copytree(upstream[1], tmp_path / "m32")
    raw = json.loads((REPO / "config/m37/exploratory-biased-policy.json").read_text())
    raw["universe"] |= {"minimum_median_turnover_inr": 1_000_000, "minimum_volatility_20": 0.0}
    raw["models"]["lightgbm"] |= {"n_estimators": 40, "num_threads": 2, "min_child_samples": 50}
    raw["models"]["logistic"]["max_iter"] = 100
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(raw))
    heuristic = json.loads((REPO / "config/m37/etf-symbol-heuristic.json").read_text())
    heuristic["etf_patterns"] = ["^SYN23$"]
    etf = tmp_path / "etf.json"
    etf.write_text(json.dumps(heuristic))
    return m31, m32, policy, etf, tmp_path


def _run(setup, ledger: Path | None = None, out: str = "out", m32: Path | None = None) -> Path:
    m31, m32_default, policy, etf, root = setup
    return build_exploratory_run(m31_run=m31, m32_run=m32 or m32_default, policy_path=policy, etf_heuristic=etf,
                                 trial_ledger=ledger or root / "ledger.jsonl", output_root=root / out)


def test_trains_lightgbm_with_baselines_and_saves_tagged_artefacts(setup):
    run = _run(setup)
    manifest = json.loads((run / "run-manifest.json").read_text())
    assert manifest["tag"] == TAG and manifest["locked_holdout_accessed"] is False
    assert manifest["rankings_written"] is False and manifest["promotion"].startswith("NEVER")
    assert "1-7d" in manifest["trained_horizons"] and "6-12m" in manifest["skipped_horizons"]
    metrics = json.loads((run / "metrics.json").read_text())
    for horizon in manifest["trained_horizons"]:
        assert set(metrics[horizon]["models"]) == {"heuristic_momentum", "logistic", "lightgbm"}
        pooled = metrics[horizon]["models"]["lightgbm"]["pooled_out_of_sample"]
        assert pooled["portfolio"]["max_drawdown_net"] <= 0 and pooled["rows"] > 0
        assert (run / f"models/{horizon}").is_dir()
        assert next((run / f"models/{horizon}").glob("lightgbm.fold*.txt")).read_text().startswith("tree")
        assert metrics[horizon]["locked_holdout"]["status"] == "NOT_ACCESSED"
    for name in ("feature-schema.json", "dataset-fingerprints.json", "config/policy.json", "gates-disclosure.json"):
        assert name in manifest["outputs"]
    universe = pd.read_csv(run / "universe.csv").set_index("symbol")
    assert universe.loc[ETF, "static_status"] == "EXCLUDED_INSTRUMENT_CLASS_ETF"
    assert pd.read_csv(run / "etf-separated.csv")["symbol"].tolist() == [ETF]
    gates = json.loads((run / "gates-disclosure.json").read_text())
    assert gates["gates"]["authoritative_calendar"]["status"] == "BLOCKED"


def test_holdout_labels_are_never_read(setup):
    m31, m32, policy, etf, root = setup
    first = _run(setup)
    holdout = json.loads((m32 / "split-plan.json").read_text())["plans"][0]["locked_holdout"]["first_as_of"]
    labels = pd.read_csv(m32 / "labels.csv", dtype=str, keep_default_na=False)
    poisoned = labels["as_of_date"] >= holdout
    assert poisoned.any()
    labels.loc[poisoned & (labels["status"] == "COMPLETE"), "gross_return_at_max_offset"] = "9.99"
    labels.to_csv(m32 / "labels.csv", index=False, lineterminator="\n")
    manifest = json.loads((m32 / "run-manifest.json").read_text())
    manifest["outputs"]["labels.csv"] = _sha(m32 / "labels.csv")
    (m32 / "run-manifest.json").write_text(json.dumps(manifest))
    second = _run(setup, ledger=root / "ledger2.jsonl", out="out2")
    assert first.name != second.name
    assert (first / "metrics.json").read_bytes() == (second / "metrics.json").read_bytes()
    for horizon, item in json.loads((first / "dataset-fingerprints.json").read_text())["horizons"].items():
        assert item["last_as_of"] < holdout, horizon


def test_shared_budget_counts_prior_trials_and_refuses_to_exceed(setup):
    root = setup[4]
    ledger = root / "ledger.jsonl"
    prior = [{"horizon": "1-7d", "model": "logistic", "trial": i + 1, "m32_run_id": "earlier", "run_id": "m35"}
             for i in range(18)]
    ledger.write_text("".join(json.dumps(e) + "\n" for e in prior))
    with pytest.raises(TrialBudgetExceeded, match="18 trials used"):
        _run(setup, ledger=ledger)
    assert len(ledger.read_text().splitlines()) == 18
    ledger.write_text("".join(json.dumps(e) + "\n" for e in prior[:6]))
    run = _run(setup, ledger=ledger)
    trials = [e for e in map(json.loads, ledger.read_text().splitlines()) if e.get("pipeline") == "m37"]
    assert [e["trial"] for e in trials if e["horizon"] == "1-7d"] == [7, 8, 9]
    assert {e["experiment_id"] for e in trials} == {run.name}
    with pytest.raises(FileExistsError):
        _run(setup, ledger=ledger)


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p["evaluation"].__setitem__("trial_budget_per_horizon", 40), "fixed at 20"),
    (lambda p: p["universe"].__setitem__("instrument_classes", ["EQUITY", "ETF"]), "ETFs stay separate"),
    (lambda p: p.__setitem__("horizons", ["intraday"]), "daily horizons only"),
    (lambda p: p.__setitem__("tag", "PRODUCTION"), "contract"),
])
def test_policy_refuses_budget_class_horizon_and_tag_changes(tmp_path, mutate, message):
    raw = json.loads((REPO / "config/m37/exploratory-biased-policy.json").read_text())
    mutate(raw)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=message):
        load_policy(path)
