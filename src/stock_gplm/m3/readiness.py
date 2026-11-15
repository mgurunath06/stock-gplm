"""Build deterministic local research datasets without mutating raw publications."""
from __future__ import annotations

import csv
from stock_gplm import locking as fcntl
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, Iterable

from stock_gplm.ingestion import validate_daily
from stock_gplm.m2.master_validation import read_shoonya_nse_eq_inventory_v1

PIPELINE_VERSION = 3
MAX_INPUT_BYTES = 16 * 1024 * 1024
EMA_PERIOD = 20
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
LARGE_CLOSE_MOVE = Decimal("0.20")
COVERAGE_FIELDS = (
    "symbol", "observed_candles", "first_observed_date", "last_observed_date",
    "published_windows", "collector_windows_absent", "non_accepted_segments",
    "observed_sessions_before_first", "observed_sessions_after_last",
    "unobserved_sessions_within_span", "longest_unobserved_run",
    "zero_volume_candles", "flat_ohlc_candles", "large_close_moves",
    "ema20_available", "rsi14_available", "macd_available", "macd_signal_available",
)


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
    return _stream_sha256(path)


def _stream_sha256(path: Path) -> str:
    """Hash a derived output; the input size bound does not apply to these."""
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
    return [
        replace(
            item,
            ema20=_decimal(ema[index]) if ema[index] is not None else None,
            rsi14=_decimal(rsi[index]) if rsi[index] is not None else None,
            macd_12_26=_decimal(macd[index]) if macd[index] is not None else None,
            macd_signal_9=_decimal(signal[index]) if signal[index] is not None else None,
            macd_histogram=_decimal(histogram[index]) if histogram[index] is not None else None,
        )
        for index, item in enumerate(rows)
    ]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


_SVG_STYLE = """
.surface { fill: #fcfcfb; } .frame { fill: none; stroke: #e4e3df; }
.reference { stroke: #c3c2b7; stroke-width: 1; stroke-dasharray: 4 4; }
.ink { fill: #0b0b0b; } .muted { fill: #52514e; }
.series-1 { stroke: #2a78d6; fill: none; stroke-width: 2; }
.series-2 { stroke: #eb6834; fill: none; stroke-width: 2; }
.swatch-1 { fill: #2a78d6; } .swatch-2 { fill: #eb6834; }
.bar { fill: #2a78d6; } .hit { fill: transparent; }
.hit:hover { fill: #52514e; fill-opacity: 0.12; }
text { font-family: sans-serif; }
@media (prefers-color-scheme: dark) {
  .surface { fill: #1a1a19; } .frame { stroke: #3a3a37; } .reference { stroke: #6f6e69; }
  .ink { fill: #f0efec; } .muted { fill: #c3c2b7; }
  .series-1 { stroke: #3987e5; } .series-2 { stroke: #d95926; }
  .swatch-1, .bar { fill: #3987e5; } .swatch-2 { fill: #d95926; }
}
"""
_SVG_LEFT, _SVG_WIDTH, _SVG_PANEL_WIDTH = 80, 1000, 860
_MISSING_LINE_BREAK_DAYS = 10


def _polyline(
    days: list[int], values: list[Decimal | None], x_of: Any, y_of: Any, css: str,
) -> str:
    """Draw connected runs; a long unobserved stretch breaks the line instead of bridging it."""
    runs: list[list[str]] = [[]]
    previous: int | None = None
    for day, value in zip(days, values):
        if value is None:
            continue
        if previous is not None and day - previous > _MISSING_LINE_BREAK_DAYS:
            runs.append([])
        runs[-1].append(f"{x_of(day):.1f},{y_of(value):.1f}")
        previous = day
    return "".join(
        f'<polyline class="{css}" points="{" ".join(run)}"/>' for run in runs if len(run) > 1
    )


