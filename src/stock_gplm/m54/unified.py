"""Unified multi-timeframe strategy discovery for one stock (``gplm discover --data/--timeframes``).

Pipeline (every stage is an integrity-checked checkpoint under the run directory; expensive
stages also checkpoint inside, in chunks):

``data``                        inventory every daily and minute partition, apply the single
                                access policy, load permitted candles, build 1m/5m/15m/30m/60m/1d/1w
                                bars, reconcile minute aggregates against daily candles
per lane (one per base timeframe):
  ``features``                  indicators over configured parameter grids on the lane, intraday
                                extras (session VWAP, opening range, time of day) and causally aligned
                                higher-timeframe features; cached on disk by content hash
  ``screen``                    single conditions (quantile thresholds fitted on discovery only),
                                ordered by discovery rank-IC and stratified across indicator families,
                                each tested at every lane horizon with a plain time exit
  ``refine``                    parameter-neighbour stability of the strongest survivors
  ``interact``                  AND-pairs of stable survivors from different indicator families
  ``cross_tf``                  base conditions AND higher-timeframe conditions
  ``exits``                     stop / target / trailing / holding-period grid per finalist (discovery)
  ``walk_forward``              rolling exit re-selection inside discovery, scored on the next fold
  ``validate``                  frozen strategies scored once on validation; recency scheme selected here
  ``test``                      validation passers scored once on the inner test segment
``rank`` and ``report``         per-symbol Benjamini-Hochberg over every discovery test of every lane,
                                evidence classification, ranked comparison, strategy library

Segments are chronological session splits of the *permitted* data with an embargo between them.
The inner test segment is not the M3.2 locked holdout: holdout-period partitions are never
opened (see :mod:`stock_gplm.m54.candles`). Test outcomes never feed back into any choice.
"""
from __future__ import annotations

import copy
import hashlib
import math
import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats as sps

from ..m3.minute import SessionPolicy
from . import CLASSIFICATION
from .backtest import Costs, ExitPolicy, LaneMarket, baseline, metrics, simulate
from .candles import AccessPolicy, coverage, daily_session_gaps, fingerprint, load_daily, load_minute, reconcile, resolve_policy
from .hypotheses import (Hypothesis, budget_sample, interactions, load_hypothesis_file, neighbours, signal_mask,
                         single_conditions, unique)
from .indicators import FeatureDef, _atr, build_features
from .recency import Scheme, schemes, select, weights
from .store import canonical_hash, read_checkpoint, read_json, utc_now, write_checkpoint, write_json_atomic
from .portable import fork_pool, usage as _usage
from .timeframes import ALL_TIMEFRAMES, Bars, align, bars_per_year, build_all, session_ids

CONFIG_CONTRACT = "stock-gplm.m54-unified.v1"
RUN_CONTRACT = "stock-gplm.m54-unified-run.v1"
LANE_STAGES = ("features", "screen", "refine", "interact", "cross_tf", "exits", "walk_forward", "validate", "test")
_CODE_FILES = ("m54", "mvp/data.py", "ingestion", "m3/minute.py")


class StopRequested(RuntimeError):
    """A STOP file was found; the run halted at a checkpoint boundary."""


def code_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for item in _CODE_FILES:
        p = root / item
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            digest.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes() + b"\0")
    return digest.hexdigest()


def normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not s or any(ch in s for ch in "/\\.") or s.startswith("-"):
        raise ValueError(f"invalid symbol {symbol!r}")
    return s if s.endswith("-EQ") else f"{s}-EQ"


