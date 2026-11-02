"""Read-only M3.4 run: minute inventory, quality, reconciliation, fusion and gates.

Inputs are the separate minute raw root and existing M3.1 and M3.2 runs, all checked
against their hashes. Nothing is fetched or re-extracted, the daily tree is only
hashed, and `training_readiness` is fixed at NOT_READY. Every decision is reported
separately so that a passing check cannot stand in for another.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from stock_gplm.m2.master_validation import read_shoonya_nse_eq_inventory_v1

from .evidence import SessionAxis
from .features import FEATURE_NAMES, compute_symbol_features
from .fusion import (
    FUSION_COLUMNS, FUSION_CONTRACT, DailySnapshot, causality_audit, fuse_session, select_daily,
)
from .minute import (
    SCHEMA_NAME, MinuteSchemaError, SegmentReport, SessionPolicy, is_session_date, validate_segment,
    verify_publication,
)
from .minute_publish import DAILY_LOCK_NAME, LOCK_NAME
from .readiness import DatasetRow, _active_sync_lock, _canonical_hash, _stream_sha256, _write_json
from .splits import LeakageError

RUN_VERSION = 2
VOLUME_GAP_REVIEW = Decimal("0.01")
SEGMENT_FIELDS = (
    "symbol", "session_date", "publication", "content_decision", "records", "regular_bars",
    "off_session_bars", "missing_regular_minutes", "flag_counts", "issues", "meta_variant",
    "fetched_at_utc", "sha256",
)
BAR_FIELDS = (
    "symbol", "session_date", "bar_label_ist", "bar_label_utc", "open", "high", "low", "close",
    "volume", "cumulative_volume", "interval_vwap", "flags", "source_sha256",
)
RECONCILIATION_FIELDS = (
    "symbol", "session_date", "daily_open", "first_bar_open", "first_regular_open", "daily_high",
    "minute_high", "daily_low", "minute_low", "daily_close", "last_bar_close", "daily_volume",
    "minute_volume", "last_cumulative_volume", "volume_gap_fraction", "findings",
)


@dataclass(frozen=True)
class _Segment:
    report: SegmentReport
    publication: str
    meta_variant: str
    fetched_at_utc: str
    sha256: str
    token: str


def _scan_minute_root(raw_root: Path, policy: SessionPolicy) -> tuple[list[_Segment], dict[str, str], list[dict[str, str]]]:
    """Validate every publication in the root; anything unexpected stays visible."""
    segments: list[_Segment] = []
    hashes: dict[str, str] = {}
    entries: list[dict[str, str]] = []
    for symbol_dir in sorted(raw_root.iterdir(), key=lambda item: item.name):
        if symbol_dir.name == LOCK_NAME:
            continue
        if symbol_dir.is_symlink() or not symbol_dir.is_dir():
            entries.append({"code": "UNSUPPORTED_RAW_ENTRY", "path": symbol_dir.name})
            continue
        for path in sorted(symbol_dir.iterdir(), key=lambda item: item.name):
            relative = str(path.relative_to(raw_root))
            if path.is_symlink() or not path.is_file():
                entries.append({"code": "UNSUPPORTED_RAW_ENTRY", "path": relative})
                continue
            hashes[relative] = _stream_sha256(path)
        for payload in sorted(symbol_dir.glob("*.json.gz")):
            day = payload.name.removesuffix(".json.gz")
            if not is_session_date(day):
                entries.append({"code": "UNEXPECTED_SEGMENT_NAME", "path": str(payload.relative_to(raw_root))})
                continue
            data = payload.read_bytes()
            meta_path = symbol_dir / f"{day}.meta.json"
            marker_path = symbol_dir / f"{day}.json.gz.done"
            try:
                publication = verify_publication(
                    data, meta_path.read_bytes(), marker_path.read_bytes(), symbol_dir.name, day,
                )
            except (OSError, MinuteSchemaError, ValueError) as exc:
                report = SegmentReport(symbol_dir.name, day, "NOT_EVALUATED", issues=[str(exc)])
                segments.append(_Segment(report, "REJECT", "", "", hashlib.sha256(data).hexdigest(), ""))
                continue
            segments.append(_Segment(
                validate_segment(data, symbol_dir.name, day, policy), "VERIFIED",
                publication.meta_variant, publication.fetched_at_utc, publication.sha256, publication.token,
            ))
    return segments, hashes, entries


def _check_run(run: Path, contract: str, names: tuple[str, ...]) -> dict[str, Any]:
    manifest = json.loads((run / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("contract") != contract:
        raise ValueError(f"{run} is not a {contract} run")
    for name in names:
        if _stream_sha256(run / name) != manifest["outputs"][name]:
            raise ValueError(f"{name} does not match its manifest")
    return manifest


def confirm_daily_publication(readiness_manifest: Mapping[str, Any], daily_root: Path) -> dict[str, Any]:
    """Hash the published daily tree against M3.1's binding; it extracts nothing."""
    daily_root = Path(daily_root).resolve()
    if _active_sync_lock(daily_root):
        raise RuntimeError("daily publication lock is active")
    bound = {
        key: value for key, value in readiness_manifest["input_sha256"].items()
        if Path(key).is_relative_to(daily_root)
    }
    mismatched: list[str] = []
    for name, digest in sorted(bound.items()):
        path = Path(name)
        if path.is_symlink() or not path.is_file() or _stream_sha256(path) != digest:
            mismatched.append(name)
    present = {
        str(item) for item in daily_root.rglob("*") if item.is_file() and item.name != DAILY_LOCK_NAME
    }
    unbound = sorted(present - set(bound))
    payloads = sum(name.endswith(".json.gz") for name in bound)
    return {
        "status": "CONFIRMED_PUBLISHED_LOCALLY" if bound and not mismatched and not unbound else "NOT_CONFIRMED",
        "daily_root": str(daily_root), "files_hashed": len(bound), "publications": payloads,
        "mismatched_files": mismatched, "unbound_files": unbound, "re_extraction_performed": False,
    }


