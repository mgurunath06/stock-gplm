"""Multi-stock execution of the unified engine, the eligible-universe builder and the strategy library.

Each symbol runs in its own worker process with an address-space cap and one BLAS thread;
a symbol's failure is recorded and never blocks the others. Completed runs are verified and
reused by the engine, so re-running the same batch resumes it. A ``STOP`` file in the batch
directory stops dispatching and halts running symbols at their next checkpoint.
"""
from __future__ import annotations

import csv
import io
import re
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .device import limit_process
from .store import canonical_hash, read_json, utc_now, write_json_atomic, write_text_atomic
from .unified import UnifiedRun, normalize_symbol

LIBRARY_USER_CONTRACT = "stock-gplm.m54-strategy.v2"


def eligible_symbols(daily_root: Path, etf_heuristic: Path) -> dict[str, Any]:
    """All ``-EQ`` symbol directories under the daily root, minus ETF-like names (M3.7 heuristic).

    Membership is the *current* set of collected directories: survivorship-biased and not point-in-time.
    """
    doc = read_json(etf_heuristic)
    pats = [re.compile(p) for p in doc["etf_patterns"]]
    keep = set(doc.get("company_exclusions", []))
    syms, etfs = [], []
    for d in sorted(Path(daily_root).iterdir()):
        if not d.is_dir() or not d.name.endswith("-EQ"):
            continue
        base = d.name.removesuffix("-EQ")
        (etfs if base not in keep and any(p.search(base) for p in pats) else syms).append(d.name)
    return {"contract": "stock-gplm.m54-universe.v1", "membership_basis": "CURRENT_CONSTITUENT_DIAGNOSTIC",
            "symbols": syms, "excluded_etf_like": etfs,
            "note": "every collected -EQ directory minus ETF-like names; survivorship-biased, not point-in-time"}


def _init(max_memory_gb: float | None) -> None:
    limit_process(max_memory_gb, threads=1)


def _one(settings: Mapping[str, Any], symbol: str, output_root: str, kw: Mapping[str, Any], stop_file: str) -> dict[str, Any]:
    try:
        run = UnifiedRun(settings, symbol, Path(output_root), workers=1, log=lambda _m: None,
                         stop_files=[Path(stop_file)], **kw)
        return run.execute()
    except Exception as exc:  # isolated per symbol
        return {"symbol": symbol, "state": "FAILED", "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=6)}


def _row(r: Mapping[str, Any]) -> dict[str, Any]:
    row = {"symbol": r.get("symbol"), "state": r.get("state"), "run_id": r.get("run_id", ""), "strategies": "",
           "robust": "", "promising": "", "error": r.get("error", "")}
    if r.get("state") == "COMPLETED":
        ranked = read_json(Path(r["dir"]) / "stages" / "rank.json")["payload"]
        t = ranked["tier_counts"]
        row.update({"strategies": len(ranked["strategies"]), "robust": t.get("ROBUST_CANDIDATE", 0),
                    "promising": t.get("PROMISING_NOT_SIGNIFICANT", 0)})
    return row


def run_unified_batch(settings: Mapping[str, Any], symbols: Sequence[str], output_root: Path, *, workers: int,
                      max_memory_gb: float | None, universe: Mapping[str, Any] | None = None,
                      log: Callable[[str], None] = print, **kw: Any) -> dict[str, Any]:
    symbols = sorted({normalize_symbol(s) for s in symbols})
    batch_id = canonical_hash({"kind": "unified", "symbols": symbols, "settings": settings,
                               **{k: str(v) for k, v in kw.items()}})
    bdir = Path(output_root).resolve() / "batches" / batch_id
    bdir.mkdir(parents=True, exist_ok=True)
    stop = bdir / "STOP"
    mpath = bdir / "batch-manifest.json"
    state: dict[str, Any] = read_json(mpath) if mpath.exists() else {
        "contract": "stock-gplm.m54-unified-batch.v1", "batch_id": batch_id, "symbols": symbols, "created_utc": utc_now(),
        "results": {}, "membership_basis": (universe or {}).get("membership_basis", "EXPLICIT_LIST")}
    state.update({"workers": workers, "max_memory_gb": max_memory_gb})
    state["results"] = {k: v for k, v in state["results"].items() if v.get("state") == "COMPLETED"}

    def persist() -> None:
        done = state["results"]
        state["updated_utc"] = utc_now()
        state["progress"] = {"total": len(symbols), "finished": len(done)}
        write_json_atomic(mpath, state)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["symbol", "state", "run_id", "strategies", "robust", "promising", "error"],
                           lineterminator="\n")
        w.writeheader()
        w.writerows(_row(r) for _, r in sorted(done.items()))
        write_text_atomic(bdir / "summary.csv", buf.getvalue())

    persist()
    pending = list(symbols)
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(max_memory_gb,)) as pool:
        running: dict[Future[dict[str, Any]], str] = {}
        while pending or running:
            while pending and len(running) < workers and not stop.exists():
                sym = pending.pop(0)
                running[pool.submit(_one, settings, sym, str(output_root), kw, str(stop))] = sym
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                sym = running.pop(fut)
                res = fut.result()
                state["results"][sym] = {k: v for k, v in res.items() if k != "traceback"}
                if res.get("state") == "FAILED":
                    write_text_atomic(bdir / "errors" / f"{sym}.txt", res.get("traceback", res.get("error", "")))
                log(f"[batch {batch_id[:12]}] {sym}: {res.get('state')}")
                persist()
    state["state"] = "STOPPED" if stop.exists() and len(state["results"]) < len(symbols) else "FINISHED"
    persist()
    return {"batch_id": batch_id, "dir": str(bdir), "summary": str(bdir / "summary.csv"), **state["progress"],
            "state": state["state"]}


# ----------------------------------------------------------------------------- library

def library_list(output_root: Path, stock: str | None) -> list[dict[str, Any]]:
    root = Path(output_root).resolve() / "library" / "defaults"
    pattern = f"{normalize_symbol(stock)}/*.json" if stock else "*/*.json"
    out = []
    for p in sorted(root.glob(pattern)):
        d = read_json(p)
        out.append({"strategy_id": d["strategy_id"], "symbol": d["symbol"], "timeframe": d["timeframe"],
                    "tier": d["evidence"]["tier"], "entry": " AND ".join(c["text"] for c in d["entry"]["conditions"]),
                    "exit": d["exit"]["text"], "file": str(p)})
    return out


def library_export(output_root: Path, strategy_id: str, name: str, dest: Path) -> dict[str, Any]:
    """Copy an immutable discovered default into an editable user strategy (never overwrites)."""
    matches = sorted((Path(output_root).resolve() / "library" / "defaults").glob(f"*/{strategy_id}*--*.json"))
    if not matches:
        raise SystemExit(f"no library default matches {strategy_id!r}")
    src = matches[-1]
    doc = read_json(src)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise SystemExit("--name may contain letters, digits, '.', '_' and '-' only")
    target = Path(dest).resolve() / f"{name}.json"
    if target.exists():
        raise SystemExit(f"{target} exists; user copies are never overwritten")
    doc.update({"editable": True, "name": name, "status": "USER_EDITABLE_COPY",
                "derived_from": {"file": str(src), "strategy_id": doc["strategy_id"]}})
    write_json_atomic(target, doc)
    return {"exported": str(target), "immutable_default_kept": str(src)}
