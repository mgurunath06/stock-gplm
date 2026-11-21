"""Splits, bonuses and rights from the official NSE MCP; candles adjusted when loaded (owner 2026-11-21).

    python -m stock_gplm.app.corp_actions fetch [--stocks @deploy/oracle/live/universe.txt] [--from 2022-10-01]

fetch  get_corporate_actions per stock (one call each, resumable) -> data/reference/corporate-actions/<SYM>.json
       (raw reply). Only events with adjustmentFactor != 1 (SPLIT / BONUS / rights) matter here.
apply  (used by ingest for every stock, nothing to run): prices before each ex-date x adjustmentFactor, volume /
       adjustmentFactor - for daily and minute candles separately, and ONLY where that data really shows the
       unadjusted step (last close before the ex-date vs first open on it within 35% of the expected ratio);
       already-adjusted data is left alone. Raw files are never changed. Decisions are logged to
       data/reference/corporate-actions/_applied.jsonl.
Why from Oct 2022: Shoonya daily starts 2022-10-31 and the older Yahoo rows are scaled onto Shoonya's level at
that point, so every action after it shifts all earlier rows (Yahoo's own history is already adjusted before it).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
CA_DIR = ROOT / "data" / "reference" / "corporate-actions"
FROM = "2022-10-01"
TOLERANCE = 0.35  # observed step vs expected ratio


def load_actions(sym: str) -> list[dict[str, Any]]:
    f = CA_DIR / f"{sym.upper().removesuffix('-EQ')}.json"
    if not f.is_file():
        return []
    try:
        acts = json.loads(f.read_text(encoding="utf-8")).get("actions") or []
    except ValueError:
        return []
    out = []
    for a in acts:
        try:
            k = float(a.get("adjustmentFactor") or 1.0)
        except (TypeError, ValueError):
            continue
        if a.get("exDate") and k > 0 and abs(k - 1.0) > 1e-6:
            out.append({"exDate": str(a["exDate"])[:10], "factor": k, "type": a.get("actionType"),
                        "purpose": a.get("purpose", "")})
    return sorted(out, key=lambda a: a["exDate"])


def _step_ok(before_close: float, after_open: float, factor: float) -> bool:
    """Unadjusted data: after_open / before_close is about ``factor`` (e.g. 0.1 for a 1:10 split)."""
    if not (before_close > 0 and after_open > 0):
        return False
    r, k = after_open / before_close, factor
    if abs(np.log(k)) < 0.05:  # small rights adjustments: indistinguishable from a normal day; Shoonya is unadjusted
        return True
    # the observed gap must be closer to the expected step than to "no step", and within the tolerance
    return abs(np.log(r / k)) < abs(np.log(r)) and abs(r / k - 1) <= TOLERANCE


def _adjust(df: pd.DataFrame, day: pd.Series, acts: list[dict], kind: str, sym: str, log: list) -> pd.DataFrame:
    if df is None or not len(df) or not acts:
        return df
    df = df.copy()
    days = day.astype(str).str[:10].to_numpy()
    price = [c for c in ("open", "high", "low", "close") if c in df.columns]
    for a in acts:
        ex = a["exDate"]
        before = np.flatnonzero(days < ex)
        after = np.flatnonzero(days >= ex)
        if not len(before) or not len(after):
            continue
        if days[after[0]] > (pd.Timestamp(ex) + pd.Timedelta(days=10)).date().isoformat():
            log.append({"symbol": sym, "data": kind, **a, "decision": "skipped: no candle near the ex-date"})
            continue
        bc = float(df["close"].iloc[before[-1]])
        ao = float(df["open"].iloc[after[0]])
        if _step_ok(bc, ao, a["factor"]):
            df.loc[df.index[before], price] = df.loc[df.index[before], price].to_numpy() * a["factor"]
            if "volume" in df.columns:
                df.loc[df.index[before], "volume"] = df.loc[df.index[before], "volume"].to_numpy() / a["factor"]
            log.append({"symbol": sym, "data": kind, **a, "decision": "applied", "step": round(ao / bc, 4)})
        else:
            log.append({"symbol": sym, "data": kind, **a, "decision": "skipped: data already adjusted or step unclear",
                        "step": round(ao / bc, 4) if bc else None})
    return df


_LOGGED: set[str] = set()


def apply(data: Any) -> Any:
    """Adjust a SymbolData in place-safe way (returns a new object when something changed)."""
    from dataclasses import replace
    acts = load_actions(data.symbol)
    if not acts:
        return data
    log: list = []
    daily = _adjust(data.daily, data.daily["date"], acts, "daily", data.symbol, log) if len(data.daily) else data.daily
    minute = (_adjust(data.minute, data.minute["session"], acts, "minute", data.symbol, log)
              if len(data.minute) else data.minute)
    applied = [x for x in log if x["decision"] == "applied"]
    key = f"{data.symbol}|{data.fingerprint}"
    if log and key not in _LOGGED:
        _LOGGED.add(key)
        CA_DIR.mkdir(parents=True, exist_ok=True)
        with (CA_DIR / "_applied.jsonl").open("a", encoding="utf-8") as f:
            for x in log:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **x}) + "\n")
    if not applied:
        return data
    tag = ";".join(f"{x['data']}:{x['exDate']}:{x['factor']}" for x in applied)
    import hashlib
    fp = hashlib.sha256(f"{data.fingerprint}|ca|{tag}".encode()).hexdigest()
    return replace(data, daily=daily, minute=minute, fingerprint=fp,
                   summary={**(data.summary or {}), "corporate_actions_applied": tag})


# ------------------------------------------------------------------ fetch
async def _fetch(stocks: list[str], start: str, lanes: int, pause: float) -> tuple[int, int]:
    from ..m39.client import NSEMCPClient
    from ..m39.comparison import tool_payload
    CA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    q: asyncio.Queue = asyncio.Queue()
    for s in stocks:
        if not (CA_DIR / f"{s}.json").is_file():
            q.put_nowait(s)
    ok = fail = 0

    async def lane() -> None:
        nonlocal ok, fail
        async with NSEMCPClient("bhavcopy", connect_timeout=10, read_timeout=60, retries=3) as cl:
            await cl.connect()
            await cl.list_tools()
            while not q.empty():
                s = q.get_nowait()
                try:
                    p = tool_payload(await cl.call_tool("get_corporate_actions",
                                                        {"symbol": s, "fromDate": start, "toDate": today}))
                except Exception as e:  # retried on the next run
                    fail += 1
                    print(f"{s}: {type(e).__name__}: {str(e)[:100]}", flush=True)
                    await asyncio.sleep(pause * 5)
                    continue
                if isinstance(p, dict) and p.get("error"):
                    fail += 1  # NSE says the lookup failed: not "no actions"; asked again next run
                    print(f"{s}: error {str(p['error'])[:100]}", flush=True)
                    continue
                tmp = CA_DIR / f".{s}.{os.getpid()}.tmp"
                tmp.write_text(json.dumps({"fetched": today, "from": start, **(p if isinstance(p, dict) else {"actions": p})}),
                               encoding="utf-8")
                os.replace(tmp, CA_DIR / f"{s}.json")
                ok += 1
                await asyncio.sleep(pause)

    await asyncio.gather(*(lane() for _ in range(lanes)))
    return ok, fail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=["fetch"])
    ap.add_argument("--stocks", default=str(ROOT / "app" / "deploy" / "oracle" / "live" / "universe.txt"))
    ap.add_argument("--from", dest="start", default=FROM)
    ap.add_argument("--lanes", type=int, default=4)
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--refresh", action="store_true", help="fetch again even if a reply is saved")
    a = ap.parse_args(argv)
    src = a.stocks[1:] if a.stocks.startswith("@") else a.stocks
    p = Path(src)
    txt = p.read_text(encoding="utf-8") if p.is_file() else a.stocks
    stocks = [x.strip().upper().removesuffix("-EQ") for x in txt.replace(",", " ").split() if x.strip()]
    if a.refresh:
        for s in stocks:
            (CA_DIR / f"{s}.json").unlink(missing_ok=True)
    ok, fail = asyncio.run(_fetch(stocks, a.start, a.lanes, a.pause))
    with_actions = sum(1 for s in stocks if load_actions(s))
    print(f"fetched {ok}, failed {fail} (run again to retry); stocks with split/bonus/rights since {a.start}: "
          f"{with_actions}; files in {CA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
