"""Baseline and LightGBM classifiers with validation-fit isotonic calibration.

Each model is fit on a training partition only. Calibration is fit on the
validation partition's predictions. Test and holdout rows are only scored.
Artefacts are JSON or LightGBM text, never pickle, so loading cannot execute code.
"""
from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

from .data import MODEL_FEATURES


class Scorer(Protocol):
    def fit(self, train: pd.DataFrame, validation: pd.DataFrame) -> None: ...
    def score(self, frame: pd.DataFrame) -> np.ndarray: ...
    def artefact(self) -> tuple[str, bytes]: ...


@dataclass
class HeuristicMomentum:
    """Pre-registered, parameter-free: the same-date percentile of one causal feature."""

    feature: str

    def fit(self, train: pd.DataFrame, validation: pd.DataFrame) -> None:
        return None

    def score(self, frame: pd.DataFrame) -> np.ndarray:
        values = frame[self.feature].groupby(frame["as_of_date"]).rank(pct=True)
        return values.fillna(0.5).to_numpy(dtype="float64")

    def artefact(self) -> tuple[str, bytes]:
        spec = {"model": "heuristic_momentum", "feature": self.feature, "missing_score": 0.5}
        return "json", json.dumps(spec, sort_keys=True).encode()


@dataclass
class Logistic:
    """Median imputation and standardisation learned on train, then L2 logistic regression."""

    C: float
    max_iter: int
    seed: int
    state: dict[str, Any] = field(default_factory=dict)

    def _matrix(self, frame: pd.DataFrame) -> np.ndarray:
        x = frame[list(MODEL_FEATURES)].to_numpy(dtype="float64")
        x = np.where(np.isnan(x), self.state["median"], x)
        return (x - self.state["mean"]) / self.state["scale"]

    def fit(self, train: pd.DataFrame, validation: pd.DataFrame) -> None:
        from sklearn.linear_model import LogisticRegression

        raw = train[list(MODEL_FEATURES)].to_numpy(dtype="float64")
        with warnings.catch_warnings():
            # A feature still in warm-up across the whole train span has no median; it becomes 0.
            warnings.simplefilter("ignore", RuntimeWarning)
            median = np.nanmedian(raw, axis=0)
        median = np.where(np.isnan(median), 0.0, median)
        filled = np.where(np.isnan(raw), median, raw)
        mean, scale = filled.mean(axis=0), filled.std(axis=0)
        scale = np.where(scale > 0, scale, 1.0)
        self.state = {"median": median, "mean": mean, "scale": scale}
        model = LogisticRegression(C=self.C, max_iter=self.max_iter, random_state=self.seed)
        model.fit((filled - mean) / scale, train["target"].to_numpy())
        self.state |= {"coef": model.coef_[0], "intercept": float(model.intercept_[0])}

    def score(self, frame: pd.DataFrame) -> np.ndarray:
        z = self._matrix(frame) @ self.state["coef"] + self.state["intercept"]
        return 1.0 / (1.0 + np.exp(-z))

    def artefact(self) -> tuple[str, bytes]:
        spec = {
            "model": "logistic", "features": list(MODEL_FEATURES),
            **{k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in self.state.items()},
        }
        return "json", json.dumps(spec, sort_keys=True).encode()


@dataclass
class LightGBMModel:
    """Gradient-boosted trees with deterministic CPU settings and validation early stopping."""

    params: Mapping[str, Any]
    seed: int
    booster: Any = None

    def fit(self, train: pd.DataFrame, validation: pd.DataFrame) -> None:
        import lightgbm as lgb

        params = {k: v for k, v in self.params.items() if k not in {"n_estimators", "early_stopping_rounds"}}
        params |= {"seed": self.seed, "deterministic": True, "force_row_wise": True, "verbosity": -1,
                   "metric": "binary_logloss"}
        columns = list(MODEL_FEATURES)
        dtrain = lgb.Dataset(train[columns], label=train["target"], free_raw_data=True)
        dvalid = lgb.Dataset(validation[columns], label=validation["target"], reference=dtrain)
        self.booster = lgb.train(
            params, dtrain, num_boost_round=int(self.params["n_estimators"]), valid_sets=[dvalid],
            callbacks=[lgb.early_stopping(int(self.params["early_stopping_rounds"]), verbose=False)],
        )

    def score(self, frame: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.booster.predict(
            frame[list(MODEL_FEATURES)], num_iteration=self.booster.best_iteration,
        ), dtype="float64")

    def artefact(self) -> tuple[str, bytes]:
        return "txt", self.booster.model_to_string(num_iteration=self.booster.best_iteration).encode()

    def importance(self) -> dict[str, float]:
        gains = self.booster.feature_importance(importance_type="gain", iteration=self.booster.best_iteration)
        return dict(sorted(zip(MODEL_FEATURES, map(float, gains)), key=lambda kv: -kv[1]))


def make_model(name: str, config: Mapping[str, Any], seed: int) -> Scorer:
    if name == "heuristic_momentum":
        return HeuristicMomentum(config["feature"])
    if name == "logistic":
        return Logistic(float(config["C"]), int(config["max_iter"]), seed)
    if name == "lightgbm":
        return LightGBMModel(config, seed)
    raise ValueError(f"unknown model {name}")


CALIBRATION_METHODS = {
    "ISOTONIC_FIT_ON_VALIDATION_PARTITION": "isotonic",
    "PLATT_FIT_ON_VALIDATION_PARTITION": "platt",
}


@dataclass
class Calibrator:
    """Map raw scores to the validation outcome frequency.

    Platt (a one-feature logistic fit) is smooth and has two parameters, so it cannot
    chase a sparse tail. Isotonic is kept for comparison; on the 2026-11-02 real-data
    walk-forward its top bins were overconfident out of sample.
    """

    method: str
    model: Any

    @classmethod
    def fit(cls, scores: np.ndarray, outcomes: np.ndarray, method: str = "platt") -> "Calibrator":
        if len(np.unique(outcomes)) < 2:
            raise ValueError("calibration needs both outcomes in the validation partition")
        if method == "isotonic":
            model: Any = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
            model.fit(scores, outcomes)
        elif method == "platt":
            from sklearn.linear_model import LogisticRegression

            model = LogisticRegression(C=1e6, max_iter=1000)
            model.fit(np.asarray(scores, dtype="float64").reshape(-1, 1), outcomes)
        else:
            raise ValueError(f"unknown calibration method {method}")
        return cls(method, model)

    def __call__(self, scores: np.ndarray) -> np.ndarray:
        if self.method == "isotonic":
            values = self.model.predict(scores)
        else:
            values = self.model.predict_proba(np.asarray(scores, dtype="float64").reshape(-1, 1))[:, 1]
        return np.clip(values, 1e-6, 1 - 1e-6)

    def artefact(self) -> bytes:
        if self.method == "isotonic":
            spec = {"method": "isotonic", "x": self.model.X_thresholds_.tolist(), "y": self.model.y_thresholds_.tolist()}
        else:
            spec = {"method": "platt", "slope": float(self.model.coef_[0][0]), "intercept": float(self.model.intercept_[0])}
        return json.dumps(spec, sort_keys=True).encode()
