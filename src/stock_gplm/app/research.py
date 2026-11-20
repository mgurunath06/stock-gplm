"""Unified research run (``gplm research-matrix``): one generic engine for every strategy origin.

Unit of work: one stock x timeframe *lane*. For each lane:

* **Library strategies.** Every enabled, applicable definition in the dynamic strategy library
  (:mod:`stock_gplm.app.strategy_library` — catalogue translations, user strategies, AI hypotheses,
  earlier ML discoveries) is executed by :mod:`stock_gplm.app.strategy_engine`: discovery-segment
  parameter optimisation, one validation read, walk-forward folds, regime breakdown. Blocked,
  deferred or inapplicable entries get an explicit row with the reason. Nothing is hard-coded: a new
  JSON file in a library root is picked up by the next run.
* **Catalogue coverage.** Every reference of the hash-bound catalogue (340 today; read dynamically)
  maps to its library interpretation(s). A reference with no interpretation is reported
  ``BLOCKED_IMPLEMENTATION``; registration is never counted as testing.
* **ML discovery.** ``evolve`` (genetic programming over the DSL, discovery segment only), plus the
  existing ``tree`` and ``screen`` methods. Evolved and tree rules are saved into the library as
  ``ML_DISCOVERED`` definitions (deduplicated) so later runs reload and re-execute them.
* **Statistics.** Benjamini-Hochberg over the lane family (every validation test on the lane,
  whatever its origin); the M5.6 gates; one final development-segment read per strategy x lane
  fingerprint, only after the gates pass. A run-wide BH and cross-stock pooled tests are reported in
  the summary. Data never extends past ``m56.development_end`` (the locked holdout stays closed).

Durability: per-strategy results are cached by (symbol, timeframe, data fingerprint, definition
SHA, engine contract, config SHA), so a rerun or a library change recomputes only what changed.
Lanes run in a process pool (``--workers``); the main process ingests and commits.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import time
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from scipy import stats as sps

from ..m54.backtest import bh_adjust
from .compute import Backend
from .context import App
from .db import sha, utc_now
from .ingest import SymbolData, ingest_many, normalize_symbol, window_data
from .lanes import all_bars, build_lane
from .m56 import _bundle_sha
from .m56_catalogue import CataloguePolicy, load_catalogue
from .m56_status import GatePolicy, derive, validation_evidence
from . import strategy_dsl as dsl
from .strategy_engine import (ENGINE_CONTRACT, TRADE_FIELDS, LaneInputs, _compact, evaluate_strategy, final_read,
                              swing_settings,
                              lane_regimes)
from .strategy_library import StrategyDef, StrategyLibrary, normalize, select, validate

CONTRACT = "stock-gplm.research-matrix.v2"
COLS = ["source", "origin", "symbol", "timeframe", "method", "strategy_id", "strategy_version", "reference_id",
        "interpretation", "category", "style", "status", "classification", "validation_status",
        "final_segment_status", "block_reason", "required_inputs", "direction", "position_basis", "fidelity", "params",
        "grid_size", "discovery_trades", "discovery_mean_net", "val_trades", "val_mean_net", "val_excess_mean",
        "val_excess_sd", "val_win_rate", "val_max_drawdown", "val_sharpe", "val_profit_factor", "val_total_net",
        "val_p_value", "val_q_value", "bh_family_size", "final_trades", "final_mean_net", "final_max_drawdown",
        "regime_perf", "entry_rule", "exit_rule", "rule", "assumptions", "reasons", "replay", "definition_file",
        "library_id", "device", "data_start", "data_end", "data_fingerprint", "strategy_sha256", "code_sha", "run_id",
        "created_utc"]
LANE_COLS = ["symbol", "timeframe", "status", "bars", "library_tested", "library_blocked", "catalogue_tested",
             "catalogue_blocked", "ml_strategies", "ml_status", "cached_results", "seconds", "data_start", "data_end",
             "data_fingerprint", "run_id", "created_utc"]
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS matrix ({", ".join(c + " TEXT" for c in COLS)}, row_key TEXT PRIMARY KEY, lane_key TEXT);
CREATE INDEX IF NOT EXISTS matrix_status ON matrix(source, status, classification);
CREATE INDEX IF NOT EXISTS matrix_lane ON matrix(symbol, timeframe);
CREATE TABLE IF NOT EXISTS matrix_superseded ({", ".join(c + " TEXT" for c in COLS)}, row_key TEXT, lane_key TEXT,
  superseded_utc TEXT);
CREATE TABLE IF NOT EXISTS lanes ({", ".join(c + " TEXT" for c in LANE_COLS)}, lane_key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS final_reads (strategy_id TEXT, symbol TEXT, timeframe TEXT, data_fingerprint TEXT,
  run_id TEXT, created_utc TEXT, PRIMARY KEY(strategy_id, symbol, timeframe, data_fingerprint));
CREATE TABLE IF NOT EXISTS catalogue (reference_id TEXT PRIMARY KEY, category TEXT, name TEXT, sha256 TEXT,
  implementation_status TEXT, supported_timeframes TEXT, required_inputs TEXT, rule_text TEXT);
CREATE TABLE IF NOT EXISTS strategy_cache (cache_key TEXT PRIMARY KEY, symbol TEXT, timeframe TEXT, strategy_id TEXT,
  result_json TEXT, created_utc TEXT);
"""
EXCLUDE_JSON = ("_mask", "_xmask", "exit", "segs")


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
        self._migrate()
        self.db.executescript(_SCHEMA)

    def _migrate(self) -> None:
        """Add columns introduced by v2 to a v1 store; v1 rows keep NULL in new columns."""
        for table, cols in (("matrix", COLS + ["lane_key"]), ("lanes", LANE_COLS), ("catalogue", ["rule_text"])):
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue
            for c in cols:
                if c not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {c} TEXT")
        self.db.commit()

    def done(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM lanes WHERE lane_key=?", (key,)).fetchone() is not None

    def consumed(self, symbol: str) -> set[tuple[str, str, str]]:
        return {(r[0], r[1], r[2]) for r in self.db.execute(
            "SELECT strategy_id, timeframe, data_fingerprint FROM final_reads WHERE symbol=?", (symbol,))}

    def cache_for(self, symbol: str) -> dict[str, dict[str, Any]]:
        return {r[0]: json.loads(r[1]) for r in self.db.execute(
            "SELECT cache_key, result_json FROM strategy_cache WHERE symbol=?", (symbol,))}

    def register_catalogue(self, refs: Sequence[Any]) -> None:
        with self.db:
            for r in refs:
                self.db.execute("INSERT OR REPLACE INTO catalogue VALUES (?,?,?,?,?,?,?,?)",
                                (r.reference_id, f"{r.category_number}: {r.category}", r.name, r.digest,
                                 r.implementation_status, ",".join(r.supported_timeframes),
                                 ",".join(r.required_inputs), r.rule_text or ""))

    def commit(self, key: str, lane: dict[str, Any], rows: list[dict[str, Any]],
               finals: list[tuple[str, str, str, str]], cache_rows: list[tuple[str, str, str, str, str]],
               run_id: str) -> None:
        with self.db:
            prev = [r[0] for r in self.db.execute("SELECT lane_key FROM lanes WHERE symbol=? AND timeframe=?",
                                                  (lane["symbol"], lane["timeframe"]))]
            if prev:
                q = ",".join("?" * len(prev))
                self.db.execute(f"INSERT INTO matrix_superseded SELECT *, ? FROM matrix WHERE lane_key IN ({q})",
                                (utc_now(), *prev))
                self.db.execute(f"DELETE FROM matrix WHERE lane_key IN ({q})", prev)
                self.db.execute(f"DELETE FROM lanes WHERE lane_key IN ({q})", prev)
            for f in finals:
                self.db.execute("INSERT OR IGNORE INTO final_reads VALUES (?,?,?,?,?,?)", (*f, run_id, utc_now()))
            for c in cache_rows:
                self.db.execute("INSERT OR REPLACE INTO strategy_cache VALUES (?,?,?,?,?,?)", (*c, utc_now()))
            for r in rows:
                rk = sha({"lane": key, "source": r["source"], "method": r.get("method"), "id": r["strategy_id"]})
                self.db.execute(f"INSERT OR REPLACE INTO matrix VALUES ({','.join('?' * (len(COLS) + 2))})",
                                [_cell(r.get(c)) for c in COLS] + [rk, key])
            self.db.execute(f"INSERT OR REPLACE INTO lanes VALUES ({','.join('?' * (len(LANE_COLS) + 1))})",
                            [_cell(lane.get(c)) for c in LANE_COLS] + [key])


def _cell(v: Any) -> Any:
    if v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (np.integer, np.floating)):
        return _cell(v.item())
    if isinstance(v, (np.bool_, bool)):
        return int(v)
    return json.dumps(v, default=str, sort_keys=True)


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items() if k not in EXCLUDE_JSON}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return float(x) if math.isfinite(float(x)) else None
    if isinstance(x, np.bool_):
        return bool(x)
    return x


