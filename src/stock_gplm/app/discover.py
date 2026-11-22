"""LLM strategy DISCOVERY from the raw data (owner 2026-11-22): new strategies, not edits of old ones.

    python -m stock_gplm.app.discover --tf 15m            # facts -> LLM rounds -> exit sweep -> gate -> report
    python -m stock_gplm.app.discover --tf 15m --phase report

1. facts   - "probe" trades on the raw candles of every universe stock: enter on EVERY liquid bar, hold H bars
             (several H; intraday long and short; daily long only), costs included. For ~20 entry-time features
             (trend distance, RSI, ATR%, volume ratio, gap, returns, range position, candle body, time of day ...)
             the program measures which conditions were followed by moves big enough to beat costs: quintile
             tables and the best 2-condition combinations, recent trades weighted more. TUNE window only.
             -> FACTS.md (this is what the LLM "learns" from - aggregates only, never candles or the CONFIRM window).
2. rounds  - the local LLM reads the facts and invents NEW strategies (DSL entry + exit); each one is backtested
             on all stocks; results go back to the LLM next round (about half new ideas, half improvements).
3. sweep   - exit sweep (rr x max swing distance) on the best entries, no LLM.
4. gate    - the best by TUNE score face the CONFIRM window (never shown to the LLM) with the tuning buckets:
             A high win / B profitable, cautious win rate and profitable months (config/tuning/tuning.json gate).
Outputs <reports>/tuning/discover-<tf>/: FACTS.md, DISCOVERY.md, candidates.json (NOT in the library until the
owner approves), state.json (checkpoint - rerun the same command to resume), llm.jsonl, prompts/.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from . import tune_eval as te
from . import tune_llm as tl
from .tune import CONFIG, Tuner

FLOOR_DAILY = "median(volume * close, 20)[1] >= 150000000 and close[1] >= 50"
FLOOR_INTRADAY = "avg_day_volume(20) * prev_day_close() >= 150000000 and prev_day_close() >= 50"
MAX_ENTRY_NODES = 70
DEFAULTS = {"provider": "ollama", "model": None, "rounds": 8, "ideas": 8, "temperature": 0.8, "check_top": 5,
            "probes": {"1d": [5, 10, 20], "15m": [2, 4, 8], "5m": [3, 6, 12], "60m": [1, 2, 4]},
            "min_cell_share": 0.01, "min_cell_trades": 300}

SYSTEM = ("You are an experienced quantitative researcher designing NEW rule-based strategies for NSE cash equities "
          "from measured market facts. Every idea must be grounded in the facts given (say which). Costs are already "
          "inside every number. Small edges vanish after costs, so look for conditions that precede LARGER moves, and "
          "combine 2-4 conditions that each help. Keep enough trades (no tiny niches). Reply with ONE JSON object only.")
SCHEMA = ('{"strategies": [{"name": "2-4 words, readable", "idea": "one sentence: which facts it uses", '
          '"from": "<id of a tried strategy it improves>" or null, "direction": "long" or "short", '
          '"entry": "<DSL condition WITHOUT the liquidity floor>", '
          '"exit": {"rr": number, "max_swing_pct": number|null, "lookback": int, "max_hold_bars": int, '
          '"when": "<optional DSL exit condition>" or null}}]}')


def _camel(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    return "".join(w[:1].upper() + w[1:] for w in words)[:32] or "Idea"


def _q(x: float) -> str:
    return f"{x:.3g}"


class Discoverer(Tuner):
    def __init__(self, tf: str, name: str, cfg: Mapping[str, Any], stocks_arg: str | None, workers: int):
        super().__init__(tf, name, cfg, stocks_arg, workers, None, None)
        self.d = {**DEFAULTS, **(cfg.get("discover") or {})}
        self.template = next(iter(self.strategies.values()), {})
        self.strategies = {}
        self.intraday = tf != "1d"
        self.floor = FLOOR_INTRADAY if self.intraday else FLOOR_DAILY
        for k, v in (("ideas", []), ("rejected", []), ("rounds_done", 0), ("swept", False), ("facts", None)):
            self.state.setdefault(k, v)

    # -------------------------------------------------------------- 1. facts from the raw data
    def facts(self) -> str:
        if self.state.get("facts"):
            return self.state["facts"]
        dirs = ["long", "short"] if self.intraday else ["long"]
        probes = [te.Variant(f"PROBE~{dr}-{h}", self.tf, self.floor, {"max_hold_bars": int(h)}, dr, True)
                  for h in self.d["probes"][self.tf] for dr in dirs]
        self.log(f"facts: {len(probes)} probe sets (every liquid bar, hold {self.d['probes'][self.tf]} bars, {dirs})")
        res = self.evaluate(probes)
        names = te.feature_names(self.intraday)
        exprs = {**te.FEATURES, **(te.INTRADAY_FEATURES if self.intraday else {})}
        L = [f"MARKET FACTS {self.tf} - {len(self.stocks)} liquid NSE stocks, tuning window {self.t['tune_start']} .. "
             f"{self.t['confirm_start']} (recent trades weigh more). 'Probe' = enter at EVERY liquid bar, exit after "
             "H bars; returns are NET of costs. Features are measured at the signal bar; use their DSL text as-is.",
             "Feature DSL: " + "; ".join(f"{k} = {exprs[k]}" for k in names), ""]
        for v in probes:
            raw = res[v.vid].get("raw")
            if raw is None or "feat" not in raw:
                continue
            day = raw["day"].astype(np.int64)
            m = (day >= self.tune_day) & (day < self.confirm_day)
            if m.sum() < 1000:
                continue
            net, feat = raw["net"][m].astype(float), raw["feat"][m].astype(float)
            w = te.weights(day[m], self.confirm_day - 1, float(self.t["decay"]))
            L += self._probe_facts(v.vid.split("~")[1], net, feat, w, names)
        txt = "\n".join(L)
        self.state["facts"] = txt
        self.save()
        (self.root / "FACTS.md").write_text("# " + txt.replace("\n", "\n\n", 2) + "\n", encoding="utf-8")
        self.log(f"facts written: {self.root / 'FACTS.md'} ({len(txt):,} chars)")
        return txt

    def _probe_facts(self, label: str, net: np.ndarray, feat: np.ndarray, w: np.ndarray, names: list[str]
                     ) -> list[str]:
        side, h = label.split("-")
        N = len(net)
        sw = w.sum()
        avg0, win0 = float((w * net).sum() / sw), float(w[net > 0].sum() / sw)
        out = [f"## {side.upper()} hold {h} bars: {N:,} probe trades, avg {100 * avg0:+.3f}%, win {win0:.0%} "
               "(the bar to beat)"]
        bins = np.full(feat.shape, -1, dtype=np.int8)
        edges: dict[int, np.ndarray] = {}
        spreads = []
        for j, name in enumerate(names):
            x = feat[:, j]
            ok = np.isfinite(x)
            if ok.sum() < 1000:
                continue
            qs = np.unique(np.quantile(x[ok], [0.2, 0.4, 0.6, 0.8]))
            edges[j] = qs
            b = np.searchsorted(qs, x[ok], side="right").astype(np.int8)
            bins[ok, j] = b
            cells, avgs = [], []
            for q in range(len(qs) + 1):
                s = ok.copy()
                s[ok] = b == q
                if w[s].sum() <= 0:
                    continue
                a = float((w[s] * net[s]).sum() / w[s].sum())
                avgs.append(a)
                cells.append(f"{self._rng(qs, q)} {100 * a:+.2f}%/{w[s & (net > 0)].sum() / w[s].sum():.0%}")
            spreads.append((max(avgs) - min(avgs) if avgs else 0, f"- {name}: " + " | ".join(cells)))
        spreads.sort(key=lambda r: -r[0])
        out.append("Quintiles (value range: avg net / win), largest spread first:")
        out += [s[1] for s in spreads[:8]]  # prompt must fit num_ctx 16k with the reply
        # best and worst 2-condition cells
        min_n = max(int(self.d["min_cell_trades"]), int(self.d["min_cell_share"] * N))
        cells = []
        J = sorted(edges)
        for ia, a in enumerate(J):
            for b in J[ia + 1:]:
                ok = (bins[:, a] >= 0) & (bins[:, b] >= 0)
                idx = bins[ok, a].astype(np.int32) * 5 + bins[ok, b]
                cnt = np.bincount(idx, minlength=25)
                sw_ = np.bincount(idx, weights=w[ok], minlength=25)
                sn = np.bincount(idx, weights=w[ok] * net[ok], minlength=25)
                swin = np.bincount(idx, weights=w[ok] * (net[ok] > 0), minlength=25)
                for c in np.flatnonzero(cnt >= min_n):
                    if sw_[c] <= 0:
                        continue
                    qa, qb = divmod(int(c), 5)
                    cells.append((sn[c] / sw_[c], swin[c] / sw_[c], cnt[c] / N,
                                  f"{names[a]} {self._rng(edges[a], qa)} AND {names[b]} {self._rng(edges[b], qb)}"))
        cells.sort(key=lambda r: -r[0])
        out.append(f"Best 2-condition combinations (>= {min_n:,} trades): avg net / win / share of bars")
        out += [f"+ {c[3]}: {100 * c[0]:+.2f}% / {c[1]:.0%} / {c[2]:.1%}" for c in cells[:10]]
        out.append("Worst combinations (avoid):")
        out += [f"- {c[3]}: {100 * c[0]:+.2f}% / {c[1]:.0%} / {c[2]:.1%}" for c in cells[-3:][::-1]]
        out.append("")
        return out

    @staticmethod
    def _rng(qs: np.ndarray, q: int) -> str:
        lo = "min" if q == 0 else _q(qs[q - 1])
        hi = "max" if q == len(qs) else _q(qs[q])
        return f"[{lo}..{hi}]"

    def expand(self, text: str) -> str:
        """The LLM often writes fact-sheet names (atr_pct, dist_ema50) instead of DSL: substitute the expression
        (only names that are not already DSL series / functions, e.g. gap and minute_of_session stay)."""
        from .strategy_dsl import FUNCTIONS, SERIES
        exprs = {**te.FEATURES, **(te.INTRADAY_FEATURES if self.intraday else {})}
        for name in sorted(exprs, key=len, reverse=True):
            if name in SERIES or name in FUNCTIONS:
                continue
            text = re.sub(rf"\b{name}\b(?!\s*\()", f"({exprs[name]})", text)
        return text

    # -------------------------------------------------------------- 2. LLM rounds
    def results_text(self) -> str:
        ideas = self.state["ideas"]
        if not ideas:
            return "- none yet (first round)"
        rank = sorted(ideas, key=lambda i: -self.score(i["tune"], 0, "profit"))
        lines = []
        for i in rank[:10]:
            ex = i["exit"].get("swing_stop") or {}
            lines.append(f"- {i['id']} [{i['name']}] {i['direction']}: {i['core']} | exit rr {ex.get('rr')}, max swing "
                         f"{ex.get('max_swing_pct')}, hold {i['exit'].get('max_hold_bars')}"
                         + (f", when {i['exit']['when']}" if i["exit"].get("when") else "")
                         + f" -> {tl._fmt_stats(i['tune'])}")
        lines += [f"- {i['id']} [{i['name']}] -> {tl._fmt_stats(i['tune'])}" for i in rank[10:20]]
        lines += [f"- REJECTED [{r['name']}]: {r['reason']}" for r in self.state["rejected"][-6:]]
        return "\n".join(lines)

    def ask(self, rnd: int, providers: list[str], model: str, k: int, temp: float) -> list[te.Variant]:  # type: ignore[override]
        short_rule = ("long or short (intraday shorts are allowed in a cash account)" if self.intraday
                      else "LONG only (overnight shorts are not possible in a cash account)")
        prompt = f"""{self.state['facts']}

