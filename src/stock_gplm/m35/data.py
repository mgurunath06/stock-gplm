"""Manifest-verified M3.1/M3.2 inputs, the eligible universe and per-horizon model frames.

Every value at as-of session `t` uses candles at or before `t`. Universe filters
are causal (trailing turnover, trailing flags) except one documented train-only
filter: a training row whose label window contains a flagged >=20% close move is
dropped, because that label may be a corporate-action artefact. Validation, test
and holdout rows are never filtered on future information.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..m3.evidence import SessionAxis
from ..m3.features import FEATURE_NAMES
from ..m3.labels import CONTRACTS_BY_NAME
from ..m3.research import _stream_sha256
from ..m3.splits import (
    Fold, LeakageError, Partition, WalkForwardPlan, build_walk_forward, verify_plan,
)
from .policy import TrainingPolicy

M31_CONTRACT = "stock-gplm.m3-readiness-run.v1"
M32_CONTRACT = "stock-gplm.m32-research-run.v1"
M31_USED = (
    "research-dataset.csv", "observed-sessions.csv", "symbol-coverage.csv", "ohlcv-flags.csv",
    "inventory.json", "coverage-quality.json",
)
M32_USED = ("features.csv", "labels.csv", "split-plan.json", "quality-gates.json")
# Written by M3.2 only when evidence exists; verified against the manifest whenever listed.
M32_OPTIONAL = ("session-axis.csv", "instrument-classes.csv")
# Price-level columns are in rupees and not comparable across symbols; they stay out of models.
PRICE_LEVEL = frozenset({
    "sma_5", "sma_10", "sma_20", "sma_50", "sma_100", "sma_200",
    "ema_9", "ema_20", "ema_50", "ema_100", "ema_200",
    "macd_line", "macd_signal", "macd_histogram", "atr_14",
    "bb_mid_20", "bb_upper_20", "bb_lower_20",
})
SCALE_FREE = tuple(name for name in FEATURE_NAMES if name not in PRICE_LEVEL)
DERIVED = ("macd_line_pct", "macd_histogram_pct", "log_median_turnover")
RANKED_SOURCES = ("momentum_5", "momentum_21", "momentum_126", "volatility_20", "volume_ratio_20", "rsi_14")
RANKED = tuple(f"xs_rank_{name}" for name in RANKED_SOURCES)
MODEL_FEATURES = (*SCALE_FREE, *DERIVED, *RANKED)
MINIMUM_ROWS_PER_DATE = 20


@dataclass
class Inputs:
    """Verified upstream artefacts. Frames are read-only by convention."""

    m31_run_id: str
    m32_run_id: str
    axis: SessionAxis
    plans: dict[str, WalkForwardPlan]
    m32_gates: Mapping[str, Any]
    coverage: pd.DataFrame
    prices: pd.DataFrame
    flags: pd.DataFrame
    features: pd.DataFrame
    labels: dict[str, pd.DataFrame]
    input_hashes: dict[str, str]
    m31_issues: list[dict[str, Any]]
    m31_raw_acceptance: str
    raw_root: Path
    instrument_classes: dict[str, str] | None = None


def _manifest(run: Path, contract: str, used: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, str]]:
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contract") != contract:
        raise ValueError(f"{run} is not a {contract} run")
    hashes: dict[str, str] = {}
    for name in used:
        expected = manifest.get("outputs", {}).get(name)
        if expected is None:
            raise ValueError(f"{run.name} lacks manifest-bound {name}")
        if _stream_sha256(run / name) != expected:
            raise ValueError(f"{run.name}/{name} does not match its manifest")
        hashes[name] = expected
    return manifest, hashes


def _plan_from_split(axis: SessionAxis, horizon: str, stored: Mapping[str, Any]) -> WalkForwardPlan:
    """Rebuild the M3.2 plan and require it to equal the stored one, then verify boundaries."""
    plan = build_walk_forward(axis, CONTRACTS_BY_NAME[horizon])
    if json.loads(json.dumps(plan.to_dict())) != stored:
        raise LeakageError(f"stored split plan for {horizon} differs from its rebuilt plan")
    verify_plan(plan, axis)
    return plan


def load_inputs(m31_run: Path, m32_run: Path, horizons: tuple[str, ...]) -> Inputs:
    """Read only manifest-verified outputs and require the M3.2 run to bind the M3.1 run."""
    m31_run, m32_run = Path(m31_run).resolve(), Path(m32_run).resolve()
    m31, m31_hashes = _manifest(m31_run, M31_CONTRACT, M31_USED)
    m32, m32_hashes = _manifest(m32_run, M32_CONTRACT, M32_USED)
    present = tuple(name for name in M32_OPTIONAL if name in m32.get("outputs", {}))
    m32_hashes |= _manifest(m32_run, M32_CONTRACT, present)[1]
    if m31.get("raw_inputs_mutated") is not False or m32.get("raw_inputs_read") is not False:
        raise ValueError("upstream runs do not attest untouched raw inputs")
    if m32.get("readiness_run_id") != m31["run_id"]:
        raise ValueError("M3.2 run was not built from this M3.1 run")
    if m32.get("readiness_outputs") != m31["outputs"]:
        raise ValueError("M3.2 run binds different M3.1 outputs")
    if "session-axis.csv" in present:
        sessions = pd.read_csv(m32_run / "session-axis.csv", dtype=str)["trading_date"]
    elif m32["session_source"] == "EMPIRICAL_UNION":
        sessions = pd.read_csv(m31_run / "observed-sessions.csv", dtype=str)["trading_date"]
    else:
        raise ValueError("M3.2 run uses an authoritative calendar but does not bind its session axis")
    axis = SessionAxis(tuple(sessions), m32["session_source"])
    classes = None
    if "instrument-classes.csv" in present:
        frame = pd.read_csv(m32_run / "instrument-classes.csv", dtype=str, keep_default_na=False)
        classes = dict(zip(frame["symbol"], frame["instrument_class"]))
    split = json.loads((m32_run / "split-plan.json").read_text(encoding="utf-8"))
    stored = {item["horizon"]: item for item in split["plans"]}
    plans = {h: _plan_from_split(axis, h, stored[h]) for h in horizons}
    gates = json.loads((m32_run / "quality-gates.json").read_text(encoding="utf-8"))
    coverage = pd.read_csv(m31_run / "symbol-coverage.csv", dtype={"symbol": str})
    prices = pd.read_csv(
        m31_run / "research-dataset.csv",
        usecols=["symbol", "trading_date", "open", "close", "volume"],
        dtype={"symbol": str, "trading_date": str, "open": "float64", "close": "float64", "volume": "float64"},
    ).rename(columns={"trading_date": "as_of_date"})
    flags = pd.read_csv(m31_run / "ohlcv-flags.csv", dtype=str)
    dtypes: dict[str, Any] = {name: "float32" for name in FEATURE_NAMES}
    dtypes.update(symbol=str, as_of_date=str)
    features = pd.read_csv(
        m32_run / "features.csv", usecols=["symbol", "as_of_date", *FEATURE_NAMES], dtype=dtypes,
    )
    labels: dict[str, pd.DataFrame] = {}
    columns = ["symbol", "as_of_date", "horizon", "status", "label_available_date", "gross_return_at_max_offset"]
    parts: dict[str, list[pd.DataFrame]] = {h: [] for h in horizons}
    for chunk in pd.read_csv(m32_run / "labels.csv", usecols=columns, dtype=str, chunksize=2_000_000):
        for horizon in horizons:
            parts[horizon].append(chunk[chunk["horizon"] == horizon])
    for horizon in horizons:
        frame = pd.concat(parts[horizon], ignore_index=True)
        frame["gross_return_at_max_offset"] = pd.to_numeric(frame["gross_return_at_max_offset"])
        labels[horizon] = frame.drop(columns="horizon")
    inventory = json.loads((m31_run / "inventory.json").read_text(encoding="utf-8"))
    quality = json.loads((m31_run / "coverage-quality.json").read_text(encoding="utf-8"))
    hashes = {f"m31/{k}": v for k, v in m31_hashes.items()} | {f"m32/{k}": v for k, v in m32_hashes.items()}
    return Inputs(
        m31["run_id"], m32["run_id"], axis, plans, gates["gates"], coverage, prices, flags,
        features, labels, hashes, list(inventory["issues"]), quality["raw_candle_acceptance"],
        Path(inventory["raw_root"]).resolve(), classes,
    )


def static_universe(
    coverage: pd.DataFrame, policy: TrainingPolicy, classes: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Per-symbol data-quality and instrument-class eligibility; uses no return information.

    With classification evidence, only the policy's classes stay (ordinary equities by default):
    ETFs and symbols of UNKNOWN class are excluded. Without it nothing is removed, and the
    instrument-classification gate keeps the run exploratory.
    """
    minimum = int(policy.universe["minimum_observations"])
    frame = coverage[["symbol", "observed_candles", "non_accepted_segments", "first_observed_date",
                      "last_observed_date", "large_close_moves"]].copy()
    reasons = np.where(
        frame["observed_candles"] <= 0, "NO_VALIDATED_CANDLES",
        np.where(frame["non_accepted_segments"] > 0, "NON_ACCEPTED_SEGMENT",
                 np.where(frame["observed_candles"] < minimum, f"FEWER_THAN_{minimum}_OBSERVATIONS", "ELIGIBLE")),
    )
    if classes is not None:
        frame["instrument_class"] = frame["symbol"].map(classes).fillna("UNKNOWN")
        excluded = (reasons == "ELIGIBLE") & ~frame["instrument_class"].isin(policy.instrument_classes)
        reasons = np.where(excluded, "EXCLUDED_INSTRUMENT_CLASS_" + frame["instrument_class"], reasons)
    else:
        frame["instrument_class"] = "NOT_ESTABLISHED"
    frame["static_status"] = reasons
    return frame.sort_values("symbol", ignore_index=True)


