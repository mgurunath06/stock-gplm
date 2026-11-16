"""M5.9B fixed approved-model execution (the only model path that can produce ``LIVE``).

One registered immutable artifact (:mod:`.governance`) is loaded from its read-only store, its SHA-256
is re-verified, its feature schema / timeframe / horizon / cost-policy / neutral-band versions and symbol
scope are checked against the lane, and it is applied to the **latest eligible completed bar** with pure
NumPy inference (:meth:`DirectionModel.predict`). Nothing is fitted, updated or calibrated here; the
probability is the artifact's raw output (``calibration = NONE``). Identical inputs give identical output.

The result is classified by the canonical gate :func:`~.contracts.live_decision` and stored append-only
with the exact ``model_id`` and ``approval_id``. A revoked, expired, altered or incompatible artifact is
refused (no forecast is computed); an unapproved ``CANDIDATE`` runs but can only be ``SHADOW``.
Adaptive rolling refits (:mod:`.rolling`) never use this path and never become ``LIVE``.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

import numpy as np

from .contracts import (LIVE, NEUTRAL_BAND_VERSION, OUTPUT_CONTRACT, UNAVAILABLE, Freshness, live_decision,
                        opportunity_missing)
from .db import AuditDB, sha
from .governance import GovernanceError, load_artifact, model_record, verify_model_file
from .targets import derive_target

FIXED_ENGINE = "gplm-fixed-model.m59b.v1"


def model_live_evidence(db: AuditDB, model_id: str, lane: Any, cost_policy: str) -> tuple[dict[str, Any], Any]:
    """(gate evidence, loaded model or None). Read-only: re-hashes and re-loads the stored artifact."""
    rec = model_record(db, model_id)
    chk = verify_model_file(db, model_id)
    model = None
    if chk["file_present"] and chk["hash_verified"] and chk["schema_ok"]:
        model = load_artifact(rec["stored_path"])
    incompatible: list[str] = []
    if not chk["schema_ok"]:
        incompatible.append(f"artifact: {chk.get('error')}")
    if rec["feature_schema"] != lane.feature_config_sha:
        incompatible.append(f"feature_schema {rec['feature_schema']} != lane {lane.feature_config_sha}")
    if model is not None and int(np.max(model.prep.columns)) >= len(lane.names):
        incompatible.append("artifact feature columns exceed the lane feature matrix")
    if rec["timeframe"] != lane.timeframe:
        incompatible.append(f"timeframe {rec['timeframe']} != {lane.timeframe}")
    if rec["cost_policy_version"] != cost_policy:
        incompatible.append(f"cost policy {rec['cost_policy_version']} != configured {cost_policy}")
    if rec["neutral_band_version"] != NEUTRAL_BAND_VERSION:
        incompatible.append(f"neutral band {rec['neutral_band_version']} != {NEUTRAL_BAND_VERSION}")
    scope = rec["scope"]
    if not (scope.get("shared") is True or lane.symbol in scope.get("symbols", [])):
        incompatible.append(f"symbol {lane.symbol} outside artifact scope")
    approval = db.model_approval(model_id, rec["sha256"])
    ev = {"model_id": model_id, "artifact_sha256": rec["sha256"],
          "approval_id": approval["approval_id"] if approval else None, "state": rec["state"],
          "revoked": rec["state"] == "REVOKED", "expired": rec["state"] == "EXPIRED",
          "hash_verified": bool(chk["hash_verified"]), "schema_compatible": not incompatible,
          "incompatibilities": incompatible, "horizon": int(rec["horizon"]), "family": rec["family"],
          "neutral_band_version": rec["neutral_band_version"], "training_cutoff": rec["training_cutoff"]}
    return ev, model


def predict_fixed(db: AuditDB, lane: Any, model_id: str, *, freshness: Freshness | None, calendar: Any,
                  universe: Mapping[str, Any] | None, pred_cfg: Mapping[str, Any], cost_policy: str) -> dict[str, Any]:
    """Apply one fixed artifact to the lane's latest eligible completed bar and store the classified output."""
    ev, model = model_live_evidence(db, model_id, lane, cost_policy)
    refuse = [r for r, bad in (("MODEL_REVOKED", ev["revoked"]), ("MODEL_EXPIRED", ev["expired"]),
                               ("MODEL_HASH_MISMATCH", not ev["hash_verified"]),
                               ("MODEL_SCHEMA_INCOMPATIBLE", not ev["schema_compatible"])) if bad]
    if refuse or model is None:
        db.event("FIXED_MODEL_REFUSED", {"model_id": model_id, "lane": lane.key, "reasons": refuse,
                                         "incompatibilities": ev["incompatibilities"]}, subject=model_id, level="WARN")
        return {"status": "REFUSED", "reasons": refuse or ["MODEL_UNAVAILABLE"], "evidence": ev, "stored": False}
    frame = lane.bars.frame
    inc = frame["incomplete"].to_numpy(dtype=bool) if len(frame) else np.zeros(0, dtype=bool)
    ok = np.flatnonzero(~inc)
    if not len(ok):
        return {"status": UNAVAILABLE, "reasons": ["MISSING_EXPECTED_BAR"], "evidence": ev, "stored": False}
    t = int(ok[-1])
    m = lane.market
    h = int(ev["horizon"])
    session = str(m.sessions[t])
    p = float(model.predict(lane.X[[t]])[0])  # inference only: no fit / update / calibration
    tgt = derive_target(lane.timeframe, session, int(m.end_ns[t]), h, calendar=calendar, observed_frame=frame)
    target = str(tgt["target"]) if tgt["status"] == "DERIVED" else f"UNAVAILABLE:{tgt['reason']}"
    close = float(m.close[t])
    atr = float(m.natr[t] * close) if math.isfinite(float(m.natr[t])) else float("nan")
    stop_mult, tgt_mult, min_p = float(pred_cfg["stop_atr"]), float(pred_cfg["target_atr"]), float(pred_cfg["min_probability"])
    stop = close - stop_mult * atr if math.isfinite(atr) else None
    target_px = close + tgt_mult * atr if math.isfinite(atr) else None
    direction = "LONG" if p >= 0.5 else "AVOID"
    exp = (p * (target_px - close) / close - (1 - p) * (close - stop) / close
           if stop is not None and target_px is not None and close > 0 else None)
    fresh_status = freshness.status if freshness is not None else "UNKNOWN"
    opp = {"action": "BUY" if direction == "LONG" else None, "entry": "buy at the open of the next session",
           "invalidation": f"exit if price trades at or below the stop {stop:.4f}" if stop is not None else None,
           "stop": stop, "target": target_px, "horizon": h, "probability": p, "expected_return": exp,
           "model_version": f"{model_id}@{ev['artifact_sha256']}", "strategy_version": None,
           "timestamp": lane.label(t), "freshness_status": fresh_status}
    missing = opportunity_missing(opp)
    blockers: list[str] = []
    if direction != "LONG":
        blockers.append("bearish or neutral; delivery shorting is not modelled")
    elif p < min_p:
        blockers.append(f"probability {p:.3f} < {min_p}")
    if p * tgt_mult - (1 - p) * stop_mult <= 0:
        blockers.append("expected R-multiple after the stop/target geometry is not positive")
    if bool(m.no_signal[t]):
        blockers.append("bar flagged (incomplete or near a price discontinuity)")
    fresh = freshness
    if tgt["status"] != "DERIVED":
        fresh = Freshness(str(tgt["reason"]), getattr(freshness, "expected_session", None),
                          getattr(freshness, "last_session", None), [str(tgt["reason"])])
    covers = bool(calendar is not None and hasattr(calendar, "covers") and calendar.covers(session, target[:10]))
    decision = live_decision(latest_eligible_bar=True, freshness=fresh, calendar_id=getattr(calendar, "calendar_id", None),
                             calendar_covers=covers, model=ev, adaptive_model=False, strategy_driven=False,
                             universe=universe, opportunity_missing_fields=missing, cost_policy_version=cost_policy,
                             neutral_band_version=ev["neutral_band_version"], gate_blockers=blockers)
    mode = "LIVE" if decision.status == LIVE else "SHADOW"
    natural = f"{FIXED_ENGINE}|{model_id}|{lane.key}|h{h}|{int(m.end_ns[t])}|{ev['approval_id'] or 'unapproved'}"
    payload = {"engine": FIXED_ENGINE, "output_contract": OUTPUT_CONTRACT, "status": decision.status,
               "status_reasons": decision.reasons, "live_decision": decision.to_dict(),
               "freshness": freshness.to_dict() if freshness else None,
               "target_contract": {"decision_bar": lane.label(t), "declared_horizon": h, "derived_target": target,
                                   "derivation": tgt["derivation"]},
               "model_id": model_id, "model_artifact_sha256": ev["artifact_sha256"], "approval_id": ev["approval_id"],
               "model_state": ev["state"], "training_cutoff": ev["training_cutoff"], "family": ev["family"],
               "neutral_band": {"version": ev["neutral_band_version"]}, "opportunity": opp,
               "not_actionable_because": blockers, "reference_close": close, "atr14": atr,
               "calibration": "NONE (fixed artifact; never fitted, updated or recalibrated at prediction time)",
               "feature_row_sha": sha([None if not np.isfinite(v) else round(float(v), 10) for v in lane.X[t]])[:16],
               "risk_warnings": ["research output, not investment advice", "costs are assumptions, not broker-verified"]}
    stored = db.add_prediction({
        "natural_key": natural, "mode": mode, "replay_id": None, "symbol": lane.symbol, "timeframe": lane.timeframe,
        "lane_key": lane.key, "source_kind": "MODEL", "source_id": model_id, "source_version": FIXED_ENGINE,
        "horizon_bars": h, "decision_label": lane.label(t), "decision_ns": int(m.end_ns[t]), "target_session": target,
        "direction": direction, "prob_raw": p, "prob_calibrated": None, "calibration": "NONE",
        "actionable": int(decision.eligible), "entry_ref": close, "stop": stop, "target": target_px,
        "cost_bps": (lane.costs.buy + lane.costs.sell) * 1e4, "data_fingerprint": lane.data_fingerprint,
        "feature_config_sha": lane.feature_config_sha, "payload": payload})
    return {"status": decision.status, "reasons": decision.reasons, "eligible": decision.eligible,
            "probability": p, "direction": direction, "decision_bar": lane.label(t), "target": target,
            "evidence": decision.evidence, "stored": stored, "natural_key": natural}


