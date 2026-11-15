"""Authoritative M5.6 status computation (M5.6.1).

Every M5.6 consumer (``run_m56``, the leaderboard, the dashboard API and the ``gplm m56-status``
report) derives a result's status here. The derivation never trusts the stored ``classification``
column. That column is append-only historical evidence. Before M5.6.1 it could hold ``VALIDATED`` for
M5.5 broad-screen rows that never faced an M5.6 gate. Derived views therefore recompute the status
from the stored metrics and keep the recorded label beside it, so corrections stay visible and the
original evidence is never rewritten.

Five independent dimensions are reported. One never implies another:

``discovery_status``
    How the candidate was generated, for example an M5.5 screen tier or an M5.4 engine tier.
``reference_backtest_status``
    Whether a hash-bound reference definition was simulated on the validation segment.
``validation_status``
    The M5.6 statistical gates: minimum effective trades, a family-wide Benjamini-Hochberg q-value,
    positive net mean, a drawdown limit and walk-forward fold stability.
``final_segment_status``
    The once-only final development segment, read only after validation passed.
``eligibility``
    ``PAPER_RESEARCH_ELIGIBLE`` only for ``VALIDATED``. Research forecasts only, never orders.

``VALIDATED`` requires both of these:

1. validation gates passed on evidence that carries every gate input, and
2. a ``TEST_ONCE`` final-segment score passed its own gates.

Missing gate inputs fail closed.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

from .db import sha

STATUS_CONTRACT = "stock-gplm.m56-status.v1"
SCREEN_PASS_TIERS = ("SCREEN_ROBUST", "SCREEN_VALIDATED", "SCREEN_VALIDATION_ONLY")
VALIDATION_STAGES = ("VALIDATION", "ADVANCED_VALIDATION")


@dataclass(frozen=True)
class GatePolicy:
    """Thresholds of the M5.6 gates. Built from the ``m56`` config section."""

    min_trades: int = 20
    fdr_q: float = 0.05
    max_drawdown: float = 0.30
    walk_forward_folds: int = 4
    cross_stock_positive_fraction: float = 0.60

    @classmethod
    def from_cfg(cls, cfg: Mapping[str, Any] | None) -> "GatePolicy":
        c = cfg or {}
        return cls(int(c.get("min_oos_trades", 20)), float(c.get("fdr_q", 0.05)), float(c.get("max_drawdown", 0.30)),
                   int(c.get("walk_forward_folds", 4)), float(c.get("cross_stock_positive_fraction", 0.60)))

    def to_dict(self) -> dict[str, Any]:
        return {"contract": STATUS_CONTRACT, **asdict(self)}

    @property
    def sha(self) -> str:
        return sha(self.to_dict())

    @property
    def required_positive_folds(self) -> int:
        return max(2, self.walk_forward_folds - 1)


def _check(passed: bool, value: Any, required: str) -> dict[str, Any]:
    return {"passed": bool(passed), "value": value, "required": required}


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN counts as missing


def validation_gates(met: Mapping[str, Any] | None, policy: GatePolicy) -> dict[str, Any]:
    """Apply every M5.6 validation gate. A missing input is a failed gate, never a pass."""
    m = met or {}
    n = _num(m.get("effective_n"))
    q = _num(m.get("q_value"))
    mean = _num(m.get("mean_net"))
    dd = _num(m.get("max_drawdown"))
    folds = m.get("folds")
    positive = (sum(1 for f in folds if _num((f or {}).get("mean_excess")) is not None and f["mean_excess"] > 0)
                if isinstance(folds, list) else None)
    checks = {
        "min_trades": _check(n is not None and n >= policy.min_trades, n, f">= {policy.min_trades} effective trades"),
        "multiple_testing": _check(q is not None and q <= policy.fdr_q, q,
                                   f"family-wide BH q <= {policy.fdr_q}"),
        "positive_net": _check(mean is not None and mean > 0, mean, "mean net return after costs > 0"),
        "drawdown": _check(dd is not None and dd >= -policy.max_drawdown, dd,
                           f"max drawdown >= -{policy.max_drawdown}"),
        "fold_stability": _check(positive is not None and positive >= policy.required_positive_folds, positive,
                                 f">= {policy.required_positive_folds} folds with positive excess"),
    }
    return _summarize(checks)


def final_gates(met: Mapping[str, Any] | None, policy: GatePolicy) -> dict[str, Any]:
    """Gates for the once-only final segment. Missing inputs fail closed."""
    m = met or {}
    n, mean, dd = _num(m.get("effective_n")), _num(m.get("mean_net")), _num(m.get("max_drawdown"))
    checks = {
        "min_trades": _check(n is not None and n >= policy.min_trades, n, f">= {policy.min_trades} effective trades"),
        "positive_net": _check(mean is not None and mean > 0, mean, "mean net return after costs > 0"),
        "drawdown": _check(dd is not None and dd >= -policy.max_drawdown, dd,
                           f"max drawdown >= -{policy.max_drawdown}"),
    }
    return _summarize(checks)


def _summarize(checks: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    failed = [k for k, v in checks.items() if not v["passed"]]
    missing = [k for k, v in checks.items() if v["value"] is None]
    return {"passed": not failed, "failed": failed, "missing": missing, "checks": dict(checks)}


def validation_evidence(met: Mapping[str, Any]) -> dict[str, Any]:
    """Compact validation inputs embedded in a final-segment row, so the row is self-contained."""
    keys = ("effective_n", "q_value", "mean_net", "max_drawdown", "folds", "p_value", "bh_family_size")
    return {k: met.get(k) for k in keys}


def _loads(value: Any) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        return value or {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _row_key(r: Mapping[str, Any]) -> tuple[Any, ...]:
    return (r.get("strategy_id"), r.get("symbol"), r.get("timeframe"), r.get("regime"), r.get("data_fingerprint"))


def derive(row: Mapping[str, Any], policy: GatePolicy,
           validation_sibling: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the authoritative status of one ``m56_results`` row.

    ``validation_sibling`` holds the metrics of the matching VALIDATION row. Legacy reference
    ``TEST_ONCE`` rows need it because they did not embed their validation evidence.
    """
    met = _loads(row.get("metrics") if "metrics" in row else row.get("metrics_json"))
    lineage = _loads(row.get("lineage") if "lineage" in row else row.get("lineage_json"))
    stage = str(row.get("evidence_stage") or "")
    recorded = str(row.get("classification") or "")
    reasons: list[str] = []
    discovery = "NOT_APPLICABLE"
    backtest = "NOT_APPLICABLE"
    validation = "NOT_EVALUATED"
    final = "NOT_SCORED"
    vg: dict[str, Any] | None = None
    fg: dict[str, Any] | None = None

    if stage == "BROAD_SCREEN":
        tier = str(met.get("tier") or "UNKNOWN")
        discovery = tier
        screen_val = met.get("validation") or {}
        vg = validation_gates({"effective_n": screen_val.get("trades"), "mean_net": screen_val.get("mean_net")},
                              policy)
        validation = "INSUFFICIENT_EVIDENCE"
        reasons.append("M5.5 broad-screen evidence: no family-wide BH q-value, drawdown or fold evidence; "
                       "M5.6 validation gates were never applied")
        if (met.get("test") or {}).get("scored"):
            final = "DEVELOPMENT_TEST_READ_BY_SCREEN"
            reasons.append("screen read the development test segment without M5.6 gates; not final-segment evidence")
        classification = "REJECTED" if tier == "SCREEN_REJECTED_VALIDATION" else "DISCOVERED"
    elif stage == "DISCOVERY_ENGINE":
        discovery = str(met.get("tier") or "UNKNOWN")
        validation = "INSUFFICIENT_EVIDENCE"
        reasons.append("M5.4 engine tier imported without M5.6 gate metrics")
        classification = "DISCOVERED"
    elif stage in VALIDATION_STAGES:
        if stage == "VALIDATION":
            backtest = "BACKTESTED_VALIDATION_SEGMENT"
        else:
            discovery = "LEARNED_TREE_PATH"
        vg = validation_gates(met, policy)
        validation = "PASSED" if vg["passed"] else "FAILED"
        classification = "EXPERIMENTAL" if vg["passed"] else "REJECTED"
        if vg["passed"]:
            reasons.append("validation gates passed; final segment not scored in this row")
    elif stage == "TEST_ONCE":
        if lineage.get("method") == "gradient_boosting_plus_decision_tree":
            discovery = "LEARNED_TREE_PATH"
        else:
            backtest = "BACKTESTED_VALIDATION_SEGMENT"
        evidence = met.get("validation_evidence")
        if evidence is None and validation_sibling is not None:
            evidence = validation_sibling
            reasons.append("validation evidence taken from the matching VALIDATION row")
        if evidence is None:
            validation = "INSUFFICIENT_EVIDENCE"
            reasons.append("final-segment row without verifiable validation evidence")
            vg = None
        else:
            vg = validation_gates(evidence, policy)
            validation = "PASSED" if vg["passed"] else "FAILED"
        fg = final_gates(met, policy)
        final = "PASSED" if fg["passed"] else "FAILED"
        if validation == "PASSED" and fg["passed"]:
            classification = "VALIDATED"
        elif not fg["passed"] or validation == "FAILED":
            classification = "REJECTED"
        else:
            classification = "EXPERIMENTAL"
    elif stage == "CROSS_STOCK_VALIDATION":
        frac = _num(met.get("positive_fraction"))
        stocks = int(met.get("stocks") or 0)
        n = _num(met.get("effective_n"))
        passed = (frac is not None and frac >= policy.cross_stock_positive_fraction and n is not None
                  and stocks >= 2 and n >= policy.min_trades * stocks)
        validation = "CROSS_STOCK_ONLY_PASSED" if passed else "FAILED"
        reasons.append("cross-stock validation-segment evidence only; never a final-segment score")
        classification = "EXPERIMENTAL" if passed else "REJECTED"
    else:
        validation = "INSUFFICIENT_EVIDENCE"
        reasons.append(f"unknown evidence stage {stage!r}; fail closed")
        classification = "REJECTED" if recorded == "REJECTED" else "DISCOVERED"

    if vg is not None and not vg["passed"]:
        reasons.append("failed validation gates: " + ", ".join(vg["failed"]))
    if fg is not None and not fg["passed"]:
        reasons.append("failed final-segment gates: " + ", ".join(fg["failed"]))
    if recorded != classification:
        reasons.append(f"recorded label {recorded} corrected to {classification} by {STATUS_CONTRACT}")
    return {
        "classification": classification,
        "recorded_classification": recorded,
        "label_corrected": recorded != classification,
        "discovery_status": discovery,
        "reference_backtest_status": backtest,
        "validation_status": validation,
        "final_segment_status": final,
        "eligibility": "PAPER_RESEARCH_ELIGIBLE" if classification == "VALIDATED" else "NOT_ELIGIBLE",
        "validation_gates": vg,
        "final_gates": fg,
        "reasons": reasons,
        "status_contract": STATUS_CONTRACT,
        "gate_policy_sha": policy.sha,
    }


