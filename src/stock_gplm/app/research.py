"""Unified research run (``gplm research``): every catalogue strategy plus local ML discovery.

The unit of work is one stock x timeframe lane. For each lane:

* **Catalogue.** Every registered reference strategy (currently 340) gets exactly one matrix row,
  with an explicit status:
  ``TESTED`` (a backtest was run) or ``BLOCKED_IMPLEMENTATION``, ``BLOCKED_MISSING_DATA``,
  ``BLOCKED_TIMEFRAME_SCOPE`` or ``BLOCKED_INSUFFICIENT_HISTORY``.
  Registering a strategy never counts as testing it. Tested strategies reuse the M5.6 components:
  reviewed translations (``compile_reference``), the cost-aware simulator, walk-forward folds,
  Benjamini-Hochberg q-values over the lane's tested catalogue family, and the authoritative
  ``m56_status`` gates. Validation-passing strategies get one final development-segment read,
  once per strategy x lane fingerprint.
* **ML.** The stock-specific discovery methods of ``ml_discovery`` (sklearn tree paths, GPU rule
  screen) run on the same lane. Their strategies are separate matrix rows with ``source = ML``.

Data never extends past ``m56.development_end``. Each lane is committed in one transaction, keyed
by the data fingerprint, the catalogue bundle and the config, so a restart skips completed lanes.
Code edits alone do not force recomputation. The code SHA is recorded on every row.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ..m54.backtest import ExitPolicy, baseline, bh_adjust, fold_metrics, metrics, simulate
from .compute import Backend
from .context import App
from .db import sha, utc_now
from .ingest import ingest_many, normalize_symbol, window_data
from .lanes import all_bars, build_lane
from .library import strategy_features, strategy_mask
from .m56 import _available_inputs, _bundle_sha, _segments, compile_reference
from .m56_catalogue import CataloguePolicy, Reference, load_catalogue
from .m56_status import GatePolicy, derive, validation_evidence

CONTRACT = "stock-gplm.research-matrix.v1"
COLS = ["source", "symbol", "timeframe", "method", "strategy_id", "strategy_version", "reference_id", "category",
        "status",
        "classification", "validation_status", "final_segment_status", "block_reason", "required_inputs",
        "val_trades", "val_mean_net", "val_win_rate", "val_max_drawdown", "val_sharpe", "val_profit_factor",
        "val_p_value", "val_q_value",
        "bh_family_size", "final_trades", "final_mean_net", "final_max_drawdown", "rule", "reasons",
        "definition_file", "device", "data_start", "data_end", "data_fingerprint", "strategy_sha256",
        "code_sha", "run_id", "created_utc"]
LANE_COLS = ["symbol", "timeframe", "status", "bars", "catalogue_tested", "catalogue_blocked", "ml_strategies",
             "ml_status", "seconds", "data_start", "data_end", "data_fingerprint", "run_id", "created_utc"]
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS matrix ({", ".join(c + " TEXT" for c in COLS)}, row_key TEXT PRIMARY KEY);
CREATE INDEX IF NOT EXISTS matrix_status ON matrix(source, status, classification);
CREATE TABLE IF NOT EXISTS lanes ({", ".join(c + " TEXT" for c in LANE_COLS)}, lane_key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS final_reads (strategy_id TEXT, symbol TEXT, timeframe TEXT, data_fingerprint TEXT,
  run_id TEXT, created_utc TEXT, PRIMARY KEY(strategy_id, symbol, timeframe, data_fingerprint));
CREATE TABLE IF NOT EXISTS catalogue (reference_id TEXT PRIMARY KEY, category TEXT, name TEXT, sha256 TEXT,
  implementation_status TEXT, supported_timeframes TEXT, required_inputs TEXT);
"""


# --------------------------------------------------------------------------- universe


