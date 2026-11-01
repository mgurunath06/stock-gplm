"""Build deterministic local research datasets without mutating raw publications."""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, Iterable

from stock_gplm.ingestion import validate_daily
from stock_gplm.m2.master_validation import read_shoonya_nse_eq_inventory_v1

PIPELINE_VERSION = 2
MAX_INPUT_BYTES = 16 * 1024 * 1024
EMA_PERIOD = 20
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9


@dataclass(frozen=True)
class DatasetRow:
    symbol: str
    trading_date: str
    source_timestamp_utc: str
    open: str
    high: str
    low: str
    close: str
    volume: int
    source_path: str
    source_sha256: str
    ema20: str | None = None
    rsi14: str | None = None
    macd_12_26: str | None = None
    macd_signal_9: str | None = None
    macd_histogram: str | None = None


def _sha256(path: Path) -> str:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _active_sync_lock(raw_root: Path) -> bool:
    lock = raw_root / ".stock-gplm-m24.lock"
    if lock.is_symlink():
        return True
    if not lock.exists():
        return False
    try:
        with lock.open("r", encoding="utf-8") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def _days_between(start: str, end: str) -> Iterable[str]:
    cursor = date.fromisoformat(start) + timedelta(days=1)
    final = date.fromisoformat(end)
    while cursor < final:
        yield cursor.isoformat()
        cursor += timedelta(days=1)


def _decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def _ema(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / Decimal(period)
    result[period - 1] = current
    alpha = Decimal(2) / Decimal(period + 1)
    for index in range(period, len(values)):
        current = (values[index] - current) * alpha + current
        result[index] = current
    return result


def _rsi(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) <= period:
        return result
    changes = [right - left for left, right in zip(values, values[1:])]
    gains = [max(change, Decimal(0)) for change in changes]
    losses = [max(-change, Decimal(0)) for change in changes]
    average_gain = sum(gains[:period]) / Decimal(period)
    average_loss = sum(losses[:period]) / Decimal(period)

    def value() -> Decimal:
        if average_loss == 0:
            return Decimal(100) if average_gain > 0 else Decimal(50)
        relative_strength = average_gain / average_loss
        return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)

    result[period] = value()
    for index in range(period + 1, len(values)):
        average_gain = (
            average_gain * Decimal(period - 1) + gains[index - 1]
        ) / Decimal(period)
        average_loss = (
            average_loss * Decimal(period - 1) + losses[index - 1]
        ) / Decimal(period)
        result[index] = value()
    return result


def _macd(
    values: list[Decimal],
) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    fast = _ema(values, MACD_FAST)
    slow = _ema(values, MACD_SLOW)
    line: list[Decimal | None] = [
        left - right if left is not None and right is not None else None
        for left, right in zip(fast, slow)
    ]
    available = [item for item in line if item is not None]
    signal_available = _ema(available, MACD_SIGNAL)
    signal: list[Decimal | None] = [None] * len(values)
    cursor = 0
    for index, item in enumerate(line):
        if item is not None:
            signal[index] = signal_available[cursor]
            cursor += 1
    histogram = [
        item - signal_item if item is not None and signal_item is not None else None
        for item, signal_item in zip(line, signal)
    ]
    return line, signal, histogram


def _with_indicators(rows: list[DatasetRow]) -> list[DatasetRow]:
    closes = [Decimal(item.close) for item in rows]
    ema = _ema(closes, EMA_PERIOD)
    rsi = _rsi(closes, RSI_PERIOD)
    macd, signal, histogram = _macd(closes)
    enriched: list[DatasetRow] = []
    for index, item in enumerate(rows):
        values = asdict(item)
        values.update(
            ema20=_decimal(ema[index]) if ema[index] is not None else None,
            rsi14=_decimal(rsi[index]) if rsi[index] is not None else None,
            macd_12_26=_decimal(macd[index]) if macd[index] is not None else None,
            macd_signal_9=_decimal(signal[index]) if signal[index] is not None else None,
            macd_histogram=_decimal(histogram[index]) if histogram[index] is not None else None,
        )
        enriched.append(DatasetRow(**values))
    return enriched


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _points(values: list[Decimal], left: int, top: int, width: int, height: int) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low or Decimal(1)
    denominator = max(1, len(values) - 1)
    return " ".join(
        f"{left + index * width / denominator:.2f},{top + height - float((value - low) / span) * height:.2f}"
        for index, value in enumerate(values)
    )


