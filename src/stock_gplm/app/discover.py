"""``gplm discover``: multi-timeframe strategy discovery over all local data.

Two complementary searches run per symbol, both on chronological splits of the symbol's own data:

1. **M5.4 unified engine** (:mod:`stock_gplm.m54.unified`), unchanged in method: feature grids on
   every eligible timeframe (1m..60m, 1d, 1w), single conditions, parameter-neighbour stability,
   interactions, cross-timeframe confirmations, exit grids, walk-forward, validation once, inner
   test once, Benjamini-Hochberg over every discovery test. Here it reads the app's canonical
   candles under the ``OWNER_AUTHORIZED_FULL_LOCAL`` basis instead of the M3.2-capped loader.
2. **Batched broad screen** (:mod:`.candidates`, GPU when available): a larger single + pair pool
   scored on the discovery segment, BH-corrected, then confirmed by exact simulation, scored once
   on validation and once on the test segment.

Every finalist enters the strategy library as an immutable, versioned default. Only strategies
that pass validation (engine tiers ROBUST_CANDIDATE / PROMISING_NOT_SIGNIFICANT / VALIDATION_ONLY,
screen rules that pass validation and test) are activated; the rest stay inactive for review.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..m54.backtest import ExitPolicy, simulate
from ..m54.candles import AccessPolicy, DailyResult, MinuteResult, fingerprint
from ..m54.store import canonical_hash, write_json_atomic
from ..m54.timeframes import build_all
from ..m54.unified import RUN_CONTRACT, UnifiedRun, load_settings
from . import ACCESS_BASIS
from .candidates import Rule, search
from .compute import Backend
from .context import App
from .ingest import Ingestor, SymbolData
from .lanes import all_bars, build_lane
from .library import Library, from_m54

BUDGETS = {
    "quick": {"engine": "quick", "screen": {"top_singles_for_pairs": 30, "max_pairs": 400, "max_selected": 5}},
    "standard": {"engine": "standard", "screen": {"top_singles_for_pairs": 80, "max_pairs": 3000, "max_selected": 8}},
    "deep": {"engine": "deep", "screen": {"top_singles_for_pairs": 250, "max_pairs": 30000, "max_selected": 12}},
}
ACTIVE_TIERS = {"ROBUST_CANDIDATE", "PROMISING_NOT_SIGNIFICANT", "VALIDATION_ONLY"}


class AppDiscoveryRun(UnifiedRun):
    """The M5.4 engine reading canonical app candles under the owner's full-local access basis."""

    def __init__(self, *args: Any, app_data: SymbolData, partitions: list[dict[str, Any]], **kw: Any):
        super().__init__(*args, **kw)
        self.app_data = app_data
        self.app_partitions = partitions

    def load(self, create: bool = True) -> None:
        d = self.s["data"]
        self.policy = AccessPolicy(ACCESS_BASIS, "9999-12-31", {"authorized_on": "2026-11-15",
                                                               "note": "owner directive; M3.2 records untouched"},
                                   self.start, self.end)
        daily = self.app_data.daily
        daily = daily[[self.policy.in_range(x) for x in daily["date"]]].reset_index(drop=True)
        minute = self.app_data.minute
        if len(minute):
            minute = minute[[self.policy.in_range(x) for x in minute["session"]]].reset_index(drop=True)
        dparts = [p for p in self.app_partitions if p["timeframe"] == "1d"]
        mparts = [p for p in self.app_partitions if p["timeframe"] == "1m"]
        self.daily = DailyResult(daily[["date", "open", "high", "low", "close", "volume"]], dparts, [],
                                 "ACCEPTED" if len(daily) else "MISSING")
        use_minute = self.data_mode != "daily"
        mb = minute[["start_ist", "open", "high", "low", "close", "volume", "session"]] if (use_minute and len(minute)) \
            else pd.DataFrame()
        self.minute = MinuteResult(mb, mparts if use_minute else [])
        self.bars = build_all(self.daily.bars, mb)
        self.lanes_plan = self._plan_lanes()
        partitions = self.daily.partitions + (self.minute.partitions if self.minute else [])
        identity = {"contract": RUN_CONTRACT, "symbol": self.symbol, "config": self.s, "code_sha256": self.code_sha,
                    "policy": self.policy.to_dict(), "data_mode": self.data_mode, "timeframes": self.timeframes,
                    "inputs": fingerprint(partitions), "lanes": self.lanes_plan, "hypotheses_file": self.extra_payload,
                    "app_candles": self.app_data.fingerprint}
        self.identity = identity
        self.run_id = canonical_hash(identity)
        self.dir = self.output_root / "unified" / self.symbol / self.run_id
        if create:
            self.dir.mkdir(parents=True, exist_ok=True)
            if not (self.dir / "identity.json").exists():
                write_json_atomic(self.dir / "identity.json", identity)