def derive_rows(rows: Iterable[Mapping[str, Any]], policy: GatePolicy) -> list[dict[str, Any]]:
    """Derive every row. Metrics are parsed once and legacy final rows are joined to their validation row."""
    out: list[dict[str, Any]] = []
    parsed = []
    for raw in rows:
        r = dict(raw)
        if "metrics" not in r:
            r["metrics"] = _loads(r.pop("metrics_json", None))
        if "lineage" not in r and "lineage_json" in r:
            r["lineage"] = _loads(r.pop("lineage_json"))
        parsed.append(r)
    siblings = {_row_key(r): r["metrics"] for r in parsed if r.get("evidence_stage") == "VALIDATION"}
    for r in parsed:
        sib = siblings.get(_row_key(r)) if r.get("evidence_stage") == "TEST_ONCE" else None
        out.append({**r, "status": derive(r, policy, sib)})
    return out


def leaderboard(rows: Sequence[Mapping[str, Any]], policy: GatePolicy) -> list[dict[str, Any]]:
    """Group derived rows per (symbol, timeframe, regime). Only derived VALIDATED rows rank."""
    derived = derive_rows(rows, policy)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in derived:
        groups.setdefault((r["symbol"], r["timeframe"], r["regime"]), []).append(r)
    out = []
    for (symbol, tf, regime), values in sorted(groups.items()):
        valid = [x for x in values if x["status"]["classification"] == "VALIDATED"]
        valid.sort(key=lambda x: (x["metrics"].get("mean_net") or -1e9, x["metrics"].get("effective_n") or 0),
                   reverse=True)
        corrected = sum(1 for x in values if x["status"]["label_corrected"])
        out.append({"symbol": symbol, "timeframe": tf, "regime": regime,
                    "status": "VALIDATED" if valid else "NO VALIDATED STRATEGY",
                    "best": valid[0] if valid else None, "result_count": len(values),
                    "corrected_labels": corrected, "status_contract": STATUS_CONTRACT})
    return out


