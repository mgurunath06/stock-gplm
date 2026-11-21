"""Clean, order and annotate the stock universe (owner 2026-11-21). Reads local files only; changes no candle.

    python -m stock_gplm.app.universe_build [--write]

1. Clean: drops ETFs, index funds, REITs, InvITs, liquid/gilt/bond funds and placeholder symbols (DUMMY*).
   Names come from the saved Yahoo replies (data/raw/yahoo) and the NSE Indices constituent files
   (data/reference/nifty-constituents); a symbol with no known name is kept and listed as "unknown".
2. Order (the cloud fills minute history in this order): Nifty 50, Bank Nifty, Nifty 100, Nifty 200, Nifty 500,
   Total Market, Microcap 250, then the rest in their current order. Index members missing from the universe are
   added.
3. Listing dates: first Shoonya daily date per stock (a listing or a rename) -> data/reference/listing_dates.json,
   copied to the cloud as ~/gplm-live/listing.json so sessions before it are neither fetched nor expected.
4. Renames: a stock whose Shoonya history starts late while Yahoo has earlier history is a rename (or demerger).
   When an old Shoonya folder ends the session before and its last close continues the new symbol's price, it is
   recorded in data/reference/symbol_aliases.json (new -> old; ingest then reads both). Manual entries are kept.
Report: state/reports/universe/<date>/UNIVERSE.md. Without --write nothing but the report is written.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
APP = ROOT / "app"
UNIVERSE = APP / "deploy" / "oracle" / "live" / "universe.txt"
REF = ROOT / "data" / "reference"
CONSTITUENTS = REF / "nifty-constituents"
SHOONYA = ROOT / "data" / "raw" / "shoonya" / "NSE"
YAHOO = ROOT / "data" / "raw" / "yahoo"
ORDER = [("Nifty 50", "ind_nifty50list"), ("Bank Nifty", "ind_niftybanklist"), ("Nifty 100", "ind_nifty100list"),
         ("Nifty 200", "ind_nifty200list"), ("Nifty 500", "ind_nifty500list"),
         ("Total Market", "ind_niftytotalmarket_list"), ("Microcap 250", "ind_niftymicrocap250_list")]
NOT_STOCK = re.compile(r"\b(ETF|BEES|FUND|FUNDS|REIT|INVIT|TRUST|LIQUID|GILT|BHARAT BOND|INDEX|NIFTY|SENSEX)\b",
                       re.IGNORECASE)
SHOONYA_BASE = "2022-11-15"  # Shoonya daily starts 2022-10-31; later first dates are listings or renames


def index_members() -> tuple[dict[str, list[str]], dict[str, tuple[str, str]]]:
    groups, names = {}, {}
    for title, f in ORDER:
        p = CONSTITUENTS / f"{f}.csv"
        if not p.is_file():
            continue
        rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
        groups[title] = [r["Symbol"].strip().upper() for r in rows]
        for r in rows:
            names.setdefault(r["Symbol"].strip().upper(), (r.get("Company Name", ""), r.get("Series", "EQ")))
    return groups, names


def yahoo_meta(sym: str) -> dict:
    files = sorted((YAHOO / sym).glob("*.json")) if (YAHOO / sym).is_dir() else []
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))["chart"]["result"][0]["meta"] if files else {}
    except (ValueError, KeyError, IndexError, TypeError):
        return {}


def _rows(folder: Path) -> dict[str, float]:
    out = {}
    for f in sorted(folder.glob("*.json.gz")):
        if not (folder / f"{f.name}.done").exists():
            continue
        for r in json.load(gzip.open(f)):
            r = json.loads(r) if isinstance(r, str) else r
            try:
                out[datetime.strptime(r["time"][:11], "%d-%b-%Y").date().isoformat()] = float(r["intc"])
            except (KeyError, ValueError):
                continue
    return out


def first_last(sym: str, cache: dict) -> tuple[str | None, str | None]:
    if sym not in cache:
        rows = _rows(SHOONYA / f"{sym}-EQ")
        cache[sym] = (min(rows), max(rows), rows) if rows else (None, None, {})
    return cache[sym][0], cache[sym][1]


def yahoo_closes(meta_sym: str) -> dict[str, float]:
    from .yahoo_history import parse
    files = sorted((YAHOO / meta_sym).glob("*.json")) if (YAHOO / meta_sym).is_dir() else []
    if not files:
        return {}
    rows, _ = parse(json.loads(files[-1].read_text(encoding="utf-8")))
    return {d: c for d, o, h, lo, c, v in rows}


def find_rename(sym: str, first: str, cache: dict, ends: dict[str, list[str]]) -> tuple[str | None, str]:
    """Old Shoonya folder ending within 10 days before ``first`` whose last close continues Yahoo's (scaled)
    history of the new symbol within 3%."""
    y = yahoo_closes(sym)
    new_rows = cache[sym][2]
    if not y or first not in y or first not in new_rows:
        return None, "no Yahoo history to compare"
    if min(y) >= first:
        return None, "new listing (Yahoo starts at the same day)"
    k = new_rows[first] / y[first]
    lo = (date.fromisoformat(first) - timedelta(days=10)).isoformat()
    hits = []
    for last_day, syms in ends.items():
        if lo <= last_day < first:
            for old in syms:
                if old == sym:
                    continue
                oc = cache[old][2].get(last_day)
                if oc and last_day in y and abs(oc / (y[last_day] * k) - 1) < 0.03:
                    hits.append(old)
    if len(hits) == 1:
        return hits[0], "rename: old folder continues the price"
    return None, ("Yahoo has earlier history but no single old Shoonya folder matches "
                  f"({len(hits)} candidates) - likely rename/demerger; add manually if known")


def build(write: bool) -> Path:
    current = [x.strip().upper().removesuffix("-EQ") for x in UNIVERSE.read_text(encoding="utf-8").split() if x.strip()]
    groups, idx_names = index_members()
    candidates = list(dict.fromkeys(current + [s for _, f in ORDER for s in groups.get(_, [])]))
    excluded, unknown, keep = {}, [], []
    for s in candidates:
        meta = yahoo_meta(s)
        name = meta.get("longName") or meta.get("shortName") or idx_names.get(s, ("", ""))[0]
        series = idx_names.get(s, ("", "EQ"))[1] or "EQ"
        if s.startswith("DUMMY"):
            excluded[s] = "placeholder symbol"
        elif meta.get("instrumentType") and meta["instrumentType"] != "EQUITY":
            excluded[s] = f"Yahoo type {meta['instrumentType']}: {name}"
        elif series.strip().upper() not in ("EQ", "BE", "BZ", ""):
            excluded[s] = f"series {series}: {name}"
        elif name and NOT_STOCK.search(name):
            excluded[s] = f"not a company share: {name}"
        else:
            if not name:
                unknown.append(s)
            keep.append(s)
    # order
    rank = {}
    for i, (title, _) in enumerate(ORDER):
        for s in groups.get(title, []):
            rank.setdefault(s, i)
    pos = {s: i for i, s in enumerate(current)}
    ordered = sorted(keep, key=lambda s: (rank.get(s, len(ORDER)), pos.get(s, 10**6)))
    added = [s for s in ordered if s not in pos]
    # listing dates + renames
    cache: dict = {}
    ends: dict[str, list[str]] = {}
    for d in SHOONYA.iterdir() if SHOONYA.is_dir() else []:
        if d.is_dir() and d.name.endswith("-EQ"):
            f, l = first_last(d.name.removesuffix("-EQ"), cache)
            if l:
                ends.setdefault(l, []).append(d.name.removesuffix("-EQ"))
    listing, renames, rename_notes = {}, {}, {}
    alias_file = REF / "symbol_aliases.json"
    aliases = json.loads(alias_file.read_text(encoding="utf-8")) if alias_file.is_file() else {}
    for s in ordered:
        f, _ = first_last(s, cache)
        if not f:
            continue
        listing[s] = f
        if f > SHOONYA_BASE and s not in aliases:
            old, why = find_rename(s, f, cache, ends)
            rename_notes[s] = (f, old, why)
            if old:
                renames[s] = old
    if write:
        stamp = date.today().isoformat()
        shutil.copy2(UNIVERSE, UNIVERSE.with_name(f"universe.before-{stamp}.txt"))
        UNIVERSE.write_text("\n".join(ordered) + "\n", encoding="utf-8")
        REF.mkdir(parents=True, exist_ok=True)
        (REF / "listing_dates.json").write_text(json.dumps(listing, indent=1, sort_keys=True), encoding="utf-8")
        alias_file.write_text(json.dumps({**aliases, **renames}, indent=1, sort_keys=True), encoding="utf-8")
    out = ROOT / "state" / "reports" / "universe" / date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    counts = {t: sum(1 for s in ordered if rank.get(s) == i) for i, (t, _) in enumerate(ORDER)}
    L = [f"# Universe {date.today()}" + ("" if write else " (dry run: nothing written but this report)"), "",
         f"Before {len(current)}, candidates {len(candidates)}, kept {len(ordered)}, excluded {len(excluded)}, "
         f"added from index files {len(added)}, unknown name (kept) {len(unknown)}.", "",
         "Fill order (first group each stock belongs to): " + ", ".join(f"{t} {n}" for t, n in counts.items())
         + f", rest {len(ordered) - sum(counts.values())}.", "",
         "## Excluded", "", "| Symbol | Reason |", "|---|---|",
         *[f"| {s} | {r} |" for s, r in sorted(excluded.items())], "",
         "## Added (index members not in the universe before)", "", ", ".join(added) or "-", "",
         "## Late Shoonya start (listing or rename)", "", "| Symbol | First Shoonya day | Old symbol | Note |",
         "|---|---|---|---|",
         *[f"| {s} | {f} | {o or '-'} | {w} |" for s, (f, o, w) in sorted(rename_notes.items())], "",
         "## Unknown names (kept)", "", ", ".join(unknown) or "-", ""]
    (out / "UNIVERSE.md").write_text("\n".join(L), encoding="utf-8")
    return out / "UNIVERSE.md"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="write universe.txt (old one kept), listing dates, aliases")
    a = ap.parse_args(argv)
    print(build(a.write))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
