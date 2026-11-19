"""M6.5 — hybrid shared/stock-specific models, calibration, and operator controls.

Model (PRD §5)
--------------
* **Shared base model**: one L2 logistic regression pooled over many stock lanes, conditioned by
  one-hot timeframe, sector (owner-supplied map; ``UNKNOWN`` when absent) and regime
  (trend/volatility flags supplied by the caller).
* **Stock-specific residual layer**: a per-symbol logit offset fitted on that stock's training rows
  and shrunk towards zero by ``n / (n + shrink)``. Used only when the stock has at least
  ``min_stock_rows`` training rows; otherwise the forecast **falls back** to the shared model and says so.
* **Calibration**: Platt scaling fitted on a calibration slice that follows the training rows in time
  (never on test rows). Brier score and expected calibration error (ECE) are reported.

Evidence strength is disclosed on every forecast: ``NONE`` (shared fallback), ``WEAK`` (< 2x minimum),
``MODERATE`` (< 5x), ``STRONG``.

Controls
--------
Append-only ``model_controls`` events: ``PAUSE_UPDATES`` / ``RESUME_UPDATES``, ``DISABLE_ALERTS`` /
``ENABLE_ALERTS`` and ``ROLLBACK`` / ``CLEAR_ROLLBACK`` (to a static, currently APPROVED fallback
artifact). Scope is ``global`` or one symbol. :func:`control_state` folds them; the alert engine treats
disabled alerts as a gate blocker and research updates honour the pause.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .db import AuditDB, jdump, utc_now

HYBRID_CONTRACT = "stock-gplm.hybrid-model.m65.v1"
CONTROL_ACTIONS = ("PAUSE_UPDATES", "RESUME_UPDATES", "DISABLE_ALERTS", "ENABLE_ALERTS", "ROLLBACK", "CLEAR_ROLLBACK")


def _sig(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35, 35)))


def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50, offset: np.ndarray | None = None
              ) -> np.ndarray:
    """Newton-IRLS L2 logistic regression with intercept (column 0 of the returned weights)."""
    Z = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(Z.shape[1])
    off = np.zeros(len(X)) if offset is None else offset
    R = np.eye(Z.shape[1]) * l2
    R[0, 0] = 0.0
    for _ in range(iters):
        p = _sig(Z @ w + off)
        g = Z.T @ (p - y) + R @ w
        H = (Z * (p * (1 - p))[:, None]).T @ Z + R + 1e-9 * np.eye(Z.shape[1])
        step = np.linalg.solve(H, g)
        w -= step
        if float(np.abs(step).max()) < 1e-8:
            break
    return w


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2)) if len(y) else math.nan


def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    if not len(y):
        return math.nan
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    tot = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            tot += m.mean() * abs(float(p[m].mean()) - float(y[m].mean()))
    return float(tot)


@dataclass
class HybridModel:
    feature_names: list[str]
    mean: np.ndarray
    std: np.ndarray
    w_shared: np.ndarray
    cond_levels: dict[str, list[str]]
    stock_offset: dict[str, float]
    stock_rows: dict[str, int]
    platt: tuple[float, float] = (1.0, 0.0)
    min_stock_rows: int = 150
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- design matrix
    def design(self, X: np.ndarray, cond: Mapping[str, Sequence[str]]) -> np.ndarray:
        Xs = (np.asarray(X, float) - self.mean) / self.std
        Xs = np.where(np.isfinite(Xs), Xs, 0.0)  # unavailable features contribute nothing (never fabricated)
        parts = [Xs]
        for k, levels in self.cond_levels.items():
            vals = np.asarray(cond.get(k, ["UNKNOWN"] * len(X)))
            parts.append(np.column_stack([(vals == lv).astype(float) for lv in levels[1:]]) if len(levels) > 1
                         else np.zeros((len(X), 0)))
        return np.column_stack(parts)

    def raw_logit(self, X: np.ndarray, cond: Mapping[str, Sequence[str]], symbols: Sequence[str]) -> tuple[np.ndarray, list[str]]:
        D = self.design(X, cond)
        z = self.w_shared[0] + D @ self.w_shared[1:]
        src = []
        for i, s in enumerate(symbols):
            if self.stock_rows.get(s, 0) >= self.min_stock_rows:
                z[i] += self.stock_offset.get(s, 0.0)
                src.append("STOCK_RESIDUAL")
            else:
                src.append("SHARED_FALLBACK")
        return z, src

    def predict(self, X: np.ndarray, cond: Mapping[str, Sequence[str]], symbols: Sequence[str]) -> list[dict[str, Any]]:
        z, src = self.raw_logit(X, cond, symbols)
        a, b = self.platt
        p_raw, p_cal = _sig(z), _sig(a * z + b)
        out = []
        for i, s in enumerate(symbols):
            n = self.stock_rows.get(s, 0)
            strength = ("NONE" if src[i] == "SHARED_FALLBACK" else "WEAK" if n < 2 * self.min_stock_rows else
                        "MODERATE" if n < 5 * self.min_stock_rows else "STRONG")
            out.append({"symbol": s, "p_raw": round(float(p_raw[i]), 6), "p_calibrated": round(float(p_cal[i]), 6),
                        "source": src[i], "stock_training_rows": n, "stock_evidence": strength,
                        "disclosure": ("shared model only: stock-specific evidence below the minimum "
                                       f"({n} < {self.min_stock_rows} rows)") if src[i] == "SHARED_FALLBACK" else
                                      f"shared model + stock residual ({n} rows, {strength.lower()} evidence)"})
        return out

    # ---------------------------------------------------------------- persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"contract": HYBRID_CONTRACT, "feature_names": self.feature_names, "mean": self.mean.tolist(),
               "std": self.std.tolist(), "w_shared": self.w_shared.tolist(), "cond_levels": self.cond_levels,
               "stock_offset": self.stock_offset, "stock_rows": self.stock_rows, "platt": list(self.platt),
               "min_stock_rows": self.min_stock_rows, "meta": self.meta}
        path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")

    @staticmethod
    def load(path: Path) -> "HybridModel":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if d.get("contract") != HYBRID_CONTRACT:
            raise ValueError(f"not a {HYBRID_CONTRACT} file")
        return HybridModel(d["feature_names"], np.array(d["mean"]), np.array(d["std"]), np.array(d["w_shared"]),
                           d["cond_levels"], {k: float(v) for k, v in d["stock_offset"].items()},
                           {k: int(v) for k, v in d["stock_rows"].items()}, tuple(d["platt"]),
                           int(d["min_stock_rows"]), d.get("meta", {}))


def fit_hybrid(X: np.ndarray, y: np.ndarray, symbols: Sequence[str], cond: Mapping[str, Sequence[str]],
               order: np.ndarray, feature_names: Sequence[str], *, calib_fraction: float = 0.2,
               min_stock_rows: int = 150, shrink: float = 100.0, l2: float = 1.0) -> tuple[HybridModel, dict[str, Any]]:
    """Fit on rows sorted by ``order`` (decision time). The last ``calib_fraction`` of the time-ordered rows
    calibrate (Platt) and evaluate; nothing in that slice trains the shared or residual layers."""
    idx = np.argsort(order, kind="stable")
    X, y = np.asarray(X, float)[idx], np.asarray(y, float)[idx]
    syms = np.asarray(symbols)[idx]
    cond = {k: np.asarray(v)[idx] for k, v in cond.items()}
    n = len(y)
    cut = int(n * (1 - calib_fraction))
    if cut < 50 or n - cut < 20:
        raise ValueError(f"not enough rows to fit and calibrate ({n})")
    tr, ca = slice(0, cut), slice(cut, n)
    mean = np.nanmean(X[tr], axis=0)
    std = np.nanstd(X[tr], axis=0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    std = np.where((std > 0) & np.isfinite(std), std, 1.0)
    levels = {k: ["UNKNOWN"] + sorted({str(x) for x in v[tr]} - {"UNKNOWN"}) for k, v in cond.items()}
    m = HybridModel(list(feature_names), mean, std, np.zeros(1), levels, {}, {}, min_stock_rows=min_stock_rows)
    D = m.design(X[tr], {k: v[tr] for k, v in cond.items()})
    m.w_shared = fit_logit(D, y[tr], l2=l2)
    base = m.w_shared[0] + D @ m.w_shared[1:]
    for s in sorted(set(syms[tr])):
        sel = syms[tr] == s
        k = int(sel.sum())
        m.stock_rows[str(s)] = k
        if k >= min_stock_rows:
            w = fit_logit(np.zeros((k, 0)), y[tr][sel], l2=0.0, offset=base[sel])
            m.stock_offset[str(s)] = float(w[0]) * k / (k + shrink)
    zc, src = m.raw_logit(X[ca], {k: v[ca] for k, v in cond.items()}, list(syms[ca]))
    wc = fit_logit(zc[:, None], y[ca], l2=0.0)
    m.platt = (float(wc[1]), float(wc[0]))
    p_raw, p_cal = _sig(zc), _sig(m.platt[0] * zc + m.platt[1])
    shared_only = _sig(m.w_shared[0] + m.design(X[ca], {k: v[ca] for k, v in cond.items()}) @ m.w_shared[1:])
    base_rate = float(y[tr].mean())
    yc = y[ca]
    report = {"contract": HYBRID_CONTRACT, "rows": n, "train_rows": cut, "calibration_rows": n - cut,
              "stocks": len(m.stock_rows), "stocks_with_residual": len(m.stock_offset),
              "fallback_share_calibration": round(float(np.mean([s == "SHARED_FALLBACK" for s in src])), 4),
              "brier": {"base_rate": round(brier(np.full(len(yc), base_rate), yc), 6),
                        "shared_only": round(brier(shared_only, yc), 6), "hybrid_raw": round(brier(p_raw, yc), 6),
                        "hybrid_calibrated": round(brier(p_cal, yc), 6)},
              "ece": {"hybrid_raw": round(ece(p_raw, yc), 6), "hybrid_calibrated": round(ece(p_cal, yc), 6)},
              "note": "calibration-slice metrics are in-sample for Platt scaling; use walk-forward for evidence"}
    m.meta = {"report": report, "fitted_utc": utc_now()}
    return m, report


# ----------------------------------------------------------------------------- operator controls

class ControlError(ValueError):
    pass


def set_control(db: AuditDB, action: str, actor: str, reason: str, scope: str = "global",
                target: str | None = None) -> dict[str, Any]:
    action = action.upper()
    if action not in CONTROL_ACTIONS:
        raise ControlError(f"action must be one of {CONTROL_ACTIONS}")
    if not actor or not str(actor).strip() or not reason or not str(reason).strip():
        raise ControlError("controls need an actor and a reason")
    if action == "ROLLBACK":
        from .governance import model_record, verify_model_file

        if not target:
            raise ControlError("ROLLBACK needs --target MODEL_ID (an approved static fallback artifact)")
        rec = model_record(db, target)
        chk = verify_model_file(db, target)
        if rec["state"] != "APPROVED" or not chk["hash_verified"]:
            raise ControlError(f"rollback target {target} must be an APPROVED, hash-verified artifact "
                               f"(state {rec['state']}, hash_verified {chk['hash_verified']})")
    with db.tx() as c:
        c.execute("INSERT INTO model_controls (scope, action, target, actor, reason, ts_utc, run_id) VALUES (?,?,?,?,?,?,?)",
                  (scope.upper() if scope.lower() != "global" else "global", action, target, actor.strip(),
                   reason.strip(), utc_now(), db.run_id))
    db.event("MODEL_CONTROL", {"action": action, "scope": scope, "target": target, "actor": actor, "reason": reason},
             subject=scope, level="WARN" if action in ("PAUSE_UPDATES", "DISABLE_ALERTS", "ROLLBACK") else "INFO")
    return control_state(db, None if scope.lower() == "global" else scope)


def control_state(db: AuditDB | None, symbol: str | None = None) -> dict[str, Any]:
    """Effective controls for ``symbol`` (global events apply to every symbol; symbol events override)."""
    st = {"updates_paused": False, "alerts_disabled": False, "rollback_to": None, "events": 0}
    if db is None:
        return st
    try:
        rows = db.q("SELECT * FROM model_controls ORDER BY seq")
    except Exception:
        return st
    for scope in ("global", (symbol or "").upper()):
        for r in rows:
            if r["scope"] != scope:
                continue
            a = r["action"]
            st["events"] += 1
            if a in ("PAUSE_UPDATES", "RESUME_UPDATES"):
                st["updates_paused"] = a == "PAUSE_UPDATES"
            elif a in ("DISABLE_ALERTS", "ENABLE_ALERTS"):
                st["alerts_disabled"] = a == "DISABLE_ALERTS"
            elif a == "ROLLBACK":
                st["rollback_to"] = r["target"]
            elif a == "CLEAR_ROLLBACK":
                st["rollback_to"] = None
    return st


__all__ = ["CONTROL_ACTIONS", "ControlError", "HYBRID_CONTRACT", "HybridModel", "brier", "control_state", "ece",
           "fit_hybrid", "fit_logit", "set_control"]
