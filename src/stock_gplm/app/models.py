"""Direction models, preprocessing, calibration and model persistence.

Models predict P(net forward return > 0) for one lane and horizon. Kinds:

* ``baseline``  the recency-weighted base rate of up outcomes (the bar every model must beat);
* ``logit``     L2 logistic regression (Adam, full batch; GPU when the backend is CUDA);
* ``mlp``       two hidden ReLU layers (64, 32), L2, Adam; warm-started between rolling steps and
                fully re-fitted every ``mlp_retrain_every`` steps.

Preprocessing is fitted on the training rows only: features with more than 40% missing training
values are dropped, the rest are standardised with recency-weighted means and deviations, and a
remaining missing value becomes 0 (the training mean). Raw probabilities are never presented as
proven confidence: :class:`PlattCalibrator` maps them using *earlier sequential out-of-sample*
outcomes only, and reports ``UNCALIBRATED`` until enough outcomes exist.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .compute import Backend, predict_network, train_network

MAX_MISSING = 0.4


@dataclass
class Prep:
    columns: np.ndarray  # indices into the lane feature matrix
    mean: np.ndarray
    std: np.ndarray

    @staticmethod
    def fit(X: np.ndarray, w: np.ndarray) -> "Prep":
        miss = np.isnan(X).mean(axis=0)
        keep = np.nonzero(miss <= MAX_MISSING)[0]
        Z = X[:, keep]
        wn = w / max(w.sum(), 1e-12)
        mask = ~np.isnan(Z)
        wsum = (mask * wn[:, None]).sum(axis=0)
        mean = np.where(wsum > 0, np.nansum(Z * wn[:, None], axis=0) / np.maximum(wsum, 1e-12), 0.0)
        var = np.where(wsum > 0, np.nansum(((Z - mean) ** 2) * wn[:, None], axis=0) / np.maximum(wsum, 1e-12), 0.0)
        std = np.sqrt(var)
        good = std > 1e-12
        return Prep(keep[good], mean[good], std[good])

    def apply(self, X: np.ndarray) -> np.ndarray:
        Z = (X[:, self.columns] - self.mean) / self.std
        Z = np.nan_to_num(Z, nan=0.0, posinf=0.0, neginf=0.0)
        return np.clip(Z, -8, 8)


@dataclass
class DirectionModel:
    kind: str
    horizon: int
    prep: Prep | None = None
    params: list[np.ndarray] = field(default_factory=list)
    base_rate: float = 0.5
    train_rows: int = 0
    steps_since_full: int = 0
    trained_label: str = ""

    def fit(self, backend: Backend, X: np.ndarray, y: np.ndarray, w: np.ndarray, cfg: Mapping[str, Any], seed: int,
            full: bool, on_epoch: Callable[[int, float, float], None] | None = None) -> dict[str, Any]:
        self.train_rows = int(len(y))
        self.base_rate = float((w * y).sum() / max(w.sum(), 1e-12))
        if self.kind == "baseline":
            return {"epochs": 0, "loss": None, "train_acc": max(self.base_rate, 1 - self.base_rate)}
        refit = full or self.prep is None or not self.params
        if refit:
            self.prep = Prep.fit(X, w)
        Z = self.prep.apply(X)  # type: ignore[union-attr]
        if self.kind == "logit":
            c = cfg["logit"]
            hidden: tuple[int, ...] = ()
            epochs, lr, l2 = int(c["epochs"]), float(c["lr"]), float(c["l2"])
            init = None if refit else self.params
            if init is not None:
                epochs = max(20, epochs // 4)
        elif self.kind == "mlp":
            c = cfg["mlp"]
            hidden = tuple(int(h) for h in c["hidden"])
            epochs = int(cfg["_epochs_full"] if refit else cfg["_epochs_update"])
            lr, l2 = float(c["lr"]), float(c["l2"])
            init = None if refit else self.params
        else:
            raise ValueError(f"unknown model kind {self.kind}")
        if init is not None and init[0].shape[0] != Z.shape[1]:
            init, refit = None, True
        hist: list[tuple[int, float, float]] = []

        def rec(ep: int, loss: float, acc: float) -> None:
            hist.append((ep, loss, acc))
            if on_epoch:
                on_epoch(ep, loss, acc)

        self.params = train_network(backend, Z, y.astype(float), w, hidden, epochs, lr, l2, seed, init, rec,
                                    record_every=max(1, epochs // 10))
        self.steps_since_full = 0 if refit else self.steps_since_full + 1
        last = hist[-1] if hist else (0, float("nan"), float("nan"))
        return {"epochs": epochs, "loss": last[1], "train_acc": last[2], "refit": refit, "history": hist,
                "features_used": int(Z.shape[1])}

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.kind == "baseline" or self.prep is None or not self.params:
            return np.full(len(X), self.base_rate)
        return predict_network(self.params, self.prep.apply(X))

    # persistence ---------------------------------------------------------------------------
    def save(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays: dict[str, np.ndarray] = {"meta": np.array([self.kind, str(self.horizon), repr(self.base_rate),
                                                           str(self.train_rows), self.trained_label,
                                                           str(self.steps_since_full)])}
        if self.prep is not None:
            arrays.update(cols=self.prep.columns, mean=self.prep.mean, std=self.prep.std)
        for i, p in enumerate(self.params):
            arrays[f"p{i}"] = p
        buf = io.BytesIO()
        np.savez_compressed(buf, **arrays)
        data = buf.getvalue()
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return _sha_bytes(data)

    @staticmethod
    def load(path: Path) -> "DirectionModel":
        with np.load(path, allow_pickle=False) as z:
            meta = [str(x) for x in z["meta"]]
            kind, horizon, base, rows, label = meta[:5]
            m = DirectionModel(kind, int(horizon), base_rate=float(base), train_rows=int(rows), trained_label=label,
                               steps_since_full=int(meta[5]) if len(meta) > 5 else 0)
            if "cols" in z.files:
                m.prep = Prep(z["cols"], z["mean"], z["std"])
            m.params = [z[f"p{i}"] for i in range(sum(1 for f in z.files if f.startswith("p")))]
        return m


def _sha_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class PlattCalibrator:
    """Logistic recalibration on earlier sequential out-of-sample (raw probability, outcome) pairs."""

    def __init__(self, min_outcomes: int):
        self.min_outcomes = min_outcomes
        self.a, self.b = 1.0, 0.0
        self.status = "UNCALIBRATED"
        self.n = 0

    def fit(self, p_raw: Sequence[float], up: Sequence[int]) -> "PlattCalibrator":
        p = np.clip(np.asarray(p_raw, dtype=float), 1e-6, 1 - 1e-6)
        y = np.asarray(up, dtype=float)
        self.n = len(p)
        if self.n < self.min_outcomes or y.min(initial=1) == y.max(initial=0):
            self.status = f"UNCALIBRATED ({self.n} sequential outcomes < {self.min_outcomes} or one class)"
            return self
        z = np.log(p / (1 - p))
        a, b = 1.0, 0.0
        for _ in range(100):  # Newton iterations with a small ridge toward identity
            q = 1 / (1 + np.exp(-np.clip(a * z + b, -40, 40)))
            g = np.array([((q - y) * z).sum() + 1.0 * (a - 1), (q - y).sum() + 1.0 * b])
            wq = q * (1 - q)
            H = np.array([[(wq * z * z).sum() + 1.0, (wq * z).sum()], [(wq * z).sum(), wq.sum() + 1.0]])
            step = np.linalg.solve(H, g)
            a, b = a - step[0], b - step[1]
            if np.abs(step).max() < 1e-8:
                break
        self.a, self.b = float(a), float(b)
        self.status = f"PLATT_ON_{self.n}_SEQUENTIAL_OOS_OUTCOMES"
        return self

    def apply(self, p_raw: np.ndarray) -> np.ndarray:
        if not self.status.startswith("PLATT"):
            return np.asarray(p_raw, dtype=float)
        p = np.clip(np.asarray(p_raw, dtype=float), 1e-6, 1 - 1e-6)
        return 1 / (1 + np.exp(-np.clip(self.a * np.log(p / (1 - p)) + self.b, -40, 40)))


def recency_weights(train_sessions: np.ndarray, current_session: int, half_life: float) -> np.ndarray:
    age = np.maximum(0, current_session - train_sessions).astype(float)
    return 0.5 ** (age / max(half_life, 1e-9))
