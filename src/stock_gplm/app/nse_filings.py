"""NSE corporate filings: financial results + shareholding patterns with exact filing times, and their XBRL files.

Owner 2026-11-22: NSE is the best free official source for point-in-time fundamentals (exact broadcast timestamps,
promoter / FII / DII / pledge). Polite: one request at a time, paced, browser-like session, resumable, raw replies
kept verbatim (immutable), nothing downloaded twice.

    python -m stock_gplm.app.nse_filings probe --symbol RELIANCE          # 4 calls, prints the reply fields
    python -m stock_gplm.app.nse_filings index --stocks @deploy/oracle/live/universe.txt [--pause 1.5]
    python -m stock_gplm.app.nse_filings xbrl [--since 2016-01-01] [--kinds results,shareholding] [--pause 0.7]
    python -m stock_gplm.app.nse_filings summary

Steps
  index    per stock: financial-results lists (Quarterly, Half-Yearly, Annual) and the shareholding list ->
           data/raw/nse-filings/<SYM>/<kind>.json (raw NSE JSON, one file per kind; a rerun only fetches stocks
           missing or older than --refresh-days) -> data/derived/nse-filings/filings.csv (one row per filing:
           symbol, kind, period, from, to, consolidated, audited, filed_at = NSE broadcast time, xbrl_url, raw keys).
  xbrl     downloads the XBRL file of every indexed filing since --since -> data/raw/nse-filings/<SYM>/xbrl/<name>
           (static files on nsearchives; skipped when already present).
  summary  counts per kind / year, stocks without filings, failures.
The parser that turns XBRL into figures (revenue, operating profit, PAT, EPS, promoter %, pledge %) is the next step;
backtests will only see a value after its filed_at time.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
DATA = REPO.parent / "data"
RAW = DATA / "raw" / "nse-filings"
DERIVED = DATA / "derived" / "nse-filings"
HOME_URL = "https://www.nseindia.com/"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
API = {
    "results_quarterly": "https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={s}&period=Quarterly",
    "results_halfyearly": "https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={s}&period=Half-Yearly",
    "results_annual": "https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={s}&period=Annual",
    "shareholding": "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={s}",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


class NSESession:
    """Cookie session like a browser: open the home page first, then the JSON API with a Referer."""

    def __init__(self, pause: float):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.pause, self.last, self.warm = pause, 0.0, 0.0

    def _get(self, url: str, referer: str | None, accept: str, timeout: float = 30) -> tuple[int, bytes]:
        wait = self.pause - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": accept, "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate", **({"Referer": referer} if referer else {})})
        try:
            with self.op.open(req, timeout=timeout) as r:
                body, enc = r.read(), (r.headers.get("Content-Encoding") or "").lower()
                code = r.status
        except urllib.error.HTTPError as e:
            body, enc, code = e.read(), (e.headers.get("Content-Encoding") or "").lower(), e.code
        finally:
            self.last = time.monotonic()
        if enc == "gzip":
            body = gzip.decompress(body)
        elif enc == "deflate":
            body = zlib.decompress(body)
        return code, body

    def warmup(self) -> None:
        if time.monotonic() - self.warm < 600:
            return
        self._get(HOME_URL, None, "text/html,application/xhtml+xml")
        self._get(REFERER, HOME_URL, "text/html,application/xhtml+xml")
        self.warm = time.monotonic()

    def json(self, url: str, tries: int = 4) -> Any:
        for k in range(tries):
            self.warmup()
            code, body = self._get(url, REFERER, "application/json, text/plain, */*")
            if code == 200:
                try:
                    return json.loads(body.decode("utf-8", "replace") or "null")
                except json.JSONDecodeError:
                    pass  # an HTML block page instead of JSON: refresh the session
            self.warm = 0.0  # force a new session
            time.sleep(min(60, 5 * 2 ** k))
        raise RuntimeError(f"NSE gave no JSON for {url} (last HTTP {code})")

    def file(self, url: str, tries: int = 3) -> bytes:
        for k in range(tries):
            code, body = self._get(url, REFERER, "*/*", timeout=60)
            if code == 200 and body:
                return body
            time.sleep(min(30, 3 * 2 ** k))
        raise RuntimeError(f"HTTP {code} for {url}")


# ------------------------------------------------------------------ parsing helpers (tolerant to field names)
def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("data", "rows", "result"):
            if isinstance(payload.get(k), list):
                return [r for r in payload[k] if isinstance(r, dict)]
    return []


def _pick(r: dict[str, Any], *names: str) -> str:
    low = {k.lower(): v for k, v in r.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, "", "-"):
            return str(v)
    return ""


def _xbrl(r: dict[str, Any]) -> str:
    for k, v in r.items():
        if isinstance(v, str) and ("xbrl" in k.lower() or v.lower().endswith(".xml")) and v.startswith("http"):
            return v
    return ""


_DT_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y %H:%M:%S",
               "%d-%m-%Y")


def iso(v: str) -> str:
    v = (v or "").strip()
    for f in _DT_FORMATS:
        try:
            d = datetime.strptime(v, f)
            return d.strftime("%Y-%m-%d %H:%M:%S") if ":" in v else d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return v


def flatten(sym: str, kind: str, payload: Any) -> list[dict[str, Any]]:
    out = []
    for r in _rows(payload):
        out.append({
            "symbol": sym, "kind": kind,
            "period": _pick(r, "period", "relatingTo", "qe_Desc"),
            "from": iso(_pick(r, "fromDate", "from_date", "fromdate")),
            "to": iso(_pick(r, "toDate", "to_date", "todate", "date", "quarterEnded", "asOnDate")),
            "consolidated": _pick(r, "consolidated", "cons_noncons", "isConsolidated"),
            "audited": _pick(r, "audited", "auditedUnaudited"),
            "filed_at": iso(_pick(r, "broadCastDate", "broadcastDate", "exchdisstime", "filingDate", "submissionDate",
                                  "sort_date", "dissemination")),
            "submitted_at": iso(_pick(r, "filingDate", "submissionDate", "submission_date")),
            "xbrl_url": _xbrl(r),
            "keys": "|".join(sorted(r))[:400],
        })
    return out


# ------------------------------------------------------------------ commands
def read_stocks(arg: str) -> list[str]:
    from .walk_forward import read_stocks as rs
    return [s for s in rs(arg) if not s.startswith("IDX_")]


def cmd_probe(a: argparse.Namespace) -> int:
    ses = NSESession(a.pause)
    for kind, url in API.items():
        try:
            p = ses.json(url.format(s=urllib.parse.quote(a.symbol)))
        except RuntimeError as exc:
            print(f"{kind}: FAILED {exc}")
            continue
        rows = _rows(p)
        print(f"\n== {kind}: {len(rows)} rows; top-level type {type(p).__name__}"
              + (f", keys {list(p)[:12]}" if isinstance(p, dict) else ""))
        if rows:
            print("fields:", sorted(rows[0]))
            print("first:", json.dumps(rows[0], ensure_ascii=False)[:700])
            f = flatten(a.symbol, kind, p)[0]
            print("parsed:", {k: f[k] for k in ("period", "from", "to", "consolidated", "filed_at", "xbrl_url")})
    return 0


def cmd_index(a: argparse.Namespace) -> int:
    stocks = read_stocks(a.stocks)
    ses = NSESession(a.pause)
    DERIVED.mkdir(parents=True, exist_ok=True)
    log = DERIVED / "index.log"
    fresh = time.time() - a.refresh_days * 86400
    done = failed = 0
    t0 = time.monotonic()
    for i, sym in enumerate(stocks, 1):
        d = RAW / sym
        d.mkdir(parents=True, exist_ok=True)
        for kind, url in API.items():
            f = d / f"{kind}.json"
            if f.is_file() and f.stat().st_mtime >= fresh:
                continue
            try:
                p = ses.json(url.format(s=urllib.parse.quote(sym)))
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps({"fetched": time.strftime("%Y-%m-%d %H:%M:%S"), "url": url.format(s=sym),
                                           "payload": p}, ensure_ascii=False), encoding="utf-8")
                tmp.replace(f)
                done += 1
            except Exception as exc:  # noqa: BLE001 - logged, retried on the next run
                failed += 1
                with log.open("a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%H:%M:%S')} {sym} {kind} FAILED {str(exc)[:200]}\n")
        if i % 25 == 0 or i == len(stocks):
            rate = (time.monotonic() - t0) / i
            print(f"{time.strftime('%H:%M:%S')} {i}/{len(stocks)} stocks, {done} lists fetched, {failed} failed, "
                  f"~{rate * (len(stocks) - i) / 60:.0f} min left", flush=True)
    build_csv(stocks)
    return 0 if not failed else 1


def build_csv(stocks: list[str] | None = None) -> Path:
    rows: list[dict[str, Any]] = []
    syms = stocks or sorted(p.name for p in RAW.iterdir() if p.is_dir())
    for sym in syms:
        for kind in API:
            f = RAW / sym / f"{kind}.json"
            if f.is_file():
                rows += flatten(sym, kind, json.loads(f.read_text(encoding="utf-8")).get("payload"))
    DERIVED.mkdir(parents=True, exist_ok=True)
    out = DERIVED / "filings.csv"
    cols = ["symbol", "kind", "period", "from", "to", "consolidated", "audited", "filed_at", "submitted_at",
            "xbrl_url", "keys"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows):,} filings -> {out}")
    return out


def cmd_xbrl(a: argparse.Namespace) -> int:
    src = DERIVED / "filings.csv"
    if not src.is_file():
        raise SystemExit("run 'index' first")
    kinds = tuple(k.strip() for k in a.kinds.split(","))
    with src.open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["xbrl_url"] and r["kind"].startswith(kinds)
                and (r["to"] or r["filed_at"])[:10] >= a.since]
    ses = NSESession(a.pause)
    got = skip = failed = 0
    t0 = time.monotonic()
    for i, r in enumerate(rows, 1):
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(urllib.parse.urlparse(r["xbrl_url"]).path).name) or f"f{i}.xml"
        dest = RAW / r["symbol"] / "xbrl" / name
        if dest.is_file() and dest.stat().st_size > 0:
            skip += 1
            continue
        try:
            body = ses.file(r["xbrl_url"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(body)
            tmp.replace(dest)
            got += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            with (DERIVED / "xbrl.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {r['symbol']} {r['xbrl_url']} FAILED {str(exc)[:160]}\n")
        if i % 200 == 0 or i == len(rows):
            rate = (time.monotonic() - t0) / max(i - skip, 1)
            print(f"{time.strftime('%H:%M:%S')} {i}/{len(rows)} files: {got} new, {skip} already had, {failed} failed, "
                  f"~{rate * (len(rows) - i) / 60:.0f} min left", flush=True)
    return 0 if not failed else 1


def cmd_summary(a: argparse.Namespace) -> int:
    src = DERIVED / "filings.csv"
    if not src.is_file():
        raise SystemExit("run 'index' first")
    import pandas as pd
    t = pd.read_csv(src, dtype=str).fillna("")
    t["year"] = (t["to"].where(t["to"] != "", t["filed_at"])).str[:4]
    print(f"{len(t):,} filings, {t['symbol'].nunique():,} stocks, {(t['xbrl_url'] != '').mean():.0%} with an XBRL link,"
          f" {(t['filed_at'] != '').mean():.0%} with a filing time")
    print(t.pivot_table(index="year", columns="kind", values="symbol", aggfunc="count", fill_value=0).tail(15))
    have = set(t["symbol"])
    missing = [p.name for p in RAW.iterdir() if p.is_dir() and p.name not in have] if RAW.is_dir() else []
    print(f"stocks with no filings parsed: {len(missing)} {missing[:20]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--symbol", default="RELIANCE")
    p.add_argument("--pause", type=float, default=1.5)
    p = sub.add_parser("index")
    p.add_argument("--stocks", default="@" + str(REPO / "deploy" / "oracle" / "live" / "universe.txt"))
    p.add_argument("--pause", type=float, default=1.5, help="seconds between requests (be polite)")
    p.add_argument("--refresh-days", type=float, default=30, help="refetch a stock's lists older than this")
    p = sub.add_parser("xbrl")
    p.add_argument("--since", default="2016-01-01")
    p.add_argument("--kinds", default="results,shareholding")
    p.add_argument("--pause", type=float, default=0.7)
    sub.add_parser("summary")
    a = ap.parse_args(argv)
    return {"probe": cmd_probe, "index": cmd_index, "xbrl": cmd_xbrl, "summary": cmd_summary}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
