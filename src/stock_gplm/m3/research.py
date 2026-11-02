"""Build a checksum-bound M3.2 feature, label and evaluation-plan run from M3.1."""
from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .evidence import ReferenceEvidence, SessionAxis
from .features import FEATURE_CONTRACT, FEATURE_NAMES, FEATURE_SPECS, compute_symbol_features
from .labels import DAILY_LABEL_CONTRACTS, LABEL_CONTRACT, LABEL_CONTRACTS, LabelRow, build_daily_labels
from .readiness import DatasetRow, _canonical_hash, _stream_sha256, _write_json
from .splits import build_walk_forward, verify_plan

RESEARCH_VERSION = 2
INSTRUMENT_CLASSES = frozenset({"EQUITY", "ETF", "UNKNOWN"})
FEATURE_PREFIX = ("symbol", "as_of_date", "run_index", "run_observation", "source_path", "source_sha256")
LABEL_FIELDS = tuple(LabelRow.__dataclass_fields__)
MINIMUM_LABELLED_SAMPLES_PER_HORIZON = 1


def load_readiness_run(run: Path) -> tuple[dict[str, list[DatasetRow]], SessionAxis, dict[str, Any]]:
    """Read M3.1 outputs only after proving they match their manifest."""
    run = Path(run)
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contract") != "stock-gplm.m3-readiness-run.v1":
        raise ValueError("input is not an M3.1 readiness run")
    if manifest.get("raw_inputs_mutated") is not False:
        raise ValueError("M3.1 run does not attest unmodified raw inputs")
    for name in ("research-dataset.csv", "observed-sessions.csv", "coverage-quality.json", "inventory.json"):
        if name not in manifest.get("outputs", {}):
            raise ValueError(f"M3.1 run predates this contract; missing {name}")
        if _stream_sha256(run / name) != manifest["outputs"][name]:
            raise ValueError(f"M3.1 output does not match its manifest: {name}")
    symbols: dict[str, list[DatasetRow]] = {}
    with (run / "research-dataset.csv").open(encoding="utf-8", newline="") as stream:
        for item in csv.DictReader(stream):
            symbols.setdefault(item["symbol"], []).append(DatasetRow(
                symbol=item["symbol"], trading_date=item["trading_date"],
                source_timestamp_utc=item["source_timestamp_utc"],
                open=item["open"], high=item["high"], low=item["low"], close=item["close"],
                volume=int(item["volume"]), source_path=item["source_path"],
                source_sha256=item["source_sha256"],
            ))
    with (run / "observed-sessions.csv").open(encoding="utf-8", newline="") as stream:
        sessions = tuple(item["trading_date"] for item in csv.DictReader(stream))
    quality = json.loads((run / "coverage-quality.json").read_text(encoding="utf-8"))
    return symbols, SessionAxis(sessions, "EMPIRICAL_UNION"), {"manifest": manifest, "quality": quality}


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _gate(passed: bool, blocker: str) -> dict[str, str]:
    return {"status": "PASS"} if passed else {"status": "BLOCKED", "blocker": blocker}


