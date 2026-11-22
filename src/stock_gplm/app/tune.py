"""LLM fine-tuning of existing strategies (owner 2026-11-22): small edits, win rate 70-80%, recent trades first.

    python -m stock_gplm.app.tune --tf 1d --name tune-1d                 # baseline + local LLM rounds
    python -m stock_gplm.app.tune --tf 1d --name tune-1d --phase api     # then API refinement (MWAPI -> StartupAPI)
    python -m stock_gplm.app.tune --tf 1d --name tune-1d --phase final --final-model <openrouter id>   # optional
    python -m stock_gplm.app.tune --tf 1d --name tune-1d --phase report  # rebuild TUNING.md only

Per timeframe, pooled over the first N universe stocks (config/tuning/tuning.json):
  1. baseline  - every enabled library strategy of that TF: TUNE and CONFIRM window stats -> BASELINE.md;
                 queue = strategies with enough trades, highest recency-weighted TUNE win rate first.
  2. local     - local LLM (Ollama) gets the strategy text, its TUNE stats, a loser analysis and everything tried
                 so far; proposes small variants (<= 2 edits); the engine tests them; 3 rounds; then an exit sweep
                 (rr x max swing) on the best entries.
  3. api       - same with the API model (MWAPI, StartupAPI as fallback), 2 rounds + exit sweep. Spend capped.
  4. final     - optional OpenRouter round (costliest; only with --final-model).
  5. gate      - the best variants by TUNE score are checked on the CONFIRM window (never shown to any LLM):
                 recency-weighted win >= 70%, PF >= 1.3, effective trades >= 8, and better than the original.
Outputs <reports>/tuning/<name>/: BASELINE.md, TUNING.md, candidates.json (accepted variants as strategy docs -
NOT added to the library; owner approves), state.json (checkpoint; rerun the same command to resume),
llm.jsonl (every call: provider, tokens, cost), prompts/ (prompt + reply text).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from . import tune_eval as te
from . import tune_llm as tl

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / "config" / "tuning" / "tuning.json"


def _day(s: str) -> int:
    return int(np.datetime64(s, "D").astype(np.int64))


def _r(x: float, k: int = 4) -> float:
    return round(float(x), k)


class Tuner:
    def __init__(self, tf: str, name: str, cfg: Mapping[str, Any], stocks_arg: str | None, workers: int,
                 limit: int | None, only: Sequence[str] | None):
        from .settings import load
        from .strategy_library import StrategyLibrary
        from .walk_forward import read_stocks

        self.tf, self.name, self.cfg = tf, name, cfg
        self.t = cfg["timeframes"][tf]
        self.config = str(REPO / self.t["config"])
        self.root = load(Path(self.config)).reports_dir / "tuning" / name
        (self.root / "prompts").mkdir(parents=True, exist_ok=True)
        uni = read_stocks(stocks_arg if stocks_arg and stocks_arg.startswith("@")
                          else "@" + str(REPO / "deploy" / "oracle" / "live" / "universe.txt"))
        n = int(stocks_arg) if stocks_arg and stocks_arg.isdigit() else int(self.t["stocks"])
        self.stocks = uni if stocks_arg and stocks_arg.startswith("@") else uni[:n]
        lib = StrategyLibrary([REPO / "config" / "strategies" / "library"], self.root / "_lib").load(record=False)
        self.strategies = {s.id: s.doc for s in lib.strategies.values()
                           if s.doc.get("enabled", True) and not s.blocked and tf in s.timeframes
                           and (not only or s.id in only)}
        self.workers, self.limit = workers, limit
        self.tune_day, self.confirm_day = _day(self.t["tune_start"]), _day(self.t["confirm_start"])
        self.today = _day(date.today().isoformat())
        self.state_path = self.root / "state.json"
        self.state: dict[str, Any] = (json.loads(self.state_path.read_text(encoding="utf-8"))
                                      if self.state_path.is_file() else {"strategies": {}, "usd": 0.0})
        self.pool: te.EvalPool | None = None
        from ..llm.registry import load_registry
        self.registry = load_registry(REPO / "config" / "research" / "providers.json")

    # -------------------------------------------------------------- plumbing
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with (self.root / "progress.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def evaluate(self, variants: list[te.Variant]) -> dict[str, dict[str, Any]]:
        if not variants:
            return {}
        if self.pool is None:
            self.log(f"starting {self.workers} workers on {len(self.stocks)} stocks ({self.tf}); first call loads data")
            self.pool = te.EvalPool(self.stocks, self.config, [self.tf], self.workers, bool(self.t["use_minute"]))
        t0 = time.monotonic()
        res, errors = self.pool.evaluate(variants, self.t["tune_start"])
        self.log(f"evaluated {len(variants)} variants in {time.monotonic() - t0:.0f}s"
                 + (f" ({len(self.pool.failed)} stocks failed to load)" if self.pool.failed else ""))
        out = {}
        for v in variants:
            r = res.get(v.vid)
            out[v.vid] = self.split(r) if r is not None else {"tune": te.stats(np.zeros(0), np.zeros(0)),
                                                                "confirm": te.stats(np.zeros(0), np.zeros(0)), "raw": None}
            out[v.vid]["errors"] = errors.get(v.vid, 0)
        return out

    def split(self, r: Mapping[str, np.ndarray]) -> dict[str, Any]:
        day = r["day"].astype(np.int64)
        per = te.period_of(day, self.t["period"])
        out: dict[str, Any] = {"raw": r}
        for key, mask, end in (("tune", (day >= self.tune_day) & (day < self.confirm_day), self.confirm_day - 1),
                               ("confirm", day >= self.confirm_day, self.today)):
            w = te.weights(day[mask], end, float(self.t["decay"]))
            out[key] = {k: (_r(v) if isinstance(v, float) else v)
                        for k, v in te.stats(r["net"][mask], w, per[mask]).items()}
            out[key]["stocks"] = int(len(np.unique(r["stock"][mask])))
        return out

    def score(self, s: Mapping[str, Any], parent_trades: int) -> float:
        sel = self.cfg["selection"]
        if (s.get("trades", 0) < self.t["min_tune_trades"] or s.get("n_eff", 0) < sel["min_tune_neff"]
                or s.get("trades", 0) < sel["min_retained"] * max(parent_trades, 1)):
            return -1000.0 + s.get("trades", 0) / 1e4
        v = 100 * s["win_w"] + 10 * min(s["pf_w"], 3.0)
        v -= 20 if s["pf_w"] < sel["target_pf"] else 0
        v -= 20 if s["avg_w"] <= 0 else 0
        return round(v, 3)

    # -------------------------------------------------------------- loser analysis
    def analysis(self, raw: Mapping[str, np.ndarray] | None) -> str:
        if raw is None or "feat" not in raw:
            return "not available"
        day = raw["day"].astype(np.int64)
        m = (day >= self.tune_day) & (day < self.confirm_day)
        if m.sum() < 30:
            return "too few trades"
        net, feat = raw["net"][m].astype(float), raw["feat"][m]
        w = te.weights(day[m], self.confirm_day - 1, float(self.t["decay"]))
        win = net > 0
        names = te.feature_names(self.tf != "1d")
        exprs = {**te.FEATURES, **te.INTRADAY_FEATURES}
        rows, singles = [], []
        for j, name in enumerate(names):
            x = feat[:, j].astype(float)
            ok = np.isfinite(x)
            if ok.sum() < 30:
                continue
            qs = np.unique(np.quantile(x[ok], [0.2, 0.4, 0.6, 0.8]))
            b = np.searchsorted(qs, x[ok], side="right")
            cells = []
            wins = []
            for q in range(len(qs) + 1):
                sel = b == q
                if sel.sum() == 0:
                    continue
                ww = w[ok][sel]
                wr = float(ww[win[ok][sel]].sum() / ww.sum()) if ww.sum() > 0 else 0.0
                av = float((ww * net[ok][sel]).sum() / ww.sum()) if ww.sum() > 0 else 0.0
                lo = "min" if q == 0 else f"{qs[q - 1]:.3g}"
                hi = "max" if q == len(qs) else f"{qs[q]:.3g}"
                cells.append(f"[{lo}..{hi}] win {wr:.0%} avg {100 * av:+.2f}%")
                wins.append(wr)
            spread = max(wins) - min(wins) if wins else 0
            rows.append((spread, f"* {name} = {exprs[name]}: " + " | ".join(cells)))
            for thr in qs:
                for side in (">=", "<"):
                    keep = ok & ((x >= thr) if side == ">=" else (x < thr))
                    if keep.sum() < 0.4 * len(x) or w[keep].sum() <= 0:
                        continue
                    wr = float(w[keep & win].sum() / w[keep].sum())
                    gl = -(w * net)[keep & ~win].sum()
                    pf = float((w * net)[keep & win].sum() / gl) if gl > 0 else 99.0
                    singles.append((wr, f"{exprs[name]} {side} {thr:.3g}: keeps {keep.mean():.0%} of trades, "
                                        f"win {wr:.0%}, PF {min(pf, 99):.2f}"))
        rows.sort(key=lambda r: -r[0])
        singles.sort(key=lambda r: -r[0])
        reasons = raw["reason"][m]
        rs = []
        for code in np.unique(reasons):
            sel = reasons == code
            rs.append(f"{te.EXIT_NAMES.get(int(code), code)} {sel.mean():.0%} (win {win[sel].mean():.0%}, "
                      f"avg {100 * net[sel].mean():+.2f}%)")
        mfe = raw["mfe"][m].astype(float)
        mae = raw["mae"][m].astype(float)
        lose = ~win
        txt = ["Exit reasons: " + "; ".join(rs),
               (f"Losing trades: median best open profit (MFE) {100 * np.median(mfe[lose]):+.2f}%, "
                f"{(mfe[lose] >= 0.01).mean():.0%} of losers were up >= 1% before losing, "
                f"{(mfe[lose] >= 0.02).mean():.0%} were up >= 2%." if lose.any() else "No losing trades."),
               (f"Winning trades: median worst open loss (MAE) {100 * np.median(mae[win]):+.2f}%, "
                f"median bars held {int(np.median(raw['held'][m][win]))}." if win.any() else ""),
               "Features with the largest win-rate spread across quintiles:",
               *[r[1] for r in rows[:8]],
               "Best single-threshold filters keeping >= 40% of trades (recency-weighted):",
               *[s[1] for s in singles[:8]]]
        return "\n".join(t for t in txt if t)

    # -------------------------------------------------------------- stages
    def variant(self, sid: str, tag: str, entry: str, exit_: Mapping[str, Any], features: bool = False) -> te.Variant:
        d = self.strategies[sid]
        return te.Variant(f"{sid}~{tag}", self.tf, entry, json.loads(json.dumps(exit_)), d.get("direction", "long"),
                          features)

    def baseline(self) -> None:
        if self.state.get("baseline_done"):
            return
        self.log(f"baseline: {len(self.strategies)} strategies on {self.tf}")
        vs = [self.variant(sid, "orig", d["entry"], d["exit"], features=True) for sid, d in self.strategies.items()]
        res = self.evaluate(vs)
        for sid, d in self.strategies.items():
            r = res[f"{sid}~orig"]
            self.state["strategies"][sid] = {
                "name": d.get("name", sid), "base": {"tune": r["tune"], "confirm": r["confirm"]},
                "analysis": self.analysis(r["raw"]), "history": [], "rejected": [], "rounds": {}, "sweep": {},
                "trials": 0}
        self.state["baseline_done"] = True
        self.save()
        self.write_baseline()

    def queue(self) -> list[str]:
        st = self.state["strategies"]
        q = [sid for sid in self.strategies if sid in st and st[sid]["base"]["tune"]["trades"] >= self.t["min_tune_trades"]]
        q.sort(key=lambda s: -st[s]["base"]["tune"]["win_w"])  # owner: highest win rate first
        return q[: self.limit] if self.limit else q

    def best(self, sid: str, top: int = 1) -> list[dict[str, Any]]:
        st = self.state["strategies"][sid]
        ptr = st["base"]["tune"]["trades"]
        base = {"tag": "orig", "label": "original", "change": "none", "entry": self.strategies[sid]["entry"],
                "exit": self.strategies[sid]["exit"], "tune": st["base"]["tune"], "confirm": st["base"]["confirm"]}
        allv = [base, *st["history"]]
        for h in allv:
            h["score"] = self.score(h["tune"], ptr)
        return sorted(allv, key=lambda h: -h["score"])[:top]

    def ask(self, sid: str, phase: str, rnd: int, providers: list[str], model: str, k: int, temp: float
            ) -> list[te.Variant]:
        st = self.state["strategies"][sid]
        d = self.strategies[sid]
        b = self.best(sid)[0]
        hist = [{"label": h["label"], "change": h["change"], "tune": h["tune"]} for h in st["history"]]
        hist += [{"label": x["label"], "change": f"REJECTED ({x['reason']}): {x['change']}", "tune": {}}
                 for x in st["rejected"][-8:]]
        stage = (f"{phase} round {rnd}; current best so far: [{b['label']}] {b['change']}"
                 if b["tag"] != "orig" else f"{phase} round {rnd}")
        prompt = tl.build_prompt(d, self.tf, st["base"]["tune"], st["analysis"], hist, k,
                                 tl.dsl_help(self.tf != "1d"), stage)
        reply, used = None, None
        for pname in providers:
            p = self.registry.providers.get(pname)
            if p is None:
                continue
            if not p.local:
                price = p.pricing or {}
                worst = (len(prompt) / 4 * float(price.get("input_per_1k", 0.003)) +
                         3000 * float(price.get("output_per_1k", 0.015))) / 1000
                if self.state["usd"] + worst > float(self.cfg["api"]["max_usd"]):
                    self.log(f"  spend cap ${self.cfg['api']['max_usd']} reached (spent ${self.state['usd']:.2f}); skip")
                    return []
            try:
                reply = tl.call(p, model, tl.SYSTEM, prompt, temperature=temp)
                used = pname
                break
            except (tl.LLMError, OSError, TimeoutError) as exc:
                self.log(f"  {pname} failed for {sid}: {str(exc)[:160]}")
        if reply is None:
            return []
        self.state["usd"] = round(self.state["usd"] + reply["usd"], 5)
        tag = f"{phase[0].upper()}{rnd}"
        pdir = self.root / "prompts" / sid.replace("/", "_")
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / f"{tag}.txt").write_text(f"=== PROMPT ({used} {model})\n{prompt}\n=== REPLY\n{reply['text']}",
                                         encoding="utf-8")
        out, rej = [], 0
        try:
            items = tl.parse_json(reply["text"]).get("variants", [])
        except (tl.LLMError, json.JSONDecodeError) as exc:
            items = []
            self.log(f"  {sid}: unreadable reply ({exc})")
        seen = {h["entry"] + json.dumps(h["exit"], sort_keys=True) for h in st["history"]}
        for j, v in enumerate(items[: k + 2]):
            if not isinstance(v, dict):
                continue
            ok, why = tl.validate_variant(d, v, self.tf)
            label = str(v.get("label") or f"variant {j + 1}")[:60]
            if ok is None:
                st["rejected"].append({"label": label, "reason": why, "change": json.dumps(
                    {k2: v.get(k2) for k2 in ("add_filter", "entry", "exit") if v.get(k2)})[:300]})
                rej += 1
                continue
            key = ok["entry"] + json.dumps(ok["exit"], sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            vv = self.variant(sid, f"{tag}-{j + 1}", ok["entry"], ok["exit"])
            vv.meta = {"label": label, "change": ok["change"], "rationale": str(v.get("rationale", ""))[:300],
                       "source": f"{used}:{model}"}
            out.append(vv)
        with (self.root / "llm.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%S"), "strategy": sid, "stage": tag,
                                "provider": used, "model": model, "in": reply["in_tokens"], "out": reply["out_tokens"],
                                "usd": reply["usd"], "seconds": reply["seconds"], "valid": len(out),
                                "rejected": rej}) + "\n")
        self.log(f"  {sid} {tag}: {used} {reply['seconds']}s -> {len(out)} valid, {rej} rejected"
                 + (f", ${reply['usd']:.3f}" if reply["usd"] else ""))
        return out

    def record(self, variants: list[te.Variant], res: Mapping[str, Mapping[str, Any]]) -> None:
        for v in variants:
            sid, tag = v.vid.split("~", 1)
            r = res[v.vid]
            st = self.state["strategies"][sid]
            meta = getattr(v, "meta", {})
            st["history"].append({"tag": tag, "label": meta.get("label", tag), "change": meta.get("change", ""),
                                  "rationale": meta.get("rationale", ""), "source": meta.get("source", ""),
                                  "entry": v.entry, "exit": v.exit, "tune": r["tune"], "confirm": r["confirm"],
                                  "errors": r.get("errors", 0)})
            st["trials"] += 1

    def rounds(self, phase: str, providers: list[str], model: str, n_rounds: int, k: int, temp: float) -> None:
        q = self.queue()
        B = int(self.cfg["batch"])
        self.log(f"{phase}: {len(q)} strategies, {n_rounds} rounds x {k} variants, model {model} via {providers}")
        for i in range(0, len(q), B):
            batch = q[i:i + B]
            for rnd in range(1, n_rounds + 1):
                todo = [s for s in batch if self.state["strategies"][s]["rounds"].get(phase, 0) < rnd]
                if not todo:
                    continue
                vs: list[te.Variant] = []
                for sid in todo:
                    vs += self.ask(sid, phase, rnd, providers, model, k, temp)
                self.record(vs, self.evaluate(vs))
                for sid in todo:
                    self.state["strategies"][sid]["rounds"][phase] = rnd
                self.save()
                self.progress_line(batch, f"{phase} round {rnd}")
            self.sweep(batch, phase)
        self.write_report()

    def sweep(self, batch: Sequence[str], phase: str) -> None:
        """Exit sweep (no LLM): best entries x rr x max swing distance."""
        es = self.cfg["exit_sweep"]
        todo = [s for s in batch if not self.state["strategies"][s]["sweep"].get(phase)]
        vs: list[te.Variant] = []
        for sid in todo:
            st = self.state["strategies"][sid]
            seen = {h["entry"] + json.dumps(h["exit"], sort_keys=True) for h in st["history"]}
            for bi, b in enumerate(self.best(sid, int(es["top"]))):
                for rr in es["rr"]:
                    for mx in es["max_swing_pct"]:
                        ex = json.loads(json.dumps(b["exit"]))
                        sw = dict(ex.get("swing_stop") or {"buffer": 0.002, "lookback": 20})
                        sw["rr"] = rr
                        if mx is None:
                            sw.pop("max_swing_pct", None)
                        else:
                            sw["max_swing_pct"] = mx
                        ex["swing_stop"] = sw
                        key = b["entry"] + json.dumps(ex, sort_keys=True)
                        if key in seen:
                            continue
                        seen.add(key)
                        v = self.variant(sid, f"{phase[0].upper()}X{bi + 1}-{rr:g}-{mx or 'none'}", b["entry"], ex)
                        v.meta = {"label": f"{b['label']} + exit rr {rr:g}, swing {mx or 'none'}"[:80],
                                  "change": (f"{b['change']}; " if b["change"] != "none" else "")
                                  + f"exit rr {rr:g}, max swing {mx or 'none'}", "source": "exit-sweep"}
                        vs.append(v)
        if vs:
            self.log(f"exit sweep ({phase}): {len(vs)} variants for {len(todo)} strategies")
            self.record(vs, self.evaluate(vs))
        for sid in todo:
            self.state["strategies"][sid]["sweep"][phase] = True
        self.save()

    def progress_line(self, batch: Sequence[str], what: str) -> None:
        for sid in batch:
            st = self.state["strategies"][sid]
            b = self.best(sid)[0]
            self.log(f"  {what} | {st['name'][:34]:<34} orig win {st['base']['tune']['win_w']:.0%} PF "
                     f"{min(st['base']['tune']['pf_w'], 99):.2f} -> best win {b['tune']['win_w']:.0%} PF "
                     f"{min(b['tune']['pf_w'], 99):.2f} ({b['tune']['trades']} tr) [{b['label']}]")

    # -------------------------------------------------------------- gate + reports
    def gate(self, sid: str) -> dict[str, Any]:
        g = self.cfg["gate"]
        st = self.state["strategies"][sid]
        pc = st["base"]["confirm"]
        last_why = "no variant beat the original on TUNE"
        for rank, b in enumerate(self.best(sid, int(g["check_top"])), 1):
            if b["tag"] == "orig" or b["score"] <= -999:
                continue
            c = b["confirm"]
            why: list[str] = []
            if c["trades"] < self.t["min_confirm_trades"]:
                why.append(f"only {c['trades']} confirm trades")
            if c["n_eff"] < g["n_eff"]:
                why.append(f"effective trades {c['n_eff']:.0f} < {g['n_eff']}")
            if c["win_w"] < g["win_w"]:
                why.append(f"win {c['win_w']:.0%} < {g['win_w']:.0%}")
            if c["pf_w"] < g["pf_w"]:
                why.append(f"PF {c['pf_w']:.2f} < {g['pf_w']}")
            if g.get("beat_parent") and pc.get("trades") and c["avg_w"] <= pc["avg_w"]:
                why.append("not better than original on confirm")
            if not why:
                return {"accepted": True, "rank": rank, "variant": b}
            if rank == 1 or last_why.startswith("no variant"):
                last_why = "; ".join(why)
        return {"accepted": False, "variant": self.best(sid)[0], "why": last_why}

    def write_baseline(self) -> None:
        st = self.state["strategies"]
        rows = sorted(st, key=lambda s: -st[s]["base"]["tune"]["win_w"])

        def cell(s):
            return (f"{s['trades']:,} | {s['win_w']:.0%} | {100 * s['avg_w']:+.2f}% | {min(s['pf_w'], 99):.2f} | "
                    f"{s['n_eff']:.0f}") if s["trades"] else "0 | - | - | - | -"
        L = [f"# Baseline {self.tf} - {len(rows)} strategies, {len(self.stocks)} stocks", "",
             f"TUNE {self.t['tune_start']} .. {self.t['confirm_start']} (what the LLM sees); CONFIRM "
             f"{self.t['confirm_start']} .. latest (never shown to the LLM). Recency-weighted (decay {self.t['decay']} "
             "trading days). Queue = TUNE trades >= " + str(self.t["min_tune_trades"]) + ", highest TUNE win first.", "",
             "| # | Strategy | Tune trades | Win | Avg | PF | n_eff | Confirm trades | Win | Avg | PF | n_eff |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for i, sid in enumerate(rows, 1):
            L.append(f"| {i} | {st[sid]['name']} | {cell(st[sid]['base']['tune'])} | {cell(st[sid]['base']['confirm'])} |")
        (self.root / "BASELINE.md").write_text("\n".join(L) + "\n", encoding="utf-8")
        self.log(f"baseline written: {self.root / 'BASELINE.md'}")

    def write_report(self) -> Path:
        st = self.state["strategies"]
        q = [s for s in self.queue() if st[s]["history"]]
        L = [f"# Tuning {self.tf} ({self.name})", "",
             f"Stocks {len(self.stocks)}; TUNE {self.t['tune_start']} .. {self.t['confirm_start']}, CONFIRM "
             f"{self.t['confirm_start']} .. latest (never shown to any LLM). Gate on CONFIRM: recency-weighted win >= "
             f"{self.cfg['gate']['win_w']:.0%}, PF >= {self.cfg['gate']['pf_w']}, effective trades >= "
             f"{self.cfg['gate']['n_eff']}, better than the original. API spend so far ${self.state['usd']:.2f}.", "",
             "| Strategy | Original tune win / PF | Best variant | Change | Tune win / PF / trades | "
             "Confirm orig win / PF | Confirm variant win / PF / trades | Trials | Verdict |",
             "|---|---|---|---|---|---|---|---|---|"]
        cands = []
        for sid in q:
            s = st[sid]
            g = self.gate(sid)
            b = g["variant"]
            bt, bc, pt, pc = b["tune"], b["confirm"], s["base"]["tune"], s["base"]["confirm"]
            verdict = f"ACCEPTED (rank {g['rank']})" if g["accepted"] else f"no: {g['why']}"
            lab, chg = str(b["label"]).replace("|", "/"), str(b["change"]).replace("|", "/")
            L.append(f"| {s['name']} | {pt['win_w']:.0%} / {min(pt['pf_w'], 99):.2f} | {lab} | {chg} | "
                     f"{bt['win_w']:.0%} / {min(bt['pf_w'], 99):.2f} / {bt['trades']} | "
                     f"{pc['win_w']:.0%} / {min(pc['pf_w'], 99):.2f} | "
                     f"{bc['win_w']:.0%} / {min(bc['pf_w'], 99):.2f} / {bc['trades']} | {s['trials']} | {verdict} |")
            if g["accepted"]:
                d = self.strategies[sid]
                n = sum(1 for c in cands if c["id"].startswith(sid)) + 1
                cands.append({**{k: d[k] for k in d if k not in ("entry", "exit")},
                              "id": f"{sid}-T{n}", "name": f"{d.get('name', sid)} (tuned: {b['label']})",
                              "description": f"{d.get('description', '')} | Tuned {date.today()} ({self.name}): "
                                             f"{b['change']}",
                              "entry": b["entry"], "exit": b["exit"],
                              "applicability": {**(d.get("applicability") or {}), "timeframes": [self.tf]},
                              "tags": [*(d.get("tags") or []), "tuned", f"tuned-from:{sid}"],
                              "evidence_tuning": {"tune": bt, "confirm": bc, "trials": s["trials"],
                                                  "source": b.get("source", "")}})
        (self.root / "candidates.json").write_text(json.dumps({"contract": "stock-gplm.tuning-candidates.v1",
                                                                "timeframe": self.tf, "strategies": cands}, indent=1),
                                                    encoding="utf-8")
        L += ["", f"Accepted: {len(cands)} of {len(q)}. Candidates in candidates.json - not in the library until the "
                  "owner approves."]
        p = self.root / "TUNING.md"
        p.write_text("\n".join(L) + "\n", encoding="utf-8")
        self.log(f"report: {p}")
        return p

    def close(self) -> None:
        if self.pool:
            self.pool.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tf", required=True, help="1d, 5m, 15m or 60m")
    ap.add_argument("--name", required=True)
    ap.add_argument("--phase", choices=["baseline", "local", "api", "final", "report"], default="local")
    ap.add_argument("--stocks", help="number of universe stocks (default from config) or @file")
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--limit", type=int, help="tune only the first N of the queue (highest win rate)")
    ap.add_argument("--strategies", help="comma list of strategy ids to include")
    ap.add_argument("--local-model", help="override the Ollama model (e.g. qwen3-coder:30b)")
    ap.add_argument("--api-model", help="override the API model id")
    ap.add_argument("--max-usd", type=float, help="override the API spend cap for this run")
    ap.add_argument("--final-model", help="OpenRouter model id for --phase final")
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    if a.tf not in cfg["timeframes"]:
        ap.error(f"--tf must be one of {sorted(cfg['timeframes'])}")
    if a.max_usd is not None:
        cfg["api"]["max_usd"] = a.max_usd
    tu = Tuner(a.tf, a.name, cfg, a.stocks, a.workers, a.limit,
               [s.strip() for s in a.strategies.split(",")] if a.strategies else None)
    try:
        if a.phase == "report":
            tu.write_baseline()
            print(tu.write_report())
            return 0
        tu.baseline()
        if a.phase == "local":
            lc = cfg["local"]
            tu.rounds("local", [lc["provider"]], a.local_model or lc["model"], lc["rounds"], lc["variants"],
                      lc["temperature"])
        elif a.phase == "api":
            ac = cfg["api"]
            tu.rounds("api", list(ac["providers"]), a.api_model or ac["model"], ac["rounds"], ac["variants"],
                      ac["temperature"])
        elif a.phase == "final":
            fc = cfg["final"]
            model = a.final_model or fc.get("model")
            if not model:
                ap.error("--phase final needs --final-model <openrouter model id> (costliest; owner choice)")
            tu.rounds("final", [fc["provider"]], model, fc["rounds"], fc["variants"], fc["temperature"])
        print(tu.root / ("TUNING.md" if a.phase != "baseline" else "BASELINE.md"))
    finally:
        tu.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
