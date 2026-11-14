"""Server-side historical research workflow behind the loopback Strategy Studio.

Clients send only a symbol list, a date range, a mode and explicit settings. The
eligibility decision always comes from :class:`HistoricalDataAdapter` on the
server; no request field can assert, override or skip it.

The requested symbol list is the universe. It is validated as a whole, and
duplicate or non-canonical entries get a 400 with per-entry reasons. If any member
is missing, blocked, rejected or ambiguous, nothing is run (422), unless a
``DIAGNOSTIC_ONLY`` request explicitly sets ``diagnostic_subset``. Runs are read
only through :mod:`.run_store`.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Mapping

from stock_gplm.v1.registry import StrategyRegistry

from .adapter import HistoricalDataAdapter
from .discovery import discover
from .evidence import ReferenceEvidence
from .models import DataEligibilityRequest, UniverseRequest, universe_problems
from .run_store import compare_runs, load_run, make_replayer, scan_runs
from .runner import BacktestSettings, run_historical_backtest

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
        base = {"strategy_id", "version", "symbols", "start", "end", "mode", "settings"}
        if set(body) not in (base, base | {"diagnostic_subset"}):
            raise ValueError(f"request fields must be exactly {sorted(base)} (plus optional diagnostic_subset)")
        symbols = body["symbols"]
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= MAX_SYMBOLS_PER_RUN:
            raise ValueError(f"symbols must be a list of 1 to {MAX_SYMBOLS_PER_RUN} entries")
        problems = universe_problems(symbols)
        if problems:
            return 400, {"error": "requested universe is invalid; nothing was run", "problems": problems}
        if not isinstance(body["settings"], dict):
            raise ValueError("settings must be an object")
        strategy = self.registry.get(str(body["strategy_id"]), int(body["version"]))
        settings = BacktestSettings.from_dict(body["settings"])
        universe = UniverseRequest(tuple(symbols), str(body["start"]), str(body["end"]), str(body["mode"]),
                                   body.get("diagnostic_subset", False))
        if not self._run_lock.acquire(blocking=False):
            return 429, {"error": "another historical backtest is running"}
        try:
            assessment = self.adapter.assess_universe(universe)
            summary = assessment.to_dict()
            if not assessment.released:
                return 422, {"error": f"requested universe is {assessment.status.value}; no member was run",
                             "universe": summary, "decisions": summary["members"]}
            run = run_historical_backtest(strategy, assessment, settings, self.reports_root,
                                          forbidden_roots=(self.raw_root.resolve().parent.parent,))
        finally:
            self._run_lock.release()
        return 201, {"run_id": run["run_id"], "classification": run["classification"],
                     "investable": run["investable"], "metrics": run["performance"]["metrics"],
                     "undefined": run["performance"]["undefined"], "benchmark": run["benchmark"],
                     "universe_status": summary["status"], "excluded": summary["excluded"],
                     "decisions": summary["members"]}

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise KeyError(f"unknown run {run_id}")
        return self.reports_root / run_id

    def runs(self) -> dict[str, Any]:
        return {"runs": scan_runs(self.reports_root)}

    def report(self, run_id: str) -> dict[str, Any]:
        verified = load_run(self._run_dir(run_id))
        return {"run_id": run_id, "verification": verified.verification, "markdown": verified.markdown,
                "run": verified.run}

    def compare(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact(body, {"run_ids"})
        ids = body["run_ids"]
        if not isinstance(ids, list) or not 2 <= len(ids) <= 10:
            raise ValueError("run_ids must list 2 to 10 runs")
        return compare_runs([self._run_dir(str(run_id)) for run_id in ids],
                            replay=make_replayer(self.adapter, self.registry))