def _svg(symbol: str, rows: list[DatasetRow], status: str) -> str:
    closes = [Decimal(item.close) for item in rows]
    volumes = [item.volume for item in rows]
    ema_values = [Decimal(item.ema20) for item in rows if item.ema20 is not None]
    rsi_values = [Decimal(item.rsi14) for item in rows if item.rsi14 is not None]
    macd_values = [Decimal(item.macd_12_26) for item in rows if item.macd_12_26 is not None]
    width, height = 1000, 780
    labels = " ".join(item.trading_date for item in rows)
    price_points = _points(closes, 80, 80, 840, 190)
    ema_points = _points(ema_values, 80, 80, 840, 190)
    max_volume = max(volumes, default=1)
    bars = []
    for index, volume in enumerate(volumes):
        x = 80 + index * 840 / max(1, len(volumes))
        bar_height = 130 * volume / max_volume
        bars.append(
            f'<rect x="{x:.2f}" y="{450 - bar_height:.2f}" width="{max(4, 700 / max(1, len(volumes))):.2f}" height="{bar_height:.2f}" fill="#6384b8"/>'
        )
    insufficient = (
        f"EMA20 unavailable: {len(rows)}/20 observations; "
        f"RSI14 unavailable: {len(rows)}/15; MACD signal unavailable: {len(rows)}/34"
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#fafafa"/>
<text x="50" y="36" font-family="sans-serif" font-size="24">{escape(symbol)} local exploratory view</text>
<text x="50" y="60" font-family="sans-serif" font-size="13" fill="#9b2c2c">{escape(status)}</text>
<text x="80" y="78" font-family="sans-serif" font-size="14">Close and EMA20</text>
<rect x="80" y="80" width="840" height="190" fill="#fff" stroke="#bbb"/>
<polyline points="{price_points}" fill="none" stroke="#1f5d8f" stroke-width="3"/>
<polyline points="{ema_points}" fill="none" stroke="#d97706" stroke-width="2"/>
<text x="80" y="300" font-family="monospace" font-size="12">dates: {escape(labels)}</text>
<text x="80" y="318" font-family="sans-serif" font-size="13">Volume</text>
<rect x="80" y="320" width="840" height="130" fill="#fff" stroke="#bbb"/>{''.join(bars)}
<text x="80" y="485" font-family="sans-serif" font-size="13">RSI14</text>
<rect x="80" y="490" width="840" height="90" fill="#fff" stroke="#bbb"/>
<text x="95" y="540" font-family="sans-serif" font-size="14">{escape('available' if rsi_values else insufficient)}</text>
<text x="80" y="620" font-family="sans-serif" font-size="13">MACD (12,26,9)</text>
<rect x="80" y="625" width="840" height="90" fill="#fff" stroke="#bbb"/>
<text x="95" y="675" font-family="sans-serif" font-size="14">{escape('available' if macd_values else insufficient)}</text>
<text x="80" y="752" font-family="sans-serif" font-size="12">No missing-date explanation, adjustment, prediction, or trading signal is inferred.</text>
</svg>
'''


def _exploration(symbol: str, rows: list[DatasetRow], blockers: list[str]) -> dict[str, Any]:
    enough = len(rows) >= MACD_SLOW + MACD_SIGNAL - 1
    return {
        "schema_version": 1,
        "symbol": symbol,
        "decision": "EXPLORATORY_READY" if enough and not blockers else "INCOMPLETE",
        "observations": len(rows),
        "first_observed_date": rows[0].trading_date if rows else None,
        "last_observed_date": rows[-1].trading_date if rows else None,
        "indicator_availability": {
            "price": bool(rows),
            "volume": bool(rows),
            "ema20": len(rows) >= EMA_PERIOD,
            "rsi14": len(rows) > RSI_PERIOD,
            "macd_12_26": len(rows) >= MACD_SLOW,
            "macd_signal_9": enough,
        },
        "blockers": blockers,
        "prohibited_inferences": [
            "no prediction", "no trading signal", "no inferred market closure",
            "no inferred listing membership", "no unproven corporate-action adjustment",
        ],
    }


def build_readiness_run(
    *, raw_root: Path, master: Path, output_root: Path,
    exploration_symbols: tuple[str, ...] = ("RELIANCE-EQ", "TCS-EQ"),
) -> Path:
    """Validate local publications and publish a checksum-bound derived run."""
    raw_root = Path(raw_root).resolve()
    master = Path(master).resolve()
    output_root = Path(output_root).resolve()
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ValueError("raw root must be a real directory")
    if output_root == raw_root or raw_root in output_root.parents:
        raise ValueError("output root must be outside the immutable raw tree")
    if _active_sync_lock(raw_root):
        raise RuntimeError("M3 refused an active M2.4 publication lock")
    master_sha256 = _sha256(master)
    current_symbols = {item["symbol"] for item in read_shoonya_nse_eq_inventory_v1(master)}
    if any(symbol not in current_symbols for symbol in exploration_symbols):
        raise ValueError("exploration symbol is absent from the current EQ master")

    input_hashes: dict[str, str] = {str(master): master_sha256}
    segments: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    rows_by_key: dict[tuple[str, str], DatasetRow] = {}
    symbol_ranges: dict[str, list[tuple[str, str]]] = {}
    for symbol_dir in sorted(raw_root.iterdir(), key=lambda item: item.name):
        if symbol_dir.name == ".stock-gplm-m24.lock":
            continue
        if symbol_dir.is_symlink() or not symbol_dir.is_dir():
            issues.append({"code": "UNSUPPORTED_RAW_ENTRY", "path": symbol_dir.name})
            continue
        symbol = symbol_dir.name
        for path in sorted(symbol_dir.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                issues.append({"code": "UNSUPPORTED_RAW_ENTRY", "path": str(path)})
                continue
            input_hashes[str(path)] = _sha256(path)
        if symbol not in current_symbols:
            issues.append({"code": "SYMBOL_NOT_IN_CURRENT_MASTER", "path": symbol})
            continue
        for payload in sorted(symbol_dir.glob("*.json.gz")):
            report = validate_daily(payload)
            segment = {
                "symbol": symbol,
                "source": str(payload.relative_to(raw_root)),
                "sha256": input_hashes[str(payload)],
                "requested_start": report.segment_start,
                "requested_end": report.segment_end,
                "publication_status": report.publication_integrity.status,
                "content_status": report.content_quality.status,
                "disposition": report.disposition,
                "records": report.content_quality.records_seen,
                "chronology": report.content_quality.chronology,
                "issues": [
                    asdict(item) for item in (
                        report.publication_integrity.issues + report.content_quality.issues
                    )
                ],
            }
            segments.append(segment)
            if report.segment_start and report.segment_end:
                symbol_ranges.setdefault(symbol, []).append(
                    (report.segment_start, report.segment_end)
                )
            if report.disposition != "ACCEPT":
                issues.append({"code": "SEGMENT_NOT_ACCEPTED", "path": segment["source"]})
                continue
            for candle in report.content_quality.normalized_candles:
                row = DatasetRow(
                    symbol=symbol,
                    trading_date=candle.trading_date,
                    source_timestamp_utc=candle.source_timestamp_utc,
                    open=str(candle.open), high=str(candle.high), low=str(candle.low),
                    close=str(candle.close), volume=candle.volume,
                    source_path=segment["source"], source_sha256=segment["sha256"],
                )
                key = (symbol, candle.trading_date)
                previous = rows_by_key.get(key)
                if previous is not None:
                    code = "DUPLICATE_CANDLE" if previous == row else "CONTRADICTORY_CANDLE"
                    issues.append({"code": code, "symbol": symbol, "date": candle.trading_date})
                else:
                    rows_by_key[key] = row

    grouped: dict[str, list[DatasetRow]] = {}
    for row in rows_by_key.values():
        grouped.setdefault(row.symbol, []).append(row)
    for symbol, rows in grouped.items():
        grouped[symbol] = _with_indicators(sorted(rows, key=lambda item: item.trading_date))
    all_rows = [row for symbol in sorted(grouped) for row in grouped[symbol]]

    coverage: list[dict[str, Any]] = []
    for symbol in exploration_symbols:
        rows = grouped.get(symbol, [])
        observed = {item.trading_date for item in rows}
        unknown_dates: list[str] = []
        for start, end in symbol_ranges.get(symbol, []):
            unknown_dates.extend(day for day in _days_between(start, end) if day not in observed)
        blockers = [
            "AUTHORITATIVE_NSE_CALENDAR_REQUIRED",
            "POINT_IN_TIME_LISTING_MEMBERSHIP_REQUIRED",
            "CORPORATE_ACTION_COVERAGE_REQUIRED",
            "HISTORICAL_ALIAS_COVERAGE_REQUIRED",
        ]
        if not rows:
            blockers.insert(0, "NO_LOCAL_VALIDATED_CANDLES")
        coverage.append({
            "symbol": symbol,
            "observed_candles": len(rows),
            "first_observed_date": rows[0].trading_date if rows else None,
            "last_observed_date": rows[-1].trading_date if rows else None,
            "unobserved_dates_within_published_ranges": sorted(set(unknown_dates)),
            "unobserved_date_classification": "UNKNOWN",
            "current_master_membership": True,
            "point_in_time_membership": "NOT_ESTABLISHED",
            "market_session_validation": "NOT_EVALUATED_NO_CALENDAR",
            "corporate_action_adjustment": "NOT_EVALUATED",
            "decision": "INCOMPLETE",
            "blockers": blockers,
        })

    binding = {
        "contract": "stock-gplm.m3-readiness-run.v1",
        "pipeline_version": PIPELINE_VERSION,
        "inputs": sorted(input_hashes.items()),
        "exploration_symbols": exploration_symbols,
    }
    run_id = _canonical_hash(binding)
    final_dir = output_root / run_id
    if final_dir.exists():
        raise FileExistsError(f"readiness run already exists: {run_id}")
    output_root.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f".{run_id}.partial-", dir=output_root))
    try:
        _write_json(partial / "inventory.json", {
            "schema_version": 1, "raw_root": str(raw_root),
            "master_sha256": master_sha256, "current_eq_symbols": len(current_symbols),
            "symbols_with_validated_candles": len(grouped),
            "zero_validated_candle_symbols": len(current_symbols - set(grouped)),
            "validated_candles": len(all_rows),
            "accepted_segments": sum(item["disposition"] == "ACCEPT" for item in segments),
            "raw_files": len(input_hashes) - 1, "segments": segments, "issues": issues,
        })
        _write_json(partial / "coverage-quality.json", {
            "schema_version": 1, "decision": "INCOMPLETE",
            "raw_candle_acceptance": "ACCEPT" if segments and not issues else "REVIEW",
            "historical_completeness": "INCOMPLETE",
            "training_readiness": "NOT_READY", "coverage": coverage,
        })
        fields = list(DatasetRow.__dataclass_fields__)
        with (partial / "research-dataset.csv").open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(asdict(item) for item in all_rows)
        for item in coverage:
            symbol = item["symbol"]
            rows = grouped.get(symbol, [])
            report = _exploration(symbol, rows, list(item["blockers"]))
            _write_json(partial / f"{symbol}.exploration.json", report)
            (partial / f"{symbol}.exploration.md").write_text(
                f"# {symbol} initial exploration\n\n"
                f"Decision: **{report['decision']}**\n\n"
                f"Validated observations: {report['observations']}.\n\n"
                "This is descriptive local evidence only. Missing dates remain UNKNOWN; "
                "no prediction, trading signal, membership, market closure, or adjustment "
                "is inferred.\n\n"
                f"Blockers: {', '.join(report['blockers'])}.\n",
                encoding="utf-8",
            )
            if rows:
                (partial / f"{symbol}.exploration.svg").write_text(
                    _svg(symbol, rows, report["decision"]), encoding="utf-8"
                )
        for path_text, expected in input_hashes.items():
            if _sha256(Path(path_text)) != expected:
                raise RuntimeError(f"immutable input changed during M3 run: {path_text}")
        output_hashes = {
            path.name: _sha256(path) for path in sorted(partial.iterdir()) if path.is_file()
        }
        _write_json(partial / "run-manifest.json", {
            **binding, "run_id": run_id, "input_sha256": input_hashes,
            "outputs": output_hashes, "raw_inputs_mutated": False,
        })
        os.rename(partial, final_dir)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return final_dir
