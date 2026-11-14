"""Server-side historical research workflow behind the loopback Strategy Studio.

Clients send only a symbol list, a date range, a mode and explicit settings. The
eligibility decision always comes from :class:`HistoricalDataAdapter` on the
server; no request field can assert, override or skip it.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Mapping

from stock_gplm.v1.registry import StrategyRegistry

from .adapter import HistoricalDataAdapter
from .discovery import discover
from .evidence import ReferenceEvidence
from .models import DataEligibilityRequest
from .runner import BacktestSettings, compare_runs, run_historical_backtest

MAX_SYMBOLS_PER_RUN = 25
_RUN_ID = re.compile(r"^[0-9a-f]{64}$")


def _exact(body: Mapping[str, Any], fields: set[str]) -> None:
    if set(body) != fields:
        raise ValueError(f"request fields must be exactly {sorted(fields)}")


class HistoricalResearchService:
    def __init__(self, raw_root: Path, evidence: ReferenceEvidence, reports_root: Path, registry: StrategyRegistry):
        self.raw_root = Path(raw_root)
        self.adapter = HistoricalDataAdapter(self.raw_root, evidence)
        self.etf = evidence.etf_heuristic
        self.reports_root = Path(reports_root)
        self.registry = registry
        self._run_lock = threading.Lock()  # one backtest at a time bounds CPU and memory
        self._discovery: dict[str, Any] | None = None

    def datasets(self) -> dict[str, Any]:
        if self._discovery is None:
            self._discovery = discover(self.raw_root, self.etf)
        summary = {k: v for k, v in self._discovery.items() if k not in {"per_symbol", "segment_windows"}}
        summary["available_symbols"] = [s["symbol"] for s in self._discovery["per_symbol"]
                                        if s["class"] == "EQUITY_HEURISTIC" and s["complete_segments"]]
        return summary

    def eligibility(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact(body, {"symbol", "start", "end", "mode"})
        decision, _ = self.adapter.assess(DataEligibilityRequest(
            str(body["symbol"]), str(body["start"]), str(body["end"]), str(body["mode"])))
        return decision.to_dict()

    def backtest(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        _exact(body, {"strategy_id", "version", "symbols", "start", "end", "mode", "settings"})
        symbols = body["symbols"]
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= MAX_SYMBOLS_PER_RUN:
            raise ValueError(f"symbols must be a list of 1 to {MAX_SYMBOLS_PER_RUN} entries")
        if not isinstance(body["settings"], dict):
            raise ValueError("settings must be an object")
        strategy = self.registry.get(str(body["strategy_id"]), int(body["version"]))
        settings = BacktestSettings.from_dict(body["settings"])
        if not self._run_lock.acquire(blocking=False):
            return 429, {"error": "another historical backtest is running"}
        try:
            decisions, datasets = [], []
            for symbol in sorted({str(s) for s in symbols}):
                decision, dataset = self.adapter.assess(DataEligibilityRequest(
                    symbol, str(body["start"]), str(body["end"]), str(body["mode"])))
                decisions.append(decision.to_dict())
                if dataset is not None:
                    datasets.append(dataset)
            if not datasets:
                return 422, {"error": "no dataset passed eligibility", "decisions": decisions}
            run = run_historical_backtest(strategy, datasets, settings, self.reports_root,
                                          forbidden_roots=(self.raw_root.resolve().parent.parent,))
        finally:
            self._run_lock.release()
        return 201, {"run_id": run["run_id"], "classification": run["classification"],
                     "investable": run["investable"], "metrics": run["performance"]["metrics"],
                     "undefined": run["performance"]["undefined"], "benchmark": run["benchmark"],
                     "decisions": [{"symbol": d["request"]["symbol"], "status": d["status"],
                                    "missing_evidence": d["missing_evidence"],
                                    "failures": [f["code"] for f in d["failures"]]} for d in decisions]}

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id) or not (self.reports_root / run_id / "run.json").is_file():
            raise KeyError(f"unknown run {run_id}")
        return self.reports_root / run_id

    def runs(self) -> dict[str, Any]:
        rows = []
        if self.reports_root.is_dir():
            for path in sorted(self.reports_root.iterdir()):
                if _RUN_ID.fullmatch(path.name) and (path / "run.json").is_file():
                    run = json.loads((path / "run.json").read_text(encoding="utf-8"))
                    rows.append({"run_id": run["run_id"], "strategy_id": run["strategy_id"],
                                 "strategy_version": run["strategy_version"], "classification": run["classification"]})
        return {"runs": rows}

    def report(self, run_id: str) -> dict[str, Any]:
        directory = self._run_dir(run_id)
        return {"run_id": run_id, "markdown": (directory / "report.md").read_text(encoding="utf-8"),
                "run": json.loads((directory / "run.json").read_text(encoding="utf-8"))}

    def compare(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact(body, {"run_ids"})
        ids = body["run_ids"]
        if not isinstance(ids, list) or not 2 <= len(ids) <= 10:
            raise ValueError("run_ids must list 2 to 10 runs")
        return compare_runs([self._run_dir(str(run_id)) for run_id in ids])
