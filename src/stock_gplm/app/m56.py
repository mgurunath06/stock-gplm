"""M5.6 reference-library coverage and autonomous local research orchestration.

The owner's 300-rule document is prior knowledge, not a search boundary.  This module keeps the
source hash-bound, produces a complete coverage row for every reference/symbol/timeframe request,
and delegates open-ended discovery to the existing causal M5.4/application engine.  Descriptive
reference prose is never evaluated as Python.  Only exact translations in :func:`compile_reference`
are executable; every other item is retained with an explicit implementation or data blocker.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..m54.backtest import ExitPolicy, baseline, bh_adjust, fold_metrics, metrics, simulate
from .compute import Backend
from .context import App
from .db import jdump, sha, utc_now
from .ingest import SymbolData, ingest_many, window_data
from .lanes import all_bars, build_lane
from .library import Library, strategy_features, strategy_mask, validate
from .m56_catalogue import (TIMEFRAMES, CataloguePolicy, Reference, load_catalogue, parse_catalogue,
                            requirements, timeframes_v1)
from .m56_status import GatePolicy, final_gates, validation_evidence, validation_gates
from .m56_status import leaderboard as status_leaderboard

CONTRACT = "stock-gplm.m56-reference-catalogue.v2"
CLASSIFICATIONS = ("REFERENCE", "DISCOVERED", "EXPERIMENTAL", "VALIDATED", "REJECTED")
# Backward-compatible aliases: the original 300 keep identical definitions and digests.
_requirements = requirements
_timeframes = timeframes_v1


def parse_reference_catalogue(path: Path, policy: CataloguePolicy | None = None,
                              supplements: Sequence[Path] = ()) -> list[Reference]:
    """Parse the catalogue (6x50 base plus 301+ extensions); refuse omissions, duplicates and malformed entries.

    Use :func:`stock_gplm.app.m56_catalogue.parse_catalogue` for a non-raising report.
    """
    return load_catalogue(Path(path), policy, supplements).references


def _condition(feature: str, op: str, threshold: float | None) -> dict[str, Any]:
    return {"feature": feature, "op": op, "threshold": threshold}


def _compiled_conditions(ref: Reference, timeframe: str) -> tuple[list[dict[str, Any]] | None, str]:
    """Exact, reviewed translations. Similar-looking prose is deliberately not approximated."""
    n = ref.name.lower()
    mappings: list[tuple[str, list[dict[str, Any]]]] = [
        ("golden cross", [_condition("MA_CROSS(fast=50,slow=200,ma_type=SMA).spread", "cross_above", 0)]),
        ("death cross", [_condition("MA_CROSS(fast=50,slow=200,ma_type=SMA).spread", "cross_below", 0)]),
        ("swing ema crossover", [_condition("MA_CROSS(fast=9,slow=21,ma_type=EMA).spread", "cross_above", 0),
                                 _condition("EMA(period=50).distance", "gt", 0)]),
        ("positional ema crossover", [_condition("MA_CROSS(fast=20,slow=50,ma_type=EMA).spread", "cross_above", 0),
                                      _condition("ADX(period=14).adx", "gt", 20)]),
        ("macd signal line bullish cross", [_condition("MACD(fast=12,slow=26,signal=9).hist_norm", "cross_above", 0)]),
        ("macd zero-line breakout", [_condition("MACD(fast=12,slow=26,signal=9).line_norm", "cross_above", 0),
                                     _condition("MACD(fast=12,slow=26,signal=9).hist_norm", "gt", 0)]),
        ("adx trend ignition", [_condition("ADX(period=14).adx", "cross_above", 25),
                                _condition("ADX(period=14).di_spread", "gt", 0)]),
        ("dmi bullish dominance", [_condition("ADX(period=14).di_spread", "cross_above", 0),
                                   _condition("ADX(period=14).adx_slope", "gt", 0)]),
        ("supertrend bullish flip", [_condition("SUPERTREND(period=10,multiplier=3.0).flip_up", "is", None)]),
        ("52-week high breakout", [_condition("DONCHIAN(period=252).breakout_up", "is", None),
                                   _condition("RVOL(period=20,spike=1.5).value", "gt", 1.5)]),
        ("donchian channel 20-day breakout", [_condition("DONCHIAN(period=20).breakout_up", "is", None)]),
        ("donchian channel 55-day breakout", [_condition("DONCHIAN(period=55).breakout_up", "is", None)]),
        ("rate of change (roc) thrust", [_condition("ROC(period=12).value", "cross_above", 0.10),
                                         _condition("RVOL(period=20,spike=1.0).value", "gt", 1.0)]),
        ("cmf institutional thrust", [_condition("CMF(period=20).value", "gt", 0.25)]),
        ("money flow index (mfi) oversold rebound", [_condition("MFI(period=14).value", "cross_above", 20)]),
        ("mfi 80+ momentum thrust", [_condition("MFI(period=14).value", "gt", 80),
                                     _condition("DONCHIAN(period=20).breakout_up", "is", None)]),
        ("classic rsi(14) oversold reclaim", [_condition("RSI(period=14).value", "cross_above", 30)]),
        ("classic rsi(14) overbought rejection", [_condition("RSI(period=14).value", "cross_below", 70)]),
        ("stochastic slow oversold cross", [_condition("STOCH(period=14,smooth=3).k_minus_d", "cross_above", 0),
                                            _condition("STOCH(period=14,smooth=3).k", "lt", 20)]),
        ("williams %r oversold hook", [_condition("WILLIAMS_R(period=14).value", "cross_above", -80)]),
        ("bollinger band lower-band bounce", [_condition("BOLLINGER(period=20,width=2.0).percent_b", "cross_above", 0)]),
        ("bollinger %b oversold", [_condition("BOLLINGER(period=20,width=2.0).percent_b", "lt", 0)]),
        ("donchian channel mean reversion", [_condition("DONCHIAN(period=20).position", "lt", 5)]),
        ("bollinger band squeeze", [_condition("BOLLINGER(period=20,width=2.0).squeeze_release", "is", None)]),
        ("donchian channel 20-day low breakout", [_condition("DONCHIAN(period=20).breakout_down", "is", None)]),
        ("inside bar", [_condition("RANGE(period=10).inside_bar", "is", None)]),
        ("nr7", [_condition("RANGE(period=7).nr", "is", None)]),
    ]
    for phrase, conds in mappings:
        if phrase in n:
            return conds, "reviewed exact translation"
    return None, "no reviewed translation for the supplied pseudo-code; descriptive text was not coerced"


def compile_reference(ref: Reference, timeframe: str) -> tuple[dict[str, Any] | None, str]:
    if timeframe not in ref.supported_timeframes:
        return None, f"semantic timeframe scope is {','.join(ref.supported_timeframes)}"
    if ref.definition is not None:
        # A reviewed, structured strategy.v3 definition supplied with the entry; only its scope is set here.
        doc = json.loads(json.dumps(ref.definition))
        doc.update(timeframe=timeframe, scope={**doc.get("scope", {}), "timeframes": [timeframe]})
        doc["provenance"] = {**doc.get("provenance", {}), "reference_id": ref.reference_id,
                             "reference_version": ref.version, "reference_sha256": ref.digest}
        try:
            return validate(doc), "structured definition supplied with the catalogue entry"
        except Exception as exc:
            return None, f"structured definition invalid for {timeframe}: {exc}"
    if ref.implementation_status in ("METHOD_SPECIFICATION", "DESCRIPTION_ONLY"):
        return None, ref.implementation_reason
    if ref.source_format != "markdown-v1" or ref.origin != "REFERENCE":
        # Reviewed translations were checked against the original v1 rule text only; a newer entry
        # sharing a name must not inherit a translation reviewed for different prose.
        return None, "no reviewed translation or structured definition; descriptive text was not coerced"
    conditions, reason = _compiled_conditions(ref, timeframe)
    if conditions is None:
        return None, reason
    hold = 8 if timeframe in ("1d", "1w") else 12
    doc = {"name": ref.name, "description": ref.explanation, "timeframe": timeframe,
           "scope": {"symbols": "*", "timeframes": [timeframe]}, "direction": ref.direction,
           "entry": {"logic": "AND", "conditions": conditions},
           "exit": {"max_hold_bars": hold, "stop_atr": 1.5, "target_atr": 2.5, "trail_atr": None},
           "provenance": {"reference_id": ref.reference_id, "reference_version": ref.version,
                          "source_rule": ref.rule_text}, "evidence": {}}
    try:
        return validate(doc), reason
    except Exception as exc:
        return None, f"reviewed translation unavailable in current indicator registry: {exc}"


def _bundle_sha(source: Path, supplements: Sequence[Path]) -> str:
    h = hashlib.sha256()
    for p in (Path(source), *map(Path, supplements)):
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def coverage_summary(refs: Sequence[Reference], registered_inputs: Sequence[str] = ("OHLCV",)) -> dict[str, Any]:
    """Executable versus blocked counts per entry, independent of any candle data."""
    executable_by_tf = {tf: 0 for tf in TIMEFRAMES}
    classes: dict[str, list[str]] = {}
    data_blocked: list[str] = []
    for ref in refs:
        ok = False
        for tf in ref.supported_timeframes:
            doc, _ = compile_reference(ref, tf)
            if doc is not None:
                executable_by_tf[tf] += 1
                ok = True
        if ok:
            cls = "EXECUTABLE"
        elif ref.implementation_status in ("METHOD_SPECIFICATION", "DESCRIPTION_ONLY"):
            cls = ref.implementation_status
        else:
            cls = "NO_REVIEWED_TRANSLATION"
        classes.setdefault(cls, []).append(ref.reference_id)
        if set(ref.required_inputs) - set(registered_inputs):
            data_blocked.append(ref.reference_id)
    return {"strategies": len(refs), "executable_strategies": len(classes.get("EXECUTABLE", [])),
            "by_class": {k: len(v) for k, v in sorted(classes.items())},
            "ids_by_class": {k: v for k, v in sorted(classes.items())},
            "executable_by_timeframe": executable_by_tf, "registered_inputs": list(registered_inputs),
            "needs_unregistered_inputs": len(data_blocked)}


def build_manifest(source: Path, output: Path, policy: CataloguePolicy | None = None,
                   supplements: Sequence[Path] = ()) -> dict[str, Any]:
    parsed = load_catalogue(Path(source), policy, supplements)
    refs = parsed.references
    raw_sha = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    rows = []
    executable = {tf: 0 for tf in TIMEFRAMES}
    for ref in refs:
        supported: dict[str, Any] = {}
        for tf in ref.supported_timeframes:
            doc, reason = compile_reference(ref, tf)
            supported[tf] = {"status": "EXECUTABLE" if doc else "BLOCKED_IMPLEMENTATION", "reason": reason,
                             "definition": doc}
            executable[tf] += int(doc is not None)
        row = asdict(ref)
        row["sha256"] = ref.digest
        row["implementations"] = supported
        rows.append(row)
    report = parsed.report()
    manifest = {"contract": CONTRACT, "catalogue_version": 2, "source_path": str(Path(source).resolve()),
                "source_sha256": raw_sha, "bundle_sha256": _bundle_sha(source, supplements),
                "strategy_count": len(refs), "reference_is_minimum_not_ceiling": True,
                "catalogue_policy": parsed.policy.to_dict(), "catalogue_report": report,
                "coverage_summary": {k: v for k, v in coverage_summary(refs).items() if k != "ids_by_class"},
                "classifications": list(CLASSIFICATIONS), "implementation_count_by_timeframe": executable,
                "strategies": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != text:
        raise ValueError(f"immutable manifest already exists with different content: {output}")
    if not output.exists():
        output.write_text(text, encoding="utf-8")
    return manifest


def _available_inputs(data: SymbolData) -> set[str]:
    out = {"OHLCV"} if len(data.daily) or len(data.minute) else set()
    # Only registered, validated adapters may add these in future. Never infer them from OHLCV.
    return out


def _segments(n: int, purge: int) -> dict[str, tuple[int, int]] | None:
    if n < max(90, purge * 6):
        return None
    a, b = int(n * 0.60), int(n * 0.80)
    return {"discovery": (0, a), "validation": (min(n, a + purge), b),
            "test": (min(n, b + purge), n)}


def _regime_masks(lane: Any) -> dict[str, np.ndarray]:
    n = len(lane.market)
    out = {"ALL": np.ones(n, dtype=bool)}
    names = {x: i for i, x in enumerate(lane.names)}
    ema = next((k for k in names if k.startswith("EMA(period=200).distance")), None)
    if ema:
        v = lane.X[:, names[ema]]
        out.update(BULL=v > 0, BEAR=v <= 0)
    vol = lane.market.natr
    finite = vol[np.isfinite(vol)]
    if finite.size:
        med = float(np.median(finite))
        out.update(HIGH_VOL=vol > med, LOW_VOL=vol <= med)
    return out


def _insert_result(app: App, *, strategy_id: str, classification: str, origin: str, symbol: str, timeframe: str,
                   regime: str, stage: str, result_metrics: Mapping[str, Any], lineage: Mapping[str, Any],
                   fingerprint: str, device: str) -> None:
    # Final evidence is keyed by the data fingerprint rather than run id: the same locked segment
    # can never acquire a second row merely because a command was restarted. Earlier-stage evidence
    # remains run-scoped so an audit can reconstruct each fresh validation decision.
    identity = {"strategy": strategy_id, "symbol": symbol, "tf": timeframe, "regime": regime,
                "stage": stage, "data_fingerprint": fingerprint}
    if stage != "TEST_ONCE":
        identity["run"] = app.run_id
    with app.db.tx() as c:
        c.execute("INSERT OR IGNORE INTO m56_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("m56r-" + sha(identity)[:20], app.run_id, strategy_id, "1", classification, origin, symbol,
                   timeframe, regime, stage, jdump(result_metrics), jdump(lineage), fingerprint, device, utc_now()))


def evaluate_references(app: App, refs: Sequence[Reference], datas: Mapping[str, SymbolData], timeframes: Sequence[str],
                        backend: Backend, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Attempt every requested combination and gate the final test behind validation evidence."""
    pending: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    gates = GatePolicy.from_cfg(cfg)
    min_folds = gates.walk_forward_folds
    total = len(refs) * len(datas) * len(timeframes)
    app.progress.set_total(total)
    eligible_references = evaluated_rules = completed_folds = 0
    for symbol, data in datas.items():
        bars = all_bars(data)
        available = _available_inputs(data)
        lanes: dict[str, Any] = {}
        for tf in timeframes:
            lanes[tf] = build_lane(data, tf, app.s.section("costs"), bars=bars,
                                   cache_dir=app.s.cache_dir / "features") if len(bars[tf]) else None
        for ref in refs:
            for tf in timeframes:
                app.progress.update(task="m56-reference", stock=symbol, timeframe=tf, note=ref.reference_id,
                                    device="cpu (NumPy trade simulation)")
                status, reason = "", ""
                missing = sorted(set(ref.required_inputs) - available)
                doc: dict[str, Any] | None = None
                checkpoint_key = "m56:reference:" + sha({"reference": ref.digest, "symbol": symbol, "tf": tf,
                                                         "data": data.fingerprint, "code": app.code_sha,
                                                         "config": app.s.sha256})
                checkpoint = app.db.get_checkpoint(checkpoint_key)
                if checkpoint:
                    status = str(checkpoint["status"])
                    reason = "reused checkpoint: " + str(checkpoint["reason"])
                    counts[status] = counts.get(status, 0) + 1
                    with app.db.tx() as c:
                        c.execute("INSERT OR IGNORE INTO m56_coverage VALUES (?,?,?,?,?,?,?,?,?)",
                                  (app.run_id, ref.reference_id, symbol, tf, status, reason,
                                   jdump({"required": ref.required_inputs, "missing": missing,
                                          "checkpoint": checkpoint_key}), ref.digest, utc_now()))
                    app.progress.advance()
                    continue
                if tf not in ref.supported_timeframes:
                    status, reason = "BLOCKED_TIMEFRAME", f"supports {','.join(ref.supported_timeframes)}"
                elif missing:
                    status, reason = "BLOCKED_MISSING_DATA", "missing registered inputs: " + ", ".join(missing)
                elif lanes[tf] is None:
                    status, reason = "BLOCKED_MISSING_DATA", f"no accepted {tf} candles"
                else:
                    doc, reason = compile_reference(ref, tf)
                    if doc is None:
                        status = "BLOCKED_IMPLEMENTATION"
                    else:
                        segs = _segments(len(lanes[tf].market), int(doc["exit"]["max_hold_bars"]))
                        if segs is None:
                            status, reason = "BLOCKED_INSUFFICIENT_HISTORY", "not enough bars for purged 60/20/20 split"
                        else:
                            status, reason = "EVALUATED_VALIDATION", "validation scored; locked test remains gated"
                            eligible_references += 1
                            evaluated_rules += 1
                            features = strategy_features(doc, bars, tf)
                            mask = strategy_mask(doc, features, len(lanes[tf].market))
                            policy = ExitPolicy.from_dict(doc["exit"])
                            for regime, rm in _regime_masks(lanes[tf]).items():
                                tr = simulate(lanes[tf].market, mask & rm, segs["validation"], policy, lanes[tf].costs)
                                base = baseline(lanes[tf].market, segs["validation"], policy, lanes[tf].costs)
                                met = metrics(tr, lanes[tf].market, segs["validation"], base)
                                folds = fold_metrics(tr, segs["validation"], min_folds, base)
                                met.update(folds=folds, q_value=None, stage="validation", regime=regime,
                                           parameter_stability="NOT_APPLICABLE_FIXED_REFERENCE")
                                completed_folds += len(folds)
                                pending.append({"ref": ref, "doc": doc, "lane": lanes[tf], "mask": mask & rm,
                                                "segments": segs, "policy": policy, "regime": regime,
                                                "metrics": met, "symbol": symbol, "tf": tf,
                                                "checkpoint_key": checkpoint_key})
                counts[status] = counts.get(status, 0) + 1
                with app.db.tx() as c:
                    c.execute("INSERT OR IGNORE INTO m56_coverage VALUES (?,?,?,?,?,?,?,?,?)",
                              (app.run_id, ref.reference_id, symbol, tf, status, reason,
                               jdump({"required": ref.required_inputs, "missing": missing}), ref.digest, utc_now()))
                if status != "EVALUATED_VALIDATION":
                    app.db.put_checkpoint(checkpoint_key, {"status": status, "reason": reason,
                                                           "data_fingerprint": data.fingerprint})
                app.progress.advance()
                app.progress.update(eligible_references=eligible_references, evaluated_rules=evaluated_rules,
                                    completed_folds=completed_folds)
    family = max(1, len(pending))
    qvals = bh_adjust([p["metrics"].get("p_value") for p in pending], family)
    validated = rejected = holdout_scores = 0
    for item, qv in zip(pending, qvals):
        met = item["metrics"]
        met["q_value"] = qv
        met["bh_family_size"] = family
        verdict = validation_gates(met, gates)
        met["validation_gates_failed"] = verdict["failed"]
        promote = verdict["passed"]
        classification = "EXPERIMENTAL" if promote else "REJECTED"
        _insert_result(app, strategy_id=item["ref"].reference_id, classification=classification, origin="REFERENCE",
                       symbol=item["symbol"], timeframe=item["tf"], regime=item["regime"], stage="VALIDATION",
                       result_metrics=met, lineage={"reference_sha256": item["ref"].digest,
                                                   "source_rule": item["ref"].rule_text},
                       fingerprint=item["lane"].data_fingerprint, device="cpu (NumPy trade simulation)")
        if not promote:
            rejected += 1
            app.db.put_checkpoint(item["checkpoint_key"], {"status": "EVALUATED_VALIDATION",
                                                            "reason": "validation gates rejected all scored regimes",
                                                            "data_fingerprint": item["lane"].data_fingerprint})
            continue
        # The final segment is touched once, and only after all validation p-values were corrected.
        prior = app.db.one("SELECT result_id FROM m56_results WHERE strategy_id=? AND symbol=? AND timeframe=? "
                           "AND regime=? AND evidence_stage='TEST_ONCE' AND data_fingerprint=?",
                           (item["ref"].reference_id, item["symbol"], item["tf"], item["regime"],
                            item["lane"].data_fingerprint))
        if prior:
            app.db.event("M56_HOLDOUT_REUSE_REFUSED", {"strategy_id": item["ref"].reference_id,
                                                        "symbol": item["symbol"], "timeframe": item["tf"],
                                                        "regime": item["regime"], "result_id": prior["result_id"]},
                         level="WARN")
            app.db.put_checkpoint(item["checkpoint_key"], {"status": "EVALUATED_VALIDATION",
                                                            "reason": "final segment already scored for this fingerprint",
                                                            "data_fingerprint": item["lane"].data_fingerprint})
            continue
        tr = simulate(item["lane"].market, item["mask"], item["segments"]["test"], item["policy"], item["lane"].costs)
        base = baseline(item["lane"].market, item["segments"]["test"], item["policy"], item["lane"].costs)
        test_met = metrics(tr, item["lane"].market, item["segments"]["test"], base)
        test_met.update(stage="test_once", regime=item["regime"], validation_q=qv,
                        validation_evidence=validation_evidence(met))
        passed = final_gates(test_met, gates)["passed"]
        final = "VALIDATED" if passed else "REJECTED"
        validated += int(passed)
        rejected += int(not passed)
        holdout_scores += 1
        _insert_result(app, strategy_id=item["ref"].reference_id, classification=final, origin="REFERENCE",
                       symbol=item["symbol"], timeframe=item["tf"], regime=item["regime"], stage="TEST_ONCE",
                       result_metrics=test_met, lineage={"validation_q": qv, "promotion_gate_passed": True},
                       fingerprint=item["lane"].data_fingerprint, device="cpu (NumPy trade simulation)")
        app.db.put_checkpoint(item["checkpoint_key"], {"status": "EVALUATED_VALIDATION",
                                                        "reason": f"final classification {final}",
                                                        "data_fingerprint": item["lane"].data_fingerprint})
    return {"combinations": total, "coverage": counts, "validation_results": len(pending),
            "holdout_scores": holdout_scores, "validated": validated, "rejected": rejected}


