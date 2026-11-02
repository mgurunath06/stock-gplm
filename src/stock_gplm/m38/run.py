"""M3.8 signal discovery and data-quality audit over development data only.

Development data ends before the locked holdout: every price, feature and label dated on
or after the earliest trained horizon's holdout start is dropped before the panel is built,
and each horizon's frame stops at its last walk-forward test date.

No new predictive model is selected here, and nothing is written to the trial ledger:

- Per-row out-of-sample scores come from replaying the trials already counted for an
  M3.7 experiment, with the same configuration. The replay is checked against the stored
  pooled metrics, so it adds no degrees of freedom.
- Hypotheses are pre-registered, signed, univariate signals evaluated by rank IC. No model
  is fitted to them. They are counted in a separate hypothesis ledger with a Bonferroni
  correction. Promoting any of them into a model would consume research trials.

Prices are never corrected. Suspected unadjusted corporate actions are listed for quarantine.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from ..m3.labels import CONTRACTS_BY_NAME
from ..m35.data import MODEL_FEATURES, build_panel, horizon_frame, load_inputs, static_universe
from ..m35.run import _fit_fold, _partitions
from ..m37.run import RUN_CONTRACT as M37_CONTRACT, _hash, etf_classes, load_policy
from . import analysis as A

M38_VERSION = 2
RUN_CONTRACT = "stock-gplm.m38-signal-diagnostics.v1"
REPLAYED_MODELS = ("heuristic_momentum", "logistic", "lightgbm")
EXPOSURE_COLUMNS = ("volatility_20", "log_median_turnover", "momentum_126", "momentum_21", "momentum_5",
                    "pct_from_high_252", "rsi_14")
# Pre-registered: each signal's sign is fixed before evaluation, so a negative IC counts against it.
HYPOTHESES: dict[str, tuple[str, Callable[[pd.DataFrame], pd.Series]]] = {
    "H1_relative_strength_12_1": ("momentum_252 minus momentum_21 (higher is better)",
                                  lambda f: f["momentum_252"] - f["momentum_21"]),
    "H2_near_52w_high": ("pct_from_high_252 (closer to the high is better)", lambda f: f["pct_from_high_252"]),
    "H3_short_term_reversal": ("-momentum_5 (recent losers rebound)", lambda f: -f["momentum_5"]),
    "H4_volatility_contraction": ("-(volatility_20 / volatility_120)", lambda f: -(f["volatility_20"] / f["volatility_120"])),
    "H5_bollinger_squeeze": ("-bb_bandwidth_20", lambda f: -f["bb_bandwidth_20"]),
    "H6_low_volatility": ("-volatility_60", lambda f: -f["volatility_60"]),
    "H7_up_volume_share": ("share of 20-session volume on up days", lambda f: f["up_volume_share_20"]),
    "H8_volume_confirmed_move": ("volume_ratio_5 x sign(momentum_5)",
                                 lambda f: f["volume_ratio_5"] * np.sign(f["momentum_5"])),
    "H9_illiquidity": ("-log_median_turnover", lambda f: -f["log_median_turnover"]),
    "H10_long_trend": ("close_to_sma_200", lambda f: f["close_to_sma_200"]),
}
REGIMES = ("breadth", "trend", "volatility")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, pd.DataFrame):
        payload = payload.to_csv(index=False, lineterminator="\n")
    data = payload.encode() if isinstance(payload, str) else (
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(data)


def load_experiment(run: Path) -> dict[str, Any]:
    """Verify an M3.7 experiment and return its manifest, policy and heuristic paths."""
    run = Path(run)
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contract") != M37_CONTRACT or manifest.get("experiment_id") != run.name:
        raise ValueError("input is not an M3.7 experiment")
    for name, digest in manifest["outputs"].items():
        if _sha(run / name) != digest:
            raise ValueError(f"M3.7 output does not match its manifest: {name}")
    policy = load_policy(run / "config" / "policy.json")
    if _hash(dict(policy.raw)) != manifest["policy_sha256"]:
        raise ValueError("M3.7 policy copy does not match its binding")
    heuristic = json.loads((run / "config" / "etf-symbol-heuristic.json").read_text(encoding="utf-8"))
    if _hash(heuristic) != manifest["etf_heuristic_sha256"]:
        raise ValueError("M3.7 ETF heuristic copy does not match its binding")
    return {"manifest": manifest, "policy": policy, "metrics": json.loads((run / "metrics.json").read_text())}


def up_volume_share(prices: pd.DataFrame, window: int = 20) -> pd.Series:
    """Causal share of the trailing window's volume traded on sessions that closed up."""
    ordered = prices.sort_values(["symbol", "as_of_date"])
    up = ordered["close"] > ordered.groupby("symbol")["close"].shift(1)
    vol_up = (ordered["volume"] * up).groupby(ordered["symbol"]).rolling(window, min_periods=window).sum()
    vol_all = ordered["volume"].groupby(ordered["symbol"]).rolling(window, min_periods=window).sum()
    share = (vol_up / vol_all.where(vol_all > 0)).reset_index(level=0, drop=True)
    return share.reindex(prices.index)


