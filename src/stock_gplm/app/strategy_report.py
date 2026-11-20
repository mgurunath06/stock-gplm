"""Per-stock strategy report from a research-matrix run (owner 2026-11-20: see progress and results per stock).

    python -m stock_gplm.app.strategy_report <research dir> [--stock RELIANCE]

Writes <research dir>/STOCK_REPORT.md (or STOCK_REPORT-<SYM>.md):
  * progress: per stock, lanes (timeframes) finished, strategies tested / blocked per lane;
  * per stock x timeframe: every library strategy with its name, trades and validation result, owner strategies
    (Chartink O2-*, O1-*, GN-*) first, then the best of the rest.
Reads only matrix.csv / matrix.sqlite (no recomputation).
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OWNER = ("O2-", "O1-", "GN-")


def names() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in (REPO / "config" / "strategies" / "library").rglob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for s in doc.get("strategies", []) if isinstance(doc, dict) else []:
            if isinstance(s, dict) and s.get("id"):
                out[s["id"]] = s.get("name") or s["id"]
    return out


def _f(x: str | None, pct: bool = False) -> str:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "-"
    return f"{v * 100:+.2f}%" if pct else f"{v:.2f}"


def build(run: Path, stock: str | None = None) -> Path:
    nm = names()
    rows = list(csv.DictReader((run / "matrix.csv").open(encoding="utf-8"))) if (run / "matrix.csv").is_file() else []
    lanes: list[dict[str, str]] = []
    db = run / "matrix.sqlite"
    if db.is_file():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        lanes = [dict(r) for r in c.execute("SELECT * FROM lanes")]
        if not rows:
            rows = [dict(r) for r in c.execute("SELECT * FROM matrix")]
        c.close()
    sym = (stock.upper() + "-EQ") if stock and not stock.upper().endswith("-EQ") else (stock.upper() if stock else None)
    if sym:
        rows = [r for r in rows if r["symbol"] == sym]
        lanes = [ln for ln in lanes if ln["symbol"] == sym]
    lines = [f"# Strategy report: {run.name}{' - ' + sym if sym else ''}", "",
             "Validation = out-of-sample period after training (net of costs). Owner strategies first.", "",
             "## Progress per stock", "", "| Stock | Timeframes done | Strategies tested | Blocked | Last update (UTC) |",
             "|---|---|---|---|---|"]
    per: dict[str, list[dict[str, str]]] = defaultdict(list)
    for ln in lanes:
        per[ln["symbol"]].append(ln)
    for s in sorted(per):
        ls = per[s]
        lines.append(f"| {s} | {', '.join(sorted(x['timeframe'] for x in ls))} | "
                     f"{sum(int(x.get('library_tested') or 0) for x in ls)} | "
                     f"{sum(int(x.get('library_blocked') or 0) for x in ls)} | "
                     f"{max((x.get('created_utc') or '')[:16] for x in ls)} |")
    by: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        if r.get("source") != "ML":
            by[(r["symbol"], r["timeframe"])].append(r)

    def key(r: dict[str, str]) -> float:
        try:
            return -float(r.get("val_mean_net") or "nan")
        except ValueError:
            return 0.0

    for (s, tf) in sorted(by):
        rs = by[(s, tf)]
        own = sorted((r for r in rs if r["strategy_id"].startswith(OWNER)), key=key)
        rest = sorted((r for r in rs if not r["strategy_id"].startswith(OWNER) and r.get("val_trades")), key=key)[:15]
        lines += ["", f"## {s} {tf}: {len(rs)} strategies ({sum(r.get('status') == 'BLOCKED' for r in rs)} blocked)", "",
                  "| Strategy | Side | Result | Val trades | Val avg net | Val win rate | Val profit factor | Note |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in own + rest:
            note = (r.get("block_reason") or r.get("reasons") or "")[:70].replace("|", "/").replace("\n", " ")
            lines.append(f"| {nm.get(r['strategy_id'], r['strategy_id'])} ({r['strategy_id']}) | {r.get('direction')} | "
                         f"{r.get('classification') or r.get('status')} | {r.get('val_trades') or '-'} | "
                         f"{_f(r.get('val_mean_net'), True)} | {_f(r.get('val_win_rate'), True).lstrip('+')} | "
                         f"{_f(r.get('val_profit_factor'))} | {note} |")
    out = run / (f"STOCK_REPORT-{sym.removesuffix('-EQ')}.md" if sym else "STOCK_REPORT.md")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", help="research output directory (contains matrix.csv / matrix.sqlite)")
    ap.add_argument("--stock")
    a = ap.parse_args(argv)
    print(build(Path(a.run), a.stock))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