def run_fixed(app: Any, symbol: str, model_id: str, *, as_of: str | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """One-stock fixed-model forecast (owner smoke): ingest one symbol, build its 1d lane, apply the artifact."""
    from .daily import now_ist, symbol_freshness
    from .governance import cost_policy_version, universe_membership
    from .ingest import Ingestor, normalize_symbol
    from .lanes import all_bars, build_lane
    from .session_calendar import expected_completed_session, load_registered_calendar

    s = app.s
    sym = normalize_symbol(symbol)
    data = Ingestor(s, app.db).ingest(sym)
    calendar = load_registered_calendar(s, app.db)
    expected, confirmed = expected_completed_session(calendar, as_of, None if as_of else (now or now_ist()))
    fresh = symbol_freshness(app, data.symbol, data.last_session, expected, confirmed)
    bars = all_bars(data, calendar)
    lane = build_lane(data, "1d", s.section("costs"), bars=bars, cache_dir=s.cache_dir / "features")
    if lane is None:
        raise GovernanceError(f"no 1d lane could be built for {sym}")
    out = predict_fixed(app.db, lane, model_id, freshness=fresh, calendar=calendar,
                        universe=universe_membership(app.db, data.symbol), pred_cfg=s.section("prediction"),
                        cost_policy=cost_policy_version(s.section("costs")))
    out.update(symbol=data.symbol, freshness=fresh.to_dict(), calendar=getattr(calendar, "source", None))
    return out


__all__ = ["FIXED_ENGINE", "model_live_evidence", "predict_fixed", "run_fixed"]
