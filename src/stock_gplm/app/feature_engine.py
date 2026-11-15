"""M5.8 configurable calculation catalogue, cache and coverage reporting.

The catalogue is external JSON (the 11-module decomposition of the owner's 340-strategy source).
Each unique DSL expression is evaluated once per symbol/timeframe/data fingerprint and persisted for
reuse.  Registration, a partial proxy, and byte-level cache success never turn an unavailable source
calculation into a computed one.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..m54.timeframes import Bars
from . import strategy_dsl as dsl
from .db import sha, utc_now
from .ingest import SymbolData, ingest_many, window_data
from .lanes import all_bars
from .settings import REPO

CONTRACT = "stock-gplm.feature-engine.v1"
CATALOGUE = REPO / "config" / "verify" / "primitives.json"
TIMEFRAMES = ("1m", "5m", "15m", "30m", "60m", "4h", "1d", "1w")
COVERAGE_FIELDS = ("module", "calculation", "symbol", "timeframe", "parameters", "expressions",
                   "implementation_status", "verification_status", "bars", "valid_observations",
                   "missing_observations", "warmup_bars", "first_valid_bar", "last_valid_bar",
                   "incomplete_candles", "cache_status", "missing_data_reason", "catalogue_sha256",
                   "data_fingerprint", "created_utc")


def load_catalogue(path: Path = CATALOGUE) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(doc.get("rows"), list):
        raise ValueError(f"{path} has no calculation rows")
    return doc, hashlib.sha256(raw).hexdigest()


def _parameters(exprs: Sequence[str]) -> str:
    """Compact, exact function-parameter inventory; the expressions remain the authoritative formula."""
    found: list[str] = []
    for expr in exprs:
        for name, args in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()]*)\)", expr):
            if any(ch.isdigit() for ch in args):
                found.append(f"{name}({args.strip()})")
    return "; ".join(dict.fromkeys(found))


def _blocked(row: Mapping[str, Any]) -> tuple[str | None, str]:
    missing = list(row.get("missing") or [])
    kinds = {str(m.get("kind")) for m in missing}
    reason = "; ".join(f"{m.get('what')}: {m.get('reason')}" for m in missing)
    if "NOT_IMPLEMENTED" in kinds:
        return "BLOCKED_IMPLEMENTATION", reason
    if kinds & {"DATA", "DEFERRED"}:
        return "BLOCKED_MISSING_DATA", reason
    return None, ""


def _context(bar: Bars) -> dsl.Context:
    return dsl.Context.from_frame(bar.frame, bar.timeframe, bar.intraday)


def _evaluate(expressions: Sequence[str], bar: Bars) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    ctx = _context(bar)
    arrays: dict[str, np.ndarray] = {}
    errors: dict[str, str] = {}
    for expr in expressions:
        try:
            value = np.asarray(dsl.evaluate(dsl.parse(expr), ctx), dtype=float)
        except dsl.MissingData as exc:
            errors[expr] = f"MISSING_DATA|{exc.requirement}: {exc}"
            continue
        except Exception as exc:
            errors[expr] = f"IMPLEMENTATION|{type(exc).__name__}: {exc}"
            continue
        if value.shape != (len(bar),):
            errors[expr] = f"IMPLEMENTATION|produced shape {value.shape}, expected {(len(bar),)}"
            continue
        arrays[expr] = value
    return arrays, errors


def _cache_paths(cache_root: Path, symbol: str, timeframe: str, key: str) -> tuple[Path, Path]:
    base = cache_root / symbol / timeframe / key
    return base.with_suffix(".npz"), base.with_suffix(".json")


def compute_lane(symbol: str, timeframe: str, bar: Bars, data_fingerprint: str, rows: Sequence[Mapping[str, Any]],
                 catalogue_sha: str, cache_root: Path) -> tuple[dict[str, np.ndarray], dict[str, str], str]:
    expressions = list(dict.fromkeys(expr for row in rows
                                     if (not row.get("intraday") or timeframe not in {"1d", "1w"})
                                     and (not row.get("timeframes") or timeframe in row["timeframes"])
                                     for expr in row.get("exprs", [])))
    key = sha({"contract": CONTRACT, "catalogue": catalogue_sha, "data": data_fingerprint,
               "timeframe": timeframe, "expressions": expressions})
    npz, meta = _cache_paths(cache_root, symbol, timeframe, key)
    if npz.is_file() and meta.is_file():
        metadata = json.loads(meta.read_text(encoding="utf-8"))
        names = metadata["expressions"]
        with np.load(npz, allow_pickle=False) as z:
            return {expr: z[f"v{i}"] for i, expr in enumerate(names)}, metadata.get("errors", {}), "REUSED"
    arrays, errors = _evaluate(expressions, bar)
    npz.parent.mkdir(parents=True, exist_ok=True)
    tmp = npz.with_name(f".{npz.stem}.{os.getpid()}.npz")
    np.savez_compressed(tmp, **{f"v{i}": arrays.get(expr, np.full(len(bar), np.nan))
                               for i, expr in enumerate(expressions)})
    os.replace(tmp, npz)
    mt = meta.with_name(f".{meta.name}.{os.getpid()}.tmp")
    mt.write_text(json.dumps({"contract": CONTRACT, "expressions": expressions, "errors": errors,
                              "catalogue_sha256": catalogue_sha, "data_fingerprint": data_fingerprint},
                             indent=2) + "\n", encoding="utf-8")
    os.replace(mt, meta)
    return {expr: arrays.get(expr, np.full(len(bar), np.nan)) for expr in expressions}, errors, "COMPUTED"


def coverage_rows(symbol: str, timeframe: str, bar: Bars | None, data_fingerprint: str,
                  rows: Sequence[Mapping[str, Any]], catalogue_sha: str, cache_root: Path) -> list[dict[str, Any]]:
    cached: dict[str, np.ndarray] = {}
    errors: dict[str, str] = {}
    cache_status = "NOT_WRITTEN"
    if bar is not None and len(bar):
        cached, errors, cache_status = compute_lane(symbol, timeframe, bar, data_fingerprint, rows, catalogue_sha,
                                                    cache_root)
    out = []
    for row in rows:
        exprs = list(row.get("exprs") or [])
        blocked, reason = _blocked(row)
        applicable = ((not row.get("intraday") or timeframe not in {"1d", "1w"}) and
                      (not row.get("timeframes") or timeframe in row["timeframes"]))
        total = len(bar) if bar is not None else 0
        status = blocked or "COMPUTED"
        verify = "NOT_VERIFIED_BLOCKED" if blocked else (
            "INDEPENDENT_REFERENCE_TEST_CONFIGURED" if row.get("verified_by") else "CAUSAL_TEST_CONFIGURED")
        valid = 0
        first = last = None
        warmup = total
        calc_reason = reason
        if not applicable:
            status, verify = "NOT_APPLICABLE", "NOT_APPLICABLE"
            calc_reason = row.get("timeframe_reason") or "intraday-only calculation"
        elif not total:
            status, verify = "BLOCKED_MISSING_DATA", "NOT_VERIFIED_BLOCKED"
            calc_reason = f"no accepted {timeframe} candles"
        elif not exprs and not blocked:
            status, verify, calc_reason = "BLOCKED_IMPLEMENTATION", "NOT_VERIFIED_BLOCKED", "no expression registered"
        elif exprs:
            vals = [cached.get(expr) for expr in exprs]
            failed = next((errors[expr] for expr in exprs if expr in errors), None)
            missing_expr = next((expr for expr, val in zip(exprs, vals) if val is None), None)
            if failed is not None and not blocked:
                kind, _, detail = failed.partition("|")
                status = "BLOCKED_MISSING_DATA" if kind == "MISSING_DATA" else "BLOCKED_IMPLEMENTATION"
                verify, calc_reason = "NOT_VERIFIED_BLOCKED", detail
            elif missing_expr is not None and not blocked:
                status, verify = "BLOCKED_IMPLEMENTATION", "NOT_VERIFIED_BLOCKED"
                calc_reason = f"expression did not compute: {missing_expr}"
            elif all(v is not None for v in vals):
                finite = np.logical_and.reduce([np.isfinite(v) for v in vals])
                idx = np.flatnonzero(finite)
                valid = int(finite.sum())
                if len(idx):
                    first, last, warmup = int(idx[0]), int(idx[-1]), int(idx[0])
                elif not blocked:
                    status, verify, calc_reason = "BLOCKED_MISSING_DATA", "NOT_VERIFIED_BLOCKED", \
                        "expressions produced no finite observations"
        f = bar.frame if bar is not None and len(bar) else None
        out.append({"module": row["module"], "calculation": row["name"], "symbol": symbol,
                    "timeframe": timeframe, "parameters": _parameters(exprs),
                    "expressions": json.dumps(exprs, separators=(",", ":")), "implementation_status": status,
                    "verification_status": verify, "bars": total, "valid_observations": valid,
                    "missing_observations": total - valid, "warmup_bars": warmup, "first_valid_bar": first,
                    "last_valid_bar": last, "incomplete_candles": int(f["incomplete"].sum()) if f is not None else 0,
                    "cache_status": cache_status, "missing_data_reason": calc_reason,
                    "catalogue_sha256": catalogue_sha, "data_fingerprint": data_fingerprint,
                    "created_utc": utc_now()})
    return out


def run(app: Any, symbols: Sequence[str], timeframes: Sequence[str], out: Path, catalogue: Path = CATALOGUE,
        start: str | None = None, end: str | None = None) -> dict[str, Any]:
    doc, cat_sha = load_catalogue(catalogue)
    out.mkdir(parents=True, exist_ok=True)
    cache_root = app.s.cache_dir / "feature-catalogue" / CONTRACT
    matrix: list[dict[str, Any]] = []
    for symbol, raw in ingest_many(app.s, app.db, symbols, use_minute=True).items():
        data = window_data(raw, start, end)
        bars = all_bars(data)
        for timeframe in timeframes:
            matrix.extend(coverage_rows(symbol, timeframe, bars.get(timeframe), data.fingerprint, doc["rows"],
                                        cat_sha, cache_root))
    path = out / "feature-coverage.csv"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COVERAGE_FIELDS)
        writer.writeheader()
        writer.writerows(matrix)
    os.replace(tmp, path)
    counts: dict[str, int] = {}
    for row in matrix:
        status = row["implementation_status"]
        counts[status] = counts.get(status, 0) + 1
    summary = {"contract": CONTRACT, "symbols": list(symbols), "timeframes": list(timeframes),
               "catalogue_rows": len(doc["rows"]), "matrix_rows": len(matrix), "status_counts": counts,
               "catalogue_sha256": cat_sha, "coverage_csv": str(path), "cache_root": str(cache_root)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


__all__ = ["CATALOGUE", "CONTRACT", "TIMEFRAMES", "compute_lane", "coverage_rows", "load_catalogue", "run"]