def resolve_universe(app: App, stocks: str | None, universe: str | None, max_symbols: int | None
                     ) -> tuple[list[str], str]:
    """Symbols plus a membership-basis label. ``universe`` may be all, a configured name, a JSON or a CSV file.

    A CSV such as NSE's ``ind_nifty500list.csv``, downloaded by a person in a browser, needs a
    ``Symbol`` column. It is today's list, so it is survivorship-biased for past periods.
    """
    from .ingest import list_symbols

    if stocks:
        syms, basis = [normalize_symbol(x) for x in stocks.split(",") if x.strip()], "EXPLICIT_LIST"
    elif universe:
        from .settings import REPO

        defaults = {"nifty500": str(REPO / "config" / "universes" / "ind_nifty500list.csv")}
        named = {**defaults, **(app.s.section("research").get("universes") or {})}.get(universe)
        target = named or universe
        if target in ("all", "all-eligible", "all-local"):
            syms, basis = list_symbols(app.s)["symbols"], "CURRENT_LOCAL_DIRECTORIES_SURVIVORSHIP_BIASED"
        else:
            path = Path(target)
            if not path.is_absolute():
                path = (app.s.path.parent / path).resolve()
            if not path.is_file():
                raise SystemExit(f"universe file not found: {path} (download the list manually in a browser)")
            if path.suffix.lower() == ".csv":
                with path.open(encoding="utf-8-sig", newline="") as fh:
                    rows = list(csv.DictReader(fh))
                col = next((c for c in (rows[0].keys() if rows else []) if c.strip().lower() == "symbol"), None)
                if col is None:
                    raise SystemExit(f"{path} has no 'Symbol' column")
                syms = [normalize_symbol(r[col].strip() + ("" if r[col].strip().endswith("-EQ") else "-EQ"))
                        for r in rows if r[col].strip()]
            else:
                doc = json.loads(path.read_text(encoding="utf-8-sig"))
                syms = [normalize_symbol(x) for x in (doc["symbols"] if isinstance(doc, dict) else doc)]
            basis = f"CURRENT_LIST_FILE_SURVIVORSHIP_BIASED:{path.name}"
    else:
        raise SystemExit("give --stocks SYMBOL[,SYMBOL...] or --universe all|nifty500|<file.csv|file.json>")
    syms = list(dict.fromkeys(syms))
    return (syms[:max_symbols] if max_symbols else syms), basis


# --------------------------------------------------------------------------- store


class Matrix:
    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(out / "matrix.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def done(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM lanes WHERE lane_key=?", (key,)).fetchone() is not None

    def final_read(self, sid: str, symbol: str, tf: str, fp: str) -> bool:
        return self.db.execute("SELECT 1 FROM final_reads WHERE strategy_id=? AND symbol=? AND timeframe=? AND "
                               "data_fingerprint=?", (sid, symbol, tf, fp)).fetchone() is not None

    def register_catalogue(self, refs: Sequence[Reference]) -> None:
        with self.db:
            for r in refs:
                self.db.execute("INSERT OR IGNORE INTO catalogue VALUES (?,?,?,?,?,?,?)",
                                (r.reference_id, f"{r.category_number}: {r.category}", r.name, r.digest,
                                 r.implementation_status, ",".join(r.supported_timeframes),
                                 ",".join(r.required_inputs)))

    def commit(self, key: str, lane: dict[str, Any], rows: list[dict[str, Any]],
               finals: list[tuple[str, str, str, str]], run_id: str) -> None:
        with self.db:
            for f in finals:
                self.db.execute("INSERT OR IGNORE INTO final_reads VALUES (?,?,?,?,?,?)", (*f, run_id, utc_now()))
            for r in rows:
                rk = sha({"lane": key, "source": r["source"], "method": r.get("method"), "id": r["strategy_id"]})
                self.db.execute(f"INSERT OR IGNORE INTO matrix VALUES ({','.join('?' * (len(COLS) + 1))})",
                                [_cell(r.get(c)) for c in COLS] + [rk])
            self.db.execute(f"INSERT OR IGNORE INTO lanes VALUES ({','.join('?' * (len(LANE_COLS) + 1))})",
                            [_cell(lane.get(c)) for c in LANE_COLS] + [key])
        _append(self.out / "matrix.csv", COLS, rows)
        _append(self.out / "lanes.csv", LANE_COLS, [lane])


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str)):
        return v
    if isinstance(v, (np.integer, np.floating)):
        return v.item()
    return json.dumps(v, default=str, sort_keys=True)