# --------------------------------------------------------------------------- lane computation (worker-safe)


def _ctx_for(bars: Any, tf: str, lane: Any, data_bars: Mapping[str, Any], refs: Mapping[str, SymbolData],
             benchmark: str | None) -> dsl.Context:
    from ..m54.timeframes import INTRADAY_MINUTES

    def htf(t: str) -> dsl.Context | None:
        b = data_bars.get(t)
        if b is None or not len(b):
            return None
        return dsl.Context.from_frame(b.frame, t, t in INTRADAY_MINUTES)

    ref_cache: dict[str, Any] = {}

    def ref(sym: str, t: str) -> dsl.Context | None:
        d = refs.get(sym)
        if d is None:
            return None
        if sym not in ref_cache:
            ref_cache[sym] = all_bars(d)
        b = ref_cache[sym].get(t)
        if b is None or not len(b):
            return None
        return dsl.Context.from_frame(b.frame, t, t in INTRADAY_MINUTES)

    feats = {n: lane.X[:, i] for i, n in enumerate(lane.names)}
    return dsl.Context.from_frame(bars.frame, tf, tf in INTRADAY_MINUTES, features=feats, benchmark_loader=ref,
                                  htf_loader=htf, default_benchmark=benchmark or dsl.Context.default_benchmark)


def _replay(s: StrategyDef, symbol: str, tf: str, data_fp: str) -> bool:
    p = s.doc.get("provenance") or {}
    return p.get("lane") == {"symbol": symbol, "timeframe": tf, "data_fingerprint": data_fp}


def _row_from_result(base: dict[str, Any], s: StrategyDef, res: Mapping[str, Any], ref_text: str = "") -> dict[str, Any]:
    d = s.doc
    met = res.get("met") or {}
    row = {**base, "source": "CATALOGUE" if s.reference_id else ("ML" if s.origin == "ML_DISCOVERED" else "LIBRARY"),
           "origin": s.origin, "method": (d.get("provenance") or {}).get("method", "library"), "strategy_id": s.id,
           "strategy_version": s.version, "reference_id": s.reference_id, "interpretation": d.get("interpretation", "A"),
           "category": d.get("category", ""), "style": d.get("style", ""), "status": res.get("status"),
           "block_reason": res.get("block_reason", ""), "required_inputs": ",".join(d.get("requires") or []),
           "direction": s.direction, "position_basis": res.get("position_basis"), "fidelity": d.get("fidelity", ""),
           "params": res.get("params"), "grid_size": res.get("grid_size"), "discovery_trades": res.get("discovery_trades"),
           "discovery_mean_net": res.get("discovery_mean_net"), "entry_rule": d.get("entry", ""),
           "exit_rule": res.get("exit_text") or json.dumps(d.get("exit"), sort_keys=True), "rule": ref_text[:300],
           "assumptions": "; ".join(d.get("assumptions") or []), "definition_file": s.path, "library_id": s.id,
           "strategy_sha256": s.sha256, "classification": "NOT_TESTED", "validation_status": "NOT_EVALUATED",
           "final_segment_status": "NOT_SCORED", "regime_perf": res.get("regimes"),
           "device": "cpu (NumPy DSL engine)"}
    if res.get("status") == "TESTED":
        row.update(val_trades=met.get("effective_n"), val_mean_net=met.get("mean_net"),
                   val_excess_mean=met.get("excess_mean"), val_excess_sd=met.get("excess_sd"),
                   val_win_rate=met.get("win_rate"), val_max_drawdown=met.get("max_drawdown"),
                   val_sharpe=met.get("sharpe_annualized"), val_profit_factor=met.get("profit_factor"),
                   val_total_net=met.get("total_net_compounded"), val_p_value=met.get("p_value"))
    return row