def _partitions(app: App, symbol: str) -> list[dict[str, Any]]:
    """Catalogue rows in the M5.4 partition shape; cross-root duplicate copies are marked as not used."""
    dup_of: dict[str, str] = {}
    for r in app.db.q("SELECT file, code, detail_json FROM data_issues WHERE symbol=? AND code IN "
                      "('DUPLICATE_IDENTICAL', 'CONFLICTING_DUPLICATE')", (symbol,)):
        detail = json.loads(r["detail_json"] or "{}")
        if r["code"] == "DUPLICATE_IDENTICAL":
            dup_of[r["file"]] = f"DUPLICATE_OF {detail.get('kept')}"
        else:
            for f in detail.get("copies", [r["file"]]):
                dup_of[f] = "CONFLICTING_DUPLICATE: copies differ"
    out = []
    for r in app.db.q("SELECT * FROM data_files WHERE symbol=? ORDER BY path", (symbol,)):
        usable = r["decision"] in {"ACCEPT", "REVIEW", "ACCEPT_CLOSING_PHASE_V2"} and r["path"] not in dup_of
        detail = json.loads(r["detail_json"] or "{}")
        p = {"timeframe": r["timeframe"], "file": r["path"], "permitted": usable, "sha256": r["sha256"],
             "disposition": dup_of.get(r["path"], r["decision"]), "records": r["records"], "bytes": r["bytes"],
             "root": str(Path(r["path"]).parent.parent)}
        if r["timeframe"] == "1d":
            p["window"] = r["period"].split("_")
        else:
            p["session"] = r["period"]
            p["missing_regular_minutes"] = detail.get("missing_regular_minutes")
            p["regular_bars"] = detail.get("regular_bars")
        out.append(p)
    return out


def _segments(lane_sessions: np.ndarray, frac: Sequence[float] = (0.6, 0.2, 0.2), embargo: int = 5
              ) -> dict[str, tuple[int, int]]:
    uniq = sorted(set(lane_sessions.tolist()))
    n = len(uniq)
    a, b = int(n * frac[0]), int(n * (frac[0] + frac[1]))
    cut = {"discovery": (0, a), "validation": (min(n, a + embargo), b), "test": (min(n, b + embargo), n)}
    out = {}
    for k, (s0, s1) in cut.items():
        i0 = int(np.searchsorted(lane_sessions, uniq[s0], side="left")) if s0 < n else len(lane_sessions)
        i1 = int(np.searchsorted(lane_sessions, uniq[s1 - 1], side="right")) if s1 > s0 else i0
        out[k] = (i0, i1)
    return out


