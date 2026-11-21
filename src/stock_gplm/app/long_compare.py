"""Long-history check (owner go 2026-11-21): are the 2026 ACTIVE win rates real out of sample?

    python -m stock_gplm.app.long_compare --insample <2026 run dir> --oos <pre-2026 frozen-exit run dir>
        [--full <full-history learned run dir>] [--out COMPARE.md]

* 2026 ACTIVE combos (stock x TF x strategy from <insample>/active.json): 2026 trades (in-sample, exits learned on
  them) vs the same combos on pre-2026 history with the 2026 exits frozen (honest out-of-sample), overall, per
  strategy and per year.
* Full history, exits re-learned on all of it: ACTIVE size, overlap with 2026 ACTIVE, and its 2026 slice.
Reads the runs' all_trades.csv / active.json only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _active_rows(run: Path) -> set[tuple[str, str, str]]:
    doc = json.loads((run / "active.json").read_text(encoding="utf-8"))
    return {(s, tf, sid) for s, tfs in doc.items() for tf, ids in tfs.items() for sid in ids}


def _trades(run: Path) -> pd.DataFrame:
    t = pd.read_csv(run / "all_trades.csv")
    t["key"] = list(zip(t.stock, t.timeframe, t.strategy_id))
    t["year"] = t.signal.astype(str).str[:4]
    return t


def _row(label: str, t: pd.DataFrame) -> str:
    if not len(t):
        return f"| {label} | 0 | - | - | - | - |"
    n = t.net_pct
    loss = -n[n <= 0].sum()
    pf = f"{n[n > 0].sum() / loss:.2f}" if loss > 0 else "-"
    return f"| {label} | {len(t):,} | {(n > 0).mean():.1%} | {n.mean():+.2f}% | {n.sum():+,.0f}% | {pf} |"


HEAD = ["| Set | Trades | Win | Avg net | Total net | PF |", "|---|---|---|---|---|---|"]


def build(insample: Path, oos: Path, full: Path | None, min_trades: int = 10, want: float = 0.70) -> str:
    act = _active_rows(insample)
    ins = _trades(insample)
    out = _trades(oos)
    ia, oa = ins[ins.key.isin(act)], out[out.key.isin(act)]
    L = ["# Long-history check: 2026 ACTIVE out of sample", "",
         f"In-sample: `{insample}` (exits learned on 2026, ACTIVE {len(act)} combos). "
         f"Out-of-sample: `{oos}` (pre-2026, same 2026 exits frozen, no learning).", "",
         "## Overall", "", *HEAD,
         _row("2026 ACTIVE combos, 2026 (in-sample)", ia),
         _row("2026 ACTIVE combos, pre-2026 (out-of-sample)", oa),
         _row("All combos, pre-2026", out), ""]
    g = oa.groupby("key").net_pct
    per = pd.DataFrame({"trades": g.size(), "win": g.apply(lambda x: (x > 0).mean()), "avg": g.mean()})
    enough = per[per.trades >= min_trades]
    L += [f"Combos with >= {min_trades} pre-2026 trades: {len(enough)} of {len(act)}; "
          f"still >= {want:.0%} win: {(enough.win >= want).sum()}; still profitable (avg > 0): {(enough.avg > 0).sum()}.", ""]
    L += ["## Per strategy (2026 ACTIVE combos)", "",
          "| Strategy | Combos | 2026 trades | 2026 win | 2026 avg | Pre-2026 trades | Pre-2026 win | Pre-2026 avg |",
          "|---|---|---|---|---|---|---|---|"]
    names = dict(zip(ins.strategy_id, ins.name))
    rows = []
    for sid in sorted({k[2] for k in act}):
        a_, o_ = ia[ia.strategy_id == sid], oa[oa.strategy_id == sid]
        rows.append((len(o_), f"| {names.get(sid, sid)} ({sid}) | {sum(1 for k in act if k[2] == sid)} | {len(a_)} | "
                     f"{(a_.net_pct > 0).mean():.0%} | {a_.net_pct.mean():+.2f}% | {len(o_)} | "
                     + (f"{(o_.net_pct > 0).mean():.0%} | {o_.net_pct.mean():+.2f}% |" if len(o_) else "- | - |")))
    L += [r for _, r in sorted(rows, reverse=True)] + [""]
    L += ["## Per year, 2026 ACTIVE combos out of sample", "", *HEAD]
    L += [_row(y, oa[oa.year == y]) for y in sorted(oa.year.unique())] + [""]
    if full is not None and (full / "active.json").is_file():
        fa = _active_rows(full)
        ft = _trades(full)
        f_act = ft[ft.key.isin(fa)]
        L += ["## Full history, exits re-learned on all of it", "", f"Run: `{full}`. ACTIVE {len(fa)} combos; "
              f"overlap with 2026 ACTIVE: {len(fa & act)}.", "", *HEAD,
              _row("Full-history ACTIVE, all years", f_act),
              _row("Full-history ACTIVE, pre-2026", f_act[f_act.year < "2026"]),
              _row("Full-history ACTIVE, 2026", f_act[f_act.year == "2026"]),
              _row("2026 ACTIVE combos, all years (re-learned exits)", ft[ft.key.isin(act)]), ""]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--insample", required=True, type=Path)
    ap.add_argument("--oos", required=True, type=Path)
    ap.add_argument("--full", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    out = a.out or a.oos.parent / "COMPARE.md"
    out.write_text(build(a.insample, a.oos, a.full), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
