"""Parse NSE XBRL filings (financial results, shareholding patterns) into point-in-time figures.

    python -m stock_gplm.app.nse_xbrl facts --file <one.xml>          # every fact of one file (to check tags)
    python -m stock_gplm.app.nse_xbrl build [--since 2016-01-01]       # all downloaded files -> fundamentals tables

Two layers (owner 2026-11-22: zero look-ahead):
  1. facts  - every numeric fact of a file: tag (local name), context id, period (instant / start..end), dimensions
              (e.g. shareholder category), unit, decimals, value. Nothing is dropped, so a mapping change never needs
              a re-download.
  2. fields - a small mapping (FIELD_TAGS) picks the figures strategies need, for the filing's own period
              (the context whose dates equal the filing's from/to, no dimensions), and stamps them with filed_at
              from filings.csv. A backtest may use a value only on bars that start after filed_at.
Outputs data/derived/nse-filings/: results.csv (one row per results filing), shareholding.csv (one row per
shareholding filing), facts/<SYM>.csv.gz (all facts), build.log (files that failed to parse).
The tag lists are a first version from the Ind-AS / SEBI taxonomies; run `facts` on a few files and extend them
where a field stays empty.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .nse_filings import DERIVED, RAW

# field -> candidate local tag names, first match wins (case-insensitive)
FIELD_TAGS: dict[str, list[str]] = {
    "revenue": ["RevenueFromOperations", "RevenueFromOperationsNet", "IncomeFromOperations", "NetSalesIncomeFromOperations",
                "TotalRevenueFromOperations", "InterestEarned"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income", "TotalIncome", "RevenueFromOperationsAndOtherIncome"],
    "total_expenses": ["Expenses", "TotalExpenses", "ExpensesTotal"],
    "finance_costs": ["FinanceCosts", "FinanceCost", "InterestExpended"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense", "DepreciationAndAmortisationExpense",
                     "DepreciationAmortisationAndImpairmentExpense"],
    "pbt": ["ProfitBeforeTax", "ProfitLossBeforeTax", "ProfitBeforeExceptionalItemsAndTax"],
    "tax": ["TaxExpense", "TotalTaxExpenses", "CurrentTax"],
    "net_profit": ["ProfitLossForPeriod", "ProfitLossForThePeriod", "ProfitLossFromContinuingOperations",
                   "NetProfitLossForThePeriod", "ProfitLossForPeriodFromContinuingOperations"],
    "net_profit_owners": ["ProfitOrLossAttributableToOwnersOfParent", "ProfitLossAttributableToOwnersOfParent"],
    "eps_basic": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                  "BasicEarningsLossPerShareFromContinuingOperations", "BasicEarningsPerShare",
                  "BasicEarningsLossPerShare"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "DilutedEarningsLossPerShareFromContinuingOperations", "DilutedEarningsPerShare"],
    "paid_up_capital": ["PaidUpValueOfEquityShareCapital", "EquityShareCapital"],
    "face_value": ["FaceValueOfEquityShareCapital"],
    "gross_npa_pct": ["PercentageOfGrossNpa", "PercentageOfGrossNPA"],
    "net_npa_pct": ["PercentageOfNpa", "PercentageOfNetNpa", "PercentageOfNetNPA"],
}
# shareholding: category dimension member keywords -> field (percentage of total shares)
SHP_TAGS = ["ShareholdingAsAPercentageOfTotalNumberOfShares", "ShareholdingAsPercentageOfTotalNumberOfShares",
            "PercentageOfShareholding"]
SHP_PLEDGE_TAGS = ["NumberOfSharesPledgedOrOtherwiseEncumberedAsAPercentageOfTotalSharesHeld",
                   "SharesPledgedOrOtherwiseEncumberedAsAPercentageOfTotalNumberOfShares",
                   "EncumberedSharesAsAPercentageOfTotalSharesHeld"]
SHP_CATEGORY = {"promoter": ["promoter"], "fii": ["foreignportfolio", "foreigninstitution", "fii", "fpi"],
                "dii": ["mutualfund", "insurance", "financialinstitution", "alternateinvestment", "pension",
                        "provident", "banks"], "public": ["public"]}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def facts(path: Path) -> list[dict[str, Any]]:
    """Every numeric fact with its context dates and dimension members."""
    root = ET.parse(path).getroot()
    ctx: dict[str, dict[str, Any]] = {}
    for c in root.iter():
        if _local(c.tag) != "context":
            continue
        cid = c.get("id", "")
        d: dict[str, Any] = {"start": "", "end": "", "instant": "", "dims": []}
        for e in c.iter():
            n = _local(e.tag)
            if n in ("startDate", "endDate", "instant"):
                d[{"startDate": "start", "endDate": "end", "instant": "instant"}[n]] = (e.text or "").strip()
            elif n in ("explicitMember", "typedMember"):
                d["dims"].append(f"{_local(e.get('dimension', ''))}={(e.text or ''.join(e.itertext())).strip()}")
        ctx[cid] = d
    out = []
    for e in root.iter():
        cref = e.get("contextRef")
        if not cref or e.text is None:
            continue
        txt = e.text.strip()
        try:
            val = float(txt.replace(",", ""))
        except ValueError:
            continue
        c = ctx.get(cref, {})
        out.append({"tag": _local(e.tag), "context": cref, "start": c.get("start", ""), "end": c.get("end", ""),
                    "instant": c.get("instant", ""), "dims": ";".join(c.get("dims", [])), "unit": e.get("unitRef", ""),
                    "decimals": e.get("decimals", ""), "value": val})
    return out


def _period_match(f: dict[str, Any], frm: str, to: str) -> bool:
    if f["dims"]:
        return False
    if f["start"] and f["end"]:
        return (not frm or f["start"] == frm) and (not to or f["end"] == to)
    return bool(f["instant"]) and (not to or f["instant"] == to)


def result_fields(fs: list[dict[str, Any]], frm: str, to: str) -> dict[str, Any]:
    by = {}
    for f in fs:
        by.setdefault(f["tag"].lower(), []).append(f)
    row: dict[str, Any] = {}
    for field, tags in FIELD_TAGS.items():
        for t in tags:
            cands = [f for f in by.get(t.lower(), []) if _period_match(f, frm, to)]
            if not cands:  # fall back: any undimensioned fact of this tag ending on the filing's end date
                cands = [f for f in by.get(t.lower(), []) if not f["dims"] and (f["end"] or f["instant"]) == to]
            if cands:
                row[field] = cands[0]["value"]
                break
    return row


def shareholding_fields(fs: list[dict[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    pct = [f for f in fs if f["tag"].lower() in {t.lower() for t in SHP_TAGS}]
    pled = [f for f in fs if f["tag"].lower() in {t.lower() for t in SHP_PLEDGE_TAGS}]
    for field, keys in SHP_CATEGORY.items():
        vals = [f["value"] for f in pct if any(k in re.sub(r"[^a-z]", "", f["dims"].lower()) for k in keys)]
        if vals:
            row[f"{field}_pct"] = max(vals)  # the category total is the largest of its sub-rows
    pv = [f["value"] for f in pled if "promoter" in f["dims"].lower()] or [f["value"] for f in pled if not f["dims"]]
    if pv:
        row["promoter_pledged_pct"] = max(pv)
    return row


def cmd_facts(a: argparse.Namespace) -> int:
    for f in facts(Path(a.file)):
        print(f"{f['tag']:<70} {f['start'] or f['instant']:>10} {f['end']:>10} {f['value']:>18,.2f} {f['unit']:<8} {f['dims'][:60]}")
    return 0


def cmd_build(a: argparse.Namespace) -> int:
    src = DERIVED / "filings.csv"
    if not src.is_file():
        raise SystemExit("run nse_filings index first")
    with src.open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["xbrl_url"] and (r["to"] or r["filed_at"])[:10] >= a.since]
    res_rows, shp_rows, failed = [], [], 0
    facts_dir = DERIVED / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    per_sym: dict[str, list[dict[str, Any]]] = {}
    for i, r in enumerate(rows, 1):
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(r["xbrl_url"]).name)
        p = RAW / r["symbol"] / "xbrl" / name
        if not p.is_file():
            continue
        try:
            fs = facts(p)
        except ET.ParseError as exc:
            failed += 1
            with (DERIVED / "build.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{p} {exc}\n")
            continue
        for f in fs:
            per_sym.setdefault(r["symbol"], []).append({"file": name, **f})
        base = {k: r[k] for k in ("symbol", "kind", "period", "from", "to", "consolidated", "audited", "filed_at")}
        if r["kind"].startswith("results"):
            res_rows.append({**base, **result_fields(fs, r["from"], r["to"]), "file": name})
        else:
            shp_rows.append({**base, "promoter_pct_list": r.get("promoter_pct", ""), **shareholding_fields(fs),
                             "file": name})
        if i % 2000 == 0:
            print(f"{i}/{len(rows)} files parsed", flush=True)
    for sym, fl in per_sym.items():
        with gzip.open(facts_dir / f"{sym}.csv.gz", "wt", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(fl[0]))
            w.writeheader()
            w.writerows(fl)
    for name, data in (("results.csv", res_rows), ("shareholding.csv", shp_rows)):
        cols = list(dict.fromkeys(k for d in data for k in d))
        with (DERIVED / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(data)
        filled = {c: sum(1 for d in data if d.get(c) not in (None, "")) for c in cols}
        print(f"{name}: {len(data):,} rows; filled per field: "
              + ", ".join(f"{c} {100 * v / max(len(data), 1):.0f}%" for c, v in filled.items()
                          if c not in ("symbol", "kind", "period", "from", "to", "consolidated", "audited", "filed_at",
                                       "file")))
    print(f"parse failures: {failed} (build.log)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("facts")
    p.add_argument("--file", required=True)
    p = sub.add_parser("build")
    p.add_argument("--since", default="2016-01-01")
    a = ap.parse_args(argv)
    return {"facts": cmd_facts, "build": cmd_build}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