def _svg(symbol: str, rows: list[DatasetRow], status: str) -> str:
    panel_height, gap = 130, 42
    tops = [90 + index * (panel_height + gap) for index in range(4)]
    height = tops[-1] + panel_height + 70
    ordinals = [date.fromisoformat(item.trading_date).toordinal() for item in rows]
    first, last = ordinals[0], ordinals[-1]
    span = max(1, last - first)

    def x_of(day: int) -> float:
        return _SVG_LEFT + (day - first) * _SVG_PANEL_WIDTH / span

    def scale(values: list[Decimal], top: int) -> Any:
        low, high = min(values), max(values)
        extent = high - low or Decimal(1)
        return lambda value: top + panel_height - float((value - low) / extent) * panel_height

    def number(value: str | None) -> Decimal | None:
        return Decimal(value) if value is not None else None

    def bounds(values: list[Decimal], top: int) -> str:
        return (
            f'<text class="muted" x="{_SVG_LEFT - 6}" y="{top + 10}" font-size="11" text-anchor="end">{max(values):.2f}</text>'
            f'<text class="muted" x="{_SVG_LEFT - 6}" y="{top + panel_height}" font-size="11" text-anchor="end">{min(values):.2f}</text>'
        )

    def legend(top: int, names: tuple[str, ...]) -> str:
        if len(names) < 2:
            return ""
        return "".join(
            f'<rect class="swatch-{index}" x="{720 + (index - 1) * 110}" y="{top - 15}" width="10" height="10" rx="2"/>'
            f'<text class="muted" x="{735 + (index - 1) * 110}" y="{top - 6}" font-size="12">{escape(name)}</text>'
            for index, name in enumerate(names, 1)
        )

    closes = [Decimal(item.close) for item in rows]
    ema = [number(item.ema20) for item in rows]
    rsi = [number(item.rsi14) for item in rows]
    macd = [number(item.macd_12_26) for item in rows]
    signal = [number(item.macd_signal_9) for item in rows]
    insufficient = {
        "ema": f"EMA20 unavailable: {len(rows)}/20 observations",
        "rsi": f"RSI14 unavailable: {len(rows)}/15 observations",
        "macd": f"MACD signal unavailable: {len(rows)}/34 observations",
    }
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_SVG_WIDTH}" height="{height}" viewBox="0 0 {_SVG_WIDTH} {height}">',
        f"<style>{_SVG_STYLE}</style>",
        f'<rect class="surface" width="{_SVG_WIDTH}" height="{height}"/>',
        f'<text class="ink" x="{_SVG_LEFT}" y="34" font-size="22">{escape(symbol)} local exploratory view</text>',
        f'<text class="muted" x="{_SVG_LEFT}" y="56" font-size="13">{escape(status)}; {len(rows)} validated observations, {escape(rows[0].trading_date)} to {escape(rows[-1].trading_date)}. Unadjusted prices.</text>',
    ]
    titles = ("Close and EMA20", "Volume", "RSI14", "MACD (12,26) and signal (9)")
    series_names: tuple[tuple[str, ...], ...] = (("Close", "EMA20"), ("Volume",), ("RSI14",), ("MACD", "Signal"))
    for top, title, names in zip(tops, titles, series_names):
        parts.append(f'<text class="ink" x="{_SVG_LEFT}" y="{top - 6}" font-size="13">{escape(title)}</text>')
        parts.append(legend(top, names))
        parts.append(f'<rect class="frame" x="{_SVG_LEFT}" y="{top}" width="{_SVG_PANEL_WIDTH}" height="{panel_height}"/>')

    price_values = closes + [item for item in ema if item is not None]
    price_y = scale(price_values, tops[0])
    parts.append(bounds(price_values, tops[0]))
    parts.append(_polyline(ordinals, list(closes), x_of, price_y, "series-1"))
    if any(item is not None for item in ema):
        parts.append(_polyline(ordinals, ema, x_of, price_y, "series-2"))
    else:
        parts.append(f'<text class="muted" x="{_SVG_LEFT + 12}" y="{tops[0] + 20}" font-size="13">{escape(insufficient["ema"])}</text>')

    max_volume = max(max(item.volume for item in rows), 1)
    bar_width = max(1.0, min(6.0, _SVG_PANEL_WIDTH / max(1, span) * 0.6))
    for day, item in zip(ordinals, rows):
        bar = panel_height * item.volume / max_volume
        parts.append(f'<rect class="bar" x="{x_of(day) - bar_width / 2:.1f}" y="{tops[1] + panel_height - bar:.1f}" width="{bar_width:.1f}" height="{bar:.1f}"/>')
    parts.append(f'<text class="muted" x="{_SVG_LEFT - 6}" y="{tops[1] + 10}" font-size="11" text-anchor="end">{max_volume:,}</text>')

    rsi_available = [item for item in rsi if item is not None]
    if rsi_available:
        limits = [Decimal(0), Decimal(100)]
        rsi_y = scale(limits, tops[2])
        parts.append(bounds(limits, tops[2]))
        for level in (30, 70):
            y = rsi_y(Decimal(level))
            parts.append(f'<line class="reference" x1="{_SVG_LEFT}" x2="{_SVG_LEFT + _SVG_PANEL_WIDTH}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(_polyline(ordinals, rsi, x_of, rsi_y, "series-1"))
    else:
        parts.append(f'<text class="muted" x="{_SVG_LEFT + 12}" y="{tops[2] + 20}" font-size="13">{escape(insufficient["rsi"])}</text>')

    macd_available = [item for item in macd + signal if item is not None]
    if macd_available:
        macd_y = scale(macd_available + [Decimal(0)], tops[3])
        parts.append(bounds(macd_available + [Decimal(0)], tops[3]))
        zero = macd_y(Decimal(0))
        parts.append(f'<line class="reference" x1="{_SVG_LEFT}" x2="{_SVG_LEFT + _SVG_PANEL_WIDTH}" y1="{zero:.1f}" y2="{zero:.1f}"/>')
        parts.append(_polyline(ordinals, macd, x_of, macd_y, "series-1"))
        parts.append(_polyline(ordinals, signal, x_of, macd_y, "series-2"))
    if not any(item is not None for item in signal):
        parts.append(f'<text class="muted" x="{_SVG_LEFT + 12}" y="{tops[3] + 20}" font-size="13">{escape(insufficient["macd"])}</text>')

    axis_y = tops[-1] + panel_height + 16
    first_date, last_date = date.fromordinal(first), date.fromordinal(last)
    for year in range(first_date.year + 1, last_date.year + 1):
        x = x_of(date(year, 1, 1).toordinal())
        parts.append(f'<text class="muted" x="{x:.1f}" y="{axis_y}" font-size="11" text-anchor="middle">{year}</text>')
    if first_date.year == last_date.year:
        parts.append(f'<text class="muted" x="{_SVG_LEFT}" y="{axis_y}" font-size="11">{first_date.isoformat()}</text>')
        parts.append(f'<text class="muted" x="{_SVG_LEFT + _SVG_PANEL_WIDTH}" y="{axis_y}" font-size="11" text-anchor="end">{last_date.isoformat()}</text>')
    parts.append(f'<text class="muted" x="{_SVG_LEFT}" y="{axis_y + 26}" font-size="12">No missing-date explanation, adjustment, prediction, or trading signal is inferred.</text>')

    column = max(2.0, _SVG_PANEL_WIDTH / max(1, span))
    for day, item in zip(ordinals, rows):
        detail = (
            f"{item.trading_date} close {item.close} volume {item.volume:,}"
            f" EMA20 {item.ema20 or 'n/a'} RSI14 {item.rsi14 or 'n/a'}"
            f" MACD {item.macd_12_26 or 'n/a'} signal {item.macd_signal_9 or 'n/a'}"
        )
        parts.append(
            f'<rect class="hit" x="{x_of(day) - column / 2:.1f}" y="{tops[0]}" width="{column:.1f}" '
            f'height="{tops[-1] + panel_height - tops[0]}"><title>{escape(detail)}</title></rect>'
        )
    parts.append("</svg>\n")
    return "".join(parts)


def _symbol_quality(
    symbol: str, rows: list[DatasetRow], sessions: list[str],
    published_windows: int, expected_windows: int | None, non_accepted: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Describe one symbol against observed sessions; causes stay UNKNOWN by contract."""
    flags: list[dict[str, str]] = []
    observed = {item.trading_date for item in rows}
    first = rows[0].trading_date if rows else None
    last = rows[-1].trading_date if rows else None
    before = after = within = longest = run = 0
    for session in sessions:
        if first is None or session < first:
            before += first is not None
        elif session > last:
            after += 1
        elif session in observed:
            run = 0
        else:
            within += 1
            run += 1
            longest = max(longest, run)
    zero_volume = flat = moves = 0
    previous: Decimal | None = None
    for item in rows:
        close = Decimal(item.close)
        if item.volume == 0:
            zero_volume += 1
            flags.append({"symbol": symbol, "trading_date": item.trading_date, "code": "ZERO_VOLUME", "value": "0"})
        if item.open == item.high == item.low == item.close:
            flat += 1
            flags.append({"symbol": symbol, "trading_date": item.trading_date, "code": "FLAT_OHLC", "value": item.close})
        if previous is not None and abs(close / previous - 1) >= LARGE_CLOSE_MOVE:
            moves += 1
            flags.append({
                "symbol": symbol, "trading_date": item.trading_date,
                "code": "LARGE_CLOSE_MOVE", "value": _decimal(close / previous - 1),
            })
        previous = close
    count = len(rows)
    return {
        "symbol": symbol, "observed_candles": count,
        "first_observed_date": first, "last_observed_date": last,
        "published_windows": published_windows,
        "collector_windows_absent": (
            expected_windows - published_windows if expected_windows is not None else None
        ),
        "non_accepted_segments": non_accepted,
        "observed_sessions_before_first": before, "observed_sessions_after_last": after,
        "unobserved_sessions_within_span": within, "longest_unobserved_run": longest,
        "zero_volume_candles": zero_volume, "flat_ohlc_candles": flat, "large_close_moves": moves,
        "ema20_available": count >= EMA_PERIOD, "rsi14_available": count > RSI_PERIOD,
        "macd_available": count >= MACD_SLOW,
        "macd_signal_available": count >= MACD_SLOW + MACD_SIGNAL - 1,
    }, flags


def _write_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


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
    collector_windows: tuple[tuple[str, str], ...] | None = None,
) -> Path:
    """Validate local publications and publish a checksum-bound derived run.

    With ``collector_windows``, other segments stay inventoried and hashed but
    contribute no candles, so an overlapping sample cannot contaminate the dataset.
    """
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
    published_windows: dict[str, int] = {}
    non_accepted: dict[str, int] = {}
    allowed_windows = set(collector_windows) if collector_windows is not None else None
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
            if allowed_windows is not None and (
                (report.segment_start, report.segment_end) not in allowed_windows
            ):
                segment["excluded"] = "OUTSIDE_COLLECTOR_WINDOWS"
                continue
            published_windows[symbol] = published_windows.get(symbol, 0) + 1
            if report.segment_start and report.segment_end:
                symbol_ranges.setdefault(symbol, []).append(
                    (report.segment_start, report.segment_end)
                )
            if report.disposition != "ACCEPT":
                non_accepted[symbol] = non_accepted.get(symbol, 0) + 1
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

    # The union of observed dates is a comparison set, never a proof of market sessions.
    symbols_by_date: dict[str, int] = {}
    for row in all_rows:
        symbols_by_date[row.trading_date] = symbols_by_date.get(row.trading_date, 0) + 1
    sessions = sorted(symbols_by_date)
    expected_windows = len(allowed_windows) if allowed_windows is not None else None
    symbol_quality: dict[str, dict[str, Any]] = {}
    ohlcv_flags: list[dict[str, str]] = []
    for symbol in sorted(current_symbols):
        quality, flags = _symbol_quality(
            symbol, grouped.get(symbol, []), sessions, published_windows.get(symbol, 0),
            expected_windows, non_accepted.get(symbol, 0),
        )
        symbol_quality[symbol] = quality
        ohlcv_flags.extend(flags)
    window_coverage: list[dict[str, Any]] = []
    for start, end in sorted(allowed_windows or ()):
        in_window = [
            item for item in segments
            if (item["requested_start"], item["requested_end"]) == (start, end)
        ]
        window_coverage.append({
            "start_date": start, "end_date": end, "segments": len(in_window),
            "accepted_segments": sum(item["disposition"] == "ACCEPT" for item in in_window),
            "records": sum(item["records"] or 0 for item in in_window),
        })
    with_candles = [item for item in symbol_quality.values() if item["observed_candles"]]
    universe = {
        "current_eq_symbols": len(current_symbols),
        "symbols_with_validated_candles": len(with_candles),
        "symbols_without_validated_candles": len(current_symbols) - len(with_candles),
        "symbols_with_all_collector_windows": (
            sum(item["collector_windows_absent"] == 0 for item in symbol_quality.values())
            if expected_windows is not None else None
        ),
        "symbols_with_rsi14": sum(item["rsi14_available"] for item in with_candles),
        "symbols_with_ema20": sum(item["ema20_available"] for item in with_candles),
        "symbols_with_macd_signal": sum(item["macd_signal_available"] for item in with_candles),
        "symbols_without_unobserved_sessions_in_span": sum(
            item["unobserved_sessions_within_span"] == 0 for item in with_candles
        ),
        "observed_session_dates": len(sessions),
        "first_observed_session": sessions[0] if sessions else None,
        "last_observed_session": sessions[-1] if sessions else None,
        "observed_session_classification": "EMPIRICAL_UNION_NOT_AUTHORITATIVE",
        "unobserved_session_classification": "UNKNOWN",
        "ohlcv_flag_counts": {
            code: sum(item["code"] == code for item in ohlcv_flags)
            for code in ("ZERO_VOLUME", "FLAT_OHLC", "LARGE_CLOSE_MOVE")
        },
        "ohlcv_flag_classification": "UNREPAIRED_CAUSE_UNKNOWN",
    }

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
            "observed_session_comparison": symbol_quality[symbol],
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
        "collector_windows": sorted(allowed_windows) if allowed_windows is not None else None,
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
            "accepted_segments": sum(
                item["disposition"] == "ACCEPT" and "excluded" not in item for item in segments
            ),
            "excluded_segments": [item["source"] for item in segments if "excluded" in item],
            "raw_files": len(input_hashes) - 1, "segments": segments, "issues": issues,
        })
        _write_json(partial / "coverage-quality.json", {
            "schema_version": 1, "decision": "INCOMPLETE",
            "raw_candle_acceptance": "ACCEPT" if segments and not issues else "REVIEW",
            "historical_completeness": "INCOMPLETE",
            "training_readiness": "NOT_READY", "universe": universe,
            "window_coverage": window_coverage, "coverage": coverage,
        })
        _write_csv(partial / "symbol-coverage.csv", COVERAGE_FIELDS, (
            symbol_quality[symbol] for symbol in sorted(symbol_quality)
        ))
        _write_csv(partial / "ohlcv-flags.csv", ("symbol", "trading_date", "code", "value"), ohlcv_flags)
        _write_csv(partial / "observed-sessions.csv", ("trading_date", "weekday", "symbols_observed"), (
            {
                "trading_date": session,
                "weekday": date.fromisoformat(session).strftime("%a"),
                "symbols_observed": symbols_by_date[session],
            }
            for session in sessions
        ))
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
            path.name: _stream_sha256(path) for path in sorted(partial.iterdir()) if path.is_file()
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