def _append(path: Path, cols: Sequence[str], rows: Sequence[dict[str, Any]]) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(cols), extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows({c: _cell(r.get(c)) for c in cols} for r in rows)


# --------------------------------------------------------------------------- catalogue lane


def _catalogue_lane(app: App, refs: Sequence[Reference], data: Any, bars: Any, tf: str, lane: Any,
                    gates: GatePolicy, store: Matrix, base: dict[str, Any]) -> tuple[list[dict[str, Any]], list]:
    available = _available_inputs(data)
    rows: list[dict[str, Any]] = []
    tested: list[dict[str, Any]] = []
    for ref in refs:
        row = {**base, "source": "CATALOGUE", "method": "reference", "strategy_id": ref.reference_id,
               "strategy_version": ref.version,
               "reference_id": ref.reference_id, "category": f"{ref.category_number}: {ref.category}",
               "strategy_sha256": ref.digest, "required_inputs": ",".join(ref.required_inputs),
               "classification": "NOT_TESTED", "validation_status": "NOT_EVALUATED",
               "final_segment_status": "NOT_SCORED", "rule": (ref.rule_text or "")[:300]}
        missing = sorted(set(ref.required_inputs) - available)
        if tf not in ref.supported_timeframes:
            row.update(status="BLOCKED_TIMEFRAME_SCOPE",
                       block_reason=f"strategy scope is {','.join(ref.supported_timeframes)}")
        elif missing:
            row.update(status="BLOCKED_MISSING_DATA", block_reason="missing registered inputs: " + ", ".join(missing))
        elif lane is None:
            row.update(status="BLOCKED_MISSING_DATA", block_reason=f"no accepted {tf} candles in window")
        else:
            doc, reason = compile_reference(ref, tf)
            if doc is None:
                row.update(status="BLOCKED_IMPLEMENTATION", block_reason=reason)
            else:
                segs = _segments(len(lane.market), int(doc["exit"]["max_hold_bars"]))
                if segs is None:
                    row.update(status="BLOCKED_INSUFFICIENT_HISTORY",
                               block_reason=f"{len(lane.market)} bars; purged 60/20/20 split needs more")
                else:
                    feats = strategy_features(doc, bars, tf)
                    mask = strategy_mask(doc, feats, len(lane.market))
                    pol = ExitPolicy.from_dict(doc["exit"])
                    tr = simulate(lane.market, mask, segs["validation"], pol, lane.costs)
                    bmean = baseline(lane.market, segs["validation"], pol, lane.costs)
                    met = metrics(tr, lane.market, segs["validation"], bmean)
                    met["folds"] = fold_metrics(tr, segs["validation"], gates.walk_forward_folds, bmean)
                    row.update(status="TESTED", device="cpu (NumPy trade simulation)",
                               block_reason="", rule=json.dumps(doc["entry"]["conditions"], sort_keys=True)[:300])
                    tested.append({"row": row, "met": met, "mask": mask, "segs": segs, "policy": pol})
        rows.append(row)
    family = max(1, len(tested))
    qvals = bh_adjust([t["met"].get("p_value") for t in tested], family)
    finals = []
    for t, qv in zip(tested, qvals):
        met, row = t["met"], t["row"]
        met.update(q_value=qv, bh_family_size=family)
        row.update(val_trades=met.get("effective_n"), val_mean_net=met.get("mean_net"),
                   val_win_rate=met.get("win_rate"), val_max_drawdown=met.get("max_drawdown"),
                   val_sharpe=met.get("sharpe_annualized"), val_profit_factor=met.get("profit_factor"),
                   val_p_value=met.get("p_value"), val_q_value=qv, bh_family_size=family)
        st = derive({"evidence_stage": "VALIDATION", "classification": "", "metrics": met}, gates)
        if st["validation_status"] == "PASSED":
            consumed = store.final_read(row["strategy_id"], row["symbol"], row["timeframe"], lane.data_fingerprint) \
                or app.db.one("SELECT 1 x FROM m56_results WHERE strategy_id=? AND symbol=? AND timeframe=? AND "
                              "regime='ALL' AND evidence_stage='TEST_ONCE' AND data_fingerprint=?",
                              (row["strategy_id"], row["symbol"], row["timeframe"], lane.data_fingerprint))
            if not consumed:
                tr = simulate(lane.market, t["mask"], t["segs"]["test"], t["policy"], lane.costs)
                bmean = baseline(lane.market, t["segs"]["test"], t["policy"], lane.costs)
                fm = metrics(tr, lane.market, t["segs"]["test"], bmean)
                fm["validation_evidence"] = validation_evidence(met)
                st = derive({"evidence_stage": "TEST_ONCE", "classification": "", "metrics": fm}, gates)
                row.update(final_trades=fm.get("effective_n"), final_mean_net=fm.get("mean_net"),
                           final_max_drawdown=fm.get("max_drawdown"))
                finals.append((row["strategy_id"], row["symbol"], row["timeframe"], lane.data_fingerprint))
            else:
                st["reasons"].append("final segment already read for this fingerprint; not re-read")
        row.update(classification=st["classification"], validation_status=st["validation_status"],
                   final_segment_status=st["final_segment_status"],
                   reasons="; ".join(r for r in st["reasons"] if not r.startswith("recorded label")))
    return rows, finals