def leaderboard(app: App, run_id: str | None = None) -> list[dict[str, Any]]:
    """Leaderboard from the authoritative derived status; the stored label is never trusted."""
    where, args = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
    rows = app.db.q(f"SELECT * FROM m56_results {where} ORDER BY created_utc", args)
    return status_leaderboard(rows, GatePolicy.from_cfg(app.s.section("m56")))


def cross_stock_evidence(app: App, datas: Mapping[str, SymbolData], cfg: Mapping[str, Any],
                         library: Library) -> dict[str, Any]:
    """Test current-run discovered rules across other stocks; this never reads a final segment."""
    rows = app.db.q("SELECT DISTINCT strategy_id, timeframe FROM m56_results WHERE run_id=? AND origin='DISCOVERED' "
                    "ORDER BY created_utc LIMIT ?", (app.run_id, int(cfg.get("cross_stock_max_candidates", 50))))
    cache: dict[tuple[str, str], tuple[Any, Any]] = {}
    emitted = experimental = 0
    min_trades = int(cfg.get("min_oos_trades", 20))
    for row in rows:
        try:
            doc = library.get(row["strategy_id"])
        except Exception:
            continue
        tf = row["timeframe"]
        per_stock = []
        fingerprints = []
        for symbol, data in datas.items():
            key = (symbol, tf)
            if key not in cache:
                bars = all_bars(data)
                lane = build_lane(data, tf, app.s.section("costs"), bars=bars,
                                  cache_dir=app.s.cache_dir / "features") if len(bars[tf]) else None
                cache[key] = (bars, lane)
            bars, lane = cache[key]
            if lane is None:
                continue
            segs = _segments(len(lane.market), int(doc["exit"]["max_hold_bars"]))
            if not segs:
                continue
            features = strategy_features(doc, bars, tf)
            mask = strategy_mask(doc, features, len(lane.market))
            policy = ExitPolicy.from_dict(doc["exit"])
            trades = simulate(lane.market, mask, segs["validation"], policy, lane.costs)
            base = baseline(lane.market, segs["validation"], policy, lane.costs)
            met = metrics(trades, lane.market, segs["validation"], base)
            per_stock.append({"symbol": symbol, "metrics": met})
            fingerprints.append(lane.data_fingerprint)
        if len(per_stock) < 2:
            continue
        positive = sum(1 for x in per_stock if (x["metrics"].get("mean_net") or 0) > 0)
        effective_n = sum(int(x["metrics"].get("effective_n") or 0) for x in per_stock)
        passed = positive / len(per_stock) >= float(cfg.get("cross_stock_positive_fraction", 0.60)) \
            and effective_n >= min_trades * len(per_stock)
        classification = "EXPERIMENTAL" if passed else "REJECTED"
        experimental += int(passed)
        aggregate = {"stocks": len(per_stock), "positive_stocks": positive, "effective_n": effective_n,
                     "positive_fraction": positive / len(per_stock), "per_stock": per_stock,
                     "stage": "cross_stock_validation_only", "final_segment_scored": False}
        _insert_result(app, strategy_id=row["strategy_id"], classification=classification, origin="DISCOVERED",
                       symbol="MARKET_WIDE", timeframe=tf, regime="CROSS_STOCK", stage="CROSS_STOCK_VALIDATION",
                       result_metrics=aggregate, lineage={"parent_strategy_id": row["strategy_id"],
                                                          "scope_level": "market_wide"},
                       fingerprint=sha(sorted(fingerprints)), device="cpu (NumPy trade simulation)")
        emitted += 1
    groups = cfg.get("stock_groups") or {}
    group_status = "NOT_REQUESTED_NO_PROVENANCE" if not groups else "CONFIGURED"
    app.db.event("M56_RESEARCH_LEVELS", {"individual": "COMPLETED", "market_wide_candidates": emitted,
                                         "market_wide_experimental": experimental,
                                         "stock_groups": group_status,
                                         "note": "sector/group membership is never inferred"})
    return {"individual": "COMPLETED", "market_wide_candidates": emitted,
            "market_wide_experimental": experimental, "stock_groups": group_status}