def _stream_daily(
    readiness_run: Path, symbols: set[str], window_start: str,
) -> tuple[dict[str, list[DatasetRow]], dict[str, int]]:
    rows: dict[str, list[DatasetRow]] = {}
    in_window: dict[str, int] = {}
    with (readiness_run / "research-dataset.csv").open(encoding="utf-8", newline="") as stream:
        for item in csv.DictReader(stream):
            if item["trading_date"] >= window_start:
                in_window[item["symbol"]] = in_window.get(item["symbol"], 0) + 1
            if item["symbol"] in symbols:
                rows.setdefault(item["symbol"], []).append(DatasetRow(
                    symbol=item["symbol"], trading_date=item["trading_date"],
                    source_timestamp_utc=item["source_timestamp_utc"], open=item["open"], high=item["high"],
                    low=item["low"], close=item["close"], volume=int(item["volume"]),
                    source_path=item["source_path"], source_sha256=item["source_sha256"],
                ))
    return rows, in_window


def _stream_features(research_run: Path, rows: Mapping[str, list[DatasetRow]]) -> dict[str, dict[str, DailySnapshot]]:
    closes = {
        (symbol, item.trading_date): item for symbol, values in rows.items() for item in values
    }
    result: dict[str, dict[str, DailySnapshot]] = {}
    with (research_run / "features.csv").open(encoding="utf-8", newline="") as stream:
        for item in csv.DictReader(stream):
            if item["symbol"] not in rows:
                continue
            source = closes[(item["symbol"], item["as_of_date"])]
            if source.source_sha256 != item["source_sha256"]:
                raise ValueError("M3.2 features and the M3.1 dataset name different sources")
            result.setdefault(item["symbol"], {})[item["as_of_date"]] = DailySnapshot(
                item["as_of_date"], Decimal(source.close), item["source_sha256"],
                {name: item[name] or None for name in FEATURE_NAMES},
            )
    return result


def _verify_daily_causality(
    rows: list[DatasetRow], snapshots: Mapping[str, DailySnapshot], axis: SessionAxis, dates: list[str],
) -> int:
    """Recompute M3.2 features on truncated history and compare with the published file."""
    checks = 0
    for day in dates:
        history = [item for item in rows if item.trading_date <= day]
        if not history or history[-1].trading_date != day:
            continue
        computed = compute_symbol_features(history, axis)[-1]
        if dict(computed.values) != dict(snapshots[day].features):
            raise LeakageError(f"{computed.symbol} {day}: M3.2 feature differs when later candles are removed")
        checks += 1
    return checks