def correction_report(rows: Sequence[Mapping[str, Any]], policy: GatePolicy) -> dict[str, Any]:
    """Summarize every row whose recorded label differs from the authoritative status."""
    derived = derive_rows(rows, policy)
    corrected = [{"result_id": r.get("result_id"), "run_id": r.get("run_id"), "strategy_id": r.get("strategy_id"),
                  "symbol": r.get("symbol"), "timeframe": r.get("timeframe"), "regime": r.get("regime"),
                  "evidence_stage": r.get("evidence_stage"), "recorded": r["status"]["recorded_classification"],
                  "derived": r["status"]["classification"], "reasons": r["status"]["reasons"]}
                 for r in derived if r["status"]["label_corrected"]]
    counts: dict[str, int] = {}
    for r in derived:
        counts[r["status"]["classification"]] = counts.get(r["status"]["classification"], 0) + 1
    return {"contract": STATUS_CONTRACT, "gate_policy": policy.to_dict(), "gate_policy_sha": policy.sha,
            "rows": len(derived), "derived_counts": counts, "corrected": corrected,
            "recorded_validated": sum(1 for r in derived if r["status"]["recorded_classification"] == "VALIDATED"),
            "derived_validated": counts.get("VALIDATED", 0)}


__all__ = ["GatePolicy", "STATUS_CONTRACT", "correction_report", "derive", "derive_rows", "final_gates",
           "leaderboard", "validation_evidence", "validation_gates"]
