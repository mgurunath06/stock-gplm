"""Read-only discovery of published Shoonya NSE daily segments.

Discovery inspects names and sidecar completeness only. With ``verify=True`` it
also runs the established :func:`validate_daily` publication/content check on
every payload. It never writes inside the raw root and never infers listing,
calendar or alias facts from what it finds.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from stock_gplm.ingestion.daily import validate_daily

from .adapter import LOCK_NAME, _segments, publication_lock_active
from .evidence import EtfHeuristic

DISCOVERY_CONTRACT = "stock-gplm.m5-discovery.v1"


def discover(raw_root: Path, etf: EtfHeuristic, *, verify: bool = False) -> dict[str, Any]:
    raw_root = Path(raw_root)
    symbols: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    dispositions: Counter[str] = Counter()
    windows: Counter[str] = Counter()
    series: Counter[str] = Counter()
    unexpected_root: list[str] = []
    for entry in sorted(raw_root.iterdir()):
        if entry.name == LOCK_NAME:
            continue
        if entry.is_symlink() or not entry.is_dir():
            unexpected_root.append(entry.name)
            continue
        groups, unexpected = _segments(entry)
        complete = [stem for stem, files in groups.items() if len(files) == 3]
        incomplete = sorted(set(groups) - set(complete))
        series[entry.name.rsplit("-", 1)[-1] if "-" in entry.name else "NONE"] += 1
        for stem in complete:
            windows[stem] += 1
        item: dict[str, Any] = {
            "symbol": entry.name, "class": "ETF_HEURISTIC" if etf.is_etf(entry.name) else "EQUITY_HEURISTIC",
            "complete_segments": len(complete), "incomplete_segments": incomplete, "unexpected_files": unexpected,
            "first_window_start": min(complete).split("_")[0] if complete else None,
            "last_window_end": max(s.split("_")[1] for s in complete) if complete else None,
        }
        if verify:
            verdicts = Counter(validate_daily(groups[stem][".json.gz"]).disposition for stem in complete)
            item["dispositions"] = dict(sorted(verdicts.items()))
            dispositions.update(verdicts)
        symbols.append(item)
        totals["complete_segments"] += len(complete)
        totals["incomplete_segments"] += len(incomplete)
        totals["unexpected_files"] += len(unexpected)
    classes = Counter(item["class"] for item in symbols)
    return {
        "contract": DISCOVERY_CONTRACT, "raw_root": str(raw_root), "publication_lock_active": publication_lock_active(raw_root),
        "symbols": len(symbols), "series": dict(series), "classification_heuristic": dict(classes),
        "complete_segments": totals["complete_segments"], "incomplete_segments": totals["incomplete_segments"],
        "unexpected_files": totals["unexpected_files"], "unexpected_root_entries": unexpected_root,
        "segment_windows": dict(sorted(windows.items())),
        "verified": verify, "dispositions": dict(sorted(dispositions.items())) if verify else None,
        "per_symbol": symbols,
        "not_inferred": ["listing dates", "trading holidays", "aliases", "corporate actions",
                         "historical membership"],
    }
