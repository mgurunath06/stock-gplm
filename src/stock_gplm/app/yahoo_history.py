"""Long daily history (10+ years) from Yahoo Finance's public chart API (owner-approved 2026-11-20).

Unofficial, free source, split-adjusted. Personal, non-commercial research only. It fills dates older
than the NSE MCP history (which starts 2021-11-22) and Shoonya (2022-10-31).

    python -m stock_gplm.app.yahoo_history all --stocks RELIANCE,TCS [--research]
    python -m stock_gplm.app.yahoo_history all --stocks @deploy/oracle/live/universe.txt

Steps (each skips work already done):
  fetch   one call per stock: <SYM>.NS daily since 2000 -> data/raw/yahoo/<SYM>/<YYYY-MM-DD>.json, the raw
          reply, kept as it came. Not asked again unless you pass --refresh.
  build   drop rows with missing or impossible prices (counted in the report). Join onto Shoonya using
          the median close ratio of the first 20 overlap days, which must agree within 1%. Keep only dates
          before Shoonya's first candle (2026-11-21: NSE MCP derived files are no longer used); cross-check vs raw NSE
          (p95 > 1% -> stock dropped before Shoonya). Write Shoonya-format
          yearly files to data/derived/yahoo-adjusted/NSE/<SYM>-EQ/, plus build-report.json.
  config  rewrite config/app/gplm.windows.longdaily.json (NSE MCP + Yahoo extra roots, home state-long).
  --research  research-matrix 1d,1w on that config (same as nse_history --research).
Shoonya and NSE MCP files are untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import nse_history as nh

URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}.NS?period1={p1}&period2={p2}"
       "&interval=1d&events=split&includeAdjustedClose=true")
UA = {"User-Agent": "Mozilla/5.0 (research client)", "Accept": "application/json"}
MAX_BYTES = 20_000_000
IST = 19800  # Yahoo timestamps are the session open in UTC; +5:30 gives the IST trading date


def paths() -> tuple[Path, Path, Path, Path]:
    shoonya, nse_raw, nse_out = nh.paths()
    derived = nse_out.parent.parent
    return shoonya, nse_raw.parent / "yahoo", derived / "yahoo-adjusted" / "NSE", nse_out


# ------------------------------------------------------------------ parsing (pure)
def parse(payload: Any) -> tuple[list[tuple], dict[str, int]]:
    """Chart payload -> (sorted [(date, o, h, l, c, v)], rejects by reason). One row per IST date."""
    rej: dict[str, int] = {}
    res = ((payload or {}).get("chart") or {}).get("result") or []
    if not res:
        return [], {"no_result": 1}
    r = res[0]
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    out: dict[str, tuple] = {}
    for i, t in enumerate(ts):
        try:
            o, h, lo, c = (q[k][i] for k in ("open", "high", "low", "close"))
            v = q.get("volume", [None] * len(ts))[i] or 0
        except (KeyError, IndexError, TypeError):
            rej["malformed"] = rej.get("malformed", 0) + 1
            continue
        if None in (o, h, lo, c):
            rej["missing_price"] = rej.get("missing_price", 0) + 1
            continue
        if min(o, h, lo, c) <= 0 or h < max(o, c, lo) - 1e-9 or lo > min(o, c, h) + 1e-9:
            rej["impossible_ohlc"] = rej.get("impossible_ohlc", 0) + 1
            continue
        if not v:
            rej["zero_volume"] = rej.get("zero_volume", 0) + 1
            continue
        d = datetime.fromtimestamp(int(t) + IST, tz=timezone.utc).date().isoformat()
        out[d] = (d, float(o), float(h), float(lo), float(c), float(v))
    return [out[d] for d in sorted(out)], rej


def link(rows: list[tuple], shoonya: dict[str, float], before: str) -> tuple[list[tuple], str]:
    """Scale onto Shoonya using the first 20 overlap days; keep dates < ``before``."""
    if not rows or not shoonya:
        return [], "no Yahoo rows" if not rows else "no Shoonya daily"
    ratios = [shoonya[d] / c for d, o, h, lo, c, v in rows if d in shoonya and c > 0][:20]
    if len(ratios) < 5:
        return [], f"overlap with Shoonya too short ({len(ratios)} days)"
    k = sorted(ratios)[len(ratios) // 2]
    if max(abs(x / k - 1) for x in ratios) > 0.01:
        return [], "Yahoo and Shoonya disagree >1% on the overlap"
    keep = [(d, o * k, h * k, lo * k, c * k, v / k) for d, o, h, lo, c, v in rows if d < before]
    return keep, "ok"


# ------------------------------------------------------------------ fetch / build
def fetch(stocks: list[str], raw: Path, pause: float, refresh: bool) -> None:
    p1 = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp())
    for n, s in enumerate(stocks, 1):
        folder = raw / s
        if not refresh and folder.is_dir() and any(folder.glob("*.json")):
            continue
        url = URL.format(sym=urllib.request.quote(s), p1=p1, p2=int(time.time()))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
                body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("reply too large")
            payload = json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # unknown on Yahoo: remember, so it is not asked again
                payload = {"chart": {"result": None, "error": {"code": 404}}}
            else:
                nh.log(f"fetch {s}: HTTP {e.code}; retried next run")
                time.sleep(pause * 5)
                continue
        except Exception as e:  # noqa: BLE001 - retried next run
            nh.log(f"fetch {s}: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(pause * 5)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"{date.today().isoformat()}.json"
        tmp = f.with_suffix(".part")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, f)
        if n % 25 == 0 or n == len(stocks):
            nh.log(f"fetch: {n}/{len(stocks)} {s}")
        time.sleep(pause)


def oldest_existing(sym: str, shoonya_root: Path, nse_out: Path) -> str | None:
    days = list(nh.shoonya_closes(shoonya_root / f"{sym}-EQ"))
    nse = nse_out / f"{sym}-EQ"
    if nse.is_dir():
        days += list(nh.shoonya_closes(nse))
    return min(days) if days else None


# owner 2026-11-21: trades and judgement from 2010; older candles are kept only to warm up long indicators
# (monthly EMA 200 / RSI 300). Rows before a stock's listing (a > 90-day gap at the start) are dropped.
HISTORY_FROM = "2000-01-01"
LISTING_GAP_DAYS = 90


def drop_prelisting(rows: list[tuple]) -> list[tuple]:
    """Keep rows after the last gap of more than LISTING_GAP_DAYS calendar days (junk before listing, e.g. TCS 2002-03)."""
    cut = 0
    for i in range(1, len(rows)):
        if (date.fromisoformat(rows[i][0]) - date.fromisoformat(rows[i - 1][0])).days > LISTING_GAP_DAYS:
            cut = i
    return rows[cut:]


NSE_CHECK_P95 = 0.01  # owner 2026-11-21: Yahoo is dropped before Shoonya when it disagrees with raw NSE
NSE_CHECK_BAD_SHARE = 0.02  # ... on more than 2% of the common days (day-to-day changes, so corporate actions don't count)


def nse_raw_closes(sym: str, nse_raw: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for f in sorted((nse_raw / sym).glob("*.json")) if (nse_raw / sym).is_dir() else []:
        try:
            for r in nh.parse_rows(json.loads(f.read_text(encoding="utf-8")), sym):
                out[r[0]] = r[4]
        except (ValueError, OSError):
            continue
    return out


def nse_check(keep: list[tuple], nse: dict[str, float]) -> tuple[bool, str]:
    """Yahoo vs raw NSE on common days, compared as day-to-day close changes (2026-11-21).

    Raw NSE is not adjusted for splits / bonuses / rights and Yahoo is, so their price LEVELS differ by a step at
    each corporate action (the first version of this check dropped 113 stocks for exactly that). Day-to-day
    changes agree except on the ex-date itself. A stock fails when more than NSE_CHECK_BAD_SHARE of the common
    day pairs differ by more than NSE_CHECK_P95 (1%)."""
    pairs = [(d, c, nse[d]) for d, o, h, lo, c, v in keep if d in nse and nse[d] > 0 and c > 0]
    if len(pairs) < 6:
        return True, "no NSE overlap to check"
    bad = steps = 0
    for (_, c0, n0), (_, c1, n1) in zip(pairs, pairs[1:]):
        diff = abs((c1 / c0) - (n1 / n0))
        if diff > NSE_CHECK_P95:
            bad += 1
            steps += diff > 0.05  # a corporate-action step (or a bad print)
    share = bad / (len(pairs) - 1)
    return share <= NSE_CHECK_BAD_SHARE, (f"vs NSE {len(pairs)} days, {bad} day(s) differ > 1% "
                                         f"({steps} corporate-action-size steps)")


def build(stocks: list[str], raw: Path, out: Path, shoonya_root: Path, nse_raw: Path,
          start: str = HISTORY_FROM) -> dict[str, str]:
    """Owner 2026-11-21: Yahoo is the only source before Shoonya (the NSE MCP adjusted build was mis-scaled and
    NSE's history has holes); it is cross-checked against raw NSE wherever both have the day."""
    report: dict[str, str] = {}
    for s in stocks:
        files = sorted((raw / s).glob("*.json"))
        if not files:
            report[s] = "not fetched"
            continue
        rows, rej = parse(json.loads(files[-1].read_text(encoding="utf-8")))
        sh = nh.shoonya_closes(shoonya_root / f"{s}-EQ")
        before = min(sh) if sh else ""
        keep, note = link(rows, sh, before)
        keep = drop_prelisting([x for x in keep if x[0] >= start])
        if keep:
            ok, chk = nse_check(keep, nse_raw_closes(s, nse_raw))
            note = f"{note}; {chk}"
            if not ok:
                note = "DROPPED: Yahoo disagrees with NSE; " + note
                keep = []
        rtxt = f"; rejected {rej}" if rej else ""
        report[s] = (f"{note}; {len(keep)} older days ({keep[0][0]}..{keep[-1][0]}){rtxt}" if keep
                     else f"{note}{rtxt}")
        folder = out / f"{s}-EQ"
        if folder.is_dir():
            for old in folder.glob("*"):  # derived output only; rebuilt from raw each time
                old.unlink()
        if not keep:
            continue
        last = keep[-1][0]
        for y in sorted({d[:4] for d, *_ in keep}):
            nh.write_part(folder, f"{y}-01-01", min(f"{y}-12-31", last), [x for x in keep if x[0][:4] == y],
                          source="yahoo-adjusted")
    out.mkdir(parents=True, exist_ok=True)
    (out / "build-report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    ok = sum(1 for v in report.values() if v.startswith("ok"))
    drop = sum(1 for v in report.values() if v.startswith("DROPPED"))
    nh.log(f"build: {ok}/{len(report)} stocks extended, {drop} dropped (disagree with NSE); details {out / 'build-report.json'}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["fetch", "build", "config", "all"])
    ap.add_argument("--stocks", required=True, help="comma list, or @file with one symbol per line")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between Yahoo calls")
    ap.add_argument("--refresh", action="store_true", help="fetch again even if a raw reply exists")
    ap.add_argument("--research", action="store_true", help="run 1d,1w research-matrix afterwards")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--from", dest="start", default=HISTORY_FROM, help="build: keep dates from this day on")
    a = ap.parse_args()
    src = a.stocks[1:] if a.stocks.startswith("@") else None
    stocks = [x.strip().split(",")[0].upper().removesuffix("-EQ")
              for x in (Path(src).read_text(encoding="utf-8").splitlines() if src else a.stocks.split(","))
              if x.strip() and not x.startswith("#")]
    shoonya, raw, out, nse_out = paths()
    if a.step in ("fetch", "all"):
        fetch(stocks, raw, a.pause, a.refresh)
    if a.step in ("build", "all"):
        build(stocks, raw, out, shoonya, nh.paths()[1], a.start)
    if a.step in ("config", "all"):
        out.mkdir(parents=True, exist_ok=True)
        conf = nh.write_config(nse_out)
        if a.research:
            cmd = [sys.executable, "-m", "stock_gplm.app", "research-matrix", "--config", str(conf),
                   "--stocks", ",".join(stocks), "--timeframes", "1d,1w", "--mode", "both",
                   "--methods", "evolve,tree,screen", "--budget", "deep", "--name", "discover-long",
                   "--workers", str(a.workers), "--quiet"]
            nh.log("research: " + " ".join(cmd))
            return subprocess.call(cmd, env={**os.environ, "OPENBLAS_NUM_THREADS": "1"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