def broad_screen(backend: Backend, app: App, data: SymbolData, timeframe: str, budget: str,
                 bars: Mapping[str, Any]) -> dict[str, Any]:
    lane = build_lane(data, timeframe, app.s.section("costs"), bars=bars, cache_dir=app.s.cache_dir / "features")
    if lane is None or len(lane.market) < 200:
        return {"timeframe": timeframe, "status": "INSUFFICIENT_DATA", "bars": 0 if lane is None else len(lane.market)}
    segs = _segments(lane.market.sessions, embargo=5 if not lane.intraday else 1)
    horizons = [h for h in app.s.section("horizons").get(timeframe, [1, 5, 10, 20]) if h <= 60]
    cfg = {**app.s.section("candidates"), **BUDGETS[budget]["screen"]}
    t0 = time.monotonic()
    res = search(backend, lane, segs["discovery"][1] - 1, horizons, cfg)
    secs = time.monotonic() - t0
    rules = [Rule.from_dict(r) for r in res["rules"]]
    out_rules = []
    for r in rules:
        mask = r.mask(lane)
        pol = ExitPolicy.from_dict(r.exit)
        val = simulate(lane.market, mask, segs["validation"], pol, lane.costs)
        vnet = [t["net_return"] for t in val]
        v_pass = len(val) >= 3 and float(np.mean(vnet)) > 0
        test = simulate(lane.market, mask, segs["test"], pol, lane.costs) if v_pass else []
        tnet = [t["net_return"] for t in test]
        t_pass = v_pass and len(test) >= 2 and float(np.mean(tnet)) > 0
        tier = ("SCREEN_ROBUST" if (r.tier == "FDR_SELECTED" and t_pass) else
                "SCREEN_VALIDATED" if t_pass else "SCREEN_VALIDATION_ONLY" if v_pass else "SCREEN_REJECTED_VALIDATION")
        out_rules.append({**r.to_dict(), "screen_tier": tier,
                          "validation": {"trades": len(val), "mean_net": float(np.mean(vnet)) if vnet else None,
                                         "win_rate": float(np.mean([x > 0 for x in vnet])) if vnet else None},
                          "test": {"trades": len(test), "mean_net": float(np.mean(tnet)) if tnet else None,
                                   "scored": v_pass}})
    return {"timeframe": timeframe, "status": "DONE", "bars": len(lane.market), "segments": {
        k: [lane.label(a) if a < len(lane.market) else None, lane.label(b - 1) if b > a else None] for k, (a, b) in segs.items()},
        "tests": res["tests"], "singles": res.get("singles"), "pairs": res.get("pairs"), "min_q": res.get("min_q"),
        "fdr_survivors": res.get("fdr_survivors"), "device": backend.name, "screen_seconds": round(secs, 3),
        "rules": out_rules, "lane": lane}


