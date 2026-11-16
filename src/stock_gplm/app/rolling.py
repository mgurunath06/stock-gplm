"""Sequential walk-forward learning and prediction (``gplm replay`` and the core of ``gplm daily``).

For every session S from ``rolling.start`` (default 2026-08-13) to the latest completed session, per
lane (symbol x timeframe), in order:

1. **Cutoff.** The prediction time is the close of the decision bar (daily lane) or the end of the
   previous session (intraday lanes, trained once before the session opens). Only bars whose
   ``end_ns`` is at or before the cutoff are "known".
2. **Evaluate.** Earlier predictions whose outcome window has now completed get an evaluation row
   (realized net return, direction correctness, Brier score, simulated trade with stop/target/time
   exit and costs). Evaluations are appended, never edited.
3. **Train.** Every eligible horizon is (re)fitted on rows whose label window ended by the cutoff:
   a :class:`LabelGuard` raises if any training label would need a later bar. Observations are
   recency weighted (half-life in sessions). ``logit`` is re-fitted each step, ``mlp`` warm-starts
   and is fully re-fitted every ``mlp_retrain_every`` steps; training histories are stored.
4. **Predict.** Predictions for the session's decision bars are written *before* any outcome is
   read. Probabilities are Platt-calibrated on earlier sequential out-of-sample outcomes only.
   Only the current champion model may mark a prediction actionable.
5. **Rules and strategies.** Machine-generated rules are re-searched every ``rediscover_every``
   steps on completed labels only (:mod:`.candidates`); active library strategies in scope signal
   on the same bars. Their outcomes are evaluated like any other prediction.
6. **Champion review.** Candidate models are compared with the accepted model on their trailing
   out-of-sample Brier score; a challenger replaces the champion only with enough outcomes and a
   margin. Promotions are recorded; the previous artifacts are kept.
7. **Checkpoint.** State is stored after each step, so an interrupted run resumes at the next
   session without repeating completed work. Predictions carry natural keys, so a repeat inserts
   nothing.

M5.9A output contract (engine ``gplm-rolling.v2``): a prediction on a historical bar is ``REPLAY``.
Only the latest *eligible completed* bar can be a current forecast, and it is ``LIVE`` only when the
absolute freshness check passed, the model artifact has a recorded owner approval (adaptive refits
never do), the opportunity contract is complete and every gate passed; otherwise it is stored as
``SHADOW`` with its ``status`` (SHADOW / NO_TRADE / UNAVAILABLE) and reasons. The target is the
horizon-derived bar, and outcomes are UP / DOWN / NEUTRAL against a versioned neutral band.
The engine version is part of the replay identity, so v1 replays are never resumed with v2 semantics.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..m54.backtest import ExitPolicy, simulate
from .candidates import Rule, search
from .compute import Backend
from .contracts import (END_OF_SESSION, LIVE, NEUTRAL, OPPORTUNITY_INCOMPLETE, OUTPUT_CONTRACT, REPLAY, SHADOW,
                        TARGET_HORIZON_MISMATCH, UNAVAILABLE, UP, DOWN, Freshness, brier_up, classify_movement,
                        direction_correct, live_decision, neutral_band, opportunity_missing)
from .db import AuditDB, sha, utc_now
from .lanes import Lane
from .library import Library, scoped, strategy_features, strategy_mask
from .models import DirectionModel, PlattCalibrator, recency_weights
from .progress import Progress
from .targets import EOD_UNAVAILABLE, derive_target, grid_position

ENGINE_VERSION = "gplm-rolling.v2"


class LookaheadError(AssertionError):
    """A label or outcome was requested before it existed at the replay clock."""


class LabelGuard:
    """The only way the engine reads outcomes; it refuses anything not yet observable."""

    def __init__(self, lane: Lane):
        self.m = lane.market
        self.cost = lane.costs.buy + lane.costs.sell

    def label_end(self, t: np.ndarray | int, h: int) -> np.ndarray | int:
        return np.asarray(t) + h

    def labels(self, rows: np.ndarray, h: int, last_known: int) -> np.ndarray:
        if rows.size and int(rows.max()) + h > last_known:
            raise LookaheadError(f"label needs bar {int(rows.max()) + h} but only bars <= {last_known} are known")
        m = self.m
        return m.close[rows + h] / m.open[rows + 1] - 1 - self.cost

    def trade(self, t: int, policy: ExitPolicy, last_known: int, costs: Any) -> dict[str, Any] | None:
        mask = np.zeros(len(self.m), dtype=bool)
        mask[t] = True
        tr = simulate(self.m, mask, (t, last_known + 1), policy, costs)
        return tr[0] if tr else None


@dataclass
class Pending:
    prediction_id: str
    source_kind: str
    source_id: str
    horizon: int
    t: int
    p: float | None
    direction: str
    actionable: bool
    exit: dict[str, Any]
    target: str | None = None


def _grid_left(lane: Lane) -> np.ndarray:
    """Intraday: grid bars left in the session after each bar (session grid, not data presence)."""
    m = lane.market
    if not lane.intraday:
        return np.zeros(len(m), dtype=np.int64)
    return np.array([grid_position(int(m.end_ns[i]), str(m.sessions[i]), lane.timeframe)[1] for i in range(len(m))],
                    dtype=np.int64)


class LaneReplay:
    def __init__(self, db: AuditDB, lane: Lane, horizons: Sequence[int], cfg: Mapping[str, Any], backend: Backend,
                 progress: Progress, models_dir: Path, library: Library | None = None,
                 bars_all: Mapping[str, Any] | None = None, rule_horizons: Sequence[int] | None = None,
                 end_session: str | None = None, max_steps: int | None = None,
                 live_context: Mapping[str, Any] | None = None):
        """``live_context``: ``freshness`` (:class:`~.contracts.Freshness` for this symbol against the
        requested session), ``calendar`` (registered session calendar or None), ``universe`` (live-alert
        membership from :func:`~.governance.universe_membership`), ``cost_policy_version``. Model outputs of this
        adaptive engine are never LIVE (M5.9B); only approved library strategies can pass the canonical gate."""
        self.db, self.lane, self.backend, self.progress = db, lane, backend, progress
        self.live_context = dict(live_context or {})
        self.freshness: Freshness | None = self.live_context.get("freshness")
        self.calendar = self.live_context.get("calendar")
        self.grid_left = _grid_left(lane)
        inc = lane.bars.frame["incomplete"].to_numpy(dtype=bool) if len(lane.bars.frame) else np.zeros(0, dtype=bool)
        ok = np.flatnonzero(~inc)
        self.latest_idx = int(ok[-1]) if len(ok) else -1  # latest eligible completed bar
        self.band = neutral_band(cfg.get("prediction", {}).get("neutral_band"), lane.costs.buy + lane.costs.sell)
        self.live_outputs: list[dict[str, Any]] = []
        self.last_decision: dict[str, Any] | None = None
        self._targets: dict[tuple[int, int], dict[str, Any]] = {}
        self.cfg = cfg
        self.r = cfg["rolling"]
        self.models_cfg = dict(cfg["models"])
        self.models_cfg["_epochs_full"] = int(self.r["mlp_epochs_full"])
        self.models_cfg["_epochs_update"] = int(self.r["mlp_epochs_update"])
        self.pred_cfg = cfg["prediction"]
        self.cand_cfg = cfg["candidates"]
        self.kinds = list(cfg["models"]["kinds"])
        self.models_dir = models_dir
        self.library = library
        self.bars_all = bars_all
        self.guard = LabelGuard(lane)
        self.end_session = end_session
        self.max_steps = max_steps
        m = lane.market
        n_sessions = int(m.session_id.max()) + 1 if len(m) else 0
        min_lab = int(cfg["horizons"].get("min_independent_labels", 12))
        self.horizons: list[int] = []
        self.skipped: dict[int, str] = {}
        for h in horizons:
            span = n_sessions if lane.intraday else len(m)
            if lane.intraday:
                ok = h < int(np.bincount(m.session_id).mean()) - 1
                reason = "horizon longer than a session"
            else:
                ok = (len(m) - h) / h >= min_lab
                reason = f"only {(len(m) - h) / h:.1f} independent {h}-bar labels < {min_lab}"
            (self.horizons.append(h) if ok else self.skipped.__setitem__(h, f"INSUFFICIENT_HISTORY: {reason}"))
            del span
        self.rule_horizons = [h for h in (rule_horizons or self.horizons) if h in self.horizons]
        ident = {"engine": ENGINE_VERSION, "lane": lane.key, "horizons": self.horizons, "start": self.r["start"],
                 "rolling": {k: self.r[k] for k in sorted(self.r)}, "models": cfg["models"],
                 "prediction": self.pred_cfg, "candidates": self.cand_cfg, "features": lane.feature_config_sha}
        self.base_replay_id = "rp-" + sha(ident)[:20]
        self.identity = ident

    # ------------------------------------------------------------------ bookkeeping
    def _prefix_hash(self, upto: int) -> str:
        """Hash of every bar up to ``upto``: detects a revision of already-processed history."""
        import hashlib

        m = self.lane.market
        arr = np.column_stack([m.open[:upto + 1], m.high[:upto + 1], m.low[:upto + 1], m.close[:upto + 1],
                               m.end_ns[:upto + 1].astype(float)])
        return hashlib.sha256(np.round(arr, 6).tobytes()).hexdigest()

    def _steps(self) -> list[tuple[str, np.ndarray]]:
        m = self.lane.market
        start = self.r["start"]
        out = []
        sess = m.sessions
        for s in sorted(set(sess[sess >= start].tolist())):
            if self.end_session and s > self.end_session:
                break
            idx = np.nonzero(sess == s)[0]
            if self.lane.intraday:
                # A decision needs a next bar on the *session grid*. The latest completed bar of a session in
                # progress therefore stays a decision bar; the session's final grid bar does not (policy
                # ``unavailable``: models are trained on in-session labels only).
                idx = idx[self.grid_left[idx] >= 1]
            out.append((s, idx))
        return out

    def _model_path(self, replay_id: str, kind: str, h: int, label: str) -> Path:
        safe = label.replace(":", "").replace(" ", "T")
        return self.models_dir / self.lane.symbol / self.lane.timeframe / replay_id / f"{kind}-h{h}-{safe}.npz"

    def _save_one(self, model: DirectionModel, kind: str, h: int, replay_id: str, label: str) -> str:
        """Persist the exact fitted state that produced this step's predictions; returns its model_id."""
        mid = f"{replay_id}|{kind}|h{h}|{label}"
        path = self._model_path(replay_id, kind, h, label)
        digest = model.save(path)
        self.model_digests = getattr(self, "model_digests", {})
        self.model_digests[mid] = digest
        self.db.conn.execute("INSERT OR IGNORE INTO models VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (mid, self.lane.key, kind, h, utc_now(), label, model.train_rows, str(path), digest,
                              self.backend.name, self.lane.feature_config_sha, self.lane.data_fingerprint,
                              json.dumps({"base_rate": model.base_rate, "features_used": int(model.prep.columns.size)
                                          if model.prep is not None else 0}), self.db.run_id))
        self.model_files[f"{kind}|{h}"] = str(path)
        return mid

    # ------------------------------------------------------------------ main loop
    def run(self) -> dict[str, Any]:
        lane, m = self.lane, self.lane.market
        steps = self._steps()
        ck_key = f"replay:{self.base_replay_id}"
        ck = self.db.get_checkpoint(ck_key) or {}
        replay_id = ck.get("replay_id", self.base_replay_id)
        done_upto = ck.get("last_session")
        if done_upto is not None:
            last_idx = int(ck["last_index"])
            if last_idx >= len(m) or ck.get("prefix_hash") != self._prefix_hash(last_idx):
                revision = int(ck.get("revision", 1)) + 1
                replay_id = f"{self.base_replay_id}-r{revision}"
                self.db.event("DATA_REVISION", {"lane": lane.key, "old_replay": ck.get("replay_id"),
                                                "new_replay": replay_id, "reason": "history up to the last processed "
                                                "step changed; earlier predictions are preserved, a new replay starts"},
                              subject=lane.key, level="WARN")
                self.progress.warn(f"{lane.key}: data revision detected -> new replay {replay_id}")
                ck = {"revision": revision}
                done_upto = None
        self.replay_id = replay_id
        models: dict[tuple[str, int], DirectionModel] = {}
        self.model_files: dict[str, str] = dict(ck.get("model_files", {})) if done_upto else {}
        for h in self.horizons:
            for k in self.kinds:
                p = self.model_files.get(f"{k}|{h}")
                path = Path(p) if p else None
                if path is not None and not path.is_file():
                    # Resolve copied Linux checkpoints against the current home,
                    # without changing checkpoint history or refitting a model.
                    parts = p.replace("\\", "/").split("/")
                    if "models" in parts:
                        candidate = self.models_dir.joinpath(*parts[parts.index("models") + 1:])
                        if candidate.resolve().is_relative_to(self.models_dir.resolve()):
                            path = candidate
                    if not path.is_file():
                        raise FileNotFoundError(f"checkpoint model unavailable: {p}")
                    recorded = self.db.one("SELECT sha256 FROM models WHERE path=?", (p,))
                    if recorded is None or hashlib.sha256(path.read_bytes()).hexdigest() != recorded["sha256"]:
                        raise ValueError(f"relocated checkpoint model digest mismatch: {p}")
                models[(k, h)] = DirectionModel.load(path) if done_upto and path else DirectionModel(k, h)
        rules = [Rule.from_dict(r) for r in ck.get("rules", [])] if done_upto else []
        rules_step = int(ck.get("rules_step", -10**9)) if done_upto else -10**9
        strategies = self._library_masks()
        todo = [(s, idx) for s, idx in steps if done_upto is None or s > done_upto]
        if self.max_steps is not None:
            todo = todo[: self.max_steps]
        self.progress.update(force=True, task="replay", stock=lane.symbol, timeframe=lane.timeframe,
                             steps=len(steps), device=self.backend.name, session=None, step=None, model=None,
                             horizon=None, epoch=None, epochs=None, train_loss=None, train_acc=None, oos_acc=None,
                             oos_n=None, trades=None, net_return=None)
        pending = self._load_pending()
        self.in_sample_skipped = 0
        stats = {"steps_done": 0, "predictions_new": 0, "evaluations_new": 0, "rule_searches": 0, "fits": 0}
        t0 = time.monotonic()
        order = {s: i for i, (s, _) in enumerate(steps)}
        for step_no, (session, dec) in enumerate(todo):
            global_step = order[session]
            if dec.size == 0:
                continue
            cutoff_idx = int(dec[0]) if not lane.intraday else int(dec[0]) - 1
            if cutoff_idx < 0:
                continue
            with self.db.tx():
                stats["evaluations_new"] += self._evaluate(pending, cutoff_idx)
                self._champion_review(replay_id, int(m.end_ns[cutoff_idx]))
                for h in self.horizons:
                    fitted = self._fit_and_predict(models, h, dec, cutoff_idx, session, global_step, replay_id, pending)
                    stats["fits"] += fitted["fits"]
                    stats["predictions_new"] += fitted["new"]
                if self.rule_horizons and global_step - rules_step >= int(self.r["rediscover_every"]):
                    res = search(self.backend, lane, cutoff_idx, self.rule_horizons, self.cand_cfg)
                    rules = [Rule.from_dict(r) for r in res["rules"]]
                    rules_step = global_step
                    stats["rule_searches"] += 1
                    self.db.event("RULE_SEARCH", {"lane": lane.key, "replay_id": replay_id, "session": session,
                                                  "cutoff": lane.label(cutoff_idx), **{k: v for k, v in res.items()
                                                                                         if k != "rules"},
                                                  "selected": [{"rule_id": r.rule_id, "tier": r.tier, "text": r.text(),
                                                                "horizon": r.horizon, "in_sample": r.in_sample}
                                                               for r in rules]}, subject=lane.key)
                stats["predictions_new"] += self._signal_predictions(rules, strategies, dec, session, replay_id, pending)
                self.db.put_checkpoint(ck_key, {"replay_id": replay_id, "revision": ck.get("revision", 1),
                                                "last_session": session, "last_index": int(dec[-1]),
                                                "prefix_hash": self._prefix_hash(int(dec[-1])),
                                                "rules": [r.to_dict() for r in rules], "rules_step": rules_step,
                                                "model_files": self.model_files,
                                                "identity": self.identity, "updated_utc": utc_now()})
            stats["steps_done"] += 1
            oos = self._oos_summary(replay_id)
            self.progress.update(session=session, step=global_step + 1, oos_acc=oos.get("acc"), oos_n=oos.get("n"),
                                 trades=oos.get("trades"), net_return=oos.get("mean_trade_net"),
                                 gpu_mem=self._gpu_mem())
            self.progress.advance()
        # final pass: evaluate outcomes that completed by the last processed bar (never beyond it)
        last_done = (self.db.get_checkpoint(ck_key) or {}).get("last_index")
        if last_done is not None:
            last_known = int(m.session_last[int(last_done)]) if lane.intraday else int(last_done)
            with self.db.tx():
                stats["evaluations_new"] += self._evaluate(pending, last_known)
                self._champion_review(replay_id, int(m.end_ns[last_known]))
        stats["seconds"] = round(time.monotonic() - t0, 2)
        stats["in_sample_strategy_signals_skipped"] = self.in_sample_skipped
        summary = {"lane": lane.key, "replay_id": replay_id, "horizons": self.horizons, "skipped_horizons": self.skipped,
                   "steps_total": len(steps), "first_session": steps[0][0] if steps else None,
                   "last_session": steps[-1][0] if steps else None, **stats, "oos": self._oos_summary(replay_id),
                   "latest_bar": self._latest_summary()}
        self.db.event("REPLAY_LANE_DONE", summary, subject=lane.key)
        return summary

    def _latest_summary(self) -> dict[str, Any]:
        """What the latest eligible completed bar produced (or why nothing could be produced)."""
        m = self.lane.market
        if self.latest_idx < 0:
            return {"bar": None, "status": UNAVAILABLE, "reasons": ["MISSING_EXPECTED_BAR"]}
        statuses: dict[str, int] = {}
        for o in self.live_outputs:
            statuses[o["status"]] = statuses.get(o["status"], 0) + 1
        out: dict[str, Any] = {"bar": self.lane.label(self.latest_idx), "statuses": statuses,
                               "freshness": self.freshness.to_dict() if self.freshness else None}
        if self.lane.intraday and self.grid_left[self.latest_idx] < 1:
            out.update(status=UNAVAILABLE, reasons=[END_OF_SESSION],
                       note="final completed bar of the session; next-session forecasts are not produced "
                            "(policy 'unavailable': models are trained on in-session labels)")
        elif not statuses:
            out.update(status=UNAVAILABLE, reasons=["NO_FORECAST_ON_LATEST_BAR"])
        else:
            out["status"] = LIVE if statuses.get(LIVE) else SHADOW if statuses.get(SHADOW) else \
                max(statuses, key=lambda k: statuses[k])
        return out

    def _gpu_mem(self) -> str | None:
        mem = self.backend.memory()
        return f"VRAM {mem['vram_used_mb']}/{mem['vram_total_mb']} MB" if mem else None

    # ------------------------------------------------------------------ models
    def _fit_and_predict(self, models: dict[tuple[str, int], DirectionModel], h: int, dec: np.ndarray, cutoff_idx: int,
                         session: str, global_step: int, replay_id: str, pending: list[Pending]) -> dict[str, int]:
        lane, m = self.lane, self.lane.market
        last_t = cutoff_idx - h  # label of row t needs bar t+h <= cutoff
        rows = np.arange(0, max(0, last_t + 1))
        if lane.intraday and rows.size:
            rows = rows[m.session_id[np.minimum(rows + h, len(m) - 1)] == m.session_id[rows]]
        if rows.size:
            rows = rows[np.isnan(lane.X[rows]).mean(axis=1) < 0.5]
            rows = rows[~m.no_signal[rows]]
        y_ret = self.guard.labels(rows, h, cutoff_idx) if rows.size else np.zeros(0)
        gross_lab = y_ret + self.guard.cost
        ok = np.isfinite(y_ret) & (np.abs(np.nan_to_num(gross_lab)) > self.band.threshold)  # NEUTRAL rows excluded
        rows, y_ret = rows[ok], y_ret[ok]
        out = {"fits": 0, "new": 0}
        if rows.size < int(self.r["min_train_rows"]):
            return out
        assert int(m.end_ns[rows.max() + h]) <= int(m.end_ns[cutoff_idx])  # explicit leak check
        y = (y_ret + self.guard.cost > self.band.threshold).astype(float)  # UP vs DOWN (gross beyond the band)
        w = recency_weights(m.session_id[rows], int(m.session_id[cutoff_idx]), float(self.r["half_life_sessions"]))
        X = lane.X[rows]
        if lane.intraday:  # an intraday h-bar prediction must finish inside its session (session grid)
            dec = dec[self.grid_left[dec] >= h]
            if dec.size == 0:
                return out
        Xd = lane.X[dec]
        cut_ns = int(m.end_ns[cutoff_idx])
        cal_rows = self._calibration_data(replay_id, h, cut_ns)
        champion = self._champion(h)
        logit_contrib: list[list[dict[str, Any]]] = [[] for _ in dec]
        for kind in self.kinds:
            model = models[(kind, h)]
            full = kind != "mlp" or model.steps_since_full + 1 >= int(self.r["mlp_retrain_every"]) or not model.params
            planned = {"baseline": 0, "logit": int(self.models_cfg["logit"]["epochs"]),
                       "mlp": int(self.r["mlp_epochs_full"] if full else self.r["mlp_epochs_update"])}[kind]
            self.progress.update(model=kind, horizon=h, epoch=0, epochs=planned)
            t0 = time.monotonic()
            hist: list[tuple[int, float, float]] = []

            def on_epoch(ep: int, loss: float, acc: float, _k: str = kind) -> None:
                hist.append((ep, loss, acc))
                self.progress.update(model=_k, horizon=h, epoch=ep, train_loss=loss, train_acc=acc)

            info = model.fit(self.backend, X, y, w, self.models_cfg, int(self.cfg["models"]["seed"]) + h, full, on_epoch)
            info["rows"] = int(rows.size)
            secs = time.monotonic() - t0
            model.trained_label = lane.label(cutoff_idx)
            out["fits"] += 1
            model_id = self._save_one(model, kind, h, replay_id, model.trained_label)
            info = {**info, "model_id": model_id, "artifact_sha256": self.model_digests.get(model_id)}
            for ep, loss, acc in (hist or [(0, info.get("loss"), info.get("train_acc"))]):
                self.db.conn.execute("INSERT INTO training_history (run_id, lane_key, model_kind, horizon, step_label, "
                                     "epoch, loss, train_acc, rows, device, seconds, ts_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (self.db.run_id, lane.key, kind, h, session, ep, loss, acc, int(rows.size),
                                      self.backend.name, round(secs, 4), utc_now()))
            p_raw = model.predict(Xd)
            cal = PlattCalibrator(int(self.r["calibration_min_outcomes"]))
            pr = [(a, b) for (k2, a, b) in cal_rows if k2 == kind]
            cal.fit([a for a, _ in pr], [b for _, b in pr])
            p_cal = cal.apply(p_raw)
            if kind == "logit" and model.prep is not None:
                coef = model.params[0][:, 0]
                Z = model.prep.apply(Xd)
                for i in range(len(dec)):
                    c = coef * Z[i]
                    top = np.argsort(-np.abs(c))[:5]
                    logit_contrib[i] = [{"feature": lane.names[int(model.prep.columns[j])], "z": round(float(Z[i, j]), 3),
                                         "contribution": round(float(c[j]), 4)} for j in top]
            hist_acc = self._accuracy(replay_id, f"{kind}", h, cut_ns)
            for i, t in enumerate(dec.tolist()):
                out["new"] += self._emit_model_prediction(kind, h, int(t), float(p_raw[i]), float(p_cal[i]), cal.status,
                                                          champion, info, hist_acc, session, replay_id, pending,
                                                          logit_contrib[i] if kind != "baseline" else [])
        return out

    def _target(self, t: int, h: int) -> dict[str, Any]:
        key = (t, h)
        if key not in self._targets:
            m = self.lane.market
            self._targets[key] = derive_target(self.lane.timeframe, str(m.sessions[t]), int(m.end_ns[t]), h,
                                               calendar=self.calendar, observed_frame=self.lane.bars.frame,
                                               eod_policy=EOD_UNAVAILABLE)
        return self._targets[key]

    def _classify(self, t: int, *, approval: Mapping[str, Any] | None, missing: Sequence[str],
                  blockers: Sequence[str], target: Mapping[str, Any], model_required: bool = True,
                  strategy_involved: bool = False, strategy: Mapping[str, Any] | None = None
                  ) -> tuple[str, str, list[str], bool]:
        """(mode, status, reasons, actionable) through the canonical M5.9B gate (``contracts.live_decision``).
        Rolling refits are adaptive and therefore never LIVE; the decision is kept in ``self.last_decision``."""
        self.last_decision = None
        if t != self.latest_idx:
            return "REPLAY", REPLAY, [], not blockers and not missing
        fresh = self.freshness
        if target["status"] != "DERIVED":
            fresh = Freshness(str(target["reason"]), getattr(fresh, "expected_session", None),
                              getattr(fresh, "last_session", None), [str(target["reason"])])
        cal = self.calendar
        tgt = str(target.get("target") or "")[:10] or None
        covers = bool(cal is not None and hasattr(cal, "covers") and cal.covers(str(self.lane.market.sessions[t]), tgt))
        # Adaptive refits are never registered fixed artifacts: no model evidence is offered to the gate, so a
        # rolling output can be at most SHADOW (ADAPTIVE_MODEL_NOT_LIVE + MODEL_NOT_APPROVED) whatever `approval` says.
        model = None
        decision = live_decision(
            latest_eligible_bar=True, freshness=fresh, calendar_id=getattr(cal, "calendar_id", None),
            calendar_covers=covers, model=model, adaptive_model=model_required, model_required=model_required,
            strategy_driven=strategy_involved, strategy=strategy, universe=self.live_context.get("universe"),
            opportunity_missing_fields=missing, cost_policy_version=self.live_context.get("cost_policy_version"),
            neutral_band_version=self.band.version, gate_blockers=blockers)
        self.last_decision = decision.to_dict()
        return ("LIVE" if decision.status == LIVE else "SHADOW"), decision.status, decision.reasons, decision.actionable

    def _opportunity(self, t: int, h: int, direction: str, p: float | None, stop: float | None, tgt: float | None,
                     model_version: str | None, strategy_version: str | None = None) -> dict[str, Any]:
        m = self.lane.market
        close = float(m.close[t])
        exp = None
        if p is not None and stop is not None and tgt is not None and close > 0:
            exp = float(p * (tgt - close) / close - (1 - p) * (close - stop) / close)
        fresh = (self.freshness.status if self.freshness is not None else "UNKNOWN") if t == self.latest_idx \
            else "HISTORICAL_REPLAY"
        opp = {"action": "BUY" if direction == "LONG" else None, "entry": "buy at the open of the next bar",
               "invalidation": f"exit if price trades at or below the stop {stop:.4f}" if stop is not None else None,
               "stop": stop, "target": tgt, "horizon": h, "probability": p, "expected_return": exp,
               "model_version": model_version, "strategy_version": strategy_version,
               "timestamp": self.lane.label(t), "freshness_status": fresh}
        return opp

    def _emit_model_prediction(self, kind: str, h: int, t: int, p_raw: float, p_cal: float, cal_status: str,
                               champion: str, info: Mapping[str, Any], hist_acc: Mapping[str, Any], session: str,
                               replay_id: str, pending: list[Pending], contrib: list[dict[str, Any]]) -> int:
        lane, m = self.lane, self.lane.market
        tgt_info = self._target(t, h)
        target = str(tgt_info["target"]) if tgt_info["status"] == "DERIVED" else f"UNAVAILABLE:{tgt_info['reason']}"
        pc = self.pred_cfg
        min_p = float(pc["min_probability"])
        direction = "LONG" if p_cal >= 0.5 else ("SHORT" if lane.intraday and pc.get("allow_short_intraday") else "AVOID")
        close = float(m.close[t])
        atr = float(m.natr[t] * close) if math.isfinite(m.natr[t]) else float("nan")
        stop_mult, tgt_mult = float(pc["stop_atr"]), float(pc["target_atr"])
        stop = close - stop_mult * atr if math.isfinite(atr) else None
        target_px = close + tgt_mult * atr if math.isfinite(atr) else None
        cost_bps = (lane.costs.buy + lane.costs.sell) * 1e4
        edge = p_cal * tgt_mult - (1 - p_cal) * stop_mult
        reasons, blockers = [], []
        if kind == "baseline":
            blockers.append("the baseline is the base-rate benchmark, never a trading signal")
        elif kind != champion:
            blockers.append(f"not the accepted (champion) model; champion is {champion}"
                            + (" (no model has beaten the base rate out of sample yet)" if champion == "baseline" else ""))
        if not cal_status.startswith("PLATT"):
            blockers.append("confidence not calibrated yet")
        if hist_acc.get("n", 0) < int(pc["min_rolling_outcomes"]):
            blockers.append(f"only {hist_acc.get('n', 0)} sequential outcomes")
        if direction != "LONG":
            blockers.append("bearish or neutral; delivery shorting is not modelled" if not lane.intraday else "not long")
        if p_cal < min_p and direction == "LONG":
            blockers.append(f"calibrated probability {p_cal:.3f} < {min_p}")
        if edge <= 0:
            blockers.append("expected R-multiple after the stop/target geometry is not positive")
        if m.no_signal[t]:
            blockers.append("bar flagged (incomplete or near a price discontinuity)")
        if tgt_info["status"] != "DERIVED":
            blockers.append(f"no eligible target bar ({tgt_info['reason']})")
        model_id, digest = info.get("model_id"), info.get("artifact_sha256")
        approval = self.db.model_approval(model_id, digest) if t == self.latest_idx else None
        opp = self._opportunity(t, h, direction, p_cal, stop, target_px,
                                f"{model_id}@{digest}" if model_id and digest else None)
        missing = opportunity_missing(opp)
        mode, status, status_reasons, actionable = self._classify(t, approval=approval, missing=missing,
                                                                  blockers=blockers, target=tgt_info)
        if actionable:
            reasons.append(f"champion {kind} calibrated P(up)={p_cal:.3f} >= {min_p}; rolling OOS accuracy "
                           f"{hist_acc.get('acc')} over {hist_acc.get('n')} outcomes")
        not_actionable = list(blockers) + ([f"{OPPORTUNITY_INCOMPLETE}: missing {missing}"] if missing else []) + \
            ([f"{status}: {', '.join(status_reasons)}"] if not actionable and status not in (REPLAY,) else [])
        natural = f"{replay_id}|MODEL|{kind}|h{h}|{int(m.end_ns[t])}"
        payload = {"decision_bar": lane.label(t), "entry_rule": "buy at the open of the next bar (market-on-open)",
                   "reference_close": close, "atr14": atr, "expected_rr": tgt_mult / stop_mult,
                   "expected_r_multiple": edge, "train_acc_last_fit": info.get("train_acc"),
                   "model_id": model_id, "model_artifact_sha256": digest,
                   "model_approval": "APPROVED" if approval else "NOT_APPROVED (adaptive refit; no recorded approval)",
                   "trained_through": self.lane.label(int(self._cutoff_for(t))),
                   "step_session": session,
                   "train_rows": info.get("rows"),
                   "historical_oos": hist_acc,
                   "supporting_indicators": contrib, "reasons": reasons, "not_actionable_because": not_actionable,
                   "market_conditions": self._conditions(t), "horizon_label": self._h_label(h),
                   "data_last_session": str(m.sessions[-1]), "data_source_of_decision_bar": str(lane.source[t])
                   if t < len(lane.source) else None, "engine": ENGINE_VERSION,
                   "risk_warnings": ["research output, not investment advice", "costs are assumptions, not broker-verified",
                                     "corporate actions are UNKNOWN; large gaps are flagged, not adjusted"],
                   "status": status, "status_reasons": status_reasons, "output_contract": OUTPUT_CONTRACT,
                   "freshness": self.freshness.to_dict() if (self.freshness and t == self.latest_idx) else None,
                   "target_contract": {"decision_bar": lane.label(t), "declared_horizon": h, "derived_target": target,
                                       "derivation": tgt_info["derivation"]},
                   "neutral_band": self.band.to_dict(), "opportunity": opp,
                   "live_decision": self.last_decision}
        new = self.db.add_prediction({
            "natural_key": natural, "mode": mode, "replay_id": replay_id, "symbol": lane.symbol,
            "timeframe": lane.timeframe, "lane_key": lane.key, "source_kind": "MODEL", "source_id": kind,
            "source_version": replay_id, "horizon_bars": h, "decision_label": lane.label(t), "decision_ns": int(m.end_ns[t]),
            "target_session": target, "direction": direction, "prob_raw": p_raw, "prob_calibrated": p_cal,
            "calibration": cal_status, "actionable": int(actionable), "entry_ref": close, "stop": stop, "target": target_px,
            "cost_bps": cost_bps, "data_fingerprint": lane.data_fingerprint, "feature_config_sha": lane.feature_config_sha,
            "payload": payload})
        if t == self.latest_idx:
            self.live_outputs.append({"source": f"MODEL:{kind}:h{h}", "status": status, "reasons": status_reasons})
        if new:
            pending.append(Pending(sha(natural)[:24], "MODEL", kind, h, t, p_cal, direction, actionable,
                                   {"max_hold_bars": h, "stop_atr": stop_mult, "target_atr": tgt_mult, "trail_atr": None},
                                   target))
        return int(new)

    def _conditions(self, t: int) -> dict[str, Any]:
        lane = self.lane
        out: dict[str, Any] = {}
        for key, name in (("rsi14", "RSI(period=14).value"), ("adx14", "ADX(period=14).adx"),
                          ("natr14", "ATR(period=14).natr"), ("dist_ema50", "EMA(period=50).distance"),
                          ("weekly_rsi14", "1w:RSI(period=14).value")):
            if name in lane.names:
                v = lane.X[t, lane.names.index(name)]
                out[key] = None if not np.isfinite(v) else round(float(v), 4)
        return out

    def _h_label(self, h: int) -> str:
        labels = self.cfg["horizons"].get("labels", {})
        return labels.get(str(h), f"{h} bars") if not self.lane.intraday else f"{h} x {self.lane.timeframe} bars (intraday)"

    # ------------------------------------------------------------------ rules and library strategies
    def _library_masks(self) -> list[tuple[dict[str, Any], np.ndarray]]:
        if self.library is None or self.bars_all is None:
            return []
        out = []
        for r in self.library.list(self.lane.symbol, self.lane.timeframe, active_only=True):
            try:
                doc = self.library.get(r["strategy_id"], r["version"])
            except Exception as exc:  # integrity failure is reported, never ignored
                self.db.event("STRATEGY_LOAD_FAILED", {"strategy_id": r["strategy_id"], "error": str(exc)},
                              subject=r["strategy_id"], level="ERROR")
                self.progress.error(f"strategy {r['strategy_id']}: {exc}")
                continue
            if not scoped(doc, self.lane.symbol, self.lane.timeframe):
                continue
            feats = strategy_features(doc, self.bars_all, self.lane.timeframe)
            mask = strategy_mask(doc, feats, len(self.lane.market)) & ~self.lane.market.no_signal
            out.append((doc, mask))
        return out

    def _signal_predictions(self, rules: Sequence[Rule], strategies: Sequence[tuple[dict[str, Any], np.ndarray]],
                            dec: np.ndarray, session: str, replay_id: str, pending: list[Pending]) -> int:
        lane, m = self.lane, self.lane.market
        new = 0
        cut_ns = int(m.end_ns[int(dec[0]) if not lane.intraday else max(0, int(dec[0]) - 1)])
        sources: list[tuple[str, str, str, np.ndarray, dict[str, Any], float | None, str, dict[str, Any]]] = []
        for r in rules:
            sources.append(("RULE", r.rule_id, replay_id, r.mask(lane), r.exit, r.in_sample.get("exact_win_rate"),
                            r.tier, {"text": r.text(), "in_sample_selection_stats": r.in_sample,
                                     "multiple_testing": "Benjamini-Hochberg over every test of the search"}))
        for doc, mask in strategies:
            sources.append(("STRATEGY", doc["strategy_id"], str(doc["version"]), mask, doc["exit"], None, "LIBRARY",
                            {"name": doc["name"], "origin": doc.get("origin"),
                             "discovered_through": (doc.get("provenance") or {}).get("discovered_through")}))
        for kind, sid, ver, mask, ex, p, tier, info in sources:
            seen_through = info.get("discovered_through")
            for t in dec.tolist():
                if not mask[t] or t + 1 > len(m):
                    continue
                if seen_through and str(m.sessions[t]) < str(seen_through):
                    # The strategy was selected on data that includes this bar's outcome: scoring it here would be
                    # in-sample evidence presented as out-of-sample, so it is skipped and counted.
                    self.in_sample_skipped += 1
                    continue
                h_sig = int(ex["max_hold_bars"])
                tgt_info = self._target(t, h_sig)
                target_label = (str(tgt_info["target"]) if tgt_info["status"] == "DERIVED"
                                else f"UNAVAILABLE:{tgt_info['reason']}")
                close = float(m.close[t])
                atr = float(m.natr[t] * close) if math.isfinite(m.natr[t]) else float("nan")
                stop = close - ex["stop_atr"] * atr if ex.get("stop_atr") and math.isfinite(atr) else None
                tgt = close + ex["target_atr"] * atr if ex.get("target_atr") and math.isfinite(atr) else None
                acc = self._accuracy(replay_id, sid, int(ex["max_hold_bars"]), cut_ns, kind=kind)
                blockers = []
                if kind == "RULE" and tier != "FDR_SELECTED":
                    blockers.append("rule did not survive the FDR correction (exploratory)")
                if acc.get("n", 0) < int(self.pred_cfg["min_rolling_outcomes"]):
                    blockers.append(f"only {acc.get('n', 0)} sequential outcomes for this rule")
                elif (acc.get("mean_trade_net") or 0) <= 0:
                    blockers.append("sequential out-of-sample mean trade return is not positive")
                if tgt_info["status"] != "DERIVED":
                    blockers.append(f"no eligible target bar ({tgt_info['reason']})")
                opp = self._opportunity(t, h_sig, "LONG", p, stop, tgt, None, f"{sid}@{ver}")
                opp["model_version"] = f"{kind}:{sid}@{ver}"
                missing = opportunity_missing(opp, strategy_involved=True)
                strategy_ev = None
                if kind == "STRATEGY" and t == self.latest_idx:
                    from .governance import strategy_state

                    st = strategy_state(self.db, sid, ver)
                    strategy_ev = {"strategy_id": sid, "version": ver, "stage": st.get("stage"), "seq": st.get("seq")}
                mode, status, status_reasons, actionable = self._classify(
                    t, approval=None, missing=missing, blockers=blockers, target=tgt_info, model_required=False,
                    strategy_involved=True, strategy=strategy_ev)
                if t == self.latest_idx:
                    self.live_outputs.append({"source": f"{kind}:{sid}", "status": status, "reasons": status_reasons})
                natural = f"{replay_id}|{kind}|{sid}@{ver}|h{ex['max_hold_bars']}|{int(m.end_ns[t])}"
                ok = self.db.add_prediction({
                    "natural_key": natural, "mode": mode, "replay_id": replay_id,
                    "symbol": lane.symbol, "timeframe": lane.timeframe, "lane_key": lane.key, "source_kind": kind,
                    "source_id": sid, "source_version": ver, "horizon_bars": int(ex["max_hold_bars"]),
                    "decision_label": lane.label(t), "decision_ns": int(m.end_ns[t]),
                    "target_session": target_label,
                    "direction": "LONG", "prob_raw": p, "prob_calibrated": None,
                    "calibration": "NOT_APPLICABLE (rule/strategy; in-sample win rate shown as raw)",
                    "actionable": int(actionable), "entry_ref": close, "stop": stop, "target": tgt,
                    "cost_bps": (lane.costs.buy + lane.costs.sell) * 1e4, "data_fingerprint": lane.data_fingerprint,
                    "feature_config_sha": lane.feature_config_sha,
                    "payload": {**info, "tier": tier, "decision_bar": lane.label(t), "exit": ex,
                                "entry_rule": "buy at the open of the next bar", "historical_oos": acc,
                                "not_actionable_because": blockers + (
                                    [f"{OPPORTUNITY_INCOMPLETE}: missing {missing}"] if missing else []) + (
                                    [f"{status}: {', '.join(status_reasons)}"] if not actionable and status != REPLAY
                                    else []),
                                "market_conditions": self._conditions(t),
                                "data_last_session": str(m.sessions[-1]), "engine": ENGINE_VERSION,
                                "status": status, "status_reasons": status_reasons, "output_contract": OUTPUT_CONTRACT,
                                "target_contract": {"decision_bar": lane.label(t), "declared_horizon": h_sig,
                                                    "derived_target": target_label,
                                                    "derivation": tgt_info["derivation"]},
                                "opportunity": opp, "live_decision": self.last_decision}})
                if ok:
                    pending.append(Pending(sha(natural)[:24], kind, sid, h_sig, int(t), p, "LONG",
                                           actionable, dict(ex), target_label))
                    new += 1
        return new

    # ------------------------------------------------------------------ evaluation
    def _load_pending(self) -> list[Pending]:
        rows = self.db.q("SELECT p.* FROM predictions p LEFT JOIN evaluations e ON e.prediction_id=p.prediction_id "
                         "WHERE p.replay_id=? AND p.lane_key=? AND e.prediction_id IS NULL", (self._replay_hint(), self.lane.key))
        out = []
        end_ns = self.lane.market.end_ns
        for r in rows:
            t = int(np.searchsorted(end_ns, int(r["decision_ns"])))
            if t >= len(end_ns) or int(end_ns[t]) != int(r["decision_ns"]):
                continue
            payload = json.loads(r["payload_json"])
            ex = payload.get("exit") or {"max_hold_bars": r["horizon_bars"], "stop_atr": self.pred_cfg["stop_atr"],
                                         "target_atr": self.pred_cfg["target_atr"], "trail_atr": None}
            out.append(Pending(r["prediction_id"], r["source_kind"], r["source_id"], int(r["horizon_bars"]), t,
                               r["prob_calibrated"] if r["prob_calibrated"] is not None else r["prob_raw"],
                               r["direction"], bool(r["actionable"]), ex, r["target_session"]))
        return out

    def _replay_hint(self) -> str:
        return getattr(self, "replay_id", self.base_replay_id)

    def _evaluate(self, pending: list[Pending], last_known: int) -> int:
        m = self.lane.market
        done = 0
        keep: list[Pending] = []
        for p in pending:
            h = p.horizon
            need = p.t + 1 + h  # the trade simulator's time exit uses the open of bar t+1+h
            if self.lane.intraday:
                need = min(need, int(m.session_last[p.t + 1]) if p.t + 1 < len(m) else need)
            if need > last_known:
                keep.append(p)
                continue
            policy = ExitPolicy.from_dict(p.exit)
            if p.source_kind == "MODEL" and p.target and not p.target.startswith("UNAVAILABLE") \
                    and p.t + h < len(m) and not self._same_bar(p.t + h, p.target):
                # The bar h positions later is not the frozen target (missing bar / data revision): never re-target.
                self.db.add_evaluation({"prediction_id": p.prediction_id, "outcome_label": "NOT_COMPARABLE",
                                        "exit_reason": "NOT_COMPARABLE",
                                        "payload_json": {"reason": TARGET_HORIZON_MISMATCH, "frozen_target": p.target,
                                                         "bar_at_horizon": self.lane.label(p.t + h)}})
                continue
            trade = self.guard.trade(p.t, policy, last_known, self.lane.costs)
            movement = None
            if p.source_kind == "MODEL":
                if self.lane.intraday and m.session_id[p.t + h] != m.session_id[p.t]:
                    realized = None
                else:
                    realized = float(self.guard.labels(np.array([p.t]), h, last_known)[0])
                movement = None if realized is None else classify_movement(realized + self.guard.cost, self.band)
                up = None if movement in (None, NEUTRAL) else int(movement == UP)
            else:
                realized = trade["net_return"] if trade else None
                up = None if realized is None else int(realized > 0)
            if up is None and trade is None:
                self.db.add_evaluation({"prediction_id": p.prediction_id, "outcome_label": "NOT_EVALUABLE",
                                        "payload_json": {"reason": "trade path blocked (discontinuity) or label crosses "
                                                                   "a session"}})
                continue
            prob = p.p
            if p.source_kind == "MODEL":
                predicted = None if prob is None else (UP if prob >= 0.5 else DOWN)
                correct = direction_correct(predicted, movement) if predicted else None
                brier = brier_up(prob, movement)
            else:
                correct = up
                brier = (prob - up) ** 2 if (prob is not None and up is not None) else None
            self.db.add_evaluation({
                "prediction_id": p.prediction_id, "outcome_label": self.lane.label(min(need, len(m) - 1)),
                "outcome_ns": int(m.end_ns[min(need, len(m) - 1)]), "realized_return": realized, "up": up,
                "correct": correct, "brier": brier,
                "trade_net_return": trade["net_return"] if trade else None,
                "exit_reason": trade["exit_reason"] if trade else None, "bars_held": trade["bars_held"] if trade else None,
                "payload_json": {"trade": {k: trade[k] for k in ("entry", "exit", "entry_price", "exit_price",
                                                                 "gross_return", "net_return", "mfe", "mae")} if trade else None,
                                 "evaluated_at_replay_bar": self.lane.label(last_known),
                                 "label_definition": "close[t+h]/open[t+1]-1 minus round-trip cost" if p.source_kind == "MODEL"
                                 else "simulated trade net return",
                                 "movement": movement, "neutral_band": self.band.to_dict(),
                                 "frozen_target": p.target, "declared_horizon": h, "engine": ENGINE_VERSION}})
            done += 1
        pending[:] = keep
        return done

    def _same_bar(self, i: int, label: str) -> bool:
        m = self.lane.market
        if "T" in label:
            from .targets import label_ns
            return int(m.end_ns[i]) == label_ns(label)
        return str(m.sessions[i]) == label[:10]

    def _calibration_data(self, replay_id: str, h: int, cutoff_ns: int) -> list[tuple[str, float, int]]:
        rows = self.db.q("SELECT p.source_id, p.prob_raw, e.up FROM predictions p JOIN evaluations e ON "
                         "e.prediction_id=p.prediction_id WHERE p.replay_id=? AND p.lane_key=? AND p.horizon_bars=? AND "
                         "p.source_kind='MODEL' AND e.up IS NOT NULL AND e.outcome_ns <= ?",
                         (replay_id, self.lane.key, h, cutoff_ns))
        return [(r["source_id"], float(r["prob_raw"]), int(r["up"])) for r in rows if r["prob_raw"] is not None]

    def _accuracy(self, replay_id: str, source_id: str, h: int, cutoff_ns: int, kind: str = "MODEL") -> dict[str, Any]:
        rows = self.db.q("SELECT e.correct, e.trade_net_return FROM predictions p JOIN evaluations e ON "
                         "e.prediction_id=p.prediction_id WHERE p.replay_id=? AND p.lane_key=? AND p.source_kind=? AND "
                         "p.source_id=? AND p.horizon_bars=? AND e.outcome_ns <= ? ORDER BY p.decision_ns",
                         (replay_id, self.lane.key, kind, source_id, h, cutoff_ns))
        c = [r["correct"] for r in rows if r["correct"] is not None]
        tr = [r["trade_net_return"] for r in rows if r["trade_net_return"] is not None]
        last = c[-20:]
        return {"n": len(c), "acc": round(float(np.mean(c)), 4) if c else None,
                "rolling20_acc": round(float(np.mean(last)), 4) if last else None,
                "trades": len(tr), "mean_trade_net": round(float(np.mean(tr)), 5) if tr else None}

    def _oos_summary(self, replay_id: str) -> dict[str, Any]:
        r = self.db.one("SELECT COUNT(e.correct) n, AVG(e.correct) acc, COUNT(e.trade_net_return) trades, "
                        "AVG(CASE WHEN p.actionable=1 THEN e.trade_net_return END) mean_trade_net, "
                        "SUM(p.actionable) actionable FROM predictions p JOIN evaluations e ON e.prediction_id=p.prediction_id "
                        "WHERE p.replay_id=? AND p.lane_key=?", (replay_id, self.lane.key))
        return {k: (round(v, 5) if isinstance(v, float) else v) for k, v in (r or {}).items()}

    # ------------------------------------------------------------------ champion / challenger
    def _champion(self, h: int) -> str:
        r = self.db.one("SELECT model_kind FROM champions WHERE lane_key=? AND horizon=?", (self._ck(), h))
        return r["model_kind"] if r else "baseline"

    def _ck(self) -> str:
        return f"{self.lane.key}|{self._replay_hint()}"

    def _champion_review(self, replay_id: str, cutoff_ns: int) -> None:
        """Promote a challenger only on paired evidence over the same decision bars.

        For the trailing ``champion_window`` outcomes known by the cutoff, the per-prediction Brier
        difference (challenger - champion) must average below ``-champion_margin_brier`` and be
        significant in a one-sided t-test (p < ``champion_p``) whose effective sample size is
        ``n / h`` because h-bar outcomes overlap. This was tightened from a margin-only rule after
        champions flip-flopped on noise in the 2026-08-13 replay; it can only make promotion rarer.
        """
        from scipy import stats as sps

        W = int(self.r["champion_window"])
        need = int(self.r["champion_min_outcomes"])
        margin = float(self.r["champion_margin_brier"])
        alpha = float(self.r.get("champion_p", 0.05))
        for h in self.horizons:
            current = self._champion(h)
            if not self.db.one("SELECT 1 FROM champions WHERE lane_key=? AND horizon=?", (self._ck(), h)):
                self.db.conn.execute("INSERT INTO champions VALUES (?,?,?,?,?,?,?,?)",
                                     (self._ck(), h, "baseline", None, utc_now(), "initial: baseline until a challenger "
                                      "shows significant out-of-sample improvement", "{}", self.db.run_id))
                current = "baseline"
            briers: dict[str, dict[int, float]] = {}
            for kind in self.kinds:
                rows = self.db.q("SELECT p.decision_ns, e.brier FROM predictions p JOIN evaluations e ON "
                                 "e.prediction_id=p.prediction_id WHERE p.replay_id=? AND p.lane_key=? AND "
                                 "p.source_kind='MODEL' AND p.source_id=? AND p.horizon_bars=? AND e.brier IS NOT NULL "
                                 "AND e.outcome_ns <= ? ORDER BY p.decision_ns DESC LIMIT ?",
                                 (replay_id, self.lane.key, kind, h, cutoff_ns, W))
                briers[kind] = {int(r["decision_ns"]): float(r["brier"]) for r in rows}
            base = briers.get(current, {})
            best: tuple[float, str, dict[str, Any]] | None = None
            for kind in self.kinds:
                if kind == current:
                    continue
                common = sorted(set(base) & set(briers.get(kind, {})))
                if len(common) < need:
                    continue
                d = np.array([briers[kind][k] - base[k] for k in common])
                n_eff = max(2.0, len(d) / max(1, h))
                sd = float(d.std(ddof=1)) if len(d) > 1 else 0.0
                tstat = float(d.mean() / (sd / np.sqrt(n_eff))) if sd > 0 else (-np.inf if d.mean() < 0 else np.inf)
                p = float(sps.t.cdf(tstat, df=max(1.0, n_eff - 1)))
                ev = {"n": len(d), "n_eff": round(n_eff, 1), "mean_brier_diff": float(d.mean()), "t": tstat, "p": p}
                if d.mean() < -margin and p < alpha and (best is None or d.mean() < best[0]):
                    best = (float(d.mean()), kind, ev)
            if best is not None:
                _, kind, ev = best
                self.db.conn.execute("INSERT INTO champions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(lane_key, horizon) DO "
                                     "UPDATE SET model_kind=excluded.model_kind, since_utc=excluded.since_utc, "
                                     "reason=excluded.reason, metrics_json=excluded.metrics_json, run_id=excluded.run_id",
                                     (self._ck(), h, kind, None, utc_now(),
                                      f"paired Brier diff {ev['mean_brier_diff']:.4f} vs {current} over {ev['n']} outcomes "
                                      f"(n_eff {ev['n_eff']}), one-sided p={ev['p']:.4f}", json.dumps(ev), self.db.run_id))
                self.db.event("MODEL_PROMOTED", {"lane": self.lane.key, "replay_id": replay_id, "horizon": h,
                                                 "from": current, "to": kind, "evidence": ev,
                                                 "rule": f"window {W}, min {need} paired outcomes, margin {margin}, "
                                                         f"one-sided p < {alpha} with n_eff = n/h"},
                              subject=self.lane.key)

    def _cutoff_for(self, t: int) -> int:
        m = self.lane.market
        if not self.lane.intraday:
            return t
        first = int(np.searchsorted(m.session_id, m.session_id[t], side='left'))
        return max(0, first - 1)