def _reference_gates(
    symbols: Mapping[str, list[DatasetRow]], axis: SessionAxis, evidence: ReferenceEvidence,
    quality: dict[str, Any], classes: Mapping[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    covered = bool(symbols) and all(
        evidence.corporate_action_coverage(symbol, rows[0].trading_date, rows[-1].trading_date) == "COMPLETE"
        for symbol, rows in symbols.items()
    )
    listed = bool(symbols) and evidence.inventory is not None and all(
        evidence.membership(symbol, row.trading_date) == "LISTED"
        for symbol, rows in symbols.items() for row in rows
    )
    return {
        "raw_candle_acceptance": _gate(
            quality.get("raw_candle_acceptance") == "ACCEPT",
            "M3.1 reports segments or candles that need review",
        ),
        "authoritative_calendar": _gate(
            axis.source == "AUTHORITATIVE_CALENDAR",
            "sessions come from the observed union; a complete NSE calendar is required",
        ),
        "point_in_time_membership": _gate(
            listed, "verified listing history must cover every observed candle",
        ),
        "alias_history": _gate(
            evidence.alias_history_complete(), "complete historical alias coverage is required",
        ),
        "corporate_action_coverage": _gate(
            covered, "complete corporate-action coverage must span every symbol's history",
        ),
        # Supplied classes separate ETFs from ordinary equities; UNKNOWN symbols are excluded downstream.
        "instrument_classification": _gate(
            classes is not None and set(symbols) <= set(classes),
            "ETF/equity classification evidence must cover every symbol",
        ),
        "minute_data": _gate(False, "no confirmed real minute-level schema or data exists"),
    }


def build_research_run(
    *, readiness_run: Path, output_root: Path, evidence: ReferenceEvidence | None = None,
    axis: SessionAxis | None = None, evidence_hashes: Mapping[str, str] | None = None,
    instrument_classes: Mapping[str, str] | None = None,
) -> Path:
    """Write features, labels, splits and gates; training readiness stays NOT_READY."""
    evidence = evidence or ReferenceEvidence()
    readiness_run = Path(readiness_run).resolve()
    output_root = Path(output_root).resolve()
    symbols, observed_axis, m31 = load_readiness_run(readiness_run)
    axis = axis or observed_axis
    raw_root = Path(json.loads((readiness_run / "inventory.json").read_text(encoding="utf-8"))["raw_root"]).resolve()
    if output_root == raw_root or raw_root in output_root.parents:
        raise ValueError("output root must be outside the immutable raw tree")
    if not symbols:
        raise ValueError("M3.1 run contains no validated candles")
    if instrument_classes is not None and not set(instrument_classes.values()) <= INSTRUMENT_CLASSES:
        raise ValueError("unsupported instrument class")
    contracts = {
        "features": {"contract": FEATURE_CONTRACT, "specs": [asdict(item) for item in FEATURE_SPECS]},
        "labels": {"contract": LABEL_CONTRACT, "horizons": [item.to_dict() for item in LABEL_CONTRACTS]},
    }
    binding = {
        "contract": "stock-gplm.m32-research-run.v1",
        "research_version": RESEARCH_VERSION,
        "readiness_run_id": m31["manifest"]["run_id"],
        "readiness_outputs": m31["manifest"]["outputs"],
        "session_source": axis.source,
        "sessions_sha256": _canonical_hash(list(axis.sessions)),
        "evidence": dict(sorted((evidence_hashes or {}).items())),
        "instrument_classes_sha256": (
            None if instrument_classes is None else _canonical_hash(dict(sorted(instrument_classes.items())))
        ),
        "contracts_sha256": _canonical_hash(contracts),
    }
    run_id = _canonical_hash(binding)
    final = output_root / run_id
    if final.exists():
        raise FileExistsError(f"research run already exists: {run_id}")
    output_root.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=output_root))
    try:
        counts: dict[str, dict[str, int]] = {item.name: {} for item in DAILY_LABEL_CONTRACTS}
        feature_rows = 0
        with (partial / "features.csv").open("x", encoding="utf-8", newline="") as feature_stream, \
                (partial / "labels.csv").open("x", encoding="utf-8", newline="") as label_stream:
            feature_writer = csv.writer(feature_stream, lineterminator="\n")
            feature_writer.writerow((*FEATURE_PREFIX, *FEATURE_NAMES))
            label_writer = csv.DictWriter(label_stream, fieldnames=LABEL_FIELDS, lineterminator="\n")
            label_writer.writeheader()
            for symbol in sorted(symbols):
                rows = symbols[symbol]
                for item in compute_symbol_features(rows, axis, evidence.discontinuities(symbol)):
                    feature_rows += 1
                    feature_writer.writerow((
                        item.symbol, item.as_of_date, item.run_index, item.run_observation,
                        item.source_path, item.source_sha256,
                        *(item.values[name] for name in FEATURE_NAMES),
                    ))
                for contract in DAILY_LABEL_CONTRACTS:
                    tally = counts[contract.name]
                    for label in build_daily_labels(rows, axis, contract, evidence):
                        tally[label.status] = tally.get(label.status, 0) + 1
                        label_writer.writerow(asdict(label))
        plans = [build_walk_forward(axis, contract) for contract in DAILY_LABEL_CONTRACTS]
        for plan in plans:
            verify_plan(plan, axis)
        _write_json(partial / "split-plan.json", {
            "schema_version": 1, "plans": [plan.to_dict() for plan in plans],
        })
        _write_json(partial / "contracts.json", {"schema_version": 1, **contracts})
        if axis.source == "AUTHORITATIVE_CALENDAR":
            # Downstream runs must rebuild plans on this axis, not on the observed union.
            _write_csv(partial / "session-axis.csv", ["trading_date"], [[day] for day in axis.sessions])
        if instrument_classes is not None:
            _write_csv(partial / "instrument-classes.csv", ["symbol", "instrument_class"],
                       [[name, instrument_classes[name]] for name in sorted(instrument_classes)])
        gates = _reference_gates(symbols, axis, evidence, m31["quality"], instrument_classes)
        references_pass = all(
            gates[name]["status"] == "PASS" for name in gates if name != "minute_data"
        )
        horizons: dict[str, Any] = {}
        for plan in plans:
            complete = counts[plan.horizon].get("COMPLETE", 0)
            blockers = [name for name, gate in gates.items() if gate["status"] != "PASS" and name != "minute_data"]
            if not plan.folds:
                blockers.append("INSUFFICIENT_HISTORY_FOR_WALK_FORWARD")
            if complete < MINIMUM_LABELLED_SAMPLES_PER_HORIZON:
                blockers.append("NO_COMPLETE_LABELS")
            horizons[plan.horizon] = {
                "label_status_counts": dict(sorted(counts[plan.horizon].items())),
                "walk_forward_folds": len(plan.folds), "blockers": blockers,
                "decision": "BLOCKED" if blockers else "ELIGIBLE_FOR_DESIGN_REVIEW",
            }
        horizons["intraday"] = {
            "label_status_counts": {}, "walk_forward_folds": 0,
            "blockers": ["minute_data"], "decision": "BLOCKED",
        }
        _write_json(partial / "quality-gates.json", {
            "schema_version": 1, "gates": gates, "horizons": horizons,
            "reference_evidence_complete": references_pass,
            "feature_rows": feature_rows, "symbols": len(symbols),
            "sessions": len(axis), "session_source": axis.source,
            "training_readiness": "NOT_READY", "model_training": "NOT_PERFORMED",
        })
        outputs = {
            path.name: _stream_sha256(path) for path in sorted(partial.iterdir()) if path.is_file()
        }
        _write_json(partial / "run-manifest.json", {
            **binding, "run_id": run_id, "outputs": outputs, "raw_inputs_read": False,
        })
        os.rename(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final