# --------------------------------------------------------------------------- orchestration


def run(app: App, symbols: Sequence[str], timeframes: Sequence[str], backend: Backend, *, catalogue: Path,
        methods: Sequence[str], out: Path, budget: str = "quick", start: str | None = None,
        end: str | None = None, universe_basis: str = "", catalogue_enabled: bool = True) -> dict[str, Any]:
    from .ml_discovery import _screen_lane, _tree_lane

    cfg = app.s.section("m56")
    dev_end = cfg.get("development_end")
    if not dev_end:
        raise ValueError("m56.development_end is not configured; refusing to run without a holdout boundary")
    if end and end > dev_end:
        raise ValueError(f"--end {end} is after m56.development_end {dev_end}; the locked holdout stays closed")
    end = end or dev_end
    gates = GatePolicy.from_cfg(cfg)
    policy = CataloguePolicy.from_cfg(cfg)
    supplements = [Path(x) for x in ((cfg.get("catalogue") or {}).get("supplements") or [])]
    parsed = load_catalogue(catalogue, policy, supplements)
    refs = parsed.references
    bundle = _bundle_sha(catalogue, supplements)
    seed = int(app.s.section("models").get("seed", 55)) + 1
    store = Matrix(out)
    store.register_catalogue(refs)
    meta = {"contract": CONTRACT, "window": {"start": start, "end": end, "development_end": dev_end},
            "universe_basis": universe_basis, "symbols": len(symbols), "timeframes": list(timeframes),
            "catalogue_enabled": catalogue_enabled,
            "ml_methods": list(methods), "budget": budget, "catalogue": {"path": str(catalogue), "bundle_sha256": bundle,
            "strategies": len(refs), "report_status": parsed.report()["status"]}, "gate_policy": gates.to_dict(),
            "costs": app.s.section("costs"), "config_sha256": app.s.sha256, "device": backend.label}
    (out / "run-config.json").write_text(json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n",
                                         encoding="utf-8")
    (out / "catalogue-report.json").write_text(json.dumps(parsed.report(), indent=2, default=str) + "\n",
                                               encoding="utf-8")
    app.db.event("RESEARCH_START", {"out": str(out), **meta["window"], "symbols": len(symbols),
                                    "catalogue": len(refs)})
    app.progress.set_total(len(symbols) * len(timeframes))
    use_minute = any(tf.endswith("m") for tf in timeframes)
    lanes_status: dict[str, int] = {}
    for symbol in symbols:
        try:
            loaded = ingest_many(app.s, app.db, [symbol], use_minute=use_minute)
        except Exception as exc:  # recorded; one bad symbol never stops the run
            loaded = {}
            app.db.event("RESEARCH_SYMBOL_FAILED", {"symbol": symbol, "error": str(exc)}, level="WARN")
        data = next(iter(loaded.values()), None)
        data = window_data(data, start, end) if data is not None else None
        bars = all_bars(data) if data is not None else {}
        for tf in timeframes:
            fp = getattr(data, "fingerprint", "none")
            key = sha({"symbol": symbol, "tf": tf, "data": fp, "catalogue": bundle if catalogue_enabled else None,
                       "config": app.s.sha256, "methods": sorted(methods), "budget": budget})
            app.progress.update(task="research", stock=symbol, timeframe=tf)
            if store.done(key):
                lanes_status["REUSED"] = lanes_status.get("REUSED", 0) + 1
                app.progress.advance()
                continue
            t0 = time.monotonic()
            has = data is not None and tf in bars and len(bars[tf]) > 0
            lane = build_lane(data, tf, app.s.section("costs"), bars=bars,
                              cache_dir=app.s.cache_dir / "features") if has else None
            daily = data.daily if data is not None else None
            base = {"symbol": symbol, "timeframe": tf,
                    "data_start": str(daily["date"].min()) if daily is not None and len(daily) else None,
                    "data_end": str(daily["date"].max()) if daily is not None and len(daily) else None,
                    "data_fingerprint": lane.data_fingerprint if lane is not None else fp,
                    "code_sha": app.code_sha, "run_id": app.run_id, "created_utc": utc_now()}
            if not catalogue_enabled:
                rows, finals = [], []
            elif data is None:
                rows, finals = [{**base, "source": "CATALOGUE", "method": "reference", "strategy_id": r.reference_id,
                                 "reference_id": r.reference_id, "status": "BLOCKED_MISSING_DATA",
                                 "classification": "NOT_TESTED", "block_reason": "no local candles for symbol",
                                 "strategy_sha256": r.digest} for r in refs], []
            else:
                rows, finals = _catalogue_lane(app, refs, data, bars, tf, lane, gates, store, base)
            ml_rows: list[dict[str, Any]] = []
            ml_status = []
            for method in methods:
                mbase = {**base}
                if lane is None:
                    ml_status.append(f"{method}:BLOCKED_MISSING_DATA")
                    continue
                if method == "tree":
                    allow = not store.final_read("ml-tree-lane", symbol, tf, lane.data_fingerprint)
                    info, mrows, read = _tree_lane(lane, cfg, seed, allow, gates, out, mbase)
                    if read:
                        finals.append(("ml-tree-lane", symbol, tf, lane.data_fingerprint))
                else:
                    info, mrows, _ = _screen_lane(app, backend, data, tf, bars, budget, gates, out, mbase)
                ml_status.append(f"{method}:{info['status']}")
                for r in mrows:
                    r.update(source="ML", status="TESTED", reference_id="", strategy_version=1,
                             block_reason="", category="ML: " + method)
                ml_rows.extend(mrows)
            tested = sum(r["status"] == "TESTED" for r in rows)
            lane_row = {**base, "status": "COMPLETED" if lane is not None else "NO_DATA",
                        "bars": len(lane.market) if lane is not None else 0, "catalogue_tested": tested,
                        "catalogue_blocked": len(rows) - tested, "ml_strategies": len(ml_rows),
                        "ml_status": ",".join(ml_status), "seconds": round(time.monotonic() - t0, 2)}
            store.commit(key, lane_row, rows + ml_rows, finals, app.run_id)
            lanes_status[lane_row["status"]] = lanes_status.get(lane_row["status"], 0) + 1
            app.progress.advance()
    summary = write_summary(store, meta)
    app.db.event("RESEARCH_DONE", {"out": str(out), "lanes": lanes_status})
    return {"status": "COMPLETED", "out": str(out), "lanes": lanes_status, **summary}