def run_m56(app: App, symbols: Sequence[str], timeframes: Sequence[str], backend: Backend, *, catalogue: Path,
            mode: str = "both", budget: str = "standard", manifest_only: bool = False,
            start: str | None = None, end: str | None = None) -> dict[str, Any]:
    cfg = app.s.section("m56")
    policy = CataloguePolicy.from_cfg(cfg)
    supplements = [Path(x) for x in ((cfg.get("catalogue") or {}).get("supplements") or [])]
    bundle = _bundle_sha(catalogue, supplements)
    manifest_path = app.s.home / "m56" / f"reference-manifest-v2-{bundle[:16]}.json"
    manifest = build_manifest(catalogue, manifest_path, policy, supplements)
    count = int(manifest["strategy_count"])
    with app.db.tx() as c:
        c.execute("INSERT OR IGNORE INTO m56_catalogues VALUES (?,?,?,?,?,?,?)",
                  (manifest["source_sha256"], str(catalogue.resolve()), CONTRACT, count, str(manifest_path),
                   utc_now(), app.run_id))
    app.db.event("M56_REFERENCE_CATALOGUE_REGISTERED", {"source_sha256": manifest["source_sha256"],
                                                        "bundle_sha256": bundle, "strategies": count,
                                                        "manifest": str(manifest_path), "minimum_not_ceiling": True,
                                                        "catalogue_status": manifest["catalogue_report"]["status"]})
    result: dict[str, Any] = {"manifest": str(manifest_path), "source_sha256": manifest["source_sha256"],
                              "reference_count": count, "mode": mode,
                              "coverage_summary": manifest["coverage_summary"]}
    if manifest_only:
        return result
    # Development-period boundary: rows after it never reach a lane, feature, label or score.
    dev_end = cfg.get("development_end")
    if dev_end:
        if end and end > dev_end:
            raise ValueError(f"--end {end} is after m56.development_end {dev_end}; the locked holdout stays closed")
        end = end or dev_end
    result["window"] = {"start": start, "end": end, "development_end": dev_end}
    app.db.event("M56_DATA_WINDOW", result["window"])
    datas = {k: window_data(v, start, end) for k, v in
             ingest_many(app.s, app.db, symbols, use_minute=any(tf.endswith("m") for tf in timeframes)).items()}
    if mode in ("reference", "both"):
        result["reference"] = evaluate_references(app, parse_reference_catalogue(catalogue, policy, supplements),
                                                   datas, timeframes,
                                                   backend, cfg)
    if mode in ("discovery", "both"):
        from .discover import discover

        dres = discover(app, symbols, data_mode="all-local", timeframes=",".join(timeframes), budget=budget,
                        backend=backend, workers=int(cfg.get("workers", 1)), start=start, end=end,
                        skip_engine=False)
        result["discovery"] = dres
        for rr in dres.get("results", []):
            symbol = rr["symbol"]
            fingerprint = datas[symbol].fingerprint
            for imported in (rr.get("engine") or {}).get("strategies", []):
                tier = str(imported.get("tier") or "DISCOVERED")
                doc = app.db.one("SELECT timeframe, provenance_json FROM strategies WHERE strategy_id=? AND version=?",
                                 (imported["strategy_id"], imported["version"]))
                tf = (doc or {}).get("timeframe") or "1d"
                # M5.4 engine tiers carry no M5.6 gate metrics: a candidate, never VALIDATED (M5.6.1 fix).
                classification = "DISCOVERED"
                _insert_result(app, strategy_id=imported["strategy_id"], classification=classification,
                               origin="DISCOVERED", symbol=symbol, timeframe=tf, regime="ALL", stage="DISCOVERY_ENGINE",
                               result_metrics={"tier": tier},
                               lineage={"engine_run": (rr.get("engine") or {}).get("run_id"),
                                        "library_version": imported["version"], "parents": []},
                               fingerprint=fingerprint, device="cpu (M5.4 unified engine)")
            for screen in rr.get("screens", []):
                tf = screen.get("timeframe", "1d")
                for rule in screen.get("rules", []):
                    lib = rule.get("library") or {}
                    if not lib.get("strategy_id"):
                        continue
                    tier = str(rule.get("screen_tier") or "DISCOVERED")
                    # M5.5 screen tiers have no BH q, drawdown or fold gates: never VALIDATED (M5.6.1 fix).
                    classification = "REJECTED" if tier == "SCREEN_REJECTED_VALIDATION" else "DISCOVERED"
                    evidence = {"tier": tier, "in_sample": rule.get("in_sample"),
                                "validation": rule.get("validation"), "test": rule.get("test"),
                                "tests_in_search": screen.get("tests")}
                    _insert_result(app, strategy_id=lib["strategy_id"], classification=classification,
                                   origin="DISCOVERED", symbol=symbol, timeframe=tf, regime="ALL",
                                   stage="BROAD_SCREEN", result_metrics=evidence,
                                   lineage={"rule_id": rule.get("rule_id"), "library_version": lib.get("version"),
                                            "search_budget": budget}, fingerprint=fingerprint, device=backend.name)
        # A genuinely different hypothesis generator: nonlinear boosting feature selection followed
        # by interpretable decision-tree path extraction. The learned paths are ordinary strategy
        # documents, so future discoveries require no Python source changes.
        from .advanced_discovery import discover_tree_strategies

        library = Library(app.s.strategies_dir, app.db)
        advanced_runs = []
        for symbol, data in datas.items():
            bars = all_bars(data)
            for tf in timeframes:
                if not len(bars[tf]):
                    advanced_runs.append({"symbol": symbol, "timeframe": tf, "status": "BLOCKED_MISSING_DATA"})
                    continue
                lane = build_lane(data, tf, app.s.section("costs"), bars=bars,
                                  cache_dir=app.s.cache_dir / "features")
                if lane is None:
                    continue
                ckey = "m56:advanced:" + sha({"symbol": symbol, "tf": tf, "data": lane.data_fingerprint,
                                               "code": app.code_sha, "config": app.s.sha256})
                prior = app.db.get_checkpoint(ckey)
                if prior:
                    advanced_runs.append({"symbol": symbol, "timeframe": tf, "status": "REUSED_CHECKPOINT",
                                          **prior})
                    continue
                # One final-segment read per lane fingerprint: learned thresholds change the strategy id,
                # so a per-strategy lock alone would let a re-tuned rerun re-select on the same segment.
                consumed = app.db.one(
                    "SELECT result_id FROM m56_results WHERE origin='DISCOVERED' AND evidence_stage='TEST_ONCE' "
                    "AND symbol=? AND timeframe=? AND data_fingerprint=? AND "
                    "json_extract(lineage_json, '$.method')='gradient_boosting_plus_decision_tree' LIMIT 1",
                    (symbol, tf, lane.data_fingerprint))
                if consumed:
                    app.db.event("M56_HOLDOUT_REUSE_REFUSED", {"method": "gradient_boosting_plus_decision_tree",
                                                                "symbol": symbol, "timeframe": tf,
                                                                "result_id": consumed["result_id"]}, level="WARN")
                advanced = discover_tree_strategies(lane, cfg, seed=int(app.s.section("models").get("seed", 55)) + 1,
                                                    allow_test=not consumed)
                accepted = 0
                for candidate in advanced.get("candidates", []):
                    added = library.add(candidate["document"], immutable=True,
                                        origin="discovery:gradient-boosting-decision-tree",
                                        active=candidate["classification"] == "VALIDATED",
                                        provenance={"engine": advanced.get("method"), "run_id": app.run_id,
                                                    "data_fingerprint": lane.data_fingerprint,
                                                    "search_budget": budget})
                    accepted += int(added["created"])
                    final_metrics = candidate.get("test") or candidate["validation"]
                    stage = "TEST_ONCE" if candidate.get("test") is not None else "ADVANCED_VALIDATION"
                    _insert_result(app, strategy_id=added["strategy_id"],
                                   classification=candidate["classification"], origin="DISCOVERED", symbol=symbol,
                                   timeframe=tf, regime="ALL", stage=stage, result_metrics=final_metrics,
                                   lineage={**candidate["lineage"], "library_version": added["version"]},
                                   fingerprint=lane.data_fingerprint, device=advanced.get("device", "cpu"))
                run_record = {"symbol": symbol, "timeframe": tf, "status": advanced["status"],
                              "generated": advanced.get("generated", 0), "registered": accepted,
                              "selected_features": advanced.get("selected_features", []),
                              "device": advanced.get("device")}
                app.db.put_checkpoint(ckey, run_record)
                with app.db.tx() as c:
                    c.execute("INSERT OR IGNORE INTO m56_method_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              ("m56m-" + sha({"run": app.run_id, "method": "tree", "symbol": symbol, "tf": tf})[:20],
                               app.run_id, "gradient_boosting_plus_decision_tree", symbol, tf, advanced["status"],
                               advanced.get("device", "cpu"), ckey, advanced.get("generated", 0),
                               len(advanced.get("candidates", [])), jdump(run_record), utc_now()))
                advanced_runs.append(run_record)
        result["advanced_discovery"] = advanced_runs
        result["research_levels"] = cross_stock_evidence(app, datas, cfg, library)
        method = {"feature_selection": "m54_unified", "parameter_search": "grid_and_neighbour_stability",
                  "interaction_search": "batched_single_and_pair_rules", "cross_timeframe": "causal_asof",
                  "gradient_boosting": "local_sklearn_feature_selection",
                  "decision_tree": "local_sklearn_rule_extraction", "neural_network": "local_mlp",
                  "external_ai": "disabled",
                  "selected_backend": backend.label, "gpu_kernels_execute_only_when_reported_by_engine": True}
        generated = sum(len(sc.get("rules", [])) for rr in dres.get("results", []) for sc in rr.get("screens", []))
        generated += sum(len((rr.get("engine") or {}).get("strategies", [])) for rr in dres.get("results", []))
        generated += sum(int(x.get("generated", 0)) for x in advanced_runs)
        rejected_row = app.db.one("SELECT COUNT(*) n FROM m56_results WHERE run_id=? AND classification='REJECTED'",
                                  (app.run_id,))
        app.progress.update(generated_candidates=generated,
                            rejected_candidates=(rejected_row or {}).get("n", 0))
        with app.db.tx() as c:
            c.execute("INSERT OR IGNORE INTO m56_method_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      ("m56m-" + sha({"run": app.run_id, "method": "autonomous_discovery"})[:20], app.run_id,
                       "autonomous_discovery", None, None, "COMPLETED", backend.name,
                       f"m56:{app.run_id}:discovery", generated, generated, jdump(method), utc_now()))
    result["leaderboard"] = leaderboard(app)
    out = app.s.reports_dir / "m56" / str(app.run_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    result["report"] = str(out / "summary.json")
    return result


__all__ = ["CLASSIFICATIONS", "CONTRACT", "Reference", "build_manifest", "compile_reference",
           "evaluate_references", "leaderboard", "parse_reference_catalogue", "run_m56"]