def raw_acceptance_scoped(inputs: Inputs, eligible: set[str]) -> bool:
    """True when M3.1 accepted everything, or every M3.1 issue is a non-accepted
    segment of a symbol outside the eligible universe. Any other issue keeps the gate blocked."""
    if inputs.m31_raw_acceptance == "ACCEPT":
        return True
    for issue in inputs.m31_issues:
        if issue.get("code") != "SEGMENT_NOT_ACCEPTED":
            return False
        if issue.get("path", "").split("/", 1)[0] in eligible:
            return False
    return True


def build_panel(inputs: Inputs, policy: TrainingPolicy, eligible_symbols: set[str]) -> pd.DataFrame:
    """Join features with causal liquidity and flag state; add scale-free derived columns."""
    rules = policy.universe
    prices = inputs.prices.sort_values(["symbol", "as_of_date"], ignore_index=True)
    prices["obs"] = prices.groupby("symbol").cumcount()
    prices["turnover"] = prices["close"] * prices["volume"]
    window = int(rules["turnover_window_observations"])
    prices["median_turnover"] = (
        prices.groupby("symbol")["turnover"].rolling(window, min_periods=window).median()
        .reset_index(level=0, drop=True)
    )
    moves = inputs.flags[inputs.flags["code"] == "LARGE_CLOSE_MOVE"][["symbol", "trading_date"]]
    moves = moves.rename(columns={"trading_date": "as_of_date"}).assign(flag=1)
    prices = prices.merge(moves, on=["symbol", "as_of_date"], how="left")
    prices["flag"] = prices["flag"].fillna(0).astype("int32")
    prices["flags_cum"] = prices.groupby("symbol")["flag"].cumsum()
    lookback = int(rules["flag_lookback_observations"])
    earlier = prices.groupby("symbol")["flags_cum"].shift(lookback).fillna(0)
    prices["flag_in_lookback"] = (prices["flags_cum"] - earlier) > 0
    panel = inputs.features.merge(
        prices[["symbol", "as_of_date", "obs", "close", "median_turnover", "flags_cum", "flag_in_lookback"]],
        on=["symbol", "as_of_date"], how="left", validate="one_to_one",
    )
    if panel["close"].isna().any():
        raise ValueError("a feature row has no matching validated candle")
    panel["macd_line_pct"] = (inputs.features["macd_line"] / panel["close"]).astype("float32")
    panel["macd_histogram_pct"] = (inputs.features["macd_histogram"] / panel["close"]).astype("float32")
    panel["log_median_turnover"] = np.log(panel["median_turnover"].where(panel["median_turnover"] > 0)).astype("float32")
    panel["static_eligible"] = panel["symbol"].isin(eligible_symbols)
    panel["liquid"] = (
        (panel["median_turnover"] >= float(rules["minimum_median_turnover_inr"]))
        & (panel["close"] >= float(rules["minimum_close_inr"]))
        # Near-constant prices (e.g. money-market units listed as EQ) carry no equity risk.
        & (panel["volatility_20"] >= float(rules["minimum_volatility_20"]))
    )
    panel["eligible"] = panel["static_eligible"] & panel["liquid"] & ~panel["flag_in_lookback"]
    for source, name in zip(RANKED_SOURCES, RANKED):
        values = panel[source].where(panel["eligible"])
        panel[name] = values.groupby(panel["as_of_date"]).rank(pct=True).astype("float32")
    return panel.drop(columns=[name for name in PRICE_LEVEL])


