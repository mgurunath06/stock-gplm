"""Synthetic daily + minute candle fixtures in the exact observed provider formats.

The fixture exists because every local minute partition lies inside the M3.2 locked-holdout
period, so the minute path cannot be exercised end to end on permitted real data. Each data
root receives a ``SYNTHETIC_FIXTURE.json`` marker declaring the fixture's own cutoff; the
access policy accepts that marker only when every root carries it and none lies inside the
real raw tree. Fixture numbers are never market evidence.

Layout written under ``out``::

    daily/NSE/<SYM>-EQ/<from>_<to>.json.gz (+ .meta.json, .json.gz.done)   ShoonyaDailyV1
    minute/NSE/<SYM>-EQ/<session>.json.gz (+ .meta.json, .json.gz.done)    ShoonyaMinuteV1 (GPLM_JOBS_V1 meta)

The process is a seeded random walk with volatility clustering. One mild effect is planted
on purpose (after three consecutive down sessions the next session drifts up) so tests can
check the engine can find real structure; minute bars inside a session are a Brownian bridge
between the session's open and close, so daily and minute data reconcile exactly.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .candles import FIXTURE_CONTRACT, FIXTURE_MARKER, IST

_MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _sessions(start: date, end: date, rng: np.random.Generator) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and rng.random() > 0.03:  # synthetic holidays
            out.append(d)
        d += timedelta(days=1)
    return out


def _gz(payload: Any) -> bytes:
    return gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0)


def _publish(path: Path, payload: bytes, meta: dict[str, Any], records: int) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    meta = {**meta, "sha256_compressed": digest, "records": records}
    path.with_name(path.name.removesuffix(".json.gz") + ".meta.json").write_text(json.dumps(meta, indent=2))
    marker = {"status": "COMPLETE", "file": path.name, "bytes": len(payload), "records": records, "sha256": digest,
              "quality_status": "UNVALIDATED_RAW", "published_at_utc": "2000-01-01T00:00:00+00:00"}
    Path(f"{path}.done").write_text(json.dumps(marker, indent=2))


def _price(x: float) -> str:
    return f"{max(x, 0.05):.2f}"


def make_fixture(out: Path, symbols: Sequence[str] = ("RELIANCE",), *, start: str = "2019-01-01",
                 end: str = "2021-12-31", minute_from: str = "2021-04-01", cutoff: str = "2021-11-01",
                 seed: int = 7, missing_minute_rate: float = 0.002) -> dict[str, Any]:
    out = Path(out).resolve()
    fixture_id = hashlib.sha256(json.dumps([list(symbols), start, end, minute_from, cutoff, seed]).encode()).hexdigest()[:16]
    daily_root, minute_root = out / "daily" / "NSE", out / "minute" / "NSE"
    summary: dict[str, Any] = {"fixture_id": fixture_id, "daily_root": str(daily_root), "minute_root": str(minute_root),
                               "declared_cutoff": cutoff, "symbols": {}}
    for k, sym in enumerate(symbols):
        rng = np.random.default_rng(seed + 1000 * k)
        name = sym.upper().removesuffix("-EQ") + "-EQ"
        days = _sessions(date.fromisoformat(start), date.fromisoformat(end), rng)
        n = len(days)
        vol = np.empty(n)
        ret = np.empty(n)
        v, down = 0.015, 0
        for i in range(n):
            v = float(np.clip(0.94 * v + 0.06 * 0.015 + 0.004 * abs(rng.standard_normal()) * (1 if rng.random() < 0.1 else 0),
                              0.006, 0.05))
            drift = 0.008 if down >= 3 else 0.0002  # planted effect
            ret[i] = drift + v * rng.standard_normal()
            down = down + 1 if ret[i] < 0 else 0
            vol[i] = v
        close = 1000 * np.exp(np.cumsum(ret))
        prev = np.concatenate(([1000.0], close[:-1]))
        open_ = prev * np.exp(0.003 * rng.standard_normal(n))
        rows: list[dict[str, Any]] = []
        mstart = date.fromisoformat(minute_from)
        minute_sessions = 0
        for i, d in enumerate(days):
            o, c = float(open_[i]), float(close[i])
            base_vol = float(rng.lognormal(14.5, 0.35))
            if d >= mstart:
                # Brownian bridge of 375 one-minute closes from o to c.
                steps = rng.standard_normal(375) * vol[i] / np.sqrt(375)
                walk = np.cumsum(steps)
                bridge = walk - np.linspace(0, 1, 375) * walk[-1]
                path = o * np.exp(bridge + np.linspace(0, np.log(c / o), 375))
                mopen = np.concatenate(([o], path[:-1]))
                spread = np.abs(rng.standard_normal(375)) * vol[i] * o / np.sqrt(375) * 0.6
                mhigh = np.maximum(mopen, path) + spread
                mlow = np.minimum(mopen, path) - spread
                mvol = np.maximum(1, (rng.dirichlet(np.ones(375) * 2) * base_vol)).astype(int)
                keep = rng.random(375) > missing_minute_rate
                keep[0] = keep[-1] = True
                recs, cum = [], 0
                t0 = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
                for j in range(375):
                    if not keep[j]:
                        continue
                    cum += int(mvol[j])
                    ts = t0 + timedelta(minutes=j)
                    vw = (mhigh[j] + mlow[j] + path[j]) / 3
                    recs.append({"stat": "Ok", "time": ts.strftime("%d-%m-%Y %H:%M:%S"),
                                 "ssboe": str(int(ts.timestamp())), "into": _price(mopen[j]), "inth": _price(mhigh[j]),
                                 "intl": _price(mlow[j]), "intc": _price(path[j]), "intvwap": _price(vw),
                                 "intv": str(int(mvol[j])), "intoi": "0", "v": str(cum), "oi": "0"})
                recs.reverse()  # provider order: newest first
                _publish(minute_root / name / f"{d.isoformat()}.json.gz", _gz(recs),
                         {"source": "shoonya", "symbol": name, "token": str(90000 + k), "session_date": d.isoformat(),
                          "session_type": "REGULAR", "quality_status": "UNVALIDATED_RAW", "expected_start": "09:15",
                          "expected_end": "15:30", "fetched_at_utc": "2000-01-01T00:00:00+00:00"}, len(recs))
                minute_sessions += 1
                kept = keep.nonzero()[0]
                hi, lo, vsum = float(mhigh[kept].max()), float(mlow[kept].min()), int(mvol[kept].sum())
                o_d, c_d = float(mopen[kept[0]]), float(path[kept[-1]])
            else:
                hi = max(o, c) * (1 + abs(rng.standard_normal()) * vol[i] * 0.5)
                lo = min(o, c) * (1 - abs(rng.standard_normal()) * vol[i] * 0.5)
                vsum, o_d, c_d = int(base_vol), o, c
            ssboe = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
            rows.append({"date": d, "rec": json.dumps({"time": f"{d.day:02d}-{_MON[d.month - 1]}-{d.year}",
                                                       "into": _price(o_d), "inth": _price(hi), "intl": _price(lo),
                                                       "intc": _price(c_d), "ssboe": str(ssboe), "intv": f"{vsum}.00"})})
        # Daily partitions: one per year, and the last year split at the cutoff (file-level exclusion test).
        cut = date.fromisoformat(cutoff)
        windows: list[tuple[date, date]] = []
        for y in range(days[0].year, days[-1].year + 1):
            a, b = date(y, 1, 1), date(y, 12, 31)
            if a < cut <= b:
                windows += [(a, cut - timedelta(days=1)), (cut, b)]
            else:
                windows.append((a, b))
        for a, b in windows:
            part = [r["rec"] for r in rows if a <= r["date"] <= b]
            if not part:
                continue
            part.reverse()
            _publish(daily_root / name / f"{a.isoformat()}_{b.isoformat()}.json.gz", _gz(part),
                     {"source": "shoonya", "symbol": name, "from": a.isoformat(), "to": b.isoformat(),
                      "quality_status": "UNVALIDATED_RAW", "fetched_at_utc": "2000-01-01T00:00:00+00:00"}, len(part))
        summary["symbols"][name] = {"daily_sessions": n, "minute_sessions": minute_sessions}
    marker = {"contract": FIXTURE_CONTRACT, "synthetic": True, "fixture_id": fixture_id, "declared_cutoff": cutoff,
              "seed": seed, "planted_effect": "after three consecutive down sessions the next session drifts +0.8%",
              "note": "SYNTHETIC test data in the observed provider formats; never market evidence"}
    for root in (daily_root, minute_root):
        root.mkdir(parents=True, exist_ok=True)
        (root / FIXTURE_MARKER).write_text(json.dumps(marker, indent=2))
    return summary