def regimes(panel: pd.DataFrame) -> pd.DataFrame:
    """Causal per-date market regimes from eligible rows on that date only."""
    eligible = panel[panel["eligible"]]
    by_date = eligible.groupby("as_of_date").agg(
        breadth=("close_to_sma_50", lambda s: float((s > 0).mean())),
        median_momentum_63=("momentum_63", "median"), median_volatility=("volatility_20", "median"))
    by_date = by_date.sort_index()
    trailing = by_date["median_volatility"].rolling(252, min_periods=60).median().shift(1)
    return pd.DataFrame({
        "breadth": np.where(by_date["breadth"] >= 0.5, "BROAD", "NARROW"),
        "trend": np.where(by_date["median_momentum_63"] > 0, "UP", "DOWN"),
        "volatility": np.where(trailing.isna(), "UNDEFINED",
                               np.where(by_date["median_volatility"] > trailing, "HIGH", "LOW")),
    }, index=by_date.index)


def _groups(dates: pd.Index, labels: pd.Series) -> dict[str, pd.Index]:
    aligned = labels.reindex(dates)
    return {str(v): dates[aligned.to_numpy() == v] for v in sorted(aligned.dropna().unique())}


def build_diagnostics(*, m31_run: Path, m32_run: Path, experiment: Path, trial_ledger: Path,
                      output_root: Path) -> Path:
    loaded = load_experiment(experiment)
    manifest, policy = loaded["manifest"], loaded["policy"]
    ledger_before = _sha(Path(trial_ledger)) if Path(trial_ledger).exists() else None
    inputs = load_inputs(m31_run, m32_run, policy.horizons)
    if (inputs.m31_run_id, inputs.m32_run_id) != (manifest["m31_run_id"], manifest["m32_run_id"]):
        raise ValueError("M3.1/M3.2 runs differ from the experiment's binding")
    output_root = Path(output_root).resolve()
    for protected in (inputs.raw_root, Path(m31_run).resolve(), Path(m32_run).resolve(), Path(experiment).resolve()):
        if output_root == protected or protected in output_root.parents:
            raise ValueError(f"output root must be outside {protected}")
    horizons = list(manifest["trained_horizons"])
    cutoff = min(inputs.plans[h].locked_holdout.first_as_of for h in horizons)
    binding = {"contract": RUN_CONTRACT, "m38_version": M38_VERSION, "experiment_id": manifest["experiment_id"],
               "m31_run_id": inputs.m31_run_id, "m32_run_id": inputs.m32_run_id, "inputs": inputs.input_hashes,
               "development_cutoff_exclusive": cutoff, "hypotheses": {k: v[0] for k, v in HYPOTHESES.items()}}
    run_id = _hash(binding)
    final = output_root / run_id
    if final.exists():
        raise FileExistsError(f"M3.8 diagnostics already exist: {run_id}")

    # Holdout-period data never enters any frame: prices, features and labels are cut first.
    inputs.prices = inputs.prices[inputs.prices["as_of_date"] < cutoff].reset_index(drop=True)
    inputs.features = inputs.features[inputs.features["as_of_date"] < cutoff].reset_index(drop=True)
    inputs.flags = inputs.flags[inputs.flags["trading_date"] < cutoff]
    for h, labels in list(inputs.labels.items()):
        plan = inputs.plans[h]
        last = plan.folds[-1].test.last_as_of if plan.folds else None
        inputs.labels[h] = labels[labels["as_of_date"] <= last] if last else labels.iloc[0:0]
    classes, _ = etf_classes(sorted(inputs.coverage["symbol"]), experiment / "config" / "etf-symbol-heuristic.json")
    universe = static_universe(inputs.coverage, policy, classes)
    eligible = set(universe.loc[universe["static_status"] == "ELIGIBLE", "symbol"])
    panel = build_panel(inputs, policy, eligible)
    prices = inputs.prices.copy()
    prices["up_volume_share_20"] = up_volume_share(prices)
    panel = panel.merge(prices[["symbol", "as_of_date", "up_volume_share_20"]], on=["symbol", "as_of_date"],
                        how="left", validate="one_to_one")
    positions = {d: i for i, d in enumerate(inputs.axis.sessions)}
    cost = policy.round_trip_cost()
    top = float(policy.evaluation["top_fraction"])
    regime = regimes(panel)

    # ------------------------------------------------------------------ OHLCV audit (development dates)
    raw_prices = pd.read_csv(Path(m31_run) / "research-dataset.csv",
                             usecols=["symbol", "trading_date", "open", "high", "low", "close", "volume"],
                             dtype={"symbol": str, "trading_date": str})
    raw_prices = raw_prices[raw_prices["trading_date"] < cutoff]
    events = A.classify_large_moves(raw_prices)
    events["instrument_class"] = events["symbol"].map(classes)
    events["in_eligible_universe"] = events["symbol"].isin(eligible)
    hygiene = A.stale_and_volume(raw_prices)
    axis_dev = [d for d in inputs.axis.sessions if d < cutoff]
    first_last = raw_prices.groupby("symbol")["trading_date"].agg(["min", "max", "size"])
    pos_dev = {d: i for i, d in enumerate(axis_dev)}
    hygiene = hygiene.merge(first_last.rename(columns={"min": "first", "max": "last", "size": "_n"}),
                            left_on="symbol", right_index=True)
    hygiene["missing_sessions_within_span"] = [pos_dev[l] - pos_dev[f] + 1 - n for f, l, n in
                                               zip(hygiene["first"], hygiene["last"], hygiene["_n"])]
    hygiene = hygiene.drop(columns="_n")
    hygiene["instrument_class"] = hygiene["symbol"].map(classes)
    quarantine = events[events["classification"] != "UNEXPLAINED_LARGE_MOVE"]
    audit = {
        "development_cutoff_exclusive": cutoff, "candles": int(len(raw_prices)),
        "symbols": int(raw_prices["symbol"].nunique()),
        "large_moves": events["classification"].value_counts().to_dict(),
        "large_moves_in_eligible_equities": events[events["in_eligible_universe"]]["classification"]
        .value_counts().to_dict(),
        "symbols_with_probable_unadjusted_action": int(events.loc[
            events["classification"] == "PROBABLE_UNADJUSTED_CORPORATE_ACTION", "symbol"].nunique()),
        "factor_counts": quarantine["nearest_factor"].value_counts().to_dict(),
        "stale_runs_ge5": int(hygiene["stale_runs"].sum()), "stale_days": int(hygiene["stale_days"].sum()),
        "symbols_with_stale_runs": int((hygiene["stale_runs"] > 0).sum()),
        "zero_volume_days": int(hygiene["zero_volume_days"].sum()),
        "volume_spikes_ge50x": int(hygiene["volume_spikes"].sum()),
        "ohlc_inconsistent": int(hygiene["ohlc_inconsistent"].sum()),
        "missing_sessions_within_span": int(hygiene["missing_sessions_within_span"].sum()),
        "symbols_with_missing_sessions": int((hygiene["missing_sessions_within_span"] > 0).sum()),
        "session_axis_note": "observed-union axis: a session absent for every symbol cannot be detected",
    }

    # ------------------------------------------------------------------ per-horizon replay and decomposition
    results: dict[str, Any] = {}
    hypothesis_rows, feature_rows, importance_rows = [], [], []
    replay_checks = {}
    for horizon in horizons:
        plan = inputs.plans[horizon]
        step = CONTRACTS_BY_NAME[horizon].max_offset
        last_test = plan.folds[-1].test.last_as_of
        frame = horizon_frame(panel[panel["as_of_date"] <= last_test], inputs.labels[horizon], horizon)
        if (frame["as_of_date"] >= cutoff).any():
            raise AssertionError("a holdout-period row reached a diagnostic frame")
        holdout_first = plan.locked_holdout.first_as_of
        embargo = plan.policy.embargo_sessions
        fold_of = pd.Series(index=frame.index, dtype=object)
        for fold in plan.folds:
            fold_of[(frame["as_of_date"] >= fold.test.first_as_of) & (frame["as_of_date"] <= fold.test.last_as_of)] = f"fold{fold.index}"
        frame["fold"] = fold_of
        frame["ca_in_window"] = A.rows_with_event_in_window(frame, quarantine, positions, step).to_numpy()
        pooled: dict[str, pd.DataFrame] = {}
        for name in REPLAYED_MODELS:
            parts = []
            for fold in plan.folds:
                train, validation, test = _partitions(frame, fold, inputs, embargo, holdout_first)
                model, calibrator = _fit_fold(name, policy, train, validation)
                parts.append(test.assign(score=model.score(test)))
                if name == "lightgbm":
                    gains = model.importance()
                    total = sum(gains.values()) or 1.0
                    importance_rows += [{"horizon": horizon, "fold": fold.index, "feature": k, "gain_share": v / total}
                                        for k, v in gains.items()]
            pooled[name] = pd.concat(parts, ignore_index=True)
            stored = loaded["metrics"][horizon]["models"][name]["pooled_out_of_sample"]["auc"]
            replayed = round(float(roc_auc_score(pooled[name]["target"], pooled[name]["score"])), 6)
            replay_checks[f"{horizon}/{name}"] = {"stored_auc": stored, "replayed_auc": replayed,
                                                  "match": abs(replayed - stored) <= 1e-6}
        test_frame = pooled["lightgbm"]
        horizon_out: dict[str, Any] = {
            "development_rows": int(len(frame)), "test_rows": int(len(test_frame)),
            "label_audit_development": A.label_audit(frame, cost),
            "label_audit_test": A.label_audit(test_frame, cost),
            "rows_with_quarantined_event_in_label_window": {
                "development": int(frame["ca_in_window"].sum()), "test": int(test_frame["ca_in_window"].sum()),
                "test_mean_gross_return_flagged": A._round(test_frame.loc[test_frame["ca_in_window"],
                                                                            "gross_return_at_max_offset"].mean()),
                "test_mean_gross_return_unflagged": A._round(test_frame.loc[~test_frame["ca_in_window"],
                                                                              "gross_return_at_max_offset"].mean())},
            "models": {}}
        for name, data in pooled.items():
            summary, periods = A.top_portfolio(data, data["score"].to_numpy(), top_fraction=top, step=step,
                                               positions=positions, round_trip=cost)
            clean = data[~data["ca_in_window"]]
            clean_summary, _ = A.top_portfolio(clean, clean["score"].to_numpy(), top_fraction=top, step=step,
                                               positions=positions, round_trip=cost)
            ranks = data.groupby("as_of_date")["score"].rank(ascending=False, method="first")
            sizes = data.groupby("as_of_date")["score"].transform("size")
            picked = ranks <= np.maximum(1, np.floor(sizes * top))
            horizon_out["models"][name] = {
                "top_decile": summary,
                "top_decile_excluding_quarantined_windows": clean_summary,
                "top_decile_rows_with_quarantined_event": int(data.loc[picked, "ca_in_window"].sum()),
                "deciles": A.decile_returns(data, data["score"].to_numpy()).round(6).to_dict("records"),
                "exposure_percentiles": A.exposures(data, data["score"].to_numpy(), EXPOSURE_COLUMNS, top),
                "regime_gross_excess": {
                    kind: {g: A._round(periods.set_index("as_of_date")["gross_excess"].reindex(idx).dropna().mean())
                           for g, idx in _groups(pd.Index(periods["as_of_date"]), regime[kind]).items()}
                    for kind in REGIMES} if len(periods) else {},
            }
            horizon_out["models"][name]["_periods"] = periods.assign(horizon=horizon, model=name)
        # Features: per-date IC with the forward return, by fold and regime; drift against fold 1 train.
        fold_groups = {f"fold{f.index}": pd.Index([d for d in positions if f.test.first_as_of <= d <= f.test.last_as_of])
                       for f in plan.folds}
        first_train = frame[frame["as_of_date"] <= plan.folds[0].train.last_as_of]
        for feature in MODEL_FEATURES:
            ic = A.daily_ic(frame, feature)
            row = {"horizon": horizon, "feature": feature, **{k: v for k, v in A.ic_summary(
                ic, positions, step, {k: ic.index.intersection(v) for k, v in fold_groups.items()}).items()
                if k != "by_group"}}
            for kind in REGIMES:
                for g, idx in _groups(ic.index, regime[kind]).items():
                    row[f"ic_{kind}_{g}"] = A._round(ic.reindex(idx).mean())
            drifts = [A.psi(first_train[feature].to_numpy(dtype=float),
                            frame.loc[frame["fold"] == f"fold{f.index}", feature].to_numpy(dtype=float))
                      for f in plan.folds]
            drifts = [d for d in drifts if d is not None]
            row["psi_median_vs_fold0_train"] = A._round(np.median(drifts), 4) if drifts else None
            row["psi_max_vs_fold0_train"] = A._round(max(drifts), 4) if drifts else None
            feature_rows.append(row)
        # Hypotheses: pre-registered signed univariate signals; no model is fitted.
        for hid, (definition, build) in HYPOTHESES.items():
            signal = build(frame).replace([np.inf, -np.inf], np.nan)
            work = frame.assign(_signal=signal)
            ic = A.daily_ic(work, "_signal")
            summary = A.ic_summary(ic, positions, step, {k: ic.index.intersection(v) for k, v in fold_groups.items()})
            usable = work.dropna(subset=["_signal"])
            port, _ = A.top_portfolio(usable, usable["_signal"].to_numpy(), top_fraction=top, step=step,
                                      positions=positions, round_trip=cost)
            row = {"horizon": horizon, "hypothesis": hid, "definition": definition, "scope": "ALL",
                   **{k: summary.get(k) for k in ("dates", "mean_ic", "independent_dates", "t", "p_student",
                                                   "enough_independent_dates", "positive_share", "groups_same_sign")},
                   "top_gross_excess": port.get("gross_excess_mean"), "turnover": port.get("turnover_mean"),
                   "top_net_excess_turnover_cost": port.get("net_excess_turnover_cost_mean")}
            hypothesis_rows.append(row)
            for kind in REGIMES:
                for g, idx in _groups(ic.index, regime[kind]).items():
                    if g == "UNDEFINED":
                        continue
                    sub = A.ic_summary(ic.reindex(idx).dropna(), positions, step)
                    hypothesis_rows.append({"horizon": horizon, "hypothesis": hid, "definition": definition,
                                            "scope": f"{kind}={g}", **{k: sub.get(k) for k in (
                                                "dates", "mean_ic", "independent_dates", "t", "p_student",
                                                "enough_independent_dates", "positive_share")}})
        results[horizon] = horizon_out

    tests = len(hypothesis_rows)
    for row in hypothesis_rows:
        p = row.get("p_student")
        row["p_bonferroni"] = None if p is None else A._round(min(1.0, p * tests), 6)
        # A result counts only with enough independent holding periods and after correction for every test run.
        row["significant_after_bonferroni_5pct"] = bool(
            row["p_bonferroni"] is not None and row["p_bonferroni"] < 0.05 and row.get("enough_independent_dates"))
    importance = pd.DataFrame(importance_rows)
    stability = {}
    for horizon, group in importance.groupby("horizon"):
        pivot = group.pivot(index="feature", columns="fold", values="gain_share").fillna(0.0)
        corr = pivot.rank().corr().to_numpy()
        stability[horizon] = {
            "mean_fold_pair_spearman": A._round(corr[np.triu_indices_from(corr, 1)].mean(), 4),
            "top10_by_mean_gain": pivot.mean(axis=1).sort_values(ascending=False).head(10).round(4).to_dict(),
            "features_with_zero_gain_in_every_fold": sorted(pivot.index[(pivot == 0).all(axis=1)]),
        }
    first = horizons[0]
    correlated = A.high_correlations(horizon_frame(panel[panel["as_of_date"] <= inputs.plans[first].folds[-1].test.last_as_of],
                                                   inputs.labels[first], first), list(MODEL_FEATURES), 0.85, 200_000,
                                     policy.seed)

    ledger_after = _sha(Path(trial_ledger)) if Path(trial_ledger).exists() else None
    if ledger_after != ledger_before:
        raise AssertionError("the trial ledger changed during diagnostics")
    output_root.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=output_root))
    try:
        periods = pd.concat([m.pop("_periods") for r in results.values() for m in r["models"].values()],
                            ignore_index=True)
        _write(partial / "decomposition.json", results)
        _write(partial / "portfolio-periods.csv", periods)
        _write(partial / "replay-check.json", replay_checks)
        _write(partial / "ohlcv-audit.json", audit)
        _write(partial / "quarantine-events.csv", events)
        _write(partial / "symbol-hygiene.csv", hygiene)
        _write(partial / "feature-evaluation.csv", pd.DataFrame(feature_rows))
        _write(partial / "feature-importance-by-fold.csv", importance)
        _write(partial / "feature-stability.json", {"importance": stability, "high_correlations_ge_0_85": correlated})
        _write(partial / "hypothesis-ledger.csv", pd.DataFrame(hypothesis_rows))
        _write(partial / "regimes.csv", regime.reset_index().rename(columns={"index": "as_of_date"}))
        outputs = {str(p.relative_to(partial)): _sha(p) for p in sorted(partial.rglob("*")) if p.is_file()}
        _write(partial / "run-manifest.json", {
            **binding, "run_id": run_id, "tag": "DIAGNOSTIC_EXPLORATORY_BIASED", "horizons": horizons,
            "trial_ledger": str(trial_ledger), "trial_ledger_sha256_unchanged": ledger_before,
            "new_model_trials": 0, "replayed_trials_of_experiment": manifest["experiment_id"],
            "replay_verified": all(v["match"] for v in replay_checks.values()),
            "hypothesis_tests": tests, "locked_holdout_accessed": False, "prices_modified": False,
            "gates_modified": False, "labels_modified": False, "outputs": outputs})
        os.rename(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final