def compute_lane(job: Mapping[str, Any]) -> dict[str, Any]:
    """Pure lane computation (runs in a worker). Returns rows, final reads, cache rows and ML discoveries."""
    from .strategy_evolve import Evolver, genome_doc

    t0 = time.monotonic()
    data: SymbolData = job["data"]
    tf: str = job["timeframe"]
    gates: GatePolicy = job["gates"]
    bars = all_bars(data)
    lane = build_lane(data, tf, job["costs"], bars=bars, cache_dir=job.get("feature_cache")) \
        if tf in bars and len(bars[tf]) else None
    daily = data.daily
    base = {"symbol": data.symbol, "timeframe": tf,
            "data_start": str(daily["date"].min()) if len(daily) else None,
            "data_end": str(daily["date"].max()) if len(daily) else None,
            "data_fingerprint": lane.data_fingerprint if lane is not None else data.fingerprint,
            "code_sha": job["code_sha"], "run_id": job["run_id"], "created_utc": utc_now()}
    strategies = [_rebuild(d) for d in job["strategies"]]
    refs_text: Mapping[str, str] = job["ref_text"]
    rows: list[dict[str, Any]] = []
    cache_rows: list[tuple[str, str, str, str, str]] = []
    finals: list[tuple[str, str, str, str]] = []
    discovered: list[dict[str, Any]] = []
    tested: list[tuple[dict[str, Any], StrategyDef, dict[str, Any]]] = []
    audit_cfg = job.get("audit") or {}
    want_trades = audit_cfg.get("trades", "validation_final") != "none"
    ledger: list[list[Any]] = []
    ml_log: list[dict[str, Any]] = []

    def keep_trades(s: StrategyDef, res: Mapping[str, Any], segment: str, trades: Sequence[Any]) -> None:
        if not want_trades or li is None:
            return
        m = li.market
        for t in trades:
            tt = list(t) if isinstance(t, (list, tuple)) else _compact(t)
            ledger.append([s.id, s.sha256[:16], s.direction, json.dumps(res.get("params") or {}, sort_keys=True),
                           segment, m.label(tt[0]), m.label(tt[0] + 1), m.label(tt[1]), *tt[2:]])
    li = None
    cached_hits = 0
    if lane is not None:
        ctx = _ctx_for(bars[tf], tf, lane, bars, job["refs"], job.get("benchmark"))
        li = LaneInputs(ctx, lane.market, lane.costs.buy, lane.costs.sell, lane.intraday, tf, gates.walk_forward_folds,
                        symbol=data.symbol,
                        audit_discovery=(job.get("audit") or {}).get("trades") == "all")
        regimes = lane_regimes(ctx, lane.market, int(len(lane.market) * 0.6))
        base_cache: dict[Any, Any] = {}
    for s in strategies:
        if tf not in s.timeframes or not s.applies(data.symbol, tf):
            res = {"status": "BLOCKED_TIMEFRAME_SCOPE" if tf not in s.timeframes else "BLOCKED_SYMBOL_SCOPE",
                   "block_reason": f"applicability: timeframes {s.timeframes}, symbols "
                                   f"{(s.doc.get('applicability') or {}).get('symbols', '*')}"}
            if s.blocked:
                res = {"status": s.blocked["status"], "block_reason": s.blocked["reason"]}
            rows.append(_row_from_result(base, s, res, refs_text.get(s.reference_id, "")))
            continue
        if lane is None:
            res = {"status": "BLOCKED_MISSING_DATA", "block_reason": f"no accepted {tf} candles in the window"}
            if s.blocked:
                res = {"status": s.blocked["status"], "block_reason": s.blocked["reason"]}
        else:
            ck = sha({"sym": data.symbol, "tf": tf, "fp": data.fingerprint, "s": s.sha256, "e": ENGINE_CONTRACT,
                      "cfg": job["config_sha"]})
            hit = job["cache"].get(ck)
            if hit is not None:
                res, cached_hits = hit, cached_hits + 1
            else:
                res = evaluate_strategy(s, li, base_cache, regimes)
                cache_rows.append((ck, data.symbol, tf, s.id, json.dumps(_jsonable(res), default=str)))
        row = _row_from_result(base, s, res, refs_text.get(s.reference_id, ""))
        if res.get("status") == "TESTED":
            replay = _replay(s, data.symbol, tf, lane.data_fingerprint)
            row["replay"] = int(replay)
            tested.append((row, s, res))
            for seg_name, trs in (res.get("trades") or {}).items():
                keep_trades(s, res, seg_name, trs)
        rows.append(row)
    ml_status: list[str] = []
    if lane is not None and "evolve" in job["methods"]:
        segs = _dev_segments(len(lane.market))
        if segs is None:
            ml_status.append("evolve:BLOCKED_INSUFFICIENT_HISTORY")
        else:
            known = {s.fingerprint() for s in strategies}
            ev = Evolver(li, segs, job["seed"], job["budget"], allow_short=True)
            out = ev.run()
            if audit_cfg.get("ml_log", True):
                ml_log.extend({"method": "evolve", **x} for x in out.get("log", []))
            prov = {"lane": {"symbol": data.symbol, "timeframe": tf, "data_fingerprint": lane.data_fingerprint},
                    "seed": job["seed"], "budget": job["budget"], "population": out["population"],
                    "generations": out["generations"], "evaluated": out["evaluated"],
                    "discovery_segment": [lane.market.label(segs[0]), lane.market.label(segs[1] - 1)],
                    "run_id": job["run_id"], "discovered_utc": utc_now(), "code_sha": job["code_sha"]}
            n_new = 0
            for g in out["selected"]:
                doc = genome_doc(g, data.symbol, tf, prov)
                try:  # one rule that cannot be built/evaluated is skipped, never the whole lane
                    sdef = _rebuild({"doc": doc, "path": "(new discovery)", "root": ""})
                    if sdef.fingerprint() in known:
                        continue
                    res = evaluate_strategy(sdef, li, base_cache, regimes)
                except Exception as exc:  # noqa: BLE001
                    ml_status.append(f"evolve-skip:{type(exc).__name__}")
                    continue
                row = _row_from_result(base, sdef, res)
                row.update(source="ML", method="evolve", device="cpu (NumPy GP over the DSL)", replay=0)
                if res.get("status") == "TESTED":
                    tested.append((row, sdef, res))
                    for seg_name, trs in (res.get("trades") or {}).items():
                        keep_trades(sdef, res, seg_name, trs)
                rows.append(row)
                discovered.append({"doc": doc, "row_index": len(rows) - 1})
                n_new += 1
            ml_status.append(f"evolve:COMPLETED({out['evaluated']} genomes, {n_new} selected)")
    if lane is not None and "llm" in job["methods"]:
        segs = _dev_segments(len(lane.market))
        if segs is None:
            ml_status.append("llm:BLOCKED_INSUFFICIENT_HISTORY")
        else:
            from .strategy_llm import llm_doc, propose
            known = {s.fingerprint() for s in strategies}
            ev = Evolver(li, segs, job["seed"], job["budget"], allow_short=False)
            try:
                out = propose(ev, data.symbol, tf, job["budget"])
            except Exception as exc:  # model down etc.: the lane's other results still count
                out = None
                ml_status.append(f"llm:FAILED({str(exc)[:80]})")
            if out is not None:
                if audit_cfg.get("ml_log", True):
                    ml_log.extend({"method": "llm", **x} for x in out.get("log", []))
                prov = {"lane": {"symbol": data.symbol, "timeframe": tf, "data_fingerprint": lane.data_fingerprint},
                        "seed": job["seed"], "budget": job["budget"], "calls": out["calls"],
                        "evaluated": out["evaluated"], "facts": out["facts"],
                        "discovery_segment": [lane.market.label(segs[0]), lane.market.label(segs[1] - 1)],
                        "run_id": job["run_id"], "discovered_utc": utc_now(), "code_sha": job["code_sha"]}
                n_new = 0
                for g in out["selected"]:
                    doc = llm_doc(genome_doc(g, data.symbol, tf, prov), out["why"].get(g.key(), ""), out["model"])
                    try:  # one unusable proposal is skipped, never the whole lane
                        sdef = _rebuild({"doc": doc, "path": "(new discovery)", "root": ""})
                        if sdef.fingerprint() in known:
                            continue
                        res = evaluate_strategy(sdef, li, base_cache, regimes)
                    except Exception as exc:  # noqa: BLE001
                        ml_status.append(f"llm-skip:{type(exc).__name__}")
                        continue
                    row = _row_from_result(base, sdef, res)
                    row.update(source="ML", method="llm", device=f"local LLM {out['model']} + NumPy backtest",
                               replay=0)
                    if res.get("status") == "TESTED":
                        tested.append((row, sdef, res))
                        for seg_name, trs in (res.get("trades") or {}).items():
                            keep_trades(sdef, res, seg_name, trs)
                    rows.append(row)
                    discovered.append({"doc": doc, "row_index": len(rows) - 1})
                    n_new += 1
                ml_status.append(f"llm:COMPLETED({out['calls']} calls, {out['evaluated']} rules, {n_new} selected)")
    # Lane-wide multiple testing over every fresh validation test (replays excluded: their read was counted
    # in the run that discovered them).
    fam = [t for t in tested if not t[0].get("replay")]
    family = max(1, len(fam))
    qvals = bh_adjust([t[2]["met"].get("p_value") for t in fam], family)
    for (row, s, res), qv in zip(fam, qvals):
        met = dict(res["met"])
        met.update(q_value=qv, bh_family_size=family)
        row.update(val_q_value=qv, bh_family_size=family)
        st = derive({"evidence_stage": "VALIDATION", "classification": "", "metrics": met}, gates)
        if st["validation_status"] == "PASSED":
            if (s.id, tf, lane.data_fingerprint) in job["consumed"]:
                st["reasons"].append("final segment already read for this fingerprint; not re-read")
            else:
                params = res.get("params") or {}
                full = res if "_mask" in res else evaluate_strategy(s, li, {}, None)
                if full.get("status") == "TESTED" and full.get("params") == params:
                    fm = final_read(full, li, {})
                    keep_trades(s, full, "final", fm.pop("_trades", []))
                    fm["validation_evidence"] = validation_evidence(met)
                    st = derive({"evidence_stage": "TEST_ONCE", "classification": "", "metrics": fm}, gates)
                    row.update(final_trades=fm.get("effective_n"), final_mean_net=fm.get("mean_net"),
                               final_max_drawdown=fm.get("max_drawdown"))
                    finals.append((s.id, data.symbol, tf, lane.data_fingerprint))
        cls = st["classification"]
        reasons = [r for r in st["reasons"] if not r.startswith("recorded label")]
        if cls == "REJECTED" and _promising(met, gates):
            cls = "DISCOVERED"
            reasons.insert(0, "PROMISING research candidate: positive cost-adjusted excess return with p < 0.10, "
                              "but not all validation gates passed (not validated)")
        row.update(classification=cls, validation_status=st["validation_status"],
                   final_segment_status=st["final_segment_status"], reasons="; ".join(reasons))
    for row, s, res in tested:
        if row.get("replay"):
            row.update(classification="REPLAY", validation_status="REPLAY_NOT_NEW_EVIDENCE",
                       reasons="strategy was discovered on this exact lane data; its validation read was counted "
                               "(and BH-adjusted) in the discovery run, so this row is not new evidence")
    for d in discovered:
        r = rows[d["row_index"]]
        d["doc"]["evidence"] = {k: r.get(k) for k in ("classification", "validation_status", "final_segment_status",
                                                     "val_trades", "val_mean_net", "val_win_rate", "val_max_drawdown",
                                                     "val_sharpe", "val_p_value", "val_q_value", "bh_family_size",
                                                     "final_trades", "final_mean_net", "reasons", "run_id")}
        d["doc"]["enabled"] = r.get("classification") != "REJECTED"
    lib_tested = sum(1 for r in rows if r["status"] == "TESTED" and r["source"] != "CATALOGUE")
    cat_rows = [r for r in rows if r["source"] == "CATALOGUE"]
    lane_row = {**base, "status": "COMPLETED" if lane is not None else "NO_DATA",
                "bars": len(lane.market) if lane is not None else 0,
                "library_tested": lib_tested, "library_blocked": sum(1 for r in rows if r["source"] == "LIBRARY"
                                                                     and r["status"] != "TESTED"),
                "catalogue_tested": sum(r["status"] == "TESTED" for r in cat_rows),
                "catalogue_blocked": sum(r["status"] != "TESTED" for r in cat_rows),
                "ml_strategies": len(discovered), "ml_status": ",".join(ml_status), "cached_results": cached_hits,
                "seconds": round(time.monotonic() - t0, 2)}
    return {"rows": rows, "lane": lane_row, "finals": finals, "cache_rows": cache_rows, "discovered": discovered,
            "audit": {"trades": ledger, "ml_log": ml_log},
            "lane_obj": lane if job.get("return_lane") else None, "key": job["key"]}