def write_summary(store: Matrix, meta: dict[str, Any]) -> dict[str, Any]:
    db = store.db
    q = lambda sql: [dict(r) for r in db.execute(sql)]  # noqa: E731
    by_status = q("SELECT source, status, COUNT(*) n FROM matrix GROUP BY 1, 2 ORDER BY 1, 3 DESC")
    by_class = q("SELECT source, classification, COUNT(*) n FROM matrix WHERE status='TESTED' GROUP BY 1, 2 "
                 "ORDER BY 1, 3 DESC")
    refs_total = db.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0]
    refs_exec = db.execute("SELECT COUNT(DISTINCT reference_id) FROM matrix WHERE source='CATALOGUE' AND "
                           "status='TESTED'").fetchone()[0]
    never = q("SELECT c.reference_id, c.implementation_status, (SELECT m.status || ': ' || m.block_reason FROM "
              "matrix m WHERE m.reference_id=c.reference_id LIMIT 1) example FROM catalogue c WHERE c.reference_id "
              "NOT IN (SELECT reference_id FROM matrix WHERE status='TESTED') ORDER BY c.rowid")
    never_reason: dict[str, int] = {}
    for r in never:
        k = (r["example"] or "NOT_REACHED").split(":")[0]
        never_reason[k] = never_reason.get(k, 0) + 1
    top = q("SELECT source, symbol, timeframe, strategy_id, classification, val_trades, val_mean_net, val_q_value, "
            "final_trades, final_mean_net FROM matrix WHERE classification IN ('VALIDATED','EXPERIMENTAL') "
            "ORDER BY classification='VALIDATED' DESC, CAST(val_mean_net AS REAL) DESC LIMIT 30")
    lanes = db.execute("SELECT COUNT(*) FROM lanes").fetchone()[0]
    stocks = db.execute("SELECT COUNT(DISTINCT symbol) FROM lanes").fetchone()[0]
    w = meta["window"]
    tbl = lambda rows, cols: ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + \
        ["| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows]  # noqa: E731
    lines = ["# GPLM unified research run: catalogue + ML", "",
             f"- Data: development window up to {w['end']}; locked holdout (after {w['development_end']}) never read",
             f"- Universe: {meta['universe_basis']}; stocks completed {stocks}; lanes {lanes}; timeframes "
             f"{', '.join(meta['timeframes'])}; ML methods {', '.join(meta['ml_methods']) or 'none'}",
             f"- Catalogue: {refs_total} registered; **{refs_exec} executed on at least one lane**; "
             f"{refs_total - refs_exec} never executed (registration is not testing)",
             "- VALIDATED = all M5.6 gates + once-only final development-segment pass. Not trading advice.", "",
             "## Matrix rows by source and status", "", *tbl(by_status, ["source", "status", "n"]), "",
             "## Tested rows by classification", "", *tbl(by_class, ["source", "classification", "n"]), "",
             "## Catalogue strategies never executed, by first blocking reason", "",
             *tbl([{"reason": k, "strategies": v} for k, v in sorted(never_reason.items())], ["reason", "strategies"]),
             "", "## VALIDATED / EXPERIMENTAL", ""]
    lines += tbl(top, ["source", "symbol", "timeframe", "strategy_id", "classification", "val_trades",
                       "val_mean_net", "val_q_value", "final_trades", "final_mean_net"]) if top else \
        ["None passed the validation gates."]
    lines += ["", "Files: matrix.csv / matrix.sqlite = full coverage (one row per stock x strategy x timeframe, "
                  "blocked and rejected included); ranked_validated.csv = only VALIDATED rows; lanes.csv, "
                  "catalogue-report.json, run-config.json, strategies/ (ML originals read-only + editable copies).", ""]
    (store.out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    # Ranked report: only rows that satisfied every declared gate (VALIDATED). Diagnostic rows stay in matrix.csv.
    ranked = q("SELECT * FROM matrix WHERE classification='VALIDATED' ORDER BY CAST(val_sharpe AS REAL) DESC, "
               "CAST(val_mean_net AS REAL) DESC")
    with (store.out / "ranked_validated.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ranked)
    doc = {"by_status": by_status, "by_classification": by_class, "catalogue_registered": refs_total,
           "catalogue_executed": refs_exec, "never_executed_by_reason": never_reason, "lanes": lanes,
           "stocks": stocks, "window": w}
    (store.out / "summary.json").write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
    return {"summary": str(store.out / "summary.md"), **{k: doc[k] for k in ("catalogue_registered",
                                                                              "catalogue_executed", "by_status")}}


__all__ = ["CONTRACT", "resolve_universe", "run", "write_summary"]
