"""Daily-data sanity audit: missing days, bad candles, and agreement where sources overlap (owner 2026-11-21).

    python -m stock_gplm.app.data_audit [--stocks @deploy/oracle/live/universe.txt | RELIANCE,TCS] [--workers 16]
        [--out H:/Guru/Learning/StockGLPM/state/reports/data-audit/<today>]

Read-only: no candle file is changed. Sources per stock:
  S   Shoonya daily              data/raw/shoonya/NSE/<SYM>-EQ          (main source, late 2022 ->)
  YR  Yahoo raw replies          data/raw/yahoo/<SYM>/<date>.json       (split-adjusted, unscaled)
  YD  Yahoo derived (used)       data/derived/yahoo-adjusted/NSE        (scaled onto Shoonya, older dates only)
  NR  NSE MCP raw replies        data/raw/nse-mcp/<SYM>/*.json          (unadjusted bhavcopy)
  ND  NSE MCP derived (used)     data/derived/nse-mcp-adjusted/NSE      (adjusted + scaled, older dates only)
  RC  NSE MCP recent (used)      data/derived/nse-mcp-recent/NSE        (dates after Shoonya's last one)
Trading calendar: NIFTY 50 index dates (data/raw/nse-index); before the index history starts, dates on which
at least 4 of 7 large stocks have a Yahoo candle.

Checks
  1. Completeness per source: first/last date, trading days expected vs present, missing %, longest gap,
     dates that are not trading days.
  2. Candle quality: impossible OHLC, zero volume, close-to-close jumps > 20% (> 40% = likely unadjusted
     split/bonus), runs of >= 5 identical closes.
  3. Overlap agreement for YR-S, NR-S, NR-YR, YR-ND, RC-YR: the other source is scaled by the median close
     ratio, then per-day close deviation (median / p95 / max, share > 1% and > 2%), high/low deviation, and
     scale drift (median ratio of the first vs last quarter of the overlap - dividend adjustment or a missed
     split shows up here). Worst days go to mismatches.csv.
  4. Stitch junctions of what the app actually reads (YD -> ND -> S -> RC): jump between the last close of one
     source and the first open of the next.
Outputs in --out: SUMMARY.md, stocks.csv (one row per stock x source), overlaps.csv (one row per stock x pair),
mismatches.csv (worst 20 days per stock x pair), junctions.csv. Per-stock results are cached in _per_stock/
so a stopped run resumes.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
DATA = ROOT / "data"
SRC = {
    "S": DATA / "raw" / "shoonya" / "NSE",
    "YD": DATA / "derived" / "yahoo-adjusted" / "NSE",
    "ND": DATA / "derived" / "nse-mcp-adjusted" / "NSE",
    "RC": DATA / "derived" / "nse-mcp-recent" / "NSE",
}
YR_ROOT = DATA / "raw" / "yahoo"
NR_ROOT = DATA / "raw" / "nse-mcp"
INDEX = DATA / "raw" / "nse-index" / "NSE" / "IDX_NIFTY_50-EQ"
BIG = ["RELIANCE", "HDFCBANK", "INFY", "ITC", "TCS", "SBIN", "LT"]
PAIRS = [("YR", "S"), ("NR", "S"), ("NR", "YR"), ("YR", "ND"), ("RC", "YR")]
CHAIN = ["YD", "ND", "S", "RC"]  # oldest -> newest, as the app stitches them


# ------------------------------------------------------------------ readers -> DataFrame[date, o, h, l, c, v]
def _df(rows: list[tuple]) -> pd.DataFrame:
    d = pd.DataFrame(rows, columns=["date", "o", "h", "l", "c", "v"])
    return d.drop_duplicates("date", keep="last").set_index("date").sort_index() if len(d) else d.set_index("date")


def _parse_time(s: str) -> str:
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s if " " in fmt else s[:11].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return date.fromisoformat(s[:10]).isoformat()


def read_shoonya_format(folder: Path) -> tuple[pd.DataFrame, int]:
    """Shoonya-format yearly files (also used by the derived roots and the index). -> (frame, duplicate rows)."""
    rows, n = [], 0
    if not folder.is_dir():
        return _df([]), 0
    for f in sorted(folder.glob("*.json.gz")):
        if not (folder / f"{f.name}.done").exists():
            continue
        for r in json.load(gzip.open(f)):
            r = json.loads(r) if isinstance(r, str) else r
            try:
                rows.append((_parse_time(r["time"]), float(r["into"]), float(r["inth"]), float(r["intl"]),
                             float(r["intc"]), float(r.get("intv") or 0)))
                n += 1
            except (KeyError, ValueError, TypeError):
                continue
    d = _df(rows)
    return d, n - len(d)


def read_yahoo_raw(sym: str) -> pd.DataFrame:
    from .yahoo_history import parse
    files = sorted((YR_ROOT / sym).glob("*.json")) if (YR_ROOT / sym).is_dir() else []
    if not files:
        return _df([])
    rows, _ = parse(json.loads(files[-1].read_text(encoding="utf-8")))
    return _df(rows)


def read_nse_raw(sym: str) -> pd.DataFrame:
    from .nse_history import parse_rows
    rows: dict[str, tuple] = {}
    d = NR_ROOT / sym
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            for r in parse_rows(json.loads(f.read_text(encoding="utf-8")), sym):
                rows[r[0]] = (r[0], r[1], r[2], r[3], r[4], r[6])
        except (ValueError, OSError):
            continue
    return _df(list(rows.values()))


def calendar() -> pd.Index:
    idx, _ = read_shoonya_format(INDEX)
    days = set(idx.index)
    start = min(days) if days else "9999"
    votes: dict[str, int] = {}
    for s in BIG:
        for d in read_yahoo_raw(s).index:
            if d < start:
                votes[d] = votes.get(d, 0) + 1
    days |= {d for d, n in votes.items() if n >= 4}
    return pd.Index(sorted(days))


# ------------------------------------------------------------------ checks
def completeness(d: pd.DataFrame, cal: pd.Index) -> dict[str, Any]:
    if d.empty:
        return {"rows": 0}
    first, last = d.index[0], d.index[-1]
    exp = cal[(cal >= first) & (cal <= last)]
    have = set(d.index)
    miss = [x for x in exp if x not in have]
    pos = np.flatnonzero(~np.isin(exp, list(have)))
    longest = 0
    if len(pos):
        runs = np.split(pos, np.flatnonzero(np.diff(pos) != 1) + 1)
        longest = max(len(r) for r in runs)
    c = d["c"].to_numpy()
    ch = np.abs(c[1:] / c[:-1] - 1) if len(c) > 1 else np.array([])
    same = (np.diff(c) == 0).astype(int) if len(c) > 1 else np.array([], int)
    flat = 0
    run = 0
    for x in same:
        run = run + 1 if x else 0
        flat += run == 4  # 5 identical closes
    bad = ((d.h < d[["o", "c", "l"]].max(axis=1) - 1e-6) | (d.l > d[["o", "c", "h"]].min(axis=1) + 1e-6)
           | (d[["o", "h", "l", "c"]].min(axis=1) <= 0))
    return {"rows": len(d), "first": first, "last": last, "expected": len(exp), "missing": len(miss),
            "missing_pct": round(100 * len(miss) / max(len(exp), 1), 2), "longest_gap": longest,
            "first_missing": ";".join(miss[:5]), "not_trading_day": int((~d.index.isin(cal)).sum()),
            "bad_ohlc": int(bad.sum()), "zero_volume": int((d.v <= 0).sum()),
            "jumps_20pct": int((ch > 0.20).sum()), "jumps_40pct": int((ch > 0.40).sum()), "flat_runs_5": int(flat)}


def overlap(a: pd.DataFrame, b: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    """b scaled onto a by the median close ratio; per-day deviations."""
    common = a.index.intersection(b.index)
    common = common[(a.loc[common].c > 0).to_numpy() & (b.loc[common].c > 0).to_numpy()] if len(common) else common
    if len(common) < 5:
        return {"overlap_days": int(len(common))}, pd.DataFrame()
    x, y = a.loc[common], b.loc[common]
    r = (x.c / y.c).to_numpy()
    k = float(np.median(r))
    dev = np.abs(x.c / (y.c * k) - 1)
    hl = np.maximum(np.abs(x.h / (y.h * k) - 1), np.abs(x.l / (y.l * k) - 1))
    q = max(len(r) // 4, 1)
    drift = float(np.median(r[-q:]) / np.median(r[:q]) - 1)
    out = {"overlap_days": int(len(common)), "from": common[0], "to": common[-1], "scale": round(k, 6),
           "scale_drift_pct": round(100 * drift, 2), "close_dev_median_pct": round(100 * float(np.median(dev)), 3),
           "close_dev_p95_pct": round(100 * float(np.quantile(dev, 0.95)), 3),
           "close_dev_max_pct": round(100 * float(dev.max()), 2), "days_over_1pct": int((dev > 0.01).sum()),
           "days_over_2pct": int((dev > 0.02).sum()), "hl_dev_p95_pct": round(100 * float(np.quantile(hl, 0.95)), 3)}
    worst = pd.DataFrame({"date": common, "close_a": x.c.to_numpy(), "close_b_scaled": (y.c * k).to_numpy(),
                          "dev_pct": (100 * dev).round(3).to_numpy()}).nlargest(20, "dev_pct")
    return out, worst


def audit_stock(sym: str, cal_list: list[str], cache: str) -> dict[str, Any]:
    cf = Path(cache) / f"{sym}.json"
    if cf.is_file():
        return json.loads(cf.read_text(encoding="utf-8"))
    cal = pd.Index(cal_list)
    frames: dict[str, pd.DataFrame] = {}
    res: dict[str, Any] = {"stock": sym, "sources": [], "overlaps": [], "mismatches": [], "junctions": []}
    for k, root in SRC.items():
        frames[k], dup = read_shoonya_format(root / f"{sym}-EQ")
        if not frames[k].empty:
            res["sources"].append({"stock": sym, "source": k, "duplicates": dup, **completeness(frames[k], cal)})
    frames["YR"] = read_yahoo_raw(sym)
    frames["NR"] = read_nse_raw(sym)
    for k in ("YR", "NR"):
        if not frames[k].empty:
            res["sources"].append({"stock": sym, "source": k, "duplicates": 0, **completeness(frames[k], cal)})
    for a, b in PAIRS:
        if frames[a].empty or frames[b].empty:
            continue
        o, w = overlap(frames[a], frames[b])
        res["overlaps"].append({"stock": sym, "pair": f"{a}-{b}", **o})
        if len(w):
            res["mismatches"] += [{"stock": sym, "pair": f"{a}-{b}", **r} for r in w.to_dict("records")]
    used = [k for k in CHAIN if not frames[k].empty]
    for p, n in zip(used, used[1:]):
        lc, fo = frames[p].c.iloc[-1], frames[n].o.iloc[0]
        res["junctions"].append({"stock": sym, "from": p, "to": n, "last_date": frames[p].index[-1],
                                 "next_date": frames[n].index[0], "last_close": lc, "next_open": fo,
                                 "jump_pct": round(100 * (fo / lc - 1), 2),
                                 "overlapping_days": int(len(frames[p].index.intersection(frames[n].index)))})
    tmp = cf.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, default=str), encoding="utf-8")
    os.replace(tmp, cf)
    return res


# ------------------------------------------------------------------ summary
def summary(src: pd.DataFrame, ov: pd.DataFrame, jn: pd.DataFrame, n_stocks: int, cal: pd.Index) -> str:
    L = [f"# Daily data audit {date.today()}", "", f"Stocks: {n_stocks}. Calendar: {len(cal)} trading days "
         f"{cal[0]} .. {cal[-1]}.", "", "## 1. Completeness and candle quality per source", "",
         "| Source | Stocks | Median first | Median missing % | Stocks > 1% missing | Stocks with gap >= 5 days | "
         "Bad OHLC rows | Zero-volume rows | Jumps > 40% | Flat runs | Non-trading-day rows |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k in ["YR", "YD", "NR", "ND", "S", "RC"]:
        x = src[src.source == k]
        if x.empty:
            continue
        L.append(f"| {k} | {len(x)} | {sorted(x['first'].astype(str))[len(x) // 2]} | "
                 f"{x.missing_pct.median():.2f} | {(x.missing_pct > 1).sum()} | {(x.longest_gap >= 5).sum()} | "
                 f"{int(x.bad_ohlc.sum())} | {int(x.zero_volume.sum())} | {int(x.jumps_40pct.sum())} | "
                 f"{int(x.flat_runs_5.sum())} | {int(x.not_trading_day.sum())} |")
    L += ["", "S = Shoonya, YR/YD = Yahoo raw/derived, NR/ND = NSE MCP raw/derived, RC = NSE MCP recent.", "",
          "## 2. Agreement where sources overlap", "",
          "| Pair | Stocks | Median overlap days | Median close dev % | p95 close dev % (median stock) | "
          "Stocks with any day > 2% | Stocks p95 > 1% | Stocks scale drift > 2% |",
          "|---|---|---|---|---|---|---|---|"]
    for p in [f"{a}-{b}" for a, b in PAIRS]:
        x = ov[(ov.pair == p) & ov.overlap_days.ge(5)] if len(ov) else ov
        if not len(x):
            continue
        L.append(f"| {p} | {len(x)} | {x.overlap_days.median():.0f} | {x.close_dev_median_pct.median():.3f} | "
                 f"{x.close_dev_p95_pct.median():.3f} | {(x.days_over_2pct > 0).sum()} | {(x.close_dev_p95_pct > 1).sum()} | "
                 f"{(x.scale_drift_pct.abs() > 2).sum()} |")
    L += ["", "Scale drift > 2% between the start and end of an overlap usually means one source is dividend-"
          "adjusted or missed a split/bonus.", "", "## 3. Stitch junctions (what the app reads)", "",
          "| From -> to | Stocks | Overlapping days (should be 0) | Junction jump > 10% | > 20% |", "|---|---|---|---|---|"]
    if len(jn):
        for (a, b), x in jn.groupby(["from", "to"]):
            L.append(f"| {a} -> {b} | {len(x)} | {(x.overlapping_days > 0).sum()} | {(x.jump_pct.abs() > 10).sum()} | "
                     f"{(x.jump_pct.abs() > 20).sum()} |")
    L += ["", "## 4. Worst stocks", ""]
    if len(ov):
        w = ov[ov.overlap_days.ge(5)].sort_values("close_dev_p95_pct", ascending=False).head(30)
        L += ["Highest p95 close deviation on an overlap:", "", "| Stock | Pair | Days | p95 % | Max % | Days > 2% | Scale drift % |",
              "|---|---|---|---|---|---|---|"]
        L += [f"| {r.stock} | {r.pair} | {r.overlap_days} | {r.close_dev_p95_pct} | {r.close_dev_max_pct} | "
              f"{r.days_over_2pct} | {r.scale_drift_pct} |" for r in w.itertuples()]
    m = src.sort_values("missing_pct", ascending=False).head(30)
    L += ["", "Most missing days:", "", "| Stock | Source | First | Last | Expected | Missing | % | Longest gap | First missing |",
          "|---|---|---|---|---|---|---|---|---|"]
    L += [f"| {r.stock} | {r.source} | {r.first} | {r.last} | {r.expected} | {r.missing} | {r.missing_pct} | "
          f"{r.longest_gap} | {r.first_missing} |" for r in m.itertuples()]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stocks", help="comma list or @file; default: every stock found in any source")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--out", type=Path, default=ROOT / "state" / "reports" / "data-audit" / date.today().isoformat())
    a = ap.parse_args(argv)
    if a.stocks:
        txt = Path(a.stocks[1:]).read_text(encoding="utf-8") if a.stocks.startswith("@") else a.stocks
        stocks = sorted({x.strip().upper().removesuffix("-EQ") for x in txt.replace(",", " ").split() if x.strip()})
    else:
        names = set()
        for root in [*SRC.values(), YR_ROOT, NR_ROOT]:
            if root.is_dir():
                names |= {p.name.removesuffix("-EQ") for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")}
        stocks = sorted(n for n in names if n != "minute")
    cache = a.out / "_per_stock"
    cache.mkdir(parents=True, exist_ok=True)
    cal = calendar()
    print(f"{len(stocks)} stocks, calendar {len(cal)} days {cal[0]}..{cal[-1]} -> {a.out}", flush=True)
    results = []
    with ProcessPoolExecutor(a.workers) as ex:
        futs = {ex.submit(audit_stock, s, list(cal), str(cache)): s for s in stocks}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                results.append(f.result())
            except Exception as e:  # one bad stock must not stop the audit
                print(f"{futs[f]}: {type(e).__name__}: {e}", flush=True)
            if i % 200 == 0:
                print(f"{i}/{len(stocks)}", flush=True)
    src = pd.DataFrame([r for x in results for r in x["sources"]])
    ov = pd.DataFrame([r for x in results for r in x["overlaps"]])
    mm = pd.DataFrame([r for x in results for r in x["mismatches"]])
    jn = pd.DataFrame([r for x in results for r in x["junctions"]])
    src.to_csv(a.out / "stocks.csv", index=False)
    ov.to_csv(a.out / "overlaps.csv", index=False)
    mm.to_csv(a.out / "mismatches.csv", index=False)
    jn.to_csv(a.out / "junctions.csv", index=False)
    (a.out / "SUMMARY.md").write_text(summary(src, ov, jn, len(stocks), cal), encoding="utf-8")
    print(a.out / "SUMMARY.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