def _promising(met: Mapping[str, Any], gates: GatePolicy) -> bool:
    n, mean, ex, p = met.get("effective_n") or 0, met.get("mean_net"), met.get("excess_mean"), met.get("p_value")
    return bool(n >= max(8, gates.min_trades // 2) and mean is not None and mean > 0 and ex is not None and ex > 0
                and p is not None and p < 0.10)


def _dev_segments(n: int) -> tuple[int, int] | None:
    return (0, int(n * 0.60)) if n >= 150 else None


def _rebuild(d: Mapping[str, Any]) -> StrategyDef:
    doc = normalize(d["doc"])
    enode, xnode, needs = validate(doc)
    from .strategy_library import sha as ssha
    sdef = StrategyDef(doc, d.get("path", ""), d.get("root", ""), enode, xnode, int(d.get("version") or 1),
                       d.get("sha256") or ssha(doc), bool(d.get("enabled", True)), needs)
    return sdef


# --------------------------------------------------------------------------- library helpers


def library_roots(app: App, extra: Sequence[str] | None = None) -> tuple[list[Path], Path]:
    from .settings import REPO

    cfg = app.s.section("research")
    roots = [Path(p) for p in (cfg.get("strategy_library_roots") or [str(REPO / "config" / "strategies" / "library")])]
    roots = [(p if p.is_absolute() else (app.s.path.parent / p).resolve()) for p in roots]
    roots += [Path(p).resolve() for p in (extra or [])]
    writable = Path(cfg.get("strategy_library_home") or (app.s.home / "strategy-library"))
    return roots, writable


def _ref_symbols(strategies: Sequence[StrategyDef], benchmark: str | None) -> set[str]:
    out: set[str] = set()

    def walk(n: Any) -> None:
        if isinstance(n, tuple) and n:
            if n[0] == "call" and n[1] in ("ref_symbol", "bench"):
                args = n[2]
                if n[1] == "ref_symbol" and args and args[0][0] == "str":
                    out.add(args[0][1])
                elif n[1] == "bench":
                    if len(args) > 1 and args[1][0] == "str":
                        out.add(args[1][1])
                    else:  # bench() without a symbol: configured benchmark, else the official Nifty 50
                        out.add(benchmark or dsl.Context.default_benchmark)
            for c in n[1:]:
                walk(c)
    for s in strategies:
        if s.blocked:
            continue
        walk(s.entry_node)
        walk(s.exit_node)
    return {normalize_symbol(dsl.resolve_symbol(x)) for x in out}


# --------------------------------------------------------------------------- orchestration


def run(app: App, symbols: Sequence[str], timeframes: Sequence[str], backend: Backend, *, catalogue: Path,
        methods: Sequence[str], out: Path, budget: str = "quick", start: str | None = None,
        end: str | None = None, universe_basis: str = "", catalogue_enabled: bool = True,
        strategy_patterns: Sequence[str] | None = None, library_extra: Sequence[str] | None = None,
        workers: int = 1, include_disabled: bool = False) -> dict[str, Any]:
    from .ml_discovery import _screen_lane, _tree_lane

    cfg = app.s.section("m56")
    rcfg = app.s.section("research")
    dev_end = cfg.get("development_end")
    if dev_end == "latest":  # owner 2026-11-20: no date lock, use every candle up to today
        from datetime import date as _d
        dev_end = _d.today().isoformat()
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
    ref_text = {r.reference_id: r.rule_text or "" for r in refs}
    bundle = _bundle_sha(catalogue, supplements)
    roots, writable = library_roots(app, library_extra)
    lib = StrategyLibrary(roots, writable)
    loaded = lib.load()
    pool = loaded.strategies
    if not catalogue_enabled:
        pool = {k: v for k, v in pool.items() if not v.reference_id}
    pool = select(pool, strategy_patterns)
    if not include_disabled:
        pool = {k: v for k, v in pool.items() if v.enabled}
    benchmark = rcfg.get("benchmark_symbol")  # None: no index series registered; bench() reports missing data
    audit_cfg = {"trades": "validation_final", "ml_log": True, **(rcfg.get("audit") or {})}
    if audit_cfg["trades"] not in ("none", "validation_final", "all"):
        raise ValueError("research.audit.trades must be none, validation_final or all")
    seed = int(app.s.section("models").get("seed", 55)) + 1
    store = Matrix(out)
    store.register_catalogue(refs)
    lib_summary = loaded.summary()
    meta = {"contract": CONTRACT, "engine": ENGINE_CONTRACT, "dsl": dsl.DSL_CONTRACT,
            "window": {"start": start, "end": end, "development_end": dev_end},
            "universe_basis": universe_basis, "symbols": len(symbols), "timeframes": list(timeframes),
            "catalogue_enabled": catalogue_enabled, "ml_methods": list(methods), "budget": budget,
            "catalogue": {"path": str(catalogue), "bundle_sha256": bundle, "references": len(refs),
                          "report_status": parsed.report()["status"]},
            "library": {"roots": [str(r) for r in lib.roots], "writable": str(writable), **lib_summary,
                        "selected": len(pool), "patterns": list(strategy_patterns or []),
                        "invalid": loaded.invalid, "new_or_changed": loaded.changes[:200]},
            "gate_policy": gates.to_dict(), "costs": app.s.section("costs"), "config_sha256": app.s.sha256,
            "device": backend.label, "workers": workers, "benchmark_symbol": benchmark,
            "audit": {**audit_cfg, "dir": str(out / "audit"), "contract": AUDIT_CONTRACT}}
    (out / "run-config.json").write_text(json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n",
                                         encoding="utf-8")
    (out / "catalogue-report.json").write_text(json.dumps(parsed.report(), indent=2, default=str) + "\n",
                                               encoding="utf-8")
    app.db.event("RESEARCH_START", {"out": str(out), **meta["window"], "symbols": len(symbols),
                                    "library": len(pool), "methods": list(methods)})
    app.progress.set_total(len(symbols) * len(timeframes))
    use_minute = any(tf.endswith("m") for tf in timeframes)
    ref_syms = _ref_symbols(list(pool.values()), benchmark)
    ref_data: dict[str, SymbolData] = {}
    for rs in sorted(ref_syms):
        try:
            d = next(iter(ingest_many(app.s, app.db, [rs], use_minute=use_minute).values()), None)
            if d is not None:
                ref_data[rs] = window_data(d, start, end)
        except Exception as exc:  # recorded; strategies needing it report BLOCKED_MISSING_DATA
            app.db.event("RESEARCH_REF_SYMBOL_FAILED", {"symbol": rs, "error": str(exc)}, level="WARN")
    lanes_status: dict[str, int] = {}
    saved = {"created": 0, "deduplicated": 0}
    ex = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    pending: list[tuple[Future, dict[str, Any]]] = []
    ml_inline = [m for m in methods if m in ("tree", "screen")]

    def finish(res: dict[str, Any], job: dict[str, Any]) -> None:
        rows, finals = res["rows"], list(res["finals"])
        for d in res["discovered"]:
            sid, created = lib.save_discovered(d["doc"])
            saved["created" if created else "deduplicated"] += 1
            r = rows[d["row_index"]]
            r["library_id"] = sid
            r["definition_file"] = str(writable / "discovered" / "ml" / f"{sid}.json")
            if sid != r["strategy_id"]:
                r["reasons"] = (r.get("reasons") or "") + f"; equivalent to existing library strategy {sid}"
        if ml_inline and job["data"] is not None:
            _ml_inline(app, backend, job, res, rows, finals, gates, cfg, seed, store, out, lib, ml_inline, budget)
        write_audit(out, job["key"], res["lane"], res.get("audit") or {}, job.get("audit") or {})
        store.commit(job["key"], res["lane"], rows, finals, res["cache_rows"], app.run_id or "")
        lanes_status[res["lane"]["status"]] = lanes_status.get(res["lane"]["status"], 0) + 1
        app.progress.update(task="research", stock=job["data"].symbol, timeframe=job["timeframe"])
        app.progress.advance()

    def drain(block_all: bool) -> None:
        while pending and (block_all or len(pending) >= workers * 2):
            fut, job = pending.pop(0)
            try:
                finish(fut.result(), job)
            except Exception as exc:  # one bad lane never stops the run
                app.db.event("RESEARCH_LANE_FAILED", {"symbol": job["data"].symbol, "tf": job["timeframe"],
                                                      "error": f"{type(exc).__name__}: {exc}",
                                                      "trace": __import__("traceback").format_exc()[-1500:]}, level="ERROR")
                lanes_status["FAILED"] = lanes_status.get("FAILED", 0) + 1
                app.progress.advance()

    try:
        for symbol in symbols:
            try:
                loaded_d = ingest_many(app.s, app.db, [symbol], use_minute=use_minute)
            except Exception as exc:  # recorded; one bad symbol never stops the run
                loaded_d = {}
                app.db.event("RESEARCH_SYMBOL_FAILED", {"symbol": symbol, "error": str(exc)}, level="WARN")
            data = next(iter(loaded_d.values()), None)
            data = window_data(data, start, end) if data is not None else None
            consumed = store.consumed(symbol)
            cache = store.cache_for(symbol)
            for tf in timeframes:
                if data is None:
                    lanes_status["NO_DATA"] = lanes_status.get("NO_DATA", 0) + 1
                    app.progress.advance()
                    continue
                appl = [s for s in pool.values() if tf in s.timeframes and s.applies(symbol, tf)]
                scope_rows = [s for s in pool.values() if s.reference_id and s not in appl]
                lane_set = sorted(s.sha256 for s in appl if not _replay_any(s, symbol, tf, data.fingerprint))
                key = sha({"symbol": symbol, "tf": tf, "data": data.fingerprint, "lib": sha(lane_set),
                           "exits": swing_settings(symbol, tf),
                           "config": app.s.sha256, "methods": sorted(methods), "budget": budget,
                           "engine": ENGINE_CONTRACT})
                if store.done(key):
                    lanes_status["REUSED"] = lanes_status.get("REUSED", 0) + 1
                    app.progress.advance()
                    continue
                job = {"data": data, "timeframe": tf, "gates": gates, "costs": app.s.section("costs"),
                       "feature_cache": app.s.cache_dir / "features", "code_sha": app.code_sha, "run_id": app.run_id,
                       "strategies": [{"doc": s.doc, "path": s.path, "root": s.root, "version": s.version,
                                       "sha256": s.sha256, "enabled": s.enabled} for s in appl + scope_rows],
                       "ref_text": ref_text, "refs": ref_data, "benchmark": benchmark, "cache": cache,
                       "config_sha": app.s.sha256, "consumed": consumed, "methods": list(methods), "seed": seed,
                       "budget": budget, "key": key, "return_lane": bool(ml_inline) and ex is None,
                       "audit": audit_cfg}
                if ex is None:
                    try:
                        finish(compute_lane(job), job)
                    except Exception as exc:
                        app.db.event("RESEARCH_LANE_FAILED", {"symbol": symbol, "tf": tf,
                                                              "error": f"{type(exc).__name__}: {exc}",
                                                      "trace": __import__("traceback").format_exc()[-1500:]}, level="ERROR")
                        lanes_status["FAILED"] = lanes_status.get("FAILED", 0) + 1
                        app.progress.advance()
                else:
                    pending.append((ex.submit(compute_lane, job), job))
                    drain(False)
        drain(True)
    finally:
        if ex is not None:
            ex.shutdown(wait=True)
    meta["library_saved"] = saved
    summary = write_summary(store, meta, gates)
    app.db.event("RESEARCH_DONE", {"out": str(out), "lanes": lanes_status, "saved": saved})
    return {"status": "COMPLETED", "out": str(out), "lanes": lanes_status, "library_saved": saved, **summary}


def _replay_any(s: StrategyDef, symbol: str, tf: str, data_fp: str) -> bool:
    lane = (s.doc.get("provenance") or {}).get("lane") or {}
    return lane.get("symbol") == symbol and lane.get("timeframe") == tf


def _ml_inline(app: App, backend: Backend, job: Mapping[str, Any], res: Mapping[str, Any], rows: list[dict[str, Any]],
               finals: list[tuple[str, str, str, str]], gates: GatePolicy, cfg: Mapping[str, Any], seed: int,
               store: Matrix, out: Path, lib: StrategyLibrary, methods: Sequence[str], budget: str) -> None:
    """Legacy ML methods (sklearn tree paths, GPU screen). Tree rules are also saved to the library."""
    from .ml_discovery import _screen_lane, _tree_lane

    data, tf = job["data"], job["timeframe"]
    lane = res.get("lane_obj")
    bars = all_bars(data)
    if lane is None:
        lane = build_lane(data, tf, app.s.section("costs"), bars=bars, cache_dir=app.s.cache_dir / "features") \
            if tf in bars and len(bars[tf]) else None
    base = {k: res["lane"].get(k) for k in ("symbol", "timeframe", "data_start", "data_end", "data_fingerprint",
                                            "run_id", "created_utc")}
    base["code_sha"] = app.code_sha
    status = [res["lane"].get("ml_status") or ""]
    for method in methods:
        if lane is None:
            status.append(f"{method}:BLOCKED_MISSING_DATA")
            continue
        if method == "tree":
            consumed = ("ml-tree-lane", tf, lane.data_fingerprint) in job["consumed"]
            info, mrows, read = _tree_lane(lane, dict(cfg), seed, not consumed, gates, out, dict(base))
            if read:
                finals.append(("ml-tree-lane", data.symbol, tf, lane.data_fingerprint))
            for r in mrows:
                doc = _tree_to_dsl(r, data.symbol, tf, lane.data_fingerprint, app.run_id or "", out)
                if doc is None:
                    r["reasons"] = (r.get("reasons") or "") + "; tree definition not found: not saved to library"
                if doc is not None:
                    try:
                        sid, _ = lib.save_discovered(doc)
                        r["library_id"] = sid
                    except Exception as exc:  # a rule that cannot be expressed stays in the matrix only
                        r["reasons"] = (r.get("reasons") or "") + f"; not saved to library: {exc}"
        else:
            info, mrows, _ = _screen_lane(app, backend, data, tf, bars, budget, gates, out, dict(base))
        status.append(f"{method}:{info['status']}")
        if (job.get("audit") or {}).get("ml_log", True):
            res.setdefault("audit", {}).setdefault("ml_log", []).extend(
                {"method": method, "lane_status": info.get("status"), "strategy_id": r.get("strategy_id"),
                 "rule": r.get("entry_rule") or r.get("rule"), "library_id": r.get("library_id"),
                 "classification": r.get("classification"), "val_trades": r.get("val_trades"),
                 "val_mean_net": r.get("val_mean_net"), "val_p_value": r.get("val_p_value"),
                 "val_q_value": r.get("val_q_value"), "definition_file": r.get("definition_file")} for r in mrows)
        for r in mrows:
            r.update(source="ML", origin="ML_DISCOVERED", status="TESTED", reference_id="", strategy_version=1,
                     block_reason="", category="ML: " + method)
        rows.extend(mrows)
        res["lane"]["ml_strategies"] = int(res["lane"].get("ml_strategies") or 0) + len(mrows)
    res["lane"]["ml_status"] = ",".join(x for x in status if x)


def _tree_to_dsl(row: Mapping[str, Any], symbol: str, tf: str, fp: str, run_id: str,
                 base: Path | None = None) -> dict[str, Any] | None:
    """Translate a tree-path definition into a DSL strategy. ``definition_file`` is stored relative to the run
    directory (``base``); before 2026-11-15 it was resolved against the working directory, so tree rules were
    never found and never saved to the library."""
    raw = row.get("definition_file")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() and base is not None:
        path = Path(base) / path
    if not path.is_file():
        return None
    src = json.loads(Path(path).read_text(encoding="utf-8"))
    conds = (src.get("entry") or {}).get("conditions") or []
    parts = []
    for c in conds:
        f = json.dumps(c["feature"])
        op, th = c["op"], c.get("threshold")
        if op == "gt":
            parts.append(f"feature({f}) > {th}")
        elif op == "lt":
            parts.append(f"feature({f}) < {th}")
        elif op == "cross_above":
            parts.append(f"cross_above(feature({f}), {th})")
        elif op == "cross_below":
            parts.append(f"cross_below(feature({f}), {th})")
        elif op == "is":
            parts.append(f"feature({f}) > 0.5")
    if not parts:
        return None
    bare = symbol[:-3] if symbol.endswith("-EQ") else symbol
    ex = src.get("exit") or {}
    return {"id": f"ML-{bare}-{tf}-tree-{row['strategy_id'][3:13]}", "origin": "ML_DISCOVERED",
            "name": f"Tree rule for {bare} {tf}", "category": "ML: decision-tree path", "style": "DISCOVERED",
            "direction": src.get("direction", "long"), "entry": " and ".join(parts),
            "exit": {"max_hold_bars": int(ex.get("max_hold_bars", 10)), "stop_atr": ex.get("stop_atr"),
                     "target_atr": ex.get("target_atr"), "trail_atr": ex.get("trail_atr")},
            "applicability": {"symbols": [bare], "timeframes": [tf]}, "requires": ["OHLCV", "LANE_FEATURES"],
            "provenance": {"method": "tree", "lane": {"symbol": symbol, "timeframe": tf, "data_fingerprint": fp},
                           "run_id": run_id, "source_definition": str(path)},
            "evidence": {k: row.get(k) for k in ("classification", "validation_status", "val_trades", "val_mean_net",
                                                 "val_q_value", "final_trades", "final_mean_net")},
            "enabled": row.get("classification") != "REJECTED"}


# --------------------------------------------------------------------------- audit trail

AUDIT_CONTRACT = "stock-gplm.research-audit.v1"
TRADE_COLUMNS = ("strategy_id", "strategy_sha16", "direction", "params", "segment", "signal_bar", "entry_bar",
                 "exit_bar") + TRADE_FIELDS[2:]


def _atomic_gz(path: Path, text: str) -> str:
    import gzip

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = text.encode("utf-8")
    with gzip.GzipFile(tmp, "wb", compresslevel=6, mtime=0) as fh:
        fh.write(data)
    os.replace(tmp, path)
    return hashlib.sha256(data).hexdigest()


def write_audit(out: Path, lane_key: str, lane: Mapping[str, Any], audit: Mapping[str, Any],
                cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Per-lane audit files, written atomically *before* the lane is committed (checkpoint-safe).

    ``audit/trades/<SYMBOL>/<tf>.csv.gz``  every validation and final-segment trade (plus discovery trades of
    the chosen configuration with ``research.audit.trades = "all"``) of every tested strategy: bar dates,
    prices, exit reason, gross/net return, MFE/MAE.
    ``audit/ml/<SYMBOL>/<tf>.jsonl.gz``   every ML candidate evaluated: GP genomes (entry, exit, fitness,
    trades, mean/excess net, baseline) and tree/screen candidates with their validation evidence.
    ``audit/index.jsonl``                 one line per committed lane: files, row counts, SHA-256 of contents.
    A rerun of an interrupted lane overwrites its files; a completed (reused) lane is never rewritten.
    """
    import csv
    import io

    sym = str(lane.get("symbol") or "UNKNOWN")
    tf = str(lane.get("timeframe") or "")
    base = out / "audit"
    files: dict[str, Any] = {}
    trades = audit.get("trades") or []
    if cfg.get("trades", "validation_final") != "none" and trades:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(TRADE_COLUMNS)
        w.writerows(trades)
        rel = f"trades/{sym}/{tf}.csv.gz"
        files[rel] = {"rows": len(trades), "sha256": _atomic_gz(base / rel, buf.getvalue())}
    log = audit.get("ml_log") or []
    if cfg.get("ml_log", True) and log:
        rel = f"ml/{sym}/{tf}.jsonl.gz"
        text = "".join(json.dumps(_jsonable(x), default=str, sort_keys=True) + "\n" for x in log)
        files[rel] = {"rows": len(log), "sha256": _atomic_gz(base / rel, text)}
    if files:
        base.mkdir(parents=True, exist_ok=True)
        with (base / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"contract": AUDIT_CONTRACT, "lane_key": lane_key, "symbol": sym, "timeframe": tf,
                                 "data_fingerprint": lane.get("data_fingerprint"), "run_id": lane.get("run_id"),
                                 "files": files, "utc": utc_now()}, sort_keys=True) + "\n")
    return files


# --------------------------------------------------------------------------- reporting


def _pooled(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = np.array([float(r["val_trades"] or 0) for r in rows])
    m = np.array([float(r["val_excess_mean"]) for r in rows])
    s = np.array([float(r["val_excess_sd"] or 0) for r in rows])
    N = n.sum()
    if N < 3:
        return {"n": int(N)}
    mean = float((n * m).sum() / N)
    ss = float(((n - 1).clip(min=0) * s ** 2).sum() + (n * (m - mean) ** 2).sum())
    sd = math.sqrt(ss / (N - 1)) if N > 1 else 0.0
    t = mean / (sd / math.sqrt(N)) if sd > 0 else None
    p = float(sps.t.sf(t, df=N - 1)) if t is not None else None
    return {"n": int(N), "stocks": len(rows), "pooled_excess_mean": mean, "t_stat": t, "p_value": p,
            "positive_fraction": float((m > 0).mean())}


def write_summary(store: Matrix, meta: dict[str, Any], gates: GatePolicy | None = None) -> dict[str, Any]:
    gates = gates or GatePolicy()
    db = store.db
    q = lambda sql, a=(): [dict(r) for r in db.execute(sql, a)]  # noqa: E731
    by_status = q("SELECT source, status, COUNT(*) n FROM matrix GROUP BY 1, 2 ORDER BY 1, 3 DESC")
    by_class = q("SELECT source, classification, COUNT(*) n FROM matrix WHERE status='TESTED' GROUP BY 1, 2 "
                 "ORDER BY 1, 3 DESC")
    by_origin = q("SELECT origin, status, COUNT(*) n FROM matrix GROUP BY 1, 2 ORDER BY 1, 3 DESC")
    refs_total = db.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0]
    refs_exec = db.execute("SELECT COUNT(DISTINCT reference_id) FROM matrix WHERE reference_id <> '' AND "
                           "status='TESTED'").fetchone()[0]
    methods_run = {m.split(":")[0] for r in q("SELECT ml_status FROM lanes") for m in (r["ml_status"] or "").split(",")
                   if "COMPLETED" in m}
    cov = []
    for c in q("SELECT reference_id, name, category FROM catalogue ORDER BY rowid"):
        rr = q("SELECT status, block_reason, strategy_id FROM matrix WHERE reference_id=?", (c["reference_id"],))
        statuses = {r["status"] for r in rr}
        if "TESTED" in statuses:
            st, why = "EXECUTED", ""
        elif rr:
            first = next(r for r in rr)
            st, why = first["status"], first["block_reason"]
            if st == "BLOCKED_METHOD":
                spec = q("SELECT block_reason FROM matrix WHERE reference_id=? LIMIT 1", (c["reference_id"],))
                if any(m in (why or "") for m in methods_run):
                    st = "EXECUTED_AS_DISCOVERY_METHOD"
        else:
            st, why = "BLOCKED_IMPLEMENTATION", "no library interpretation applicable to the requested lanes"
        cov.append({**c, "coverage": st, "reason": why, "interpretations": len({r["strategy_id"] for r in rr}),
                    "lanes_tested": sum(1 for r in rr if r["status"] == "TESTED")})
    cov_counts: dict[str, int] = {}
    for c in cov:
        cov_counts[c["coverage"]] = cov_counts.get(c["coverage"], 0) + 1
    _write_csv(store.out / "catalogue_coverage.csv", cov)
    # Run-wide BH across every fresh validation test in the run (all lanes, all origins)
    tested = q("SELECT row_key, val_p_value FROM matrix WHERE status='TESTED' AND val_p_value IS NOT NULL AND "
               "COALESCE(replay,'0') IN ('0', 0)")
    gq = bh_adjust([float(r["val_p_value"]) for r in tested], len(tested)) if tested else []
    gmap = {r["row_key"]: v for r, v in zip(tested, gq)}
    # Cross-stock pooled evidence per strategy (validation segments only)
    pooled = []
    for sid in [r["strategy_id"] for r in q("SELECT strategy_id, COUNT(*) k FROM matrix WHERE status='TESTED' AND "
                                            "val_excess_mean IS NOT NULL GROUP BY 1 HAVING k >= 3")]:
        rows = q("SELECT * FROM matrix WHERE strategy_id=? AND status='TESTED' AND val_excess_mean IS NOT NULL", (sid,))
        pr = _pooled(rows)
        if pr.get("p_value") is not None:
            pooled.append({"strategy_id": sid, "name": rows[0].get("category"), "origin": rows[0].get("origin"),
                           "timeframes": ",".join(sorted({r["timeframe"] for r in rows})),
                           "entry_rule": rows[0].get("entry_rule"), **pr})
    pq = bh_adjust([p["p_value"] for p in pooled], len(pooled)) if pooled else []
    for p, v in zip(pooled, pq):
        p["q_value"] = v
        p["status"] = ("CROSS_STOCK_SUPPORTED" if v is not None and v <= gates.fdr_q and
                       p["positive_fraction"] >= gates.cross_stock_positive_fraction else "NOT_SUPPORTED")
    pooled.sort(key=lambda p: (p["q_value"] if p["q_value"] is not None else 1.0))
    _write_csv(store.out / "strategy_cross_stock.csv", pooled)
    cand = q("SELECT * FROM matrix WHERE classification IN ('VALIDATED','EXPERIMENTAL','DISCOVERED') ORDER BY "
             "classification='VALIDATED' DESC, classification='EXPERIMENTAL' DESC, CAST(val_sharpe AS REAL) DESC")
    for c in cand:
        c["global_q_value"] = gmap.get(c["row_key"])
        c["validated_run_wide"] = int(c["classification"] == "VALIDATED" and c["global_q_value"] is not None
                                      and c["global_q_value"] <= gates.fdr_q)
    report_cols = ["classification", "validated_run_wide", "symbol", "timeframe", "source", "origin", "strategy_id",
                   "category", "direction", "position_basis", "entry_rule", "exit_rule", "params", "val_trades",
                   "val_mean_net", "val_win_rate", "val_profit_factor", "val_max_drawdown", "val_sharpe",
                   "val_p_value", "val_q_value", "global_q_value", "bh_family_size", "final_trades", "final_mean_net",
                   "regime_perf", "fidelity", "assumptions", "reasons", "definition_file"]
    _write_csv(store.out / "ranked_candidates.csv", cand, report_cols)
    _write_csv(store.out / "ranked_validated.csv", [c for c in cand if c["classification"] == "VALIDATED"], report_cols)
    all_rows = q("SELECT * FROM matrix ORDER BY symbol, timeframe, source, strategy_id")
    _write_csv(store.out / "matrix.csv", all_rows, COLS)
    lanes = db.execute("SELECT COUNT(*) FROM lanes").fetchone()[0]
    stocks = db.execute("SELECT COUNT(DISTINCT symbol) FROM lanes").fetchone()[0]
    executed_rows = db.execute("SELECT COUNT(*) FROM matrix WHERE status='TESTED'").fetchone()[0]
    w = meta["window"]
    tbl = lambda rows, cols: ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + \
        ["| " + " | ".join(_fmt(r.get(c)) for c in cols) + " |" for r in rows]  # noqa: E731
    val_run = sum(c["validated_run_wide"] for c in cand)
    lines = ["# GPLM research matrix: library + catalogue + ML discovery", "",
             f"- Data: development window up to {w['end']}; the locked holdout (after {w['development_end']}) is never read",
             f"- Universe: {meta['universe_basis']}; stocks {stocks}; lanes {lanes}; timeframes "
             f"{', '.join(meta['timeframes'])}; ML methods {', '.join(meta['ml_methods']) or 'none'}",
             f"- Strategy library: {meta['library'].get('strategies')} definitions loaded "
             f"({meta['library'].get('by_origin')}), {meta['library'].get('selected')} selected for this run, "
             f"{meta['library'].get('invalid')} invalid file entries; ML discoveries saved this run: "
             f"{meta.get('library_saved')}",
             f"- Catalogue: {refs_total} references; **{refs_exec} executed and backtested with costs on at least one "
             f"lane**; coverage {cov_counts}",
             f"- Backtests executed (rows with status TESTED): {executed_rows}",
             f"- Statistical families: lane-level BH (every fresh validation test on a stock x timeframe lane) drives "
             f"the M5.6 gates; run-wide BH over {len(tested)} tests is reported as global_q_value. "
             f"VALIDATED rows that also survive run-wide BH: {val_run}.",
             "- VALIDATED = every M5.6 gate on the validation segment + the once-only final development-segment read. "
             "EXPERIMENTAL = validation gates passed, final segment not yet passed. DISCOVERED = promising but gates "
             "not met. REJECTED = failed. Historical backtests are not forecasts or trading advice.", "",
             "## Rows by source and status", "", *tbl(by_status, ["source", "status", "n"]), "",
             "## Rows by origin and status", "", *tbl(by_origin, ["origin", "status", "n"]), "",
             "## Tested rows by classification", "", *tbl(by_class, ["source", "classification", "n"]), "",
             "## Catalogue coverage (per reference)", "",
             *tbl([{"coverage": k, "references": v} for k, v in sorted(cov_counts.items())], ["coverage", "references"]),
             "", "## Top candidates", ""]
    top = cand[:40]
    lines += tbl(top, ["classification", "symbol", "timeframe", "strategy_id", "direction", "val_trades",
                       "val_mean_net", "val_win_rate", "val_sharpe", "val_q_value", "global_q_value", "final_mean_net"]) \
        if top else ["No strategy reached DISCOVERED or better."]
    lines += ["", "## Cross-stock pooled evidence (validation segments; strategies tested on >= 3 lanes)", "",
              "Pooled trades across stocks are not independent (common market moves), so these p-values are optimistic.",
              ""]
    lines += tbl(pooled[:25], ["strategy_id", "origin", "stocks", "n", "pooled_excess_mean", "positive_fraction",
                               "p_value", "q_value", "status"]) if pooled else ["No strategy was tested on >= 3 lanes."]
    lines += ["", "Files: matrix.csv / matrix.sqlite (every stock x strategy x timeframe row, blocked and rejected "
                  "included), ranked_candidates.csv, ranked_validated.csv, catalogue_coverage.csv, "
                  "strategy_cross_stock.csv, lanes in matrix.sqlite, run-config.json, catalogue-report.json.", ""]
    (store.out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    doc = {"by_status": by_status, "by_classification": by_class, "by_origin": by_origin,
           "catalogue_registered": refs_total, "catalogue_executed": refs_exec, "catalogue_coverage": cov_counts,
           "tested_rows": executed_rows, "run_wide_tests": len(tested), "validated_run_wide": val_run,
           "cross_stock_supported": sum(1 for p in pooled if p["status"] == "CROSS_STOCK_SUPPORTED"),
           "lanes": lanes, "stocks": stocks, "window": w}
    (store.out / "summary.json").write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
    return {"summary": str(store.out / "summary.md"), **{k: doc[k] for k in (
        "catalogue_registered", "catalogue_executed", "catalogue_coverage", "by_status", "tested_rows")}}


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
        return f"{f:.4g}" if not float(f).is_integer() or abs(f) < 1 else str(int(f))
    except (TypeError, ValueError):
        return str(v).replace("|", "/")[:80]


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], cols: Sequence[str] | None = None) -> None:
    cols = list(cols or (rows[0].keys() if rows else []))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows({c: _cell(r.get(c)) for c in cols} for r in rows)
    os.replace(tmp, path)


__all__ = ["CONTRACT", "compute_lane", "library_roots", "resolve_universe", "run", "write_summary"]