def horizon_frame(panel: pd.DataFrame, labels: pd.DataFrame, horizon: str) -> pd.DataFrame:
    """Eligible rows with COMPLETE labels and the benchmark-relative binary target.

    The target is 1 when the gross return to the bucket's last session exceeds the
    median gross return of eligible COMPLETE rows sharing the same as-of date.
    """
    max_offset = CONTRACTS_BY_NAME[horizon].max_offset
    frame = panel[panel["eligible"]].merge(labels, on=["symbol", "as_of_date"], how="left", validate="one_to_one")
    frame = frame[frame["status"] == "COMPLETE"].copy()
    future = panel[["symbol", "obs", "flags_cum"]].copy()
    future["obs"] -= max_offset
    frame = frame.merge(future.rename(columns={"flags_cum": "flags_cum_exit"}), on=["symbol", "obs"], how="left")
    frame["flag_in_label_window"] = (frame["flags_cum_exit"].fillna(frame["flags_cum"]) - frame["flags_cum"]) > 0
    counts = frame.groupby("as_of_date")["symbol"].transform("size")
    frame = frame[counts >= MINIMUM_ROWS_PER_DATE].copy()
    median = frame.groupby("as_of_date")["gross_return_at_max_offset"].transform("median")
    frame["benchmark_return"] = frame.groupby("as_of_date")["gross_return_at_max_offset"].transform("mean")
    frame["target"] = (frame["gross_return_at_max_offset"] > median).astype("int8")
    return frame.sort_values(["as_of_date", "symbol"], ignore_index=True)


def select(
    frame: pd.DataFrame, partition: Partition, *, next_first: str | None, axis: SessionAxis,
    embargo: int,
) -> pd.DataFrame:
    """Rows in a partition; any label resolving inside the next boundary raises LeakageError."""
    rows = frame[(frame["as_of_date"] >= partition.first_as_of) & (frame["as_of_date"] <= partition.last_as_of)]
    if next_first is not None and len(rows):
        latest = rows["label_available_date"].max()
        if axis.position(next_first) - axis.position(latest) <= embargo:
            raise LeakageError(f"label resolving {latest} crosses the boundary before {next_first}")
    return rows


def final_fold(plan: WalkForwardPlan, axis: SessionAxis) -> Fold:
    """Train on everything purged before the last test span, calibrate on it, test on the holdout."""
    if not plan.folds or plan.locked_holdout is None:
        raise ValueError("a final fold needs walk-forward folds and a locked holdout")
    last = plan.folds[-1]
    separation = plan.purge_sessions + plan.policy.embargo_sessions + 1
    train_last = axis.sessions[axis.position(last.test.first_as_of) - separation]
    return Fold(
        len(plan.folds), Partition(axis.sessions[0], train_last), last.test, plan.locked_holdout,
    )