def _reconcile(report: SegmentReport, candle: DatasetRow | None, regular: set[str]) -> dict[str, Any]:
    bars = report.bars
    row: dict[str, Any] = {"symbol": report.symbol, "session_date": report.session_date}
    if candle is None:
        return {**row, "findings": "NO_DAILY_CANDLE"}
    in_session = [item for item in bars if item.clock in regular]
    high = max(item.high for item in bars)
    low = min(item.low for item in bars)
    comparable = all(item.volume >= 0 for item in bars)
    volume = sum(item.volume for item in in_session)
    gap = (int(candle.volume) - volume) / Decimal(candle.volume) if candle.volume and comparable else None
    findings: list[str] = []
    if bars[0].open != Decimal(candle.open):
        findings.append("OPEN_DIFFERS")
    if high != Decimal(candle.high):
        findings.append("MINUTE_HIGH_ABOVE_DAILY" if high > Decimal(candle.high) else "MINUTE_HIGH_BELOW_DAILY")
    if low != Decimal(candle.low):
        findings.append("MINUTE_LOW_BELOW_DAILY" if low < Decimal(candle.low) else "MINUTE_LOW_ABOVE_DAILY")
    if bars[-1].close != Decimal(candle.close):
        findings.append("CLOSE_DIFFERS")
    if not comparable:
        findings.append("VOLUME_NOT_COMPARABLE_NEGATIVE_BARS")
    elif gap is None or abs(gap) > VOLUME_GAP_REVIEW:
        findings.append("VOLUME_GAP_OVER_1_PERCENT")
    return {
        **row, "daily_open": candle.open, "first_bar_open": str(bars[0].open),
        "first_regular_open": str(in_session[0].open) if in_session else "",
        "daily_high": candle.high, "minute_high": str(high), "daily_low": candle.low, "minute_low": str(low),
        "daily_close": candle.close, "last_bar_close": str(bars[-1].close),
        "daily_volume": candle.volume, "minute_volume": volume if comparable else "",
        "last_cumulative_volume": bars[-1].cumulative_volume,
        "volume_gap_fraction": format(gap.quantize(Decimal("0.000001")), "f") if gap is not None else "",
        "findings": "|".join(findings),
    }


def _gate(passed: bool, blocker: str) -> dict[str, str]:
    return {"status": "PASS"} if passed else {"status": "BLOCKED", "blocker": blocker}


