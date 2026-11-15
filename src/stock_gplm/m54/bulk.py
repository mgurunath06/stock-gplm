"""Bounded, restartable multi-stock execution over the single-stock engine.

Each symbol runs in its own worker process (``ProcessPoolExecutor``) with an address
space limit and one BLAS thread, so CPU-bound Python work actually runs in parallel.
A symbol's failure or quarantine is recorded and never blocks the others. Scheduling
order is the sorted symbol list. Completed runs are reused by the engine itself, so
re-running the same batch resumes it. A ``STOP`` file in the batch directory stops
dispatching and makes running symbols halt at their next checkpoint.
"""
from __future__ import annotations

import csv
import io
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .device import limit_process
from .engine import Run, Settings, normalize_symbol
from .store import canonical_hash, read_json, utc_now, write_json_atomic, write_text_atomic


def load_universe(path: Path) -> dict[str, Any]:
    doc = read_json(path)
    if doc.get("contract") != "stock-gplm.m54-universe.v1":
        raise ValueError(f"{path} is not a stock-gplm.m54-universe.v1 document")
    if doc.get("membership_basis") not in {"EXPLICIT_LIST", "CURRENT_CONSTITUENT_DIAGNOSTIC", "POINT_IN_TIME_VERIFIED"}:
        raise ValueError("universe must declare membership_basis")
    return doc


def _worker_init(max_memory_gb: float | None) -> None:
    limit_process(max_memory_gb, threads=1)


def _run_one(config: str, budget: str, symbol: str, output_root: str, overrides: Mapping[str, Any],
             hypotheses: str | None, stop_file: str, device: str) -> dict[str, Any]:
    try:
        settings = Settings.load(Path(config), budget, overrides)
        run = Run(settings, symbol, Path(output_root), hypotheses_file=Path(hypotheses) if hypotheses else None,
                  log=lambda _m: None, stop_files=[Path(stop_file)], device=device)
        return run.execute()
    except Exception as exc:  # isolated per symbol
        return {"symbol": symbol, "state": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=5)}


def _summary_row(result: Mapping[str, Any]) -> dict[str, Any]:
    row = {"symbol": result.get("symbol"), "state": result.get("state"), "run_id": result.get("run_id", ""),
           "finalists": "", "validation_pass": "", "test_consistent": "", "error": result.get("error", "")}
    if result.get("state") == "COMPLETED":
        f = read_json(Path(result["dir"]) / "findings.json")
        counts = f["status_counts"]
        row.update({"finalists": len(f["findings"]),
                    "validation_pass": sum(v for k, v in counts.items() if k.startswith(("VALIDATION_PASS", "TEST_"))),
                    "test_consistent": counts.get("TEST_CONSISTENT", 0)})
    return row


def run_batch(config: Path, budget: str, symbols: Sequence[str], output_root: Path, *, workers: int,
              max_memory_gb: float | None, overrides: Mapping[str, Any] | None = None, hypotheses: Path | None = None,
              universe: Mapping[str, Any] | None = None, device: str = "cpu",
              log: Callable[[str], None] = print) -> dict[str, Any]:
    symbols = sorted({normalize_symbol(s) for s in symbols})
    if workers < 1:
        raise ValueError("workers must be >= 1")
    settings = Settings.load(config, budget, overrides)
    batch_id = canonical_hash({"symbols": symbols, "config": settings.config,
                               "hypotheses": read_json(hypotheses) if hypotheses else None})
    bdir = Path(output_root).resolve() / "batches" / batch_id
    bdir.mkdir(parents=True, exist_ok=True)
    stop_file = bdir / "STOP"
    manifest_path = bdir / "batch-manifest.json"
    state: dict[str, Any] = read_json(manifest_path) if manifest_path.exists() else {
        "contract": "stock-gplm.m54-batch.v1", "batch_id": batch_id, "budget": budget, "symbols": symbols,
        "created_utc": utc_now(), "results": {},
        "membership_basis": (universe or {}).get("membership_basis", "EXPLICIT_LIST"),
        "survivorship_note": "Current-membership universes are survivorship-biased; point-in-time membership is unknown."
        if (universe or {}).get("membership_basis") != "POINT_IN_TIME_VERIFIED" else None}
    state["workers"], state["max_memory_gb"], state["device"] = workers, max_memory_gb, device

    def persist() -> None:
        state["updated_utc"] = utc_now()
        done = state["results"]
        state["progress"] = {"total": len(symbols), "finished": len(done),
                             "by_state": {s: sum(1 for r in done.values() if r.get("state") == s)
                                          for s in sorted({r.get("state") for r in done.values()})}}
        write_json_atomic(manifest_path, state)
        rows = [_summary_row(r) for _, r in sorted(done.items())]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["symbol", "state", "run_id", "finalists", "validation_pass",
                                            "test_consistent", "error"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        write_text_atomic(bdir / "summary.csv", buf.getvalue())

    # Symbols completed in an earlier invocation are re-submitted: the engine verifies and reuses them cheaply.
    state["results"] = {k: v for k, v in state["results"].items() if v.get("state") == "COMPLETED"}
    persist()
    pending = list(symbols)
    args = (str(Path(config).resolve()), budget)
    ov = dict(overrides or {})
    with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init, initargs=(max_memory_gb,)) as pool:
        running: dict[Future[dict[str, Any]], str] = {}
        while pending or running:
            while pending and len(running) < workers and not stop_file.exists():
                sym = pending.pop(0)
                running[pool.submit(_run_one, *args, sym, str(output_root), ov,
                                    str(hypotheses) if hypotheses else None, str(stop_file), device)] = sym
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                sym = running.pop(fut)
                result = fut.result()
                state["results"][sym] = {k: v for k, v in result.items() if k != "traceback"}
                if result.get("state") == "FAILED":
                    write_text_atomic(bdir / "errors" / f"{sym}.txt", result.get("traceback", result.get("error", "")))
                log(f"[batch {batch_id[:12]}] {sym}: {result.get('state')}")
                persist()
    state["state"] = "STOPPED" if stop_file.exists() and len(state["results"]) < len(symbols) else "FINISHED"
    persist()
    return {"batch_id": batch_id, "dir": str(bdir), **state["progress"], "state": state["state"]}