def load_settings(path: Path, budget: str, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    path = Path(path).resolve()
    cfg = read_json(path)
    if cfg.get("contract") != CONFIG_CONTRACT:
        raise ValueError(f"{path} is not a {CONFIG_CONTRACT} document")
    budgets = read_json((path.parent / cfg["budgets"]).resolve())
    if budget not in budgets["budgets"]:
        raise ValueError(f"unknown budget {budget!r}; choose one of {sorted(budgets['budgets'])}")
    s = copy.deepcopy(cfg)
    s["budget_name"], s["budget"] = budget, budgets["budgets"][budget]
    s["search_space"] = read_json((path.parent / cfg["search_space"]).resolve())
    s.pop("budgets")
    d = s["data"]
    for key, value in (overrides or {}).items():
        if value is not None:
            d[key] = [str(Path(v).resolve()) for v in value] if isinstance(value, list) else str(Path(value).resolve())
    for key in ("daily_root", "real_raw_tree", "session_policy", "m32_run"):
        if not Path(d[key]).is_absolute():
            d[key] = str((path.parent / d[key]).resolve())
    d["minute_roots"] = [r if Path(r).is_absolute() else str((path.parent / r).resolve()) for r in d["minute_roots"]]
    sp = s["split"]
    if not (0 < sp["discovery"] and 0 < sp["validation"] and 0 < sp["test"]
            and abs(sp["discovery"] + sp["validation"] + sp["test"] - 1) < 1e-9):
        raise ValueError("split fractions must be positive and sum to 1")
    return s


# ----------------------------------------------------------------------------- parallel evaluation

_CTX: dict[str, Any] = {}


def _row(ctx: Mapping[str, Any], h: Hypothesis, p: ExitPolicy, seg_name: str, *, full: bool = False,
         w_scheme: Scheme | None = None) -> dict[str, Any]:
    m: LaneMarket = ctx["market"]
    seg = ctx["segs"][seg_name]
    mask = signal_mask(h, ctx["features"])
    trades = simulate(m, mask, seg, p, ctx["costs"])
    base = baseline(m, seg, p, ctx["costs"], ctx["base_cache"])
    met = metrics(trades, m, seg, base)
    row = {"hypothesis": h.to_dict(), "exit": p.to_dict(), "exit_key": p.key(), "horizon": p.max_hold,
           "segment": seg_name, **{k: met.get(k) for k in ("trades", "effective_n", "mean_net", "excess_mean",
                                                            "t_stat", "p_value", "win_rate", "profit_factor",
                                                            "baseline_mean_net")}}
    if full:
        row["metrics"] = met
        row["trades_detail"] = trades
    return row


def _eval_task(task: Sequence[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for hd, pd_ in task:
        out.append(_row(_CTX, Hypothesis.from_dict(hd, hd["origin"]), ExitPolicy.from_dict(pd_), "discovery"))
    return out


# ----------------------------------------------------------------------------- lane

def _quarantine(arrays: dict[str, np.ndarray], blocked: np.ndarray, k: int) -> None:
    """Blank feature values for k bars after a price discontinuity (lookbacks would straddle it)."""
    idx = np.nonzero(blocked)[0]
    if not idx.size or k <= 0:
        return
    bad = np.zeros(len(blocked), dtype=bool)
    for i in idx:
        bad[i:i + k + 1] = True
    for v in arrays.values():
        v[bad] = np.nan


def _blocked(f: pd.DataFrame, intraday: bool, q: Mapping[str, Any]) -> np.ndarray:
    prev = f["close"].shift(1)
    gap = (f["open"] / prev - 1).abs()
    move = (f["close"] / prev - 1).abs()
    if intraday:
        new_session = f["session"] != f["session"].shift(1)
        flag = (new_session & (gap > q["max_abs_gap"])) | (~new_session & (move > q["intraday_max_abs_bar_return"]))
    else:
        flag = (gap > q["max_abs_gap"]) | (move > q["max_abs_bar_return"])
    return flag.fillna(False).to_numpy(dtype=bool)


def _intraday_extras(f: pd.DataFrame, minutes: int) -> tuple[dict[str, np.ndarray], dict[str, FeatureDef]]:
    """Session VWAP distance, opening-range position/breakout and time of day (minute data only)."""
    arrays: dict[str, np.ndarray] = {}
    defs: dict[str, FeatureDef] = {}
    g = f.groupby("session", sort=False)
    cum_pv, cum_v = g["vwap_pv"].cumsum(), g["volume"].cumsum()
    vwap = cum_pv / cum_v.where(cum_v > 0)
    vd = (f["close"] / vwap - 1).to_numpy(dtype=float)

    def put(key: str, values: Any, ind: str, output: str, kind: str, params: tuple = ()) -> None:
        v = np.asarray(values, dtype=float)
        v[~np.isfinite(v)] = np.nan
        arrays[key], defs[key] = v, FeatureDef(key, "intraday", ind, params, output, kind)

    put("VWAP().session_distance", vd, "VWAP", "session_distance", "signed")
    cross = (vd > 0) & (np.concatenate(([np.nan], vd[:-1])) <= 0) & (f["session"] == f["session"].shift(1)).to_numpy()
    put("VWAP().reclaim", cross.astype(float), "VWAP", "reclaim", "binary")
    mos = f["minute_of_session"].to_numpy(dtype=float)
    put("SESSION().minute_of_session", mos, "SESSION", "minute_of_session", "ratio")
    prev_close = g["close"].last().shift(1)
    sess_open = g["open"].transform("first")
    gap = (sess_open / f["session"].map(prev_close) - 1).to_numpy(dtype=float)
    put("SESSION().opening_gap", gap, "SESSION", "opening_gap", "signed")
    for orm in (15, 30):
        if orm < minutes:
            continue
        in_range = f["minute_of_session"] + minutes <= orm
        hi = f["high"].where(in_range).groupby(f["session"]).transform("max")
        lo = f["low"].where(in_range).groupby(f["session"]).transform("min")
        done = (f["minute_of_session"] + minutes > orm).to_numpy()  # opening range complete at this bar's close
        pos = ((f["close"] - lo) / (hi - lo).where(hi > lo)).to_numpy(dtype=float)
        pos[~done] = np.nan
        brk = ((f["close"] > hi).to_numpy() & done).astype(float)
        brk[~done] = np.nan
        p = (("minutes", orm),)
        put(f"ORB(minutes={orm}).position", pos, "ORB", "position", "signed", p)
        put(f"ORB(minutes={orm}).breakout_up", brk, "ORB", "breakout_up", "binary", p)
    return arrays, defs


def _segments(session_id: np.ndarray, split: Mapping[str, float], embargo: int) -> dict[str, tuple[int, int]]:
    n = int(session_id.max()) + 1 if session_id.size else 0
    nd = int(n * split["discovery"])
    nv = int(n * split["validation"])
    ranges = {"discovery": (0, nd), "validation": (nd + embargo, nd + embargo + nv), "test": (nd + 2 * embargo + nv, n)}
    out = {}
    for k, (s0, s1) in ranges.items():
        a = int(np.searchsorted(session_id, s0, side="left"))
        b = int(np.searchsorted(session_id, s1, side="left"))
        out[k] = (a, max(a, b))
    return out


def _ic_rank(features: Mapping[str, np.ndarray], m: LaneMarket, seg: tuple[int, int], h: int) -> dict[str, float]:
    """|Spearman rank-IC| of each feature with the forward h-bar open-to-open return on discovery,
    sampled every h bars so overlapping labels are not counted repeatedly."""
    a, b = seg
    idx = np.arange(a, b - 1 - h, max(1, h))
    if idx.size < 30:
        return {}
    fwd = m.open[idx + 1 + h] / m.open[idx + 1] - 1
    if m.intraday:  # never label across a session boundary
        keep = m.session_id[idx + 1 + h] == m.session_id[idx]
        idx, fwd = idx[keep], fwd[keep]
    out = {}
    for k, v in features.items():
        x = v[idx]
        ok = np.isfinite(x) & np.isfinite(fwd)
        if ok.sum() < 30 or np.nanstd(x[ok]) == 0:
            continue
        rho = sps.spearmanr(x[ok], fwd[ok]).statistic
        if np.isfinite(rho):
            out[k] = abs(float(rho))
    return out


class Lane:
    def __init__(self, run: "UnifiedRun", tf: str, bars: Bars, htf: Mapping[str, Bars]):
        self.run, self.tf, self.bars, self.htf = run, tf, bars, htf
        self.cfg = run.s["lanes"][tf]
        f = bars.frame
        self.frame = f
        sid = session_ids(f["session"].to_numpy())
        q = run.s["quality"]
        blocked = _blocked(f, bars.intraday, q)
        natr = (_atr(f, 14) / f["close"]).to_numpy(dtype=float)
        k = int(run.s["budget"]["max_period"])
        no_signal = f["incomplete"].to_numpy(dtype=bool).copy()
        for i in np.nonzero(blocked)[0]:
            no_signal[i:i + k + 1] = True
        self.market = LaneMarket(tf, f["session"].to_numpy(), sid, f["end_ns"].to_numpy(dtype=np.int64),
                                 *(f[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "volume")),
                                 natr, blocked, no_signal, bars.intraday, bars_per_year(tf))
        emb = int(run.s["split"]["embargo_sessions"]["intraday" if bars.intraday else tf])
        self.segs = _segments(sid, run.s["split"], emb)
        self.costs = Costs.from_bps(run.s["costs"][self.cfg["cost_profile"]])
        self.horizons = [h for h in self.cfg["horizons"]]
        trend_p = int(run.s["regimes"]["trend_sma_bars"])
        sma = f["close"].rolling(trend_p, min_periods=trend_p).mean()
        self.trend = np.where(sma.isna(), "undefined", np.where(f["close"] > sma, "above_sma", "below_sma"))
        disc = slice(*self.segs["discovery"])
        med = float(np.nanmedian(natr[disc])) if np.isfinite(natr[disc]).any() else float("nan")
        self.vol = np.where(np.isnan(natr), "undefined", np.where(natr > med, "high_vol", "low_vol"))
        self.base_cache: dict[Any, Any] = {}

    @property
    def dir(self) -> Path:
        return self.run.dir / "stages" / self.tf

    def ctx(self) -> dict[str, Any]:
        return {"market": self.market, "segs": self.segs, "features": self.features, "costs": self.costs,
                "base_cache": self.base_cache}

    # ---------------------------------------------------------------- features (cached)
    def build_features(self) -> dict[str, Any]:
        run, f = self.run, self.frame
        b = run.s["budget"]
        htf_cfg = run.s["htf_search_space"]
        cache_key = canonical_hash({"code": run.code_sha, "tf": self.tf, "bars": int(pd.util.hash_pandas_object(
            f[["session", "open", "high", "low", "close", "volume", "end_ns"]], index=False).sum() % (1 << 62)),
            "htf": {t: int(pd.util.hash_pandas_object(h.frame[["session", "close", "avail_ns"]], index=False).sum()
                           % (1 << 62)) for t, h in self.htf.items()},
            "space": run.s["search_space"], "htf_space": htf_cfg, "max_period": b["max_period"],
            "quality": run.s["quality"]})
        cache = run.output_root / "cache" / "features" / f"{cache_key}.npz"
        meta_path = cache.with_suffix(".json")
        if cache.is_file() and meta_path.is_file():
            meta = read_json(meta_path)
            with np.load(cache) as z:
                self.features = {k: z[k] for k in z.files}
            self.defs = {k: FeatureDef(k, d["family"], d["indicator"], tuple(sorted(d["params"].items())), d["output"],
                                       d["kind"], tuple(d["levels"])) for k, d in meta["defs"].items()}
            meta["cache"] = {"hit": True, "path": str(cache)}
            return meta
        t0 = time.monotonic()
        df = f[["open", "high", "low", "close", "volume"]].reset_index(drop=True)
        arrays, defs, skipped = build_features(df, run.s["search_space"], max_period=int(b["max_period"]))
        if self.bars.intraday:
            ea, ed = _intraday_extras(f.reset_index(drop=True), int(self.tf.rstrip("m")))
            arrays.update(ea)
            defs.update(ed)
        _quarantine(arrays, self.market.blocked, int(b["max_period"]))
        htf_counts = {}
        for t, hb in self.htf.items():
            hf = hb.frame.reset_index(drop=True)
            ha, hd, _ = build_features(hf[["open", "high", "low", "close", "volume"]], htf_cfg, max_period=None)
            _quarantine(ha, _blocked(hf, hb.intraday, run.s["quality"]), int(htf_cfg.get("quarantine_bars", 20)))
            avail = hf["avail_ns"].to_numpy(dtype=np.int64)
            n = 0
            for key, d in hd.items():
                k2 = f"{t}:{key}"
                arrays[k2] = align(self.market.end_ns, avail, ha[key])
                defs[k2] = FeatureDef(k2, f"htf_{t}", f"{t}:{d.indicator}", d.params, d.output, d.kind, d.levels)
                n += 1
            htf_counts[t] = n
        self.features, self.defs = arrays, defs
        ic = _ic_rank({k: v for k, v in arrays.items() if not defs[k].family.startswith("htf_")}, self.market,
                      self.segs["discovery"], self.horizons[len(self.horizons) // 2])
        top_ic = sorted(ic.items(), key=lambda x: -x[1])[:25]
        meta = {"features": len(arrays), "base_features": len(arrays) - sum(htf_counts.values()),
                "htf_features": htf_counts, "families": sorted({d.family for d in defs.values()}),
                "indicators": sorted({d.indicator for d in defs.values()}), "skipped": skipped,
                "ic_horizon": self.horizons[len(self.horizons) // 2], "ic": ic, "top_ic": top_ic,
                "defs": {k: d.to_dict() for k, d in defs.items()}, "compute_seconds": round(time.monotonic() - t0, 2)}
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f".{cache.stem}.{os.getpid()}.npz")
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, cache)
        write_json_atomic(meta_path, meta)
        meta["cache"] = {"hit": False, "path": str(cache)}
        return meta

    # ---------------------------------------------------------------- evaluation helpers
    def _passes(self, r: Mapping[str, Any]) -> bool:
        c = self.run.s["criteria"]
        need = c["min_effective_n_discovery_intraday" if self.bars.intraday else "min_effective_n_discovery"]
        return ((r["effective_n"] or 0) >= need and r["mean_net"] is not None and r["mean_net"] > 0
                and (r["excess_mean"] or 0) > 0 and r["p_value"] is not None and r["p_value"] <= c["screen_max_p"])

    def _batch(self, name: str, jobs: list[tuple[Hypothesis, ExitPolicy]]) -> list[dict[str, Any]]:
        """Evaluate (hypothesis, exit) jobs on discovery in checkpointed chunks, with one forked pool per batch."""
        global _CTX
        chunk = int(self.run.s["budget"]["checkpoint_every"])
        workers = self.run.workers
        paths = [self.dir / f"{name}-chunks" / f"{i // chunk:05d}.json" for i in range(0, len(jobs), chunk)]
        cached = [read_checkpoint(p) for p in paths]
        todo = [i for i, c in enumerate(cached) if c is None]
        if todo:
            for p in {pp.key(): pp for i in todo for _, pp in jobs[i * chunk:(i + 1) * chunk]}.values():
                baseline(self.market, self.segs["discovery"], p, self.costs, self.base_cache)  # warm before fork
            _CTX = self.ctx()
            pool = fork_pool(workers)  # serial where fork is unavailable (Windows)
            try:
                for i in todo:
                    self.run.check_stop()
                    part = [(h.to_dict(), p.to_dict()) for h, p in jobs[i * chunk:(i + 1) * chunk]]
                    if pool is None:
                        rows = _eval_task(part)
                    else:
                        size = max(1, math.ceil(len(part) / (workers * 4)))
                        rows = [r for res in pool.map(_eval_task, [part[k:k + size] for k in range(0, len(part), size)])
                                for r in res]
                    cached[i] = {"rows": rows}
                    write_checkpoint(paths[i], cached[i])
                    self.run.status(f"{self.tf}/{name}", "RUNNING",
                                    progress=f"{min((i + 1) * chunk, len(jobs))}/{len(jobs)}")
            finally:
                if pool is not None:
                    pool.close()
                    pool.join()
        return [r for c in cached for r in c["rows"]]  # type: ignore[index]

    def _hyp(self, raw: Mapping[str, Any]) -> Hypothesis:
        return Hypothesis.from_dict(raw, raw["origin"])

    # ---------------------------------------------------------------- stages
    def stage_screen(self, feat: Mapping[str, Any]) -> dict[str, Any]:
        b, s = self.run.s["budget"], self.run.s
        disc = slice(*self.segs["discovery"])
        base_defs = {k: d for k, d in self.defs.items() if not d.family.startswith("htf_")}
        enumerated = single_conditions(base_defs, self.features, disc, s["threshold_quantiles"])
        limit = int(b["max_single_candidates"])
        ic = feat.get("ic", {})
        # Behaviour-guided half: hypotheses on the features with the strongest discovery rank-IC.
        guided: list[Hypothesis] = []
        if ic and limit < len(enumerated):
            order = {k: i for i, (k, _) in enumerate(sorted(ic.items(), key=lambda x: -x[1]))}
            ranked = sorted((h for h in enumerated if h.conditions[0].feature in order),
                            key=lambda h: (order[h.conditions[0].feature], h.hypothesis_id))
            guided = ranked[: limit // 2]
        guided_ids = {g.hypothesis_id for g in guided}
        rest = [h for h in enumerated if h.hypothesis_id not in guided_ids]
        sampled = guided + budget_sample(rest, base_defs, limit - len(guided), int(s["seed"]))
        extra = [h for h in self.run.extra if all(c.feature in self.features for c in h.conditions)]
        hyps = unique(sampled + extra)
        jobs = [(h, ExitPolicy(hz)) for h in hyps for hz in self.horizons]
        rows = self._batch("screen", jobs)
        surv = sorted((r for r in rows if self._passes(r)),
                      key=lambda r: (-(r["t_stat"] or 0), r["hypothesis"]["hypothesis_id"], r["horizon"]))
        return {"enumerated": len(enumerated), "tested_hypotheses": len(hyps), "ic_guided": len(guided),
                "file_hypotheses": len(extra), "tests": len(rows), "budget_limited": len(enumerated) > limit,
                "rows": rows, "survivors": [[r["hypothesis"]["hypothesis_id"], r["horizon"]] for r in surv]}

    def stage_refine(self, screen: Mapping[str, Any]) -> dict[str, Any]:
        b, c = self.run.s["budget"], self.run.s["criteria"]
        by_id = {r["hypothesis"]["hypothesis_id"]: r["hypothesis"] for r in screen["rows"]}
        disc = slice(*self.segs["discovery"])
        grids = self.run.s["search_space"]["indicators"]
        plan, jobs = [], []
        for hid, hz in screen["survivors"][: int(b["refine_top_k"])]:
            h = self._hyp(by_id[hid])
            ns = neighbours(h, self.defs, self.features, disc, grids, self.run.s["threshold_quantiles"]) \
                if len(h.conditions) == 1 and h.conditions[0].feature in self.defs else []
            plan.append((hid, hz, [n.hypothesis_id for n in ns]))
            jobs += [(n, ExitPolicy(hz)) for n in ns]
        uniq = {(h.hypothesis_id, p.key()): (h, p) for h, p in jobs}
        rows = self._batch("refine", [uniq[k] for k in sorted(uniq)])
        idx = {(r["hypothesis"]["hypothesis_id"], r["horizon"]): r for r in rows}
        results = []
        for hid, hz, nids in plan:
            vals = [idx[(n, hz)] for n in nids if (n, hz) in idx]
            pos = [v for v in vals if (v["excess_mean"] or 0) > 0]
            frac = len(pos) / len(vals) if vals else None
            stab = "NO_NEIGHBOURS" if len(vals) < 2 else (
                "STABLE" if frac is not None and frac >= c["min_neighbour_positive_fraction"] else "UNSTABLE")
            results.append({"hypothesis_id": hid, "horizon": hz, "neighbours": len(vals),
                            "neighbour_positive_fraction": frac, "stability": stab})
        return {"tests": len(rows), "rows": rows, "results": results}

    def stage_interact(self, screen: Mapping[str, Any], refine: Mapping[str, Any]) -> dict[str, Any]:
        b = self.run.s["budget"]
        by_id = {r["hypothesis"]["hypothesis_id"]: r["hypothesis"] for r in screen["rows"]}
        pool: list[Hypothesis] = []
        for r in refine["results"]:
            if r["stability"] == "UNSTABLE":
                continue
            h = self._hyp(by_id[r["hypothesis_id"]])
            if len(h.conditions) == 1 and h.hypothesis_id not in {x.hypothesis_id for x in pool}:
                pool.append(h)
        pairs = interactions(pool[: int(b["interaction_pool"])], self.defs, int(b["max_interactions"]))
        rows = self._batch("interact", [(h, ExitPolicy(hz)) for h in pairs for hz in self.horizons])
        surv = sorted((r for r in rows if self._passes(r)), key=lambda r: (-(r["t_stat"] or 0), r["hypothesis"]["hypothesis_id"]))
        return {"tests": len(rows), "pairs": len(pairs), "rows": rows,
                "survivors": [[r["hypothesis"]["hypothesis_id"], r["horizon"]] for r in surv]}

    def stage_cross_tf(self, screen: Mapping[str, Any]) -> dict[str, Any]:
        b, s = self.run.s["budget"], self.run.s
        htf_defs = {k: d for k, d in self.defs.items() if d.family.startswith("htf_")}
        if not htf_defs:
            return {"tests": 0, "rows": [], "survivors": [], "htf_conditions": 0, "base_pool": 0,
                    "note": f"no higher-timeframe bars available for lane {self.tf}"}
        disc = slice(*self.segs["discovery"])
        htf_conds = single_conditions(htf_defs, self.features, disc, s["threshold_quantiles"])
        htf_conds = budget_sample(htf_conds, htf_defs, int(b["cross_tf_htf_conditions"]), int(s["seed"]) + 1)
        # Base pool: strongest single conditions by discovery t-stat with positive mean, passers first.
        best: dict[str, dict[str, Any]] = {}
        for r in screen["rows"]:
            if r["mean_net"] is None or r["t_stat"] is None or r["mean_net"] <= 0:
                continue
            hid = r["hypothesis"]["hypothesis_id"]
            key = (self._passes(r), r["t_stat"])
            if hid not in best or key > (self._passes(best[hid]), best[hid]["t_stat"]):
                best[hid] = r
        base = sorted(best.values(), key=lambda r: (not self._passes(r), -r["t_stat"], r["hypothesis"]["hypothesis_id"]))
        base = base[: int(b["cross_tf_base_pool"])]
        jobs: list[tuple[Hypothesis, ExitPolicy]] = []
        for r in base:
            h = self._hyp(r["hypothesis"])
            for hc in htf_conds:
                jobs.append((Hypothesis(h.conditions + hc.conditions, "cross_timeframe"), ExitPolicy(r["horizon"])))
                if len(jobs) >= int(b["cross_tf_max"]):
                    break
            if len(jobs) >= int(b["cross_tf_max"]):
                break
        rows = self._batch("cross_tf", jobs)
        surv = sorted((r for r in rows if self._passes(r)), key=lambda r: (-(r["t_stat"] or 0), r["hypothesis"]["hypothesis_id"]))
        return {"tests": len(rows), "rows": rows, "htf_conditions": len(htf_conds), "base_pool": len(base),
                "htf_timeframes": sorted(self.htf), "survivors": [[r["hypothesis"]["hypothesis_id"], r["horizon"]] for r in surv]}

    def _index(self, *stages: Mapping[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
        idx: dict[tuple[str, int], dict[str, Any]] = {}
        for st in stages:
            for r in st["rows"]:
                idx.setdefault((r["hypothesis"]["hypothesis_id"], r["horizon"]), r)
        return idx

    def finalists(self, screen: Mapping[str, Any], refine: Mapping[str, Any], interact: Mapping[str, Any],
                  cross: Mapping[str, Any]) -> list[dict[str, Any]]:
        b = self.run.s["budget"]
        idx = self._index(screen, interact, cross)
        stab = {(r["hypothesis_id"], r["horizon"]): r["stability"] for r in refine["results"]}
        pool = {tuple(k): "screen+refine" for k in screen["survivors"][: int(b["refine_top_k"])]
                if stab.get(tuple(k)) != "UNSTABLE"}
        pool.update({tuple(k): "interaction" for k in interact["survivors"]})
        pool.update({tuple(k): "cross_timeframe" for k in cross["survivors"]})
        ranked = sorted(pool, key=lambda k: (-(idx[k]["t_stat"] or 0), k))
        # Keep cross-timeframe and same-timeframe candidates both represented.
        cap = int(b["max_finalists"])
        chosen = ranked[:cap]
        out = [{"hypothesis_id": k[0], "horizon": k[1], "source": pool[k], "fallback": False,
                "entry_stability": stab.get(k, "NOT_APPLICABLE")} for k in chosen]
        need = int(b["min_finalists"]) - len(out)
        if need > 0:  # retain the strongest exploratory candidates so exit rules are still examined
            rest = [r for r in screen["rows"] + cross["rows"] if r["mean_net"] is not None and r["t_stat"] is not None
                    and (r["hypothesis"]["hypothesis_id"], r["horizon"]) not in pool]
            rest.sort(key=lambda r: (-r["t_stat"], r["hypothesis"]["hypothesis_id"], r["horizon"]))
            seen = {(o["hypothesis_id"], o["horizon"]) for o in out}
            for r in rest:
                k = (r["hypothesis"]["hypothesis_id"], r["horizon"])
                if k in seen:
                    continue
                seen.add(k)
                out.append({"hypothesis_id": k[0], "horizon": k[1], "fallback": True,
                            "source": "fallback_top_t (did not pass the screen)",
                            "entry_stability": stab.get(k, "NOT_ASSESSED")})
                if len(out) >= int(b["min_finalists"]):
                    break
        return out

    def _exit_grid(self, horizon: int) -> list[ExitPolicy]:
        g = self.run.s["budget"]["exit_grid"]
        out = []
        for hm in g["hold_multipliers"]:
            for st in g["stop_atr"]:
                for tg in g["target_atr"]:
                    for tr in g["trail_atr"]:
                        if tr is not None and st is None:
                            continue
                        out.append(ExitPolicy(max(1, int(horizon * hm)), st, tg, tr))
        return list({p.key(): p for p in out}.values())

    @staticmethod
    def _grid_neighbours(p: ExitPolicy, grid: Sequence[ExitPolicy], g: Mapping[str, Any]) -> list[ExitPolicy]:
        dims = {"stop_atr": g["stop_atr"], "target_atr": g["target_atr"], "trail_atr": g["trail_atr"]}
        out = []
        for q in grid:
            diffs = []
            for d, vals in dims.items():
                a, b2 = getattr(p, d), getattr(q, d)
                if a != b2:
                    diffs.append(abs(vals.index(a) - vals.index(b2)) if a in vals and b2 in vals else 99)
            if q.max_hold != p.max_hold:
                diffs.append(1)
            if len(diffs) == 1 and diffs[0] == 1:
                out.append(q)
        return out

    def _pick_exit(self, rows: Sequence[Mapping[str, Any]], min_n: int) -> dict[str, Any] | None:
        ok = [r for r in rows if (r["effective_n"] or 0) >= min_n and r["excess_mean"] is not None]
        if not ok:
            return None
        simplicity = lambda r: sum(r["exit"][k] is not None for k in ("stop_atr", "target_atr", "trail_atr"))  # noqa: E731
        return max(ok, key=lambda r: (r["excess_mean"], -simplicity(r), -r["exit"]["max_hold_bars"]))

    def stage_exits(self, finals: Sequence[Mapping[str, Any]], idx: Mapping[tuple[str, int], Mapping[str, Any]]
                    ) -> dict[str, Any]:
        c = self.run.s["criteria"]
        g = self.run.s["budget"]["exit_grid"]
        min_n = c["min_effective_n_discovery_intraday" if self.bars.intraday else "min_effective_n_discovery"]
        jobs = []
        for f in finals:
            h = self._hyp(idx[(f["hypothesis_id"], f["horizon"])]["hypothesis"])
            jobs += [(h, p) for p in self._exit_grid(f["horizon"])]
        rows = self._batch("exits", jobs)
        by = {}
        for r in rows:
            by.setdefault(r["hypothesis"]["hypothesis_id"], []).append(r)
        chosen = []
        for f in finals:
            grid = self._exit_grid(f["horizon"])
            keys = {p.key() for p in grid}
            mine = [r for r in by.get(f["hypothesis_id"], []) if r["exit_key"] in keys]
            best = self._pick_exit(mine, min_n)
            if best is None:
                chosen.append({**f, "key": f"{f['hypothesis_id']}@{f['horizon']}", "exit": None,
                               "exit_note": "no exit policy produced enough discovery trades"})
                continue
            p = ExitPolicy.from_dict(best["exit"])
            nkeys = {q.key() for q in self._grid_neighbours(p, grid, g)}
            nb = [r for r in mine if r["exit_key"] in nkeys]
            frac = (sum(1 for r in nb if (r["excess_mean"] or 0) > 0) / len(nb)) if nb else None
            time_only = next((r for r in mine if r["exit_key"] == ExitPolicy(f["horizon"]).key()), None)
            chosen.append({**f, "key": f"{f['hypothesis_id']}@{f['horizon']}", "exit": best["exit"], "exit_key": best["exit_key"], "discovery": {k: best[k] for k in (
                "trades", "effective_n", "mean_net", "excess_mean", "t_stat", "p_value", "win_rate", "profit_factor")},
                "exit_neighbours": len(nb), "exit_neighbour_positive_fraction": frac,
                "time_exit_only_excess": time_only["excess_mean"] if time_only else None, "grid_size": len(grid)})
        return {"tests": len(rows), "rows": rows, "chosen": chosen}

    def stage_walk_forward(self, exits: Mapping[str, Any]) -> dict[str, Any]:
        folds = int(self.run.s["budget"]["wf_folds"])
        a, b = self.segs["discovery"]
        edges = np.linspace(a, b, folds + 1).astype(int)
        c = self.run.s["criteria"]
        min_n = max(3, c["min_effective_n_discovery_intraday" if self.bars.intraday else "min_effective_n_discovery"] // 2)
        out = []
        for ch in exits["chosen"]:
            if ch.get("exit") is None:
                continue
            h = Hypothesis.from_dict(next(r["hypothesis"] for r in exits["rows"] if r["hypothesis"]["hypothesis_id"] == ch["hypothesis_id"]),
                                     "walk_forward")
            mask = signal_mask(h, self.features)
            grid = self._exit_grid(ch["horizon"])
            fold_rows, oos = [], []
            for i in range(1, folds):
                train, test = (a, int(edges[i])), (int(edges[i]), int(edges[i + 1]))
                best, best_key = None, None
                for p in grid:
                    tr = simulate(self.market, mask, train, p, self.costs)
                    base = baseline(self.market, train, p, self.costs, self.base_cache)
                    met = metrics(tr, self.market, train, base)
                    if (met["effective_n"] or 0) >= min_n and met["excess_mean"] is not None:
                        score = (met["excess_mean"], -sum(v is not None for v in (p.stop_atr, p.target_atr, p.trail_atr)))
                        if best is None or score > best_key:
                            best, best_key = p, score
                if best is None:
                    fold_rows.append({"fold": i, "train_end": self.market.label(train[1] - 1), "selected": None})
                    continue
                tr = simulate(self.market, mask, test, best, self.costs)
                base = baseline(self.market, test, best, self.costs, self.base_cache)
                met = metrics(tr, self.market, test, base)
                oos += [t["net_return"] - (base or 0.0) for t in tr]
                fold_rows.append({"fold": i, "train_end": self.market.label(train[1] - 1), "selected": best.key(),
                                  "test_trades": met["trades"], "test_mean_net": met["mean_net"],
                                  "test_excess_mean": met["excess_mean"]})
            informative = [r for r in fold_rows if r.get("test_trades")]
            out.append({"key": ch["key"], "hypothesis_id": ch["hypothesis_id"], "horizon": ch["horizon"], "folds": fold_rows,
                        "oos_trades": len(oos), "oos_mean_excess": float(np.mean(oos)) if oos else None,
                        "positive_fold_fraction": (sum(1 for r in informative if (r["test_excess_mean"] or 0) > 0)
                                                   / len(informative)) if informative else None})
        return {"folds": folds, "rows": out,
                "note": "entry thresholds come from the full discovery segment; walk-forward re-selects exit policies only"}

    def _weighted_scores(self, chosen: Sequence[Mapping[str, Any]], hyps: Mapping[str, Hypothesis]
                         ) -> dict[str, dict[str, float | None]]:
        cfg = self.run.s["recency"]
        a, b = self.segs["discovery"]
        end_sid = int(self.market.session_id[b - 1])
        cur = str(self.trend[b - 1])
        out: dict[str, dict[str, float | None]] = {}
        for sc in schemes(cfg):
            col = {}
            for ch in chosen:
                p = ExitPolicy.from_dict(ch["exit"])
                tr = simulate(self.market, signal_mask(hyps[ch["key"]], self.features), (a, b), p, self.costs)
                base = baseline(self.market, (a, b), p, self.costs, self.base_cache)
                if not tr:
                    col[ch["key"]] = None
                    continue
                w = weights(sc, [t["session_id"] for t in tr], end_sid, [str(self.trend[t["t"]]) for t in tr], cur)
                ex = np.array([t["net_return"] - (base or 0.0) for t in tr])
                col[ch["key"]] = float(np.average(ex, weights=w)) if w.sum() > 0 else None
            out[sc.name] = col
        return out

    def stage_validate(self, exits: Mapping[str, Any], wf: Mapping[str, Any]) -> dict[str, Any]:
        c = self.run.s["criteria"]
        need = c["min_effective_n_validation_intraday" if self.bars.intraday else "min_effective_n_validation"]
        chosen = [ch for ch in exits["chosen"] if ch.get("exit") is not None]
        hyps = {ch["key"]: Hypothesis.from_dict(next(r["hypothesis"] for r in exits["rows"]
                                                              if r["hypothesis"]["hypothesis_id"] == ch["hypothesis_id"]))
                for ch in chosen}
        ctx = self.ctx()
        rows = []
        for ch in chosen:
            r = _row(ctx, hyps[ch["key"]], ExitPolicy.from_dict(ch["exit"]), "validation", full=True)
            met = r["metrics"]
            reasons = []
            if (met["effective_n"] or 0) < need:
                reasons.append(f"too few independent validation observations ({met['effective_n']} < {need})")
            if met["mean_net"] is None or met["mean_net"] <= 0:
                reasons.append("validation mean net return <= 0 after costs")
            if met["excess_mean"] is None or met["excess_mean"] <= 0:
                reasons.append("does not beat the unconditional same-exit baseline on validation")
            trades = r.pop("trades_detail")
            reg: dict[str, Any] = {}
            for name, labels in (("trend", self.trend), ("volatility", self.vol)):
                groups: dict[str, list[float]] = {}
                for t in trades:
                    groups.setdefault(str(labels[t["t"]]), []).append(t["net_return"])
                reg[name] = {k: {"n": len(v), "mean_net": float(np.mean(v))} for k, v in sorted(groups.items())}
            rows.append({"key": ch["key"], "hypothesis_id": ch["hypothesis_id"], "horizon": ch["horizon"], "exit_key": ch["exit_key"],
                         "passed": not reasons, "failure_reasons": reasons, "metrics": met, "regimes": reg})
        # Recency: each scheme ranks strategies on discovery; the scheme whose top-K did best on validation wins.
        scores = self._weighted_scores(chosen, hyps)
        vmap = {r["key"]: r["metrics"]["mean_net"] for r in rows}
        k = int(self.run.s["recency"]["select_top_k"])
        per_scheme = {}
        for name, col in scores.items():
            top = sorted((h for h, v in col.items() if v is not None), key=lambda h: -col[h])[:k]
            per_scheme[name] = [vmap.get(h) for h in top]
        chosen_scheme, table = select(per_scheme)
        return {"rows": rows, "recency": {"schemes": [s.to_dict() for s in schemes(self.run.s["recency"])],
                                          "table": table, "selected": chosen_scheme, "discovery_scores": scores,
                                          "rule": f"top {k} by discovery weighted excess, scored by mean validation net"}}

    def stage_test(self, exits: Mapping[str, Any], validate: Mapping[str, Any]) -> dict[str, Any]:
        c = self.run.s["criteria"]
        need = c["min_effective_n_test_intraday" if self.bars.intraday else "min_effective_n_test"]
        sel = validate["recency"]["selected"]
        score = validate["recency"]["discovery_scores"].get(sel, {})
        passers = [r for r in validate["rows"] if r["passed"]]
        passers.sort(key=lambda r: (-(score.get(r["key"]) or -1e9), r["key"]))
        cap = int(self.run.s["budget"]["max_test"])
        by = {ch["key"]: ch for ch in exits["chosen"] if ch.get("exit")}
        hyps = {r["hypothesis"]["hypothesis_id"]: r["hypothesis"] for r in exits["rows"]}
        ctx = self.ctx()
        rows = []
        for r in passers[:cap]:
            ch = by[r["key"]]
            row = _row(ctx, Hypothesis.from_dict(hyps[r["hypothesis_id"]]), ExitPolicy.from_dict(ch["exit"]), "test", full=True)
            met = row["metrics"]
            if (met["effective_n"] or 0) < need:
                verdict = "TEST_INSUFFICIENT_EVENTS"
            elif (met["mean_net"] or 0) > 0 and (met["excess_mean"] or 0) > 0:
                verdict = "TEST_CONSISTENT"
            else:
                verdict = "TEST_INCONSISTENT"
            rows.append({"key": r["key"], "hypothesis_id": r["hypothesis_id"], "horizon": r["horizon"], "verdict": verdict, "metrics": met,
                         "trades": row["trades_detail"]})
        return {"rows": rows, "not_tested_budget": [r["key"] for r in passers[cap:]], "evaluated_once": True}


# ----------------------------------------------------------------------------- run

class UnifiedRun:
    def __init__(self, settings: Mapping[str, Any], symbol: str, output_root: Path, *, timeframes: str = "auto",
                 data: str = "all-available", workers: int = 1, start: str | None = None, end: str | None = None,
                 hypotheses_file: Path | None = None, log: Callable[[str], None] = print,
                 stop_files: Sequence[Path] = (), device: str = "cpu"):
        self.s = copy.deepcopy(dict(settings))
        self.symbol = normalize_symbol(symbol)
        self.output_root = Path(output_root).resolve()
        self.timeframes, self.data_mode, self.workers = timeframes, data, max(1, int(workers))
        self.start, self.end = start, end
        self.log, self.stop_files, self.device = log, list(stop_files), device
        if data not in {"all-available", "daily", "minute"}:
            raise ValueError("--data must be all-available, daily or minute")
        self.extra: list[Hypothesis] = []
        self.extra_payload: Any = None
        if hypotheses_file is not None:
            self.extra_payload = read_json(hypotheses_file)
            self.extra = load_hypothesis_file(str(hypotheses_file), self.extra_payload)
        raw = Path(self.s["data"]["real_raw_tree"]).resolve()
        for r in [Path(self.s["data"]["daily_root"]), *map(Path, self.s["data"]["minute_roots"])]:
            r = r.resolve()
            if self.output_root == r or r in self.output_root.parents:
                raise ValueError("output root must be outside every raw-data tree")
        if self.output_root == raw or raw in self.output_root.parents:
            raise ValueError("output root must be outside the immutable raw-data tree")
        self.code_sha = code_sha256()

    # ---------------------------------------------------------------- loading and identity
    def load(self, create: bool = True) -> None:
        d = self.s["data"]
        minute_roots = [Path(r) for r in d["minute_roots"]] if self.data_mode != "daily" else []
        self.policy: AccessPolicy = resolve_policy(d, Path(d["daily_root"]), minute_roots, Path(d["real_raw_tree"]),
                                                   self.start, self.end)
        self.daily = load_daily(Path(d["daily_root"]), self.symbol, self.policy)
        sp = SessionPolicy.load(Path(d["session_policy"]))
        self.minute = load_minute(minute_roots, self.symbol, self.policy, sp) if minute_roots else None
        mbars = self.minute.bars if self.minute is not None else pd.DataFrame()
        self.bars = build_all(self.daily.bars if self.daily.status == "ACCEPTED" else pd.DataFrame(), mbars)
        self.lanes_plan = self._plan_lanes()
        partitions = self.daily.partitions + (self.minute.partitions if self.minute else [])
        identity = {"contract": RUN_CONTRACT, "symbol": self.symbol, "config": self.s, "code_sha256": self.code_sha,
                    "policy": self.policy.to_dict(), "data_mode": self.data_mode, "timeframes": self.timeframes,
                    "inputs": fingerprint(partitions), "lanes": self.lanes_plan, "hypotheses_file": self.extra_payload,
                    "session_policy": read_json(Path(d["session_policy"]))}
        self.identity = identity
        self.run_id = canonical_hash(identity)
        self.dir = self.output_root / "unified" / self.symbol / self.run_id
        if create:
            self.dir.mkdir(parents=True, exist_ok=True)
            if not (self.dir / "identity.json").exists():
                write_json_atomic(self.dir / "identity.json", identity)

    def _plan_lanes(self) -> dict[str, Any]:
        b = self.s["budget"]
        requested = list(b["auto_timeframes"]) if self.timeframes == "auto" else \
            [t.strip() for t in self.timeframes.split(",") if t.strip()]
        bad = [t for t in requested if t not in ALL_TIMEFRAMES]
        if bad:
            raise ValueError(f"unknown timeframes {bad}; choose from {ALL_TIMEFRAMES}")
        plan: dict[str, Any] = {}
        for tf in ALL_TIMEFRAMES:
            bars = self.bars[tf]
            cfg = self.s["lanes"][tf]
            entry = {"bars": len(bars), "sessions": bars.sessions(), "source": bars.source}
            intraday = bars.intraday
            if tf not in requested:
                entry["decision"] = "NOT_REQUESTED" if self.timeframes != "auto" else f"NOT_IN_{self.s['budget_name'].upper()}_AUTO_SET"
            elif self.data_mode == "daily" and intraday:
                entry["decision"] = "EXCLUDED_BY_--data daily"
            elif self.data_mode == "minute" and not intraday:
                entry["decision"] = "EXCLUDED_BY_--data minute (used only as higher-timeframe context)"
            elif not len(bars):
                entry["decision"] = "NO_PERMITTED_DATA"
            elif intraday and bars.sessions() < cfg["min_sessions"]:
                entry["decision"] = f"INSUFFICIENT_DATA ({bars.sessions()} sessions < {cfg['min_sessions']})"
            elif not intraday and len(bars) < cfg["min_bars"]:
                entry["decision"] = f"INSUFFICIENT_DATA ({len(bars)} bars < {cfg['min_bars']}); HTF context only"
            else:
                entry["decision"] = "RUN"
            entry["htf"] = [t for t in cfg["htf"] if len(self.bars[t])]
            plan[tf] = entry
        return plan

    # ---------------------------------------------------------------- bookkeeping
    def status(self, stage: str, state: str, **extra: Any) -> None:
        path = self.dir / "status.json"
        prior = read_json(path) if path.exists() else {"started_utc": utc_now()}
        u = _usage(include_children=True)
        write_json_atomic(path, {"run_id": self.run_id, "symbol": self.symbol, "state": state, "stage": stage,
                                 "pid": os.getpid(), "started_utc": prior.get("started_utc", utc_now()),
                                 "updated_utc": utc_now(), "max_rss_mb": u["max_rss_mb"],
                                 "cpu_seconds": u["cpu_seconds"],
                                 "budget": self.s["budget_name"], "device": self.device, "workers": self.workers,
                                 "kind": "unified", **extra})

    def check_stop(self) -> None:
        for path in [self.dir / "STOP", *self.stop_files]:
            if path.exists():
                raise StopRequested(str(path))

    def stage(self, name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        path = self.dir / "stages" / f"{name}.json"
        cached = read_checkpoint(path)
        if cached is not None:
            self.reused.append(name)
            return cached
        self.check_stop()
        self.status(name, "RUNNING")
        t0 = time.monotonic()
        result = fn()
        result["_seconds"] = round(time.monotonic() - t0, 3)
        write_checkpoint(path, result)
        self.computed.append(name)
        self.log(f"[{self.symbol}] {name} done in {result['_seconds']}s")
        return result

    # ---------------------------------------------------------------- stages
    def stage_data(self) -> dict[str, Any]:
        partitions = self.daily.partitions + (self.minute.partitions if self.minute else [])
        mb = self.minute.bars if self.minute is not None else pd.DataFrame()
        return {"symbol": self.symbol, "policy": self.policy.to_dict(), "daily_status": self.daily.status,
                "daily_issues": self.daily.issues, "coverage": coverage(partitions), "partitions": partitions,
                "minute_duplicates": self.minute.duplicates if self.minute else [],
                "minute_roots": self.s["data"]["minute_roots"] if self.data_mode != "daily" else [],
                "daily_root": self.s["data"]["daily_root"],
                "timeframes": {tf: b.summary() for tf, b in self.bars.items()}, "lanes": self.lanes_plan,
                "reconciliation": reconcile(self.daily.bars, mb),
                "daily_calendar_gaps": daily_session_gaps(list(self.daily.bars["date"]) if not self.daily.bars.empty else []),
                "corporate_actions": "UNKNOWN: no registered corporate-action evidence; suspected discontinuities "
                                     "(gap or bar move above the configured limits) block trades and blank features "
                                     "for max_period bars after them"}

    def execute(self) -> dict[str, Any]:
        self.reused: list[str] = []
        self.computed: list[str] = []
        self.load()
        manifest = self.dir / "run-manifest.json"
        if manifest.exists() and read_json(manifest).get("state") == "COMPLETED":
            from .unified_report import verify_outputs
            verify_outputs(self.dir)
            self.log(f"[{self.symbol}] reusing completed run {self.run_id[:12]} (outputs verified)")
            return {"run_id": self.run_id, "symbol": self.symbol, "dir": str(self.dir), "reused": True,
                    "state": "COMPLETED", "report": str(self.dir / "report.md")}
        try:
            data = self.stage("data", self.stage_data)
            lanes: dict[str, dict[str, Any]] = {}
            for tf, plan in self.lanes_plan.items():
                if plan["decision"] != "RUN":
                    continue
                lane = Lane(self, tf, self.bars[tf], {t: self.bars[t] for t in plan["htf"]})
                feat = self.stage(f"{tf}/features", lane.build_features)
                if not hasattr(lane, "features"):
                    lane.build_features()  # cache hit path loads arrays
                screen = self.stage(f"{tf}/screen", lambda: lane.stage_screen(feat))
                refine = self.stage(f"{tf}/refine", lambda: lane.stage_refine(screen))
                inter = self.stage(f"{tf}/interact", lambda: lane.stage_interact(screen, refine))
                cross = self.stage(f"{tf}/cross_tf", lambda: lane.stage_cross_tf(screen))
                finals = lane.finalists(screen, refine, inter, cross)
                idx = lane._index(screen, inter, cross)
                exits = self.stage(f"{tf}/exits", lambda: lane.stage_exits(finals, idx))
                wf = self.stage(f"{tf}/walk_forward", lambda: lane.stage_walk_forward(exits))
                val = self.stage(f"{tf}/validate", lambda: lane.stage_validate(exits, wf))
                test = self.stage(f"{tf}/test", lambda: lane.stage_test(exits, val))
                lanes[tf] = {"lane": lane, "features": feat, "screen": screen, "refine": refine, "interact": inter,
                             "cross_tf": cross, "exits": exits, "walk_forward": wf, "validate": val, "test": test}
            from .unified_report import rank, write_outputs
            ranked = self.stage("rank", lambda: rank(self, lanes))
            # Outputs are always (re)written from the checkpointed stages, so a lost manifest or report is rebuilt.
            self.check_stop()
            write_outputs(self, data, lanes, ranked)
            self.status("report", "COMPLETED")
            return {"run_id": self.run_id, "symbol": self.symbol, "dir": str(self.dir), "reused": False,
                    "state": "COMPLETED", "report": str(self.dir / "report.md"),
                    "stages_computed": len(self.computed), "stages_reused": len(self.reused)}
        except StopRequested as stop:
            self.status("stopped", "STOPPED", stop_file=str(stop))
            return {"run_id": self.run_id, "symbol": self.symbol, "dir": str(self.dir), "state": "STOPPED"}
        except Exception as exc:
            self.status("error", "FAILED", error=f"{type(exc).__name__}: {exc}")
            raise


def classification() -> str:
    return CLASSIFICATION