STRATEGIES TRIED SO FAR (tuning window, all stocks pooled, after costs), best first:
{self.results_text()}

TASK (round {rnd}): propose {k} strategies for the {self.tf} timeframe, direction {short_rule}.
{'All NEW ideas built from the facts.' if rnd == 1 else 'About half NEW ideas from the facts, half IMPROVEMENTS of the best tried ones (set "from").'}
Aim: average net per trade clearly above the probe average and above zero, profit factor >= 1.3, ideally win rate
>= 60%; at least {self.t['min_tune_trades']} trades over all stocks in the tuning window. Prefer conditions from the
best combinations, avoid the worst ones. You may use the fact-sheet feature names directly (e.g. atr_pct > 0.006);
they are replaced by their DSL formulas. Price-unit values must be divided by close (atr(14) / close < 0.03).
Exit = stop 0.2% beyond the last swing, target rr x risk{', square-off 15:15, no entries after 14:45' if self.intraday else ''};
ranges rr 0.3-4, max_swing_pct 0.005-0.08 or null, lookback 3-60, max_hold_bars 2-120; optional "when" exit rule.
Do NOT include the liquidity floor (added automatically). No ref_symbol(), feature(){', no bench() (daily only)' if self.intraday else '; bench(x) = x on Nifty 50 (data from 2024 only)'}.
Output JSON exactly like: {SCHEMA}