def build_minute_run(
    *, minute_root: Path, daily_root: Path, master: Path, policy_path: Path, readiness_run: Path,
    research_run: Path, import_audit: Path, output_root: Path, source_label: str,
) -> Path:
    """Write the M3.4 evidence run; no gate here can make training ready."""
    minute_root, daily_root = Path(minute_root).resolve(), Path(daily_root).resolve()
    readiness_run, research_run = Path(readiness_run).resolve(), Path(research_run).resolve()
    output_root = Path(output_root).resolve()
    for root in (minute_root, daily_root):
        if output_root == root or root in output_root.parents:
            raise ValueError("output root must be outside the immutable raw trees")
    if (minute_root / DAILY_LOCK_NAME).exists() or daily_root == minute_root:
        raise ValueError("minute raw root must be separate from the daily publication tree")
    policy = SessionPolicy.load(policy_path)
    m31 = _check_run(readiness_run, "stock-gplm.m3-readiness-run.v1", ("research-dataset.csv", "observed-sessions.csv"))
    m32 = _check_run(research_run, "stock-gplm.m32-research-run.v1", ("features.csv", "quality-gates.json"))
    if m32["readiness_run_id"] != m31["run_id"]:
        raise ValueError("M3.2 run was not built from this M3.1 run")
    daily_confirmation = confirm_daily_publication(m31, daily_root)
    segments, raw_hashes, raw_entries = _scan_minute_root(minute_root, policy)
    if not segments:
        raise ValueError("minute raw root has no publications")
    master_symbols = {item["symbol"]: item["token"] for item in read_shoonya_nse_eq_inventory_v1(master)}
    audit_path = Path(import_audit) / "import-audit.json"
    binding = {
        "contract": "stock-gplm.m34-minute-run.v1", "version": RUN_VERSION, "schema": SCHEMA_NAME,
        "fusion_contract": FUSION_CONTRACT, "minute_inputs_sha256": _canonical_hash(raw_hashes),
        "import_audit_sha256": _stream_sha256(audit_path), "master_sha256": _stream_sha256(Path(master)),
        "session_policy_sha256": _stream_sha256(Path(policy_path)), "readiness_run_id": m31["run_id"],
        "research_run_id": m32["run_id"], "source_label": source_label,
    }
    run_id = _canonical_hash(binding)
    final = output_root / run_id
    if final.exists():
        raise FileExistsError(f"minute run already exists: {run_id}")

    with (readiness_run / "observed-sessions.csv").open(encoding="utf-8", newline="") as stream:
        axis = SessionAxis(tuple(item["trading_date"] for item in csv.DictReader(stream)), "EMPIRICAL_UNION")
    window_start = min(item.report.session_date for item in segments)
    window_end = max(item.report.session_date for item in segments)
    symbols = {item.report.symbol for item in segments}
    daily_rows, in_window = _stream_daily(readiness_run, symbols, window_start)
    snapshots = _stream_features(research_run, daily_rows)
    candles = {(symbol, item.trading_date): item for symbol, values in daily_rows.items() for item in values}
    regular = set(policy.regular_minutes)
    usable = [item for item in segments if item.publication == "VERIFIED" and item.report.decision != "REJECT"]
    fusable = [item for item in usable if item.report.decision != "QUARANTINE"]

    output_root.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=output_root))
    try:
        with (partial / "minute-segments.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, SEGMENT_FIELDS, lineterminator="\n")
            writer.writeheader()
            for item in segments:
                report = item.report
                writer.writerow({
                    "symbol": report.symbol, "session_date": report.session_date,
                    "publication": item.publication, "content_decision": report.decision,
                    "records": report.records, "regular_bars": report.regular_bars,
                    "off_session_bars": report.off_session_bars,
                    "missing_regular_minutes": "|".join(report.missing_regular_minutes),
                    "flag_counts": json.dumps(report.flag_counts, sort_keys=True),
                    "issues": "|".join(report.issues), "meta_variant": item.meta_variant,
                    "fetched_at_utc": item.fetched_at_utc, "sha256": item.sha256,
                })
        flag_totals: dict[str, int] = {}
        bars_total = 0
        with (partial / "minute-bars.csv").open("x", encoding="utf-8", newline="") as bars_stream, \
                (partial / "bar-flags.csv").open("x", encoding="utf-8", newline="") as flags_stream:
            bars_writer = csv.DictWriter(bars_stream, BAR_FIELDS, lineterminator="\n")
            flags_writer = csv.DictWriter(flags_stream, BAR_FIELDS, lineterminator="\n")
            bars_writer.writeheader()
            flags_writer.writeheader()
            for item in usable:
                for bar in item.report.bars:
                    bars_total += 1
                    row = {
                        "symbol": item.report.symbol, "session_date": item.report.session_date,
                        "bar_label_ist": bar.start_ist.isoformat(),
                        "bar_label_utc": bar.start_utc.isoformat().replace("+00:00", "Z"),
                        "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
                        "volume": bar.volume, "cumulative_volume": bar.cumulative_volume,
                        "interval_vwap": bar.interval_vwap, "flags": "|".join(bar.flags),
                        "source_sha256": item.sha256,
                    }
                    bars_writer.writerow(row)
                    if bar.flags:
                        flags_writer.writerow(row)
                    for flag in bar.flags:
                        flag_totals[flag] = flag_totals.get(flag, 0) + 1
        reconciliation: list[dict[str, Any]] = []
        with (partial / "daily-reconciliation.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, RECONCILIATION_FIELDS, lineterminator="\n", restval="")
            writer.writeheader()
            for item in usable:
                row = _reconcile(item.report, candles.get((item.report.symbol, item.report.session_date)), regular)
                writer.writerow(row)
                reconciliation.append(row)
        finding_totals: dict[str, int] = {}
        for row in reconciliation:
            for finding in filter(None, row["findings"].split("|")):
                finding_totals[finding] = finding_totals.get(finding, 0) + 1

        # Fusion and its audit. A LeakageError aborts the run; nothing is published.
        fused_rows = 0
        status_counts: dict[str, int] = {}
        prefix_checks = 0
        with (partial / "fused-features.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, FUSION_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for item in fusable:
                daily = snapshots.get(item.report.symbol, {})
                prefix_checks += causality_audit(item.report, daily, axis.sessions, policy)
                for row in fuse_session(item.report, daily, axis.sessions, policy):
                    writer.writerow(row)
                    fused_rows += 1
                    status_counts[row["daily_status"] or ""] = status_counts.get(row["daily_status"] or "", 0) + 1
        daily_checks = 0
        # M3.2 restarts series at supplied actions and may use a supplied axis, so a
        # bare recomputation is comparable only for an evidence-free run.
        recomputable = not m32["evidence"]
        for symbol, rows in sorted(daily_rows.items()) if recomputable else ():
            read = sorted({
                snapshot.as_of_date for snapshot in (
                    select_daily(snapshots.get(symbol, {}), axis.sessions, item.report.session_date)[1]
                    for item in fusable if item.report.symbol == symbol
                ) if snapshot is not None
            })
            daily_checks += _verify_daily_causality(rows, snapshots[symbol], axis, read)
        _write_json(partial / "leakage-audit.json", {
            "schema_version": 1, "status": "PASS", "session_prefix_checks": prefix_checks,
            "daily_truncation_checks": daily_checks, "fused_rows": fused_rows,
            "daily_truncation": "EVALUATED" if recomputable else "NOT_EVALUATED_WITH_REFERENCE_EVIDENCE",
            "rule": "daily_as_of_date < session_date and daily availability <= bar availability",
            "evidence_limit": (
                "Proves this pipeline reads no later input. Files were fetched after the sessions they "
                "describe, so a later provider revision or retroactive adjustment cannot be detected."
            ),
        })

        symbol_days: dict[str, list[str]] = {}
        for item in segments:
            symbol_days.setdefault(item.report.symbol, []).append(item.report.session_date)
        with (partial / "symbol-coverage.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow((
                "symbol", "minute_sessions", "first_minute_session", "last_minute_session",
                "daily_sessions_in_window", "coverage_reason",
            ))
            for symbol in sorted(master_symbols):
                days = sorted(symbol_days.get(symbol, ()))
                writer.writerow((
                    symbol, len(days), days[0] if days else "", days[-1] if days else "",
                    in_window.get(symbol, 0), "PARTIAL_OR_ABSENT_REASON_UNKNOWN" if not days else "",
                ))
        gaps: list[dict[str, str]] = []
        axis_window = [item for item in axis.sessions if window_start <= item <= window_end]
        for symbol, days in sorted(symbol_days.items()):
            have = set(days)
            daily_days = {item.trading_date for item in daily_rows.get(symbol, ())}
            for day in axis_window:
                if day not in have:
                    gaps.append({
                        "symbol": symbol, "session_date": day, "kind": "EMPIRICAL_SESSION_WITHOUT_MINUTE_FILE",
                        "symbol_has_daily_candle": str(day in daily_days).lower(),
                        "first_minute_session_for_symbol": min(days),
                        "weekday": str(date.fromisoformat(day).weekday()), "reason": "UNKNOWN",
                    })
            for day in sorted(have - set(axis.sessions)):
                gaps.append({
                    "symbol": symbol, "session_date": day, "kind": "MINUTE_FILE_OUTSIDE_DAILY_AXIS",
                    "symbol_has_daily_candle": "false", "first_minute_session_for_symbol": min(days),
                    "weekday": str(date.fromisoformat(day).weekday()), "reason": "UNKNOWN",
                })
        with (partial / "coverage-gaps.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, (
                "symbol", "session_date", "kind", "symbol_has_daily_candle",
                "first_minute_session_for_symbol", "weekday", "reason",
            ), lineterminator="\n")
            writer.writeheader()
            writer.writerows(gaps)

        decisions: dict[str, int] = {}
        for item in segments:
            decisions[item.report.decision] = decisions.get(item.report.decision, 0) + 1
        minute_only_symbols = sorted(symbols - set(master_symbols))
        token_mismatch = sorted(
            item.report.symbol for item in segments
            if item.token and master_symbols.get(item.report.symbol) != item.token
        )
        m32_gates = json.loads((research_run / "quality-gates.json").read_text(encoding="utf-8"))
        gates: dict[str, dict[str, str]] = {
            "minute_publication": _gate(
                all(item.publication == "VERIFIED" for item in segments) and not raw_entries,
                "a minute publication failed its contract",
            ),
            "minute_schema": _gate(
                decisions.get("REJECT", 0) == 0 and decisions.get("NOT_EVALUATED", 0) == 0,
                "a segment does not satisfy ShoonyaMinuteV1",
            ),
            "minute_content": _gate(
                set(decisions) <= {"ACCEPT"}, "segments carry unrepaired flags that need review",
            ),
            "minute_session_policy": _gate(
                policy.status == "AUTHORITATIVE" and "QUARANTINE" not in decisions,
                "regular-session bars are not exchange-sourced for every date; some sessions are quarantined",
            ),
            "minute_universe_coverage": _gate(
                not set(master_symbols) - symbols and not minute_only_symbols,
                f"{len(set(master_symbols) & symbols)} of {len(master_symbols)} current EQ symbols have minute files",
            ),
            "minute_history_coverage": _gate(not gaps, f"{len(gaps)} symbol-sessions lack a minute file or daily axis entry"),
            "daily_minute_reconciliation": _gate(
                not finding_totals, "minute and daily values differ; causes are unknown and unrepaired",
            ),
            "leakage_audit": {"status": "PASS"},
            **{f"daily_{name}": value for name, value in m32_gates["gates"].items() if name != "minute_data"},
        }
        blockers = [name for name, gate in gates.items() if gate["status"] != "PASS"]
        _write_json(partial / "quality-gates.json", {
            "schema_version": 1, "gates": gates, "blockers": blockers,
            "intraday_horizon": "BLOCKED" if blockers else "ELIGIBLE_FOR_DESIGN_REVIEW",
            "fused_features_use": "DESCRIPTIVE_RESEARCH_ONLY",
            "training_readiness": "NOT_READY", "model_training": "NOT_PERFORMED",
        })
        _write_json(partial / "daily-confirmation.json", daily_confirmation)
        _write_json(partial / "coverage-quality.json", {
            "schema_version": 1, "schema": SCHEMA_NAME, "interval_minutes": 1,
            "interval_evidence": "requested interval=1; whole-minute labels; no sub-minute bars observed",
            "timezone_evidence": "time read as IST equals ssboe on every accepted bar",
            "bar_label_semantics": "UNKNOWN_START_OR_END; usable one minute after the label either way",
            "volume_semantics": {
                "intv": "per-bar volume on sessions before 2026-08-03; negative values from then on have unknown meaning; features use only unquarantined sessions",
                "v": "appears cumulative and can include volume absent from intv; never a feature",
                "intvwap": "checked against the bar range; never a feature",
            },
            "session_policy": {
                "first_bar": policy.first_bar, "last_bar": policy.last_bar,
                "status": policy.status, "provenance": policy.provenance,
                "valid_through": policy.valid_through,
            },
            "window": {"first_minute_session": window_start, "last_minute_session": window_end},
            "universe": {
                "current_eq_symbols": len(master_symbols), "symbols_with_minute_files": len(symbols),
                "symbols_not_in_master": minute_only_symbols, "token_mismatches": sorted(set(token_mismatch)),
            },
            "symbols": {
                symbol: {
                    "minute_sessions": len(days), "first": min(days), "last": max(days),
                    "daily_sessions_in_window": in_window.get(symbol, 0),
                    "fused_sessions": sum(item.report.symbol == symbol for item in fusable),
                    "quarantined_sessions": sorted(
                        item.report.session_date for item in segments
                        if item.report.symbol == symbol and item.report.decision == "QUARANTINE"
                    ),
                } for symbol, days in sorted(symbol_days.items())
            },
            "segments": len(segments), "segment_decisions": dict(sorted(decisions.items())),
            "bars": bars_total, "flag_counts": dict(sorted(flag_totals.items())),
            "reconciled_sessions": len(reconciliation), "reconciliation_findings": dict(sorted(finding_totals.items())),
            "gaps": len(gaps), "fused_rows": fused_rows, "daily_status_counts": dict(sorted(status_counts.items())),
            "unsupported_raw_entries": raw_entries,
        })
        again = {
            path: _stream_sha256(minute_root / path) for path in raw_hashes
        }
        if again != raw_hashes:
            raise RuntimeError("a minute raw file changed during the run")
        outputs = {path.name: _stream_sha256(path) for path in sorted(partial.iterdir()) if path.is_file()}
        _write_json(partial / "run-manifest.json", {
            **binding, "run_id": run_id, "outputs": outputs, "raw_inputs_mutated": False,
            "minute_input_sha256": dict(sorted(raw_hashes.items())),
        })
        os.rename(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final
