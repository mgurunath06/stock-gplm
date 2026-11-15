"""Local ML strategy discovery program (``gplm ml-discover``).

One pass per stock x timeframe, all on local CPU/GPU:

1. Load the stored NSE candles (no download) and keep only the development window: no row after
   ``m56.development_end`` reaches a feature, label or score.
2. Build the existing causal feature lane.
3. Discover candidates:
   ``tree``   - scikit-learn gradient boosting chooses features, a decision tree learns entry paths
                (``advanced_discovery``, CPU);
   ``screen`` - the batched single/pair rule screen (``discover.broad_screen``, CUDA when available).
4. Backtest every candidate with configured transaction costs on a temporally separated
   validation segment. The final development segment is read at most once per lane fingerprint.
5. Classify each candidate with the authoritative M5.6 gates (``m56_status``). Screen candidates
   carry no family-wide q-value, so they are never ``VALIDATED``.
6. Write one result row per stock x strategy x timeframe, including rejected ones, together with
   an immutable original and an editable copy of every declarative ``strategy.v3`` definition.
   Each lane commits on its own, so rerunning the same command resumes an interrupted run.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any, Sequence

from .compute import Backend
from .context import App
from .db import sha, utc_now
from .ingest import ingest_many, window_data
from .lanes import all_bars, build_lane
from .m56_status import GatePolicy, derive

CONTRACT = "stock-gplm.ml-discovery.v1"
METHODS = ("tree", "screen")
RESULT_COLS = ["symbol", "timeframe", "method", "strategy_id", "classification", "validation_status",
               "final_segment_status", "discovery_status", "val_trades", "val_mean_net", "val_win_rate",
               "val_max_drawdown", "val_q_value", "val_p_value", "final_trades", "final_mean_net",
               "final_max_drawdown", "rule", "reasons", "definition_file", "editable_file", "device",
               "data_start", "data_end", "data_fingerprint", "run_id", "created_utc"]
LANE_COLS = ["symbol", "timeframe", "method", "status", "bars", "candidates", "experimental", "validated",
             "selected_features", "device", "seconds", "data_start", "data_end", "data_fingerprint", "detail",
             "run_id", "created_utc"]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS lanes ({", ".join(c + " TEXT" for c in LANE_COLS)}, lane_key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS results ({", ".join(c + " TEXT" for c in RESULT_COLS)}, result_key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS final_reads (symbol TEXT, timeframe TEXT, method TEXT, data_fingerprint TEXT,
  run_id TEXT, created_utc TEXT, PRIMARY KEY(symbol, timeframe, method, data_fingerprint));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    """Output directory: SQLite is authoritative, CSVs are appended after every lane."""

    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(out / "results.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def done(self, lane_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM lanes WHERE lane_key=?", (lane_key,)).fetchone() is not None

    def final_read(self, symbol: str, tf: str, method: str, fingerprint: str) -> bool:
        return self.db.execute("SELECT 1 FROM final_reads WHERE symbol=? AND timeframe=? AND method=? AND "
                               "data_fingerprint=?", (symbol, tf, method, fingerprint)).fetchone() is not None

    def commit_lane(self, lane_key: str, lane: dict[str, Any], rows: list[dict[str, Any]],
                    final_read: tuple[str, str, str, str] | None, run_id: str) -> None:
        with self.db:
            if final_read:
                self.db.execute("INSERT OR IGNORE INTO final_reads VALUES (?,?,?,?,?,?)",
                                (*final_read, run_id, utc_now()))
            for r in rows:
                key = sha({"lane": lane_key, "strategy": r["strategy_id"]})
                self.db.execute(f"INSERT OR IGNORE INTO results VALUES ({','.join('?' * (len(RESULT_COLS) + 1))})",
                                [_cell(r.get(c)) for c in RESULT_COLS] + [key])
            self.db.execute(f"INSERT OR IGNORE INTO lanes VALUES ({','.join('?' * (len(LANE_COLS) + 1))})",
                            [_cell(lane.get(c)) for c in LANE_COLS] + [lane_key])
        _append_csv(self.out / "results.csv", RESULT_COLS, rows)
        _append_csv(self.out / "lanes.csv", LANE_COLS, [lane])


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str)):
        return v
    return json.dumps(v, default=str, sort_keys=True)


def _append_csv(path: Path, cols: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(cols), extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({c: _cell(r.get(c)) for c in cols})


def _write_definition(out: Path, symbol: str, tf: str, sid: str, doc: dict[str, Any]) -> tuple[str, str]:
    """Immutable original (read-only, never overwritten) plus an editable user copy (created once)."""
    orig = out / "strategies" / "original" / symbol / tf / f"{sid}.json"
    edit = out / "strategies" / "editable" / symbol / tf / f"{sid}.json"
    text = json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n"
    for p, readonly in ((orig, True), (edit, False)):
        if p.exists():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if readonly:
            os.chmod(p, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return str(orig.relative_to(out)), str(edit.relative_to(out))


def _rule_text(conditions: Sequence[dict[str, Any]]) -> str:
    def one(c: dict[str, Any]) -> str:
        thr = c.get("threshold")
        return f"{c.get('feature')} {c.get('op')}" + ("" if thr is None else f" {thr:.6g}" if isinstance(thr, float)
                                                       else f" {thr}")
    return " AND ".join(one(c) for c in conditions)


def _tree_lane(lane: Any, cfg: dict[str, Any], seed: int, allow_final: bool, gates: GatePolicy, out: Path,
               base: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    from .advanced_discovery import discover_tree_strategies

    res = discover_tree_strategies(lane, cfg, seed=seed, allow_test=allow_final)
    rows = []
    for cand in res.get("candidates", []):
        doc = cand["document"]
        sid = "ml-" + sha(doc)[:16]
        stage = "TEST_ONCE" if cand.get("test") is not None else "ADVANCED_VALIDATION"
        met = dict(cand["test"]) if cand.get("test") is not None else dict(cand["validation"])
        if stage == "TEST_ONCE":
            met["validation_evidence"] = {k: cand["validation"].get(k) for k in
                                          ("effective_n", "q_value", "mean_net", "max_drawdown", "folds")}
        st = derive({"evidence_stage": stage, "classification": cand["classification"], "metrics": met,
                     "lineage": cand["lineage"]}, gates)
        doc_files = _write_definition(out, lane.symbol, lane.timeframe, sid, doc)
        rows.append(_row(base, "tree", sid, st, cand["validation"], cand.get("test"),
                         _rule_text(doc["entry"]["conditions"]), doc_files, res.get("device", "cpu (scikit-learn)")))
    lane_info = {"status": res["status"], "selected_features": res.get("selected_features"),
                 "device": res.get("device", "cpu (scikit-learn)"),
                 "detail": {k: v for k, v in res.items() if k in ("reason", "rows", "bars", "train_rows", "generated",
                                                                  "segments", "final_segment_read")}}
    return lane_info, rows, bool(res.get("final_segment_read")) and res["status"] == "COMPLETED"


def _screen_lane(app: App, backend: Backend, data: Any, tf: str, bars: Any, budget: str, gates: GatePolicy,
                 out: Path, base: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    from .discover import broad_screen

    sc = broad_screen(backend, app, data, tf, budget, bars)
    sc.pop("lane", None)
    rows = []
    for r in sc.get("rules", []):
        doc = {"contract": "stock-gplm.strategy.v3", "name": f"{data.symbol} {tf} screen: {r['text']}"[:120],
               "timeframe": tf, "scope": {"symbols": [data.symbol], "timeframes": [tf]}, "direction": "long",
               "entry": {"logic": "AND", "conditions": r["conditions"]}, "exit": r["exit"],
               "provenance": {"method": "batched_rule_screen", "device": backend.name, "budget": budget},
               "evidence": {"screen_tier": r["screen_tier"], "in_sample": r["in_sample"]}}
        sid = "scr-" + sha(doc)[:16]
        st = derive({"evidence_stage": "BROAD_SCREEN", "classification": "DISCOVERED",
                     "metrics": {"tier": r["screen_tier"], "validation": r["validation"], "test": r["test"]}}, gates)
        val = {"effective_n": r["validation"].get("trades"), "mean_net": r["validation"].get("mean_net"),
               "win_rate": r["validation"].get("win_rate"), "p_value": (r.get("in_sample") or {}).get("p_value"),
               "q_value": (r.get("in_sample") or {}).get("q_bh")}
        test = {"effective_n": r["test"].get("trades"), "mean_net": r["test"].get("mean_net")}
        rows.append(_row(base, "screen", sid, st, val, test, r["text"],
                         _write_definition(out, data.symbol, tf, sid, doc), backend.label))
    status = "COMPLETED" if sc.get("status") == "DONE" else str(sc.get("status"))
    info = {"status": status, "device": backend.label,
            "detail": {k: sc.get(k) for k in ("tests", "min_q", "fdr_survivors", "segments", "screen_seconds",
                                               "bars")}}
    return info, rows, False


def _row(base: dict[str, Any], method: str, sid: str, st: dict[str, Any], val: dict[str, Any] | None,
         test: dict[str, Any] | None, rule: str, files: tuple[str, str], device: str) -> dict[str, Any]:
    v, t = val or {}, test or {}
    return {**base, "method": method, "strategy_id": sid, "classification": st["classification"],
            "validation_status": st["validation_status"], "final_segment_status": st["final_segment_status"],
            "discovery_status": st["discovery_status"], "val_trades": v.get("effective_n"),
            "val_mean_net": v.get("mean_net"), "val_win_rate": v.get("win_rate"),
            "val_max_drawdown": v.get("max_drawdown"), "val_q_value": v.get("q_value"),
            "val_p_value": v.get("p_value"), "final_trades": t.get("effective_n") if test else None,
            "final_mean_net": t.get("mean_net") if test else None,
            "final_max_drawdown": t.get("max_drawdown") if test else None, "rule": rule,
            "reasons": "; ".join(st["reasons"]), "definition_file": files[0], "editable_file": files[1],
            "device": device}


def run(app: App, symbols: Sequence[str], timeframes: Sequence[str], backend: Backend, *, methods: Sequence[str],
        out: Path, budget: str = "quick", start: str | None = None, end: str | None = None) -> dict[str, Any]:
    cfg = app.s.section("m56")
    dev_end = cfg.get("development_end")
    if not dev_end:
        raise ValueError("m56.development_end is not configured; refusing to read data without a holdout boundary")
    if end and end > dev_end:
        raise ValueError(f"--end {end} is after m56.development_end {dev_end}; the locked holdout stays closed")
    end = end or dev_end
    bad = sorted(set(methods) - set(METHODS))
    if bad:
        raise ValueError(f"unknown methods {bad}; choose from {METHODS}")
    gates = GatePolicy.from_cfg(cfg)
    seed = int(app.s.section("models").get("seed", 55)) + 1
    store = Store(out)
    run_cfg = {"contract": CONTRACT, "window": {"start": start, "end": end, "development_end": dev_end},
               "timeframes": list(timeframes), "methods": list(methods), "budget": budget,
               "gate_policy": gates.to_dict(), "costs": app.s.section("costs"), "config_sha256": app.s.sha256,
               "code_sha": app.code_sha, "device": backend.label}
    (out / "run-config.json").write_text(json.dumps(run_cfg, indent=2, sort_keys=True, default=str) + "\n",
                                         encoding="utf-8")
    app.db.event("ML_DISCOVERY_START", {"out": str(out), **run_cfg["window"], "symbols": len(symbols)})
    total = len(symbols) * len(timeframes) * len(methods)
    app.progress.set_total(total)
    counts: dict[str, int] = {}
    use_minute = any(tf.endswith("m") for tf in timeframes)
    for symbol in symbols:
        try:
            loaded = ingest_many(app.s, app.db, [symbol], use_minute=use_minute)
        except Exception as exc:  # one bad symbol never stops the run; it is recorded
            loaded = {}
            app.db.event("ML_DISCOVERY_SYMBOL_FAILED", {"symbol": symbol, "error": str(exc)}, level="WARN")
        data = next(iter(loaded.values()), None)
        data = window_data(data, start, end) if data is not None else None
        bars = all_bars(data) if data is not None else {}
        for tf in timeframes:
            lane = None
            for method in methods:
                fp = getattr(data, "fingerprint", "none")
                lane_key = sha({"symbol": symbol, "tf": tf, "method": method, "data": fp, "config": app.s.sha256,
                                "code": app.code_sha, "budget": budget})
                app.progress.update(task="ml-discover", stock=symbol, timeframe=tf, note=method)
                if store.done(lane_key):
                    counts["REUSED"] = counts.get("REUSED", 0) + 1
                    app.progress.advance()
                    continue
                t0 = time.monotonic()
                period = (str(data.daily["date"].min()) if data is not None and len(data.daily) else None,
                          str(data.daily["date"].max()) if data is not None and len(data.daily) else None)
                base = {"symbol": symbol, "timeframe": tf, "data_start": period[0], "data_end": period[1],
                        "data_fingerprint": fp, "run_id": app.run_id, "created_utc": utc_now()}
                rows: list[dict[str, Any]] = []
                final = None
                if data is None:
                    info = {"status": "BLOCKED_NO_DATA", "detail": "symbol could not be loaded"}
                elif tf not in bars or not len(bars[tf]):
                    info = {"status": "BLOCKED_MISSING_DATA",
                            "detail": f"no {tf} candles on or before {end} (minute files start 2026-01-01)"
                            if tf.endswith("m") else f"no {tf} candles in window"}
                elif method == "tree":
                    if lane is None:
                        lane = build_lane(data, tf, app.s.section("costs"), bars=bars,
                                          cache_dir=app.s.cache_dir / "features")
                    if lane is None:
                        info = {"status": "BLOCKED_MISSING_DATA", "detail": "lane could not be built"}
                    else:
                        base["data_fingerprint"] = lane.data_fingerprint
                        allow = not store.final_read(symbol, tf, method, lane.data_fingerprint)
                        info, rows, read = _tree_lane(lane, cfg, seed, allow, gates, out, base)
                        if read:
                            final = (symbol, tf, method, lane.data_fingerprint)
                        if not allow:
                            info["detail"] = {**(info.get("detail") or {}),
                                              "final_segment": "already read for this fingerprint; not re-read"}
                else:
                    info, rows, _ = _screen_lane(app, backend, data, tf, bars, budget, gates, out, base)
                lane_row = {**base, "method": method, "status": info["status"],
                            "bars": len(bars[tf]) if tf in bars else 0, "candidates": len(rows),
                            "experimental": sum(r["classification"] == "EXPERIMENTAL" for r in rows),
                            "validated": sum(r["classification"] == "VALIDATED" for r in rows),
                            "selected_features": info.get("selected_features"), "device": info.get("device", "cpu"),
                            "seconds": round(time.monotonic() - t0, 2), "detail": info.get("detail")}
                store.commit_lane(lane_key, lane_row, rows, final, app.run_id)
                counts[info["status"]] = counts.get(info["status"], 0) + 1
                app.progress.update(generated_candidates=len(rows))
                app.progress.advance()
            del lane
    summary = write_summary(store, run_cfg)
    app.db.event("ML_DISCOVERY_DONE", {"out": str(out), "lane_status": counts})
    return {"status": "COMPLETED", "out": str(out), "lane_status": counts, **summary}


def write_summary(store: Store, run_cfg: dict[str, Any]) -> dict[str, Any]:
    db = store.db
    lanes = [dict(r) for r in db.execute("SELECT status, COUNT(*) n FROM lanes GROUP BY status ORDER BY n DESC")]
    cls = [dict(r) for r in db.execute("SELECT method, classification, COUNT(*) n FROM results GROUP BY 1, 2 "
                                       "ORDER BY 1, 2")]
    top = [dict(r) for r in db.execute(
        "SELECT symbol, timeframe, method, strategy_id, classification, val_trades, val_mean_net, val_q_value, "
        "final_trades, final_mean_net, rule FROM results WHERE classification IN ('VALIDATED','EXPERIMENTAL') "
        "ORDER BY classification='VALIDATED' DESC, CAST(val_mean_net AS REAL) DESC LIMIT 25")]
    best_candidates = [dict(r) for r in db.execute(
        "SELECT symbol, timeframe, method, strategy_id, classification, val_trades, val_mean_net, val_q_value, rule "
        "FROM results WHERE CAST(val_trades AS INTEGER) >= 10 ORDER BY CAST(val_mean_net AS REAL) DESC LIMIT 15")]
    stocks = db.execute("SELECT COUNT(DISTINCT symbol) FROM lanes").fetchone()[0]
    n_results = db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    w = run_cfg["window"]
    lines = [
        "# GPLM local ML strategy discovery: summary", "",
        f"- Data period: development window, start {w['start'] or 'earliest stored'} to end {w['end']} "
        f"(locked holdout begins after {w['development_end']} and is never read)",
        f"- Stocks: {stocks}; timeframes: {', '.join(run_cfg['timeframes'])}; methods: "
        f"{', '.join(run_cfg['methods'])}; device: {run_cfg['device']}",
        f"- Strategy results recorded (including rejected): {n_results}",
        f"- Gates for VALIDATED: {json.dumps(run_cfg['gate_policy'])}",
        "- VALIDATED requires all validation gates (BH q across the lane's candidates) AND a once-only final "
        "development-segment pass. Screen candidates never qualify. Nothing here is a trading recommendation.", "",
        "## Lanes (stock x timeframe x method)", "", "| status | n |", "|---|---|",
        *[f"| {r['status']} | {r['n']} |" for r in lanes], "",
        "## Strategy classifications", "", "| method | classification | n |", "|---|---|---|",
        *[f"| {r['method']} | {r['classification']} | {r['n']} |" for r in cls], "",
        "## VALIDATED / EXPERIMENTAL strategies", "",
    ]
    if top:
        lines += ["| symbol | tf | method | id | class | val n | val mean net | q | final n | final mean net | rule |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        lines += [f"| {r['symbol']} | {r['timeframe']} | {r['method']} | {r['strategy_id']} | {r['classification']} | "
                  f"{r['val_trades']} | {_fmt(r['val_mean_net'])} | {_fmt(r['val_q_value'])} | {r['final_trades']} | "
                  f"{_fmt(r['final_mean_net'])} | {r['rule']} |" for r in top]
    else:
        lines.append("None. No discovered strategy passed the validation gates. That is a valid result.")
    lines += ["", "## Highest validation mean net with at least 10 trades (diagnostic only, NOT validated)", "",
              "| symbol | tf | method | id | class | val n | val mean net | q | rule |", "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r['symbol']} | {r['timeframe']} | {r['method']} | {r['strategy_id']} | {r['classification']} | "
              f"{r['val_trades']} | {_fmt(r['val_mean_net'])} | {_fmt(r['val_q_value'])} | {r['rule']} |"
              for r in best_candidates]
    lines += ["", "Files: results.csv (every stock x strategy x timeframe), lanes.csv, results.sqlite, "
                  "strategies/original (read-only), strategies/editable (your copies), run-config.json.", ""]
    (store.out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    doc = {"lanes": lanes, "classifications": cls, "stocks": stocks, "results": n_results, "window": w}
    (store.out / "summary.json").write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
    return {"summary": str(store.out / "summary.md"), "results": n_results, "classifications": cls}


def _fmt(v: Any) -> str:
    try:
        return f"{float(v):.4g}"
    except (TypeError, ValueError):
        return "" if v is None else str(v)


__all__ = ["CONTRACT", "METHODS", "run", "write_summary"]