def discover(app: App, symbols: Sequence[str], *, data_mode: str, timeframes: str, budget: str, backend: Backend,
             workers: int = 1, start: str | None = None, end: str | None = None, skip_engine: bool = False
             ) -> dict[str, Any]:
    s = app.s
    disc = s.section("discovery")
    eng_cfg = (s.path.parent / disc["engine_config"]).resolve()
    settings = load_settings(eng_cfg, BUDGETS[budget]["engine"])
    lib = Library(s.strategies_dir, app.db)
    ing = Ingestor(s, app.db)
    prog = app.progress
    prog.set_total(len(symbols))
    results = []
    for sym in symbols:
        prog.update(force=True, task="discover", stock=sym, note="ingesting")
        data = ing.ingest(sym, use_minute=data_mode != "daily")
        bars = all_bars(data)
        record: dict[str, Any] = {"symbol": data.symbol, "candles": {k: data.summary.get(k) for k in (
            "first", "last", "daily_rows", "minute_sessions_used", "minute_first", "minute_last")}}
        if not skip_engine:
            prog.update(force=True, note="M5.4 unified engine (CPU)", device="cpu")
            run = AppDiscoveryRun(settings, data.symbol, s.home / "discovery", timeframes=timeframes,
                                  data={"all-local": "all-available"}.get(data_mode, data_mode), workers=workers,
                                  start=start, end=end, log=lambda msg: prog.update(note=msg[:90]),
                                  app_data=data, partitions=_partitions(app, data.symbol))
            out = run.execute()
            record["engine"] = {k: out.get(k) for k in ("run_id", "state", "dir", "reused", "stages_computed",
                                                         "stages_reused")}
            app.db.event("DISCOVERY_ENGINE_RUN", {"symbol": data.symbol, **record["engine"],
                                                  "lanes": run.lanes_plan}, subject=data.symbol)
            sj = Path(out["dir"]) / "strategies.json"
            imported = []
            if sj.is_file():
                doc = json.loads(sj.read_text(encoding="utf-8"))
                for st in doc.get("strategies", []):
                    tier = st.get("evidence", {}).get("tier") or st.get("tier")
                    v3 = from_m54(st, data.symbol)
                    v3["evidence"] = {**st.get("evidence", {}), "tier": tier, "engine_run": out["run_id"]}
                    res = lib.add(v3, immutable=True, origin=f"discovery:{out['run_id'][:16]}", active=tier in ACTIVE_TIERS,
                                  provenance={"engine": "m54-unified", "run_id": out["run_id"], "budget": budget,
                                              "access_basis": ACCESS_BASIS, "discovered_through": end or data.last_session})
                    imported.append({**res, "tier": tier})
            record["engine"]["strategies"] = imported
            record["engine"]["lanes"] = {tf: p["decision"] for tf, p in run.lanes_plan.items()}
        # batched broad screen on every lane with enough data
        screens = []
        tfs = ["1d", "1w", "60m", "15m", "5m"] if timeframes == "auto" else [t.strip() for t in timeframes.split(",")]
        for tf in tfs:
            if data_mode == "daily" and tf not in ("1d", "1w"):
                continue
            if data_mode == "minute" and tf in ("1d", "1w"):
                continue
            if tf not in bars or not len(bars[tf]):
                continue
            if tf not in ("1d", "1w") and bars[tf].sessions() < int(s.section("timeframes")["intraday_min_sessions"]):
                screens.append({"timeframe": tf, "status": f"INSUFFICIENT_DATA ({bars[tf].sessions()} sessions)"})
                continue
            prog.update(force=True, timeframe=tf, note=f"broad screen ({backend.name})", device=backend.name,
                        gpu_mem=(lambda mm: f"VRAM {mm['vram_used_mb']}/{mm['vram_total_mb']} MB" if mm else None)(backend.memory()))
            sc = broad_screen(backend, app, data, tf, budget, bars)
            lane = sc.pop("lane", None)
            for r in sc.get("rules", []):
                doc = {"name": f"{data.symbol} {tf} screen: {r['text']}"[:120], "timeframe": tf,
                       "scope": {"symbols": [data.symbol], "timeframes": [tf]},
                       "entry": {"logic": "AND", "conditions": r["conditions"]}, "exit": r["exit"],
                       "evidence": {"screen_tier": r["screen_tier"], "in_sample": r["in_sample"],
                                    "validation": r["validation"], "test": r["test"], "segments": sc["segments"],
                                    "tests_in_search": sc["tests"]}}
                res = lib.add(doc, immutable=True, origin="gpu-screen" if backend.is_gpu else "cpu-screen",
                              active=r["screen_tier"] in ("SCREEN_ROBUST", "SCREEN_VALIDATED"),
                              provenance={"engine": "app-broad-screen", "device": backend.name, "budget": budget,
                                          "discovered_through": end or data.last_session})
                r["library"] = res
            app.db.event("BROAD_SCREEN", {"symbol": data.symbol, **{k: v for k, v in sc.items() if k != "rules"},
                                          "rules": [{k: r[k] for k in ("rule_id", "text", "screen_tier", "validation",
                                                                        "test", "in_sample")} for r in sc.get("rules", [])]},
                         subject=data.symbol)
            screens.append(sc)
            del lane
        record["screens"] = screens
        results.append(record)
        prog.advance()
    return {"symbols": [r["symbol"] for r in results], "results": results, "device": backend.name,
            "device_stats": backend.stats, "budget": budget}
