"""Section 5: ML discovery: genetic programming novelty, discovery-only fitness, backtest, persistence, trees."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

from ..app import strategy_dsl as dsl
from ..app import strategy_evolve as evo
from ..app.research import _tree_to_dsl
from ..app.strategy_engine import evaluate_strategy, segments
from ..app.strategy_library import StrategyDef, StrategyLibrary, normalize, validate
from . import fixtures as fx
from . import pipeline as pl
from .core import Check, Section
from .suite_validation import _lane

REPO = Path(__file__).resolve().parents[3]


def _rebuild(doc: dict[str, Any]) -> StrategyDef:
    d = normalize(doc)
    en, xn, needs = validate(d)
    return StrategyDef(d, "(new)", "", en, xn, 1, "x", True, needs)


def run(sec: Section, ev: dict[str, Any], work: Path) -> None:
    frame = fx.planted_edge()
    li = _lane(frame)
    n = len(frame)
    seg = (0, int(n * 0.6))
    log: list[dict[str, Any]] = []
    ev["evolve_log"] = log
    state: dict[str, Any] = {}

    def evolve(c: Check) -> None:
        calls: list[tuple[int, int]] = []
        real_sim = evo.simulate
        real_fit = evo.Evolver.fitness

        def spy(m: Any, mask: Any, s: tuple[int, int], *a: Any, **k: Any) -> Any:
            calls.append(tuple(s))
            return real_sim(m, mask, s, *a, **k)

        def logged(self: Any, g: Any) -> float:
            fresh = g.key() not in self.fit_cache
            f = real_fit(self, g)
            if fresh:
                log.append({"seq": len(log) + 1, "entry": g.entry(), "direction": g.direction, "exit": g.exit(),
                            "fitness": f if np.isfinite(f) else None, **{k: v for k, v in g.stats.items()}})
            return f
        with mock.patch.object(evo, "simulate", spy), mock.patch.object(evo.Evolver, "fitness", logged):
            out = evo.Evolver(li, seg, seed=55, budget="quick").run()
        out2 = evo.Evolver(li, seg, seed=55, budget="quick").run()
        state["out"] = out
        sel = [g.entry() for g in out["selected"]]
        c.evidence.update(population=out["population"], generations=out["generations"], evaluated=out["evaluated"],
                          best_fitness_by_generation=out["best_by_generation"], selected=sel,
                          segments_used=sorted(set(calls)), discovery_segment=seg)
        c.expect(out["evaluated"] > out["population"], "evolution did not go beyond the initial population")
        c.expect(set(calls) <= {seg}, f"fitness read data outside the discovery segment: {sorted(set(calls))}")
        c.expect(sel == [g.entry() for g in out2["selected"]], "same seed must reproduce the same discoveries")
        c.expect(len(sel) > 0, "no genome survived selection on a fixture with a planted edge")
        neg = [(g.entry(), g.stats.get("mean_net")) for g in out["selected"] if (g.stats.get("mean_net") or 0) <= 0]
        if neg:
            c.warn(f"{len(neg)} of {len(sel)} selected genomes lose money in absolute terms on the discovery segment "
                   f"(mean net {', '.join(f'{m:.4f}' for _, m in neg)}); they were selected for positive *excess* "
                   f"return over the baseline. See validation/baseline_uses_same_exit_policy: with a signal exit the "
                   f"baseline is not like-for-like, which favours exit genes with 'when'. "
                   f"Exit genes of the selected: {sorted({json.dumps(g.exit(), sort_keys=True) for g in out['selected']})}")
        hist = [h for h in out["best_by_generation"] if h is not None]
        c.expect(len(hist) >= 2 and hist[-1] >= hist[0], "elitism: the best fitness must never decrease")
        c.note(f"{out['evaluated']} distinct genomes evaluated over {out['generations']} generations; "
               f"{len(sel)} selected; every fitness call used bars {seg[0]}..{seg[1] - 1} only (full log in "
               f"ml-evolve-log.jsonl)")
    sec.run("gp_evolves_on_discovery_data_only", "Genetic programming evaluates new genomes over generations, is "
            "deterministic per seed, and computes fitness on the discovery segment only", evolve)

    def novelty(c: Check) -> None:
        out = state.get("out")
        if not out:
            c.fail("evolution did not run")
            return
        lib = StrategyLibrary([REPO / "config" / "strategies" / "library"], work / "ml-novelty").load(record=False)
        cat = [s for s in lib.strategies.values() if not s.blocked and "1d" in s.timeframes
               and not s.needs.get("benchmark") and not s.needs.get("features") and not s.needs.get("htf")]
        cat_text = {dsl.unparse(dsl.substitute(s.entry_node, s.defaults())) for s in cat}
        masks = {}
        for s in cat:
            try:
                masks[s.id] = dsl.signal(s.entry_node, li.ctx, s.defaults())
            except (dsl.DSLError, dsl.MissingData):
                continue
        rows = []
        for g in out["selected"]:
            text = dsl.unparse(dsl.parse(g.entry()))
            m = evo.Evolver(li, seg, 0).mask(g)
            best, best_j = None, 0.0
            for sid, cm in masks.items():
                u = int((m | cm).sum())
                j = int((m & cm).sum()) / u if u else 0.0
                if j > best_j:
                    best, best_j = sid, j
            rows.append({"entry": g.entry(), "direction": g.direction, "exit": g.exit(), "atoms": len(g.atoms),
                         "fitness": g.fitness, "in_catalogue_text": text in cat_text,
                         "nearest_catalogue_strategy": best, "signal_jaccard_to_nearest": round(best_j, 4),
                         "signals": int(m.sum())})
            c.expect(text not in cat_text, f"evolved rule is a catalogue rule verbatim: {g.entry()}")
            if best_j >= 0.95:
                c.warn(f"evolved rule fires almost exactly like {best} (Jaccard {best_j:.2f}): {g.entry()}")
        c.evidence.update(catalogue_rules_compared=len(masks), discoveries=rows)
        c.note(f"each discovery compared with {len(masks)} executable catalogue rules by canonical text and by "
               f"signal overlap; max overlap {max([r['signal_jaccard_to_nearest'] for r in rows] or [0]):.2f}")
    sec.run("gp_expressions_are_new", "Evolved rules are new expressions: not catalogue text, and their signals "
            "differ from every executable catalogue rule", novelty)

    def persist(c: Check) -> None:
        out = state.get("out")
        if not out or not out["selected"]:
            c.fail("nothing to persist")
            return
        lib = StrategyLibrary([], work / "ml-library")
        prov = {"lane": {"symbol": "SYN-EQ", "timeframe": "1d", "data_fingerprint": "verify-planted"}, "seed": 55,
                "budget": "quick", "population": out["population"], "generations": out["generations"],
                "evaluated": out["evaluated"], "discovery_segment": [0, seg[1] - 1], "run_id": "verify"}
        saved = []
        for g in out["selected"]:
            doc = evo.genome_doc(g, "SYN-EQ", "1d", prov)
            first = evaluate_strategy(_rebuild(doc), li, {}, None)
            doc["evidence"] = {"val_trades": first["met"].get("effective_n"), "val_mean_net": first["met"].get("mean_net"),
                               "val_p_value": first["met"].get("p_value")}
            sid, created = lib.save_discovered(doc)
            again = lib.save_discovered({**doc, "id": doc["id"] + "-dup"})
            saved.append({"id": sid, "created": created, "duplicate_saved_as_new": again[1],
                          "validation_trades": first["met"].get("effective_n"),
                          "validation_mean_net": first["met"].get("mean_net"), "first": first})
            c.expect(created and not again[1], f"{sid}: save/dedupe failed (created={created}, dup={again[1]})")
        reloaded = lib.load().strategies
        out_rows = []
        for s in saved:
            d = reloaded.get(s["id"])
            if not c.expect(d is not None, f"{s['id']} not reloaded from {lib.writable}"):
                continue
            p = Path(d.path)
            res = evaluate_strategy(d, li, {}, None)
            same = (res["status"] == s["first"]["status"] and res.get("params") == s["first"].get("params")
                    and res["met"].get("trades") == s["first"]["met"].get("trades")
                    and res["met"].get("mean_net") == s["first"]["met"].get("mean_net"))
            out_rows.append({"id": s["id"], "file": str(p), "origin": d.origin, "applies_to": d.doc["applicability"],
                             "provenance_keys": sorted(d.doc.get("provenance") or {}),
                             "evidence": d.doc.get("evidence"), "reload_rebacktest_identical": same,
                             "validation_trades": s["validation_trades"], "validation_mean_net": s["validation_mean_net"]})
            c.expect(d.origin == "ML_DISCOVERED" and p.is_file(), f"{s['id']}: wrong origin or missing file")
            c.expect({"seed", "generations", "evaluated", "discovery_segment", "discovery_fitness"}
                     <= set(d.doc.get("provenance") or {}), f"{s['id']}: provenance incomplete")
            c.expect(same, f"{s['id']}: reloaded definition does not reproduce its backtest")
        log_path = lib.writable / "discovered" / "_rediscoveries.jsonl"
        c.evidence.update(saved=out_rows, rediscovery_log=str(log_path),
                          rediscoveries_logged=len(log_path.read_text(encoding="utf-8").splitlines())
                          if log_path.is_file() else 0)
    sec.run("gp_rules_backtested_saved_reloaded", "Each selected rule is backtested (validation read once), saved "
            "as a reusable strategy-def JSON with provenance and evidence, deduplicated, reloaded by the library and "
            "re-backtests identically", persist)

    def tree_convert(c: Check) -> None:
        src = {"direction": "long", "exit": {"max_hold_bars": 7, "stop_atr": 1.5, "target_atr": 2.5},
               "entry": {"conditions": [{"feature": "RSI(period=14).value", "op": "lt", "threshold": 35.0},
                                        {"feature": "ADX(period=14).adx", "op": "gt", "threshold": 20.0},
                                        {"feature": "MACD(fast=12,signal=9,slow=26).hist_norm", "op": "cross_above",
                                         "threshold": 0.0}]}}
        p = work / "tree-def.json"
        p.write_text(json.dumps(src), encoding="utf-8")
        doc = _tree_to_dsl({"definition_file": str(p), "strategy_id": "ml-0123456789abcdef", "classification": "REJECTED"},
                           "SYN-EQ", "1d", "fp", "verify")
        missing = _tree_to_dsl({"definition_file": str(work / "absent.json"), "strategy_id": "ml-x"}, "SYN-EQ", "1d",
                               "fp", "verify")
        rng = np.random.default_rng(2)
        feats = {"RSI(period=14).value": rng.uniform(0, 100, 300), "ADX(period=14).adx": rng.uniform(0, 60, 300),
                 "MACD(fast=12,signal=9,slow=26).hist_norm": rng.normal(0, 1, 300)}
        ctx = dsl.Context.from_frame(fx.ohlcv(300), "1d", False, features=feats)
        got = dsl.signal(doc["entry"], ctx)
        h = feats["MACD(fast=12,signal=9,slow=26).hist_norm"]
        want = (feats["RSI(period=14).value"] < 35) & (feats["ADX(period=14).adx"] > 20) & \
            np.concatenate(([False], (h[1:] > 0) & (h[:-1] <= 0)))
        c.expect(np.array_equal(got, want), "converted tree rule does not reproduce the tree path conditions")
        c.expect(doc["exit"]["max_hold_bars"] == 7 and doc["exit"]["stop_atr"] == 1.5, "exit not carried over")
        lib = StrategyLibrary([], work / "tree-library")
        sid, created = lib.save_discovered(doc)
        back = lib.load().strategies.get(sid)
        c.expect(created and back is not None and back.needs.get("features"), "tree rule not reloaded as a "
                                                                             "feature-based strategy")
        c.expect(missing is None, "a tree row without its definition file must not be invented")
        c.evidence.update(tree_conditions=src["entry"]["conditions"], dsl_entry=doc["entry"], saved_id=sid,
                          signals=int(got.sum()), missing_definition_returns=missing)
        c.note("tree rows whose definition file is missing stay in the matrix only (documented limitation)")
    sec.run("tree_rules_converted_and_persisted", "A decision-tree path converts to an equivalent DSL rule (same "
            "bars fire), keeps its exit, and is saved and reloaded as an ML_DISCOVERED feature strategy", tree_convert)

    def pipeline_ml(c: Check) -> None:
        p = ev.get("pipeline")
        if p is None:
            c.fail("pipeline run unavailable")
            return
        rows = [r for r in pl.rows(p["out"]) if r["source"] == "ML"]
        lib = StrategyLibrary([], p["home"] / "strategy-library").load(record=False).strategies
        out = []
        for r in rows:
            lid = r.get("library_id") or ""
            d = lib.get(lid)
            f = Path(r.get("definition_file") or "")
            out.append({"method": r["method"], "strategy_id": r["strategy_id"], "library_id": lid,
                        "classification": r["classification"], "val_trades": r["val_trades"],
                        "in_library": d is not None, "definition_file_exists": f.is_file(),
                        "entry": (d.doc.get("entry") if d else r.get("entry_rule"))})
            if r["method"] == "evolve":
                c.expect(d is not None, f"evolve row {r['strategy_id']} not in the writable library")
                if d is not None:
                    c.expect(bool(d.doc.get("evidence")) and d.doc["provenance"].get("run_id"),
                             f"{lid}: evidence/provenance not attached")
            elif r["method"] == "tree" and d is None:
                c.warn(f"tree row {r['strategy_id']} kept in the matrix but not saved to the library")
        by = {m: sum(1 for x in out if x["method"] == m) for m in ("evolve", "tree")}
        c.expect(by["evolve"] > 0 and by["tree"] > 0, f"pipeline produced {by}")
        c.evidence.update(ml_rows=out, library_dir=str(p["home"] / "strategy-library" / "discovered"))
        c.note(f"research-matrix ML rows: {by}; files under {p['home'] / 'strategy-library' / 'discovered'}")
    sec.run("pipeline_ml_rows_persisted", "In the real research-matrix run, evolve and tree discoveries appear as ML "
            "rows and are written to the writable strategy library with evidence and provenance", pipeline_ml)