{tl.dsl_help(self.intraday)}
"""
        reply, used = None, None
        for pname in providers:
            p = self.registry.providers.get(pname)
            if p is None:
                continue
            try:
                reply = tl.call(p, model, SYSTEM, prompt, max_tokens=5000, temperature=temp)
                used = pname
                break
            except (tl.LLMError, OSError, TimeoutError) as exc:
                self.log(f"  {pname} failed: {str(exc)[:160]}")
        if reply is None:
            return []
        self.state["usd"] = round(self.state.get("usd", 0.0) + reply["usd"], 5)
        (self.root / "prompts" / f"D{rnd}.txt").write_text(
            f"=== PROMPT ({used} {model})\n{prompt}\n=== REPLY\n{reply['text']}", encoding="utf-8")
        try:
            items = tl.parse_json(reply["text"]).get("strategies", [])
        except (tl.LLMError, json.JSONDecodeError) as exc:
            items = []
            self.log(f"  round {rnd}: unreadable reply ({exc})")
        seen = {i["entry"] + json.dumps(i["exit"], sort_keys=True) for i in self.state["ideas"]}
        ids = {i["id"] for i in self.state["ideas"]}
        out, rej = [], 0
        for v in items[: k + 2]:
            if not isinstance(v, dict):
                continue
            ok, why = self.validate_idea(v)
            nm = str(v.get("name") or "idea")[:40]
            if ok is None:
                self.state["rejected"].append({"name": nm, "reason": why, "entry": str(v.get("entry"))[:200]})
                rej += 1
                continue
            key = ok["entry"] + json.dumps(ok["exit"], sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            base = f"DISC-{self.tf}-{_camel(nm)}"
            sid, n = base, 2
            while sid in ids:
                sid, n = f"{base}-{n}", n + 1
            ids.add(sid)
            vv = te.Variant(f"{sid}~D{rnd}", self.tf, ok["entry"], ok["exit"], ok["direction"])
            vv.meta = {"id": sid, "name": nm, "idea": str(v.get("idea", ""))[:300], "core": ok["core"],
                       "from": v.get("from"), "round": rnd, "source": f"{used}:{model}"}
            out.append(vv)
        with (self.root / "llm.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": f"D{rnd}", "provider": used,
                                "model": model, "in": reply["in_tokens"], "out": reply["out_tokens"],
                                "usd": reply["usd"], "seconds": reply["seconds"], "valid": len(out),
                                "rejected": rej}) + "\n")
        self.log(f"  round {rnd}: {used} {reply['seconds']}s -> {len(out)} valid, {rej} rejected")
        return out

    def validate_idea(self, v: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
        from . import strategy_dsl as dsl
        from .strategy_library import StrategyConfigError, validate
        direction = str(v.get("direction") or "long").lower()
        if direction not in ("long", "short"):
            return None, f"direction {direction}"
        if direction == "short" and not self.intraday:
            return None, "daily short not executable in a cash account"
        core = self.expand(tl.repair_dsl(str(v.get("entry") or "").strip().rstrip(";")))
        if core.startswith(self.floor):
            core = core[len(self.floor):].strip()
            core = core[4:].strip() if core.lower().startswith("and ") else core
        if not core:
            return None, "empty entry"
        try:
            node = dsl.parse(core)
        except dsl.DSLError as exc:
            return None, f"entry does not parse: {exc}"
        if dsl.node_size(node) > MAX_ENTRY_NODES:
            return None, "entry too complex"
        bad = dsl.functions_used(node) & (tl.DENY_FUNCS | ({"bench"} if self.intraday else set()))
        if bad:
            return None, f"uses unavailable functions {sorted(bad)}"
        e = v.get("exit") or {}
        sw: dict[str, Any] = {"buffer": 0.002}
        for key, dflt in (("rr", 2.0), ("lookback", 20)):
            lo, hi = tl.EXIT_RANGES[key]
            try:
                val = min(max(float(e.get(key, dflt) or dflt), lo), hi)
            except (TypeError, ValueError):
                val = dflt
            sw[key] = int(round(val)) if key == "lookback" else round(val, 4)
        if e.get("max_swing_pct") not in (None, "", "null"):
            lo, hi = tl.EXIT_RANGES["max_swing_pct"]
            try:
                sw["max_swing_pct"] = round(min(max(float(e["max_swing_pct"]), lo), hi), 4)
            except (TypeError, ValueError):
                pass
        lo, hi = tl.EXIT_RANGES["max_hold_bars"]
        try:
            hold = int(min(max(int(e.get("max_hold_bars") or 25), lo), hi))
        except (TypeError, ValueError):
            hold = 25
        ex: dict[str, Any] = {"max_hold_bars": hold, "swing_stop": sw}
        when = self.expand(tl.repair_dsl(str(e.get("when") or "").strip()))
        if when and when.lower() not in ("null", "none"):
            try:
                dsl.parse(when)
                ex["when"] = when
            except dsl.DSLError as exc:
                return None, f"exit rule does not parse: {exc}"
        entry = f"{self.floor} and ({core})"
        doc = {**{k: self.template[k] for k in self.template
                  if k not in ("id", "name", "description", "entry", "exit", "direction")},
               "id": "DISC-CHECK", "name": "check", "entry": entry, "exit": ex, "direction": direction,
               "applicability": {**(self.template.get("applicability") or {}), "timeframes": [self.tf]}}
        try:
            validate(doc)
        except StrategyConfigError as exc:
            return None, f"invalid: {exc}"
        return {"entry": entry, "core": core, "exit": ex, "direction": direction}, ""

    def record_ideas(self, vs: list[te.Variant], res: Mapping[str, Mapping[str, Any]]) -> None:
        for v in vs:
            r = res[v.vid]
            self.state["ideas"].append({**v.meta, "direction": v.direction, "entry": v.entry, "exit": v.exit,
                                        "tune": r["tune"], "confirm": r["confirm"], "errors": r.get("errors", 0)})

    def run_rounds(self, providers: list[str], model: str) -> None:
        R, k = int(self.d["rounds"]), int(self.d["ideas"])
        self.log(f"discovery {self.tf}: {R} rounds x {k} ideas, model {model}")
        for rnd in range(int(self.state["rounds_done"]) + 1, R + 1):
            vs = self.ask(rnd, providers, model, k, float(self.d["temperature"]))
            self.record_ideas(vs, self.evaluate(vs))
            self.state["rounds_done"] = rnd
            self.save()
            new = [i for i in self.state["ideas"] if i.get("round") == rnd]
            for i in sorted(new, key=lambda i: -self.score(i["tune"], 0, "profit"))[:3]:
                self.log(f"    {i['id']:<34} {tl._fmt_stats(i['tune'])[:110]}")
            self.write_report()

    def sweep_best(self) -> None:
        if self.state["swept"]:
            return
        es = self.cfg["exit_sweep"]
        tops: dict[str, dict] = {}
        for obj in ("win", "profit"):
            for i in self.ranked(obj)[: int(es["top"])]:
                tops[i["id"]] = i
        seen = {i["entry"] + json.dumps(i["exit"], sort_keys=True) for i in self.state["ideas"]}
        vs = []
        for i in tops.values():
            for rr in es["rr"]:
                for mx in es["max_swing_pct"]:
                    ex = json.loads(json.dumps(i["exit"]))
                    ex["swing_stop"]["rr"] = rr
                    ex["swing_stop"].pop("max_swing_pct", None)
                    if mx is not None:
                        ex["swing_stop"]["max_swing_pct"] = mx
                    key = i["entry"] + json.dumps(ex, sort_keys=True)
                    if key in seen:
                        continue
                    seen.add(key)
                    sid = f"{i['id']}-X{rr:g}-{mx or 'none'}"
                    v = te.Variant(f"{sid}~X", self.tf, i["entry"], ex, i["direction"])
                    v.meta = {"id": sid, "name": f"{i['name']} (exit rr {rr:g}, swing {mx or 'none'})",
                              "idea": i.get("idea", ""), "core": i["core"], "from": i["id"], "round": "sweep",
                              "source": "exit-sweep"}
                    vs.append(v)
        if vs:
            self.log(f"exit sweep: {len(vs)} variants of {len(tops)} best ideas")
            self.record_ideas(vs, self.evaluate(vs))
        self.state["swept"] = True
        self.save()

    def ranked(self, objective: str) -> list[dict[str, Any]]:
        return sorted(self.state["ideas"], key=lambda i: -self.score(i["tune"], 0, objective))

    # -------------------------------------------------------------- 3. gate + report
    def gate_idea(self, i: Mapping[str, Any]) -> tuple[str, str]:
        g = self.cfg["gate"]
        notes = []
        for bucket, rule in (("A high win", g), ("B profitable", g["profitable"])):
            why = self._fails(i["confirm"], {}, rule, False)
            if not why:
                return bucket, ""
            notes.append(f"{bucket[0]}: " + "; ".join(why))
        return "-", " / ".join(notes)

    def write_report(self) -> Path:  # type: ignore[override]
        ideas = self.state["ideas"]
        n_check = int(self.d["check_top"])
        checked: dict[str, dict] = {}
        for obj in ("win", "profit"):
            for i in self.ranked(obj)[:n_check]:
                if self.score(i["tune"], 0, obj) > -999:
                    checked[i["id"]] = i
        L = [f"# Discovery {self.tf} ({self.name})", "",
             f"Stocks {len(self.stocks)}; TUNE {self.t['tune_start']} .. {self.t['confirm_start']} (what the LLM sees, "
             f"via FACTS.md); CONFIRM {self.t['confirm_start']} .. latest (never shown). Ideas tried: {len(ideas)} "
             f"(LLM rounds {self.state['rounds_done']}, exit sweep {'done' if self.state['swept'] else 'pending'}). "
             f"Only the top {n_check} by tuning win and by tuning profit face the gate (fewer looks = less luck).", "",
             "| Strategy | Idea | Side | Tune trades / win / avg / PF | Confirm trades / win / avg / PF / months + | Verdict |",
             "|---|---|---|---|---|---|"]
        cands = []
        for i in self.ranked("profit")[:25]:
            t, c = i["tune"], i["confirm"]
            m, mp = self.months(c)
            if i["id"] in checked:
                bucket, why = self.gate_idea(i)
                verdict = f"**{bucket}**" if bucket != "-" else f"rejected: {why}"
            else:
                bucket, verdict = "-", "not checked (not top by tuning)"
            cf = (f"{c['trades']} / {c['win_w']:.0%} / {100 * c['avg_w']:+.2f}% / {min(c['pf_w'], 99):.2f} / "
                  f"{mp} of {m}") if c.get("trades") else "0"
            L.append(f"| {i['name']} ({i['id']}) | {str(i.get('idea', '')).replace('|', '/')[:120]} | {i['direction']} | "
                     f"{t['trades']} / {t['win_w']:.0%} / {100 * t['avg_w']:+.2f}% / {min(t['pf_w'], 99):.2f} | {cf} | "
                     f"{verdict} |")
        for i in checked.values():
            bucket, _ = self.gate_idea(i)
            if bucket == "-":
                continue
            cands.append({"id": i["id"], "name": f"Discovered: {i['name']}", "direction": i["direction"],
                          "description": f"{i.get('idea', '')} | Discovered {date.today()} ({self.name}, "
                                         f"{i.get('source', '')}).",
                          "entry": i["entry"], "exit": i["exit"], "enabled": True,
                          "applicability": {"symbols": "*", "timeframes": [self.tf]},
                          "tags": ["discovered", "bucket:" + bucket.split()[0]],
                          "evidence_tuning": {"bucket": bucket, "tune": i["tune"], "confirm": i["confirm"],
                                              "ideas_tried": len(ideas), "source": i.get("source", "")}})
        (self.root / "candidates.json").write_text(json.dumps(
            {"contract": "stock-gplm.tuning-candidates.v1", "timeframe": self.tf, "strategies": cands,
             "keep_original": []}, indent=1), encoding="utf-8")
        nA = sum(1 for c in cands if c["evidence_tuning"]["bucket"].startswith("A"))
        L += ["", f"Bucket A (high win): {nA}; bucket B (profitable): {len(cands) - nA}. Accepted ideas are in "
                  "candidates.json - not in the library until the owner approves; then paper-trade them unchanged."]
        p = self.root / "DISCOVERY.md"
        p.write_text("\n".join(L) + "\n", encoding="utf-8")
        return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tf", required=True, help="1d, 5m, 15m or 60m")
    ap.add_argument("--name", help="output folder name (default discover-<tf>)")
    ap.add_argument("--phase", choices=["all", "facts", "report"], default="all")
    ap.add_argument("--stocks", help="number of universe stocks (default from config) or @file")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--rounds", type=int, help="LLM rounds (default config discover.rounds)")
    ap.add_argument("--ideas", type=int, help="ideas per round")
    ap.add_argument("--model", help="override the model (default config discover.model, else local.model)")
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    if a.tf not in cfg["timeframes"]:
        ap.error(f"--tf must be one of {sorted(cfg['timeframes'])}")
    dc = cfg.setdefault("discover", {})
    if a.rounds:
        dc["rounds"] = a.rounds
    if a.ideas:
        dc["ideas"] = a.ideas
    du = Discoverer(a.tf, a.name or f"discover-{a.tf}", cfg, a.stocks, a.workers)
    model = a.model or du.d.get("model") or cfg["local"]["model"]
    try:
        if a.phase == "report":
            print(du.write_report())
            return 0
        du.facts()
        if a.phase == "facts":
            print(du.root / "FACTS.md")
            return 0
        du.run_rounds([du.d["provider"]], model)
        du.sweep_best()
        p = du.write_report()
        du.log(f"report: {p}")
        print(p)
    finally:
        du.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
