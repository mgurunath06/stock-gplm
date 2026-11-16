"""M5.9A shared prediction-output contract: one status vocabulary, one movement vocabulary,
the neutral band, the actionable-opportunity contract, freshness reasons and the LIVE gate.

Every corrected prediction path (M5.8 frozen forecasts and the rolling ``gplm daily`` engine)
uses these definitions; nothing here fabricates data, approvals, stops or targets. Missing
evidence always resolves to a non-actionable, non-LIVE classification with explicit reasons.

Status vocabulary (authoritative meaning)
-----------------------------------------
``RESEARCH``     a frozen research forecast (M5.8 ``predict``/``walk-forward``); never actionable
                 as a live opportunity, scored only by a separate reveal.
``REPLAY``       produced on a historical bar during sequential replay; outcomes may already exist
                 in the data, so it is evidence about the method, never a current opportunity.
``SHADOW``       produced on the latest eligible completed bar from fresh, complete inputs, but
                 the model/strategy lacks an approved immutable artifact (e.g. an adaptive refit).
``LIVE``         latest eligible completed bar, fresh complete non-quarantined data, an approved
                 immutable model artifact (and approved strategy where one is involved), an
                 eligible universe, the full opportunity contract and every safety gate passed.
``NO_TRADE``     inputs and model are acceptable, but the action gates or the opportunity
                 contract did not pass; informational only.
``UNAVAILABLE``  no forecast may be shown: stale, incomplete, quarantined or calendar-unknown
                 inputs, or no eligible target bar. Always carries a reason code.

Legacy mapping: ``mode='HISTORICAL_FROZEN'`` -> RESEARCH; legacy rolling ``mode='LIVE'`` rows written
before M5.9A (engine ``gplm-rolling.v1``) -> ``LEGACY_UNVERIFIED_LIVE`` (they never passed this gate);
``direction='UNCERTAIN'`` is the abstention token (confidence), never a price class; legacy
``actual_direction='FLAT'`` -> NEUTRAL with a zero band.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

OUTPUT_CONTRACT = "stock-gplm.prediction-output.m59a.v1"

RESEARCH, REPLAY, SHADOW, LIVE, NO_TRADE, UNAVAILABLE = (
    "RESEARCH", "REPLAY", "SHADOW", "LIVE", "NO_TRADE", "UNAVAILABLE")
STATUSES = (RESEARCH, REPLAY, SHADOW, LIVE, NO_TRADE, UNAVAILABLE)
LEGACY_STATUS_MAP = {"HISTORICAL_FROZEN": RESEARCH, "REPLAY": REPLAY, "LIVE": "LEGACY_UNVERIFIED_LIVE"}

UP, DOWN, NEUTRAL = "UP", "DOWN", "NEUTRAL"
MOVEMENTS = (UP, DOWN, NEUTRAL)
ABSTAIN_TOKEN = "UNCERTAIN"  # stored prediction token meaning "abstain"; NOT a movement class

# Freshness / availability reason codes
FRESH = "FRESH"
STALE_DATA = "STALE_DATA"
INCOMPLETE_SESSION = "INCOMPLETE_SESSION"
MISSING_EXPECTED_BAR = "MISSING_EXPECTED_BAR"
CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"
QUARANTINED_INPUT = "QUARANTINED_INPUT"
END_OF_SESSION = "END_OF_SESSION"
TARGET_HORIZON_MISMATCH = "TARGET_HORIZON_MISMATCH"
MODEL_NOT_APPROVED = "MODEL_NOT_APPROVED"
STRATEGY_NOT_APPROVED = "STRATEGY_NOT_APPROVED"
UNIVERSE_NOT_ELIGIBLE = "UNIVERSE_NOT_ELIGIBLE"
OPPORTUNITY_INCOMPLETE = "OPPORTUNITY_INCOMPLETE"
NOT_LATEST_BAR = "NOT_LATEST_BAR"
GATES_FAILED = "GATES_FAILED"


# ----------------------------------------------------------------------------- movement / neutral band
NEUTRAL_BAND_VERSION = "neutral-band.v1"
_BAND_DEFAULTS = {"mode": "cost_aware", "floor_bps": 0.0, "cost_multiplier": 1.0}


@dataclass(frozen=True)
class NeutralBand:
    """|gross move| <= ``threshold`` is NEUTRAL (the boundary itself is NEUTRAL)."""

    threshold: float
    mode: str
    version: str = NEUTRAL_BAND_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "mode": self.mode, "threshold": self.threshold}


def neutral_band(cfg: Mapping[str, Any] | None, round_trip_cost: float | None) -> NeutralBand:
    """Versioned neutral band. ``cost_aware`` (default) uses ``max(floor, multiplier * round-trip cost)``
    when a cost policy exists; ``fixed`` uses ``floor_bps`` only."""
    c = {**_BAND_DEFAULTS, **dict(cfg or {})}
    floor = float(c["floor_bps"]) / 1e4
    if c["mode"] == "fixed" or round_trip_cost is None:
        return NeutralBand(floor, "fixed" if c["mode"] == "fixed" else "fixed (no cost policy)")
    if c["mode"] != "cost_aware":
        raise ValueError(f"unsupported neutral band mode {c['mode']!r}")
    return NeutralBand(max(floor, float(c["cost_multiplier"]) * float(round_trip_cost)), "cost_aware")


def classify_movement(gross: float | None, band: NeutralBand | float) -> str | None:
    """UP if gross > band, DOWN if gross < -band, NEUTRAL inside [-band, band]; None if unknown."""
    if gross is None or not math.isfinite(float(gross)):
        return None
    thr = band.threshold if isinstance(band, NeutralBand) else float(band)
    g = float(gross)
    return UP if g > thr else DOWN if g < -thr else NEUTRAL


def direction_correct(predicted: str, movement: str | None) -> int | None:
    """Directional correctness: only UP/DOWN calls against UP/DOWN outcomes are scored.
    Abstentions and NEUTRAL outcomes are not directional evidence (None)."""
    pred = {"LONG": UP, "SHORT": DOWN}.get(predicted, predicted)
    if pred not in (UP, DOWN) or movement not in (UP, DOWN):
        return None
    return int(pred == movement)


def brier_up(prob_up: float | None, movement: str | None) -> float | None:
    """Brier score of P(UP) against a non-neutral outcome; NEUTRAL outcomes are not scored."""
    if prob_up is None or movement not in (UP, DOWN):
        return None
    return (float(prob_up) - (1.0 if movement == UP else 0.0)) ** 2


# ----------------------------------------------------------------------------- actionable contract
ACTIONABLE_FIELDS = ("action", "entry", "invalidation", "stop", "target", "horizon", "probability",
                     "expected_return", "model_version", "timestamp", "freshness_status")


def opportunity_missing(fields: Mapping[str, Any], strategy_involved: bool = False) -> list[str]:
    """Names of mandatory action fields that are absent (None/empty/non-finite). Never fills them."""
    need = list(ACTIONABLE_FIELDS) + (["strategy_version"] if strategy_involved else [])
    missing = []
    for k in need:
        v = fields.get(k)
        if v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and not math.isfinite(v)):
            missing.append(k)
    return missing


def validate_actionable_row(row: Mapping[str, Any], payload: Mapping[str, Any]) -> list[str]:
    """Schema check applied to every stored prediction with ``actionable=1``."""
    opp = payload.get("opportunity") or {}
    fields = {"action": row.get("direction"), "entry": opp.get("entry") or payload.get("entry_rule"),
              "invalidation": opp.get("invalidation"), "stop": row.get("stop"), "target": row.get("target"),
              "horizon": row.get("horizon_bars"),
              "probability": row.get("prob_calibrated") if row.get("prob_calibrated") is not None else row.get("prob_raw"),
              "expected_return": opp.get("expected_return"), "model_version": opp.get("model_version"),
              "timestamp": row.get("decision_label"), "freshness_status": opp.get("freshness_status"),
              "strategy_version": row.get("source_version")}
    return opportunity_missing(fields, strategy_involved=row.get("source_kind") in {"STRATEGY", "RULE"})


# ----------------------------------------------------------------------------- freshness
@dataclass
class Freshness:
    status: str  # FRESH or one reason code
    expected_session: str | None
    last_session: str | None
    reasons: list[str] = field(default_factory=list)

    @property
    def fresh(self) -> bool:
        return self.status == FRESH

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "expected_session": self.expected_session,
                "last_session": self.last_session, "reasons": list(self.reasons)}


def evaluate_freshness(last_session: str | None, expected_session: str | None, *, calendar_confirmed: bool,
                       last_bar_incomplete: bool = False, quarantined: bool = False) -> Freshness:
    """Absolute freshness of one symbol against the requested/as-of exchange session (never against
    other symbols in the batch). ``expected_session`` None means the calendar could not establish it."""
    reasons: list[str] = []
    if expected_session is None:
        reasons.append(CALENDAR_UNKNOWN)
    elif last_session is None or last_session < expected_session:
        reasons.append(STALE_DATA)
    elif last_session > expected_session:
        pass  # data runs past the as-of session: historical replay, not staleness
    if quarantined:
        reasons.append(QUARANTINED_INPUT)
    if last_bar_incomplete:
        reasons.append(INCOMPLETE_SESSION)
    if expected_session is not None and not calendar_confirmed and CALENDAR_UNKNOWN not in reasons:
        reasons.append(CALENDAR_UNKNOWN)
    status = FRESH if not reasons else reasons[0]
    return Freshness(status, expected_session, last_session, reasons)


# ----------------------------------------------------------------------------- LIVE gate
@dataclass
class LiveCheck:
    status: str
    reasons: list[str]
    actionable: bool

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons), "actionable": self.actionable,
                "contract": OUTPUT_CONTRACT}


def live_gate(*, latest_eligible_bar: bool, freshness: Freshness | None, model_approval: Mapping[str, Any] | None,
              strategy_involved: bool = False, strategy_approved: bool = False, universe_eligible: bool = True,
              missing_fields: Iterable[str] = (), gate_blockers: Iterable[str] = ()) -> LiveCheck:
    """Classify a latest-bar output. LIVE only when *every* requirement holds; otherwise the most
    fundamental failure decides: UNAVAILABLE (inputs) > REPLAY (not latest) > SHADOW (approval) > NO_TRADE."""
    missing, blockers = list(missing_fields), list(gate_blockers)
    if freshness is None or not freshness.fresh:
        return LiveCheck(UNAVAILABLE, list(freshness.reasons if freshness else [CALENDAR_UNKNOWN]), False)
    if not latest_eligible_bar:
        return LiveCheck(REPLAY, [NOT_LATEST_BAR], False)
    reasons: list[str] = []
    if not model_approval or not model_approval.get("model_id") or not model_approval.get("artifact_sha256"):
        reasons.append(MODEL_NOT_APPROVED)
    if strategy_involved and not strategy_approved:
        reasons.append(STRATEGY_NOT_APPROVED)
    if reasons:
        return LiveCheck(SHADOW, reasons + ([f"{OPPORTUNITY_INCOMPLETE}:{','.join(missing)}"] if missing else []), False)
    if not universe_eligible:
        reasons.append(UNIVERSE_NOT_ELIGIBLE)
    if missing:
        reasons.append(f"{OPPORTUNITY_INCOMPLETE}:{','.join(missing)}")
    if blockers:
        reasons.append(GATES_FAILED)
    if reasons:
        return LiveCheck(NO_TRADE, reasons + blockers, False)
    return LiveCheck(LIVE, [], True)


__all__ = [n for n in dir() if n.isupper() or n in {
    "NeutralBand", "neutral_band", "classify_movement", "direction_correct", "brier_up", "opportunity_missing",
    "validate_actionable_row", "Freshness", "evaluate_freshness", "LiveCheck", "live_gate"}]
