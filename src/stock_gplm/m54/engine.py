"""Staged, budgeted, resumable single-stock behaviour discovery.

Stages (each persisted as an integrity-checked checkpoint under the run directory):

1. ``data``      load pre-cutoff daily bars through the M3.2-pinned loader; flag discontinuities
2. ``screen``    broad budgeted screen of single conditions on the discovery segment
3. ``refine``    parameter-neighbour stability for the strongest screen survivors
4. ``interact``  AND-pairs of stable survivors from different indicator families
5. ``validate``  finalists evaluated once on the validation segment (selection is frozen first)
6. ``test``      validation passers evaluated once on the inner test segment
7. ``report``    report.md, findings.json, results.csv, events.csv, run-manifest.json

Every (hypothesis, horizon) evaluation on discovery data counts as a test. The
total drives Benjamini-Hochberg and Bonferroni adjustments. The inner test
segment is not the M3.2 locked holdout, which is never opened (only segments
whose declared window ends before the cutoff are read).
"""
from __future__ import annotations

import copy
import hashlib
import os
import resource
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from ..mvp.data import load_symbol, pin_holdout
from ..v1.portfolio import CostModel
from . import CLASSIFICATION
from .evaluate import Market, bh_adjust, bonferroni, evaluate
from .hypotheses import (Hypothesis, budget_sample, interactions, load_hypothesis_file, neighbours, signal_mask,
                         single_conditions, unique)
from .indicators import _atr, build_features
from .store import canonical_hash, read_checkpoint, read_json, utc_now, write_checkpoint, write_json_atomic

CONFIG_CONTRACT = "stock-gplm.m54-discovery.v1"
RUN_CONTRACT = "stock-gplm.m54-run.v1"
STAGES = ("data", "screen", "refine", "interact", "validate", "test", "report")
_CODE_FILES = ("m54", "mvp/data.py", "ingestion", "v1/portfolio.py")


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


@dataclass(frozen=True)
class Settings:
    """A fully resolved discovery configuration (the run identity covers all of it)."""

    config: dict[str, Any]
    base: Path

    @staticmethod
    def load(path: Path, budget: str, overrides: Mapping[str, Any] | None = None) -> "Settings":
        path = Path(path).resolve()
        cfg = read_json(path)
        if cfg.get("contract") != CONFIG_CONTRACT:
            raise ValueError(f"{path} is not a {CONFIG_CONTRACT} document")
        budgets = read_json((path.parent / cfg["budgets"]).resolve())
        if budget not in budgets["budgets"]:
            raise ValueError(f"unknown budget {budget!r}; choose one of {sorted(budgets['budgets'])}")
        resolved = copy.deepcopy(cfg)
        resolved["budget_name"] = budget
        resolved["budget"] = budgets["budgets"][budget]
        resolved["search_space"] = read_json((path.parent / cfg["search_space"]).resolve())
        resolved.pop("budgets")
        for key, value in (overrides or {}).items():
            if value is not None:
                resolved["data"][key] = str(Path(value).resolve())
        for key in ("raw_root", "m32_run"):
            if not Path(resolved["data"][key]).is_absolute():
                resolved["data"][key] = str((path.parent / resolved["data"][key]).resolve())
        seg = resolved["segments"]
        if not (seg["discovery"][0] <= seg["discovery"][1] < seg["validation"][0] <= seg["validation"][1]
                < seg["test"][0] <= seg["test"][1]):
            raise ValueError("segments must be chronological and non-overlapping: discovery < validation < test")
        return Settings(resolved, path.parent)


def _segment(dates: Sequence[str], span: Sequence[str]) -> tuple[int, int]:
    arr = np.array(dates)
    a = int(np.searchsorted(arr, span[0], side="left"))
    b = int(np.searchsorted(arr, span[1], side="right"))
    return a, b


class Run:
    def __init__(self, settings: Settings, symbol: str, output_root: Path, *, hypotheses_file: Path | None = None,
                 log: Callable[[str], None] = print, stop_files: Sequence[Path] = (), device: str = "cpu"):
        self.s = settings.config
        self.symbol = normalize_symbol(symbol)
        self.output_root = Path(output_root).resolve()
        self.log = log
        self.stop_files = list(stop_files)
        self.device = device
        self.extra: list[Hypothesis] = []
        self.extra_payload: Any = None
        if hypotheses_file is not None:
            self.extra_payload = read_json(hypotheses_file)
            self.extra = load_hypothesis_file(str(hypotheses_file), self.extra_payload)
        raw = Path(self.s["data"]["raw_root"]).resolve()
        if self.output_root == raw or raw in self.output_root.parents or self.output_root in raw.parents:
            raise ValueError("output root must be outside the immutable raw-data tree")

    # ------------------------------------------------------------------ identity and status
    def _load(self, create: bool = True) -> None:
        d = self.s["data"]
        self.pin = pin_holdout(Path(d["m32_run"]), d["m32_run_id"], d["split_plan_sha256"])
        if self.s["segments"]["test"][1] >= self.pin.cutoff:
            raise ValueError(f"test segment end {self.s['segments']['test'][1]} reaches the M3.2 locked holdout "
                             f"starting {self.pin.cutoff}")
        self.data = load_symbol(Path(d["raw_root"]), self.symbol, self.pin.cutoff)
        identity = {"contract": RUN_CONTRACT, "symbol": self.symbol, "config": self.s, "code_sha256": code_sha256(),
                    "holdout_pin": self.pin.to_dict(), "data_status": self.data.status,
                    "bars_sha256": self.data.bars_sha256() if self.data.bars else None,
                    "hypotheses_file": self.extra_payload}
        self.run_id = canonical_hash(identity)
        self.identity = identity
        self.dir = self.output_root / "runs" / self.symbol / self.run_id
        if not create:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.dir / "identity.json").exists():
            write_json_atomic(self.dir / "identity.json", identity)

    def _status(self, stage: str, state: str, **extra: Any) -> None:
        path = self.dir / "status.json"
        prior = read_json(path) if path.exists() else {"started_utc": utc_now()}
        done = [s for s in STAGES if (self.dir / "stages" / f"{s}.json").exists()]
        usage = resource.getrusage(resource.RUSAGE_SELF)
        write_json_atomic(path, {"run_id": self.run_id, "symbol": self.symbol, "state": state, "stage": stage,
                                 "completed_stages": done, "pending_stages": [s for s in STAGES if s not in done],
                                 "pid": os.getpid(), "started_utc": prior.get("started_utc", utc_now()),
                                 "updated_utc": utc_now(), "max_rss_mb": round(usage.ru_maxrss / 1024, 1),
                                 "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 1),
                                 "budget": self.s["budget_name"], "device": self.device, **extra})

    def _check_stop(self) -> None:
        for path in [self.dir / "STOP", *self.stop_files]:
            if path.exists():
                raise StopRequested(str(path))

    def _stage(self, name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        path = self.dir / "stages" / f"{name}.json"
        cached = read_checkpoint(path)
        if cached is not None:
            return cached
        self._check_stop()
        self._status(name, "RUNNING")
        t0 = time.monotonic()
        result = fn()
        result["_seconds"] = round(time.monotonic() - t0, 3)
        write_checkpoint(path, result)
        self.log(f"[{self.symbol}] stage {name} done in {result['_seconds']}s")
        return result

    # ------------------------------------------------------------------ market and features
    def _prepare(self) -> None:
        bars = self.data.bars
        df = pd.DataFrame(list(bars))
        self.df = df
        self.dates = tuple(df["date"].astype(str))
        cfg = self.s["quality"]
        gap = (df["open"] / df["close"].shift(1) - 1).abs()
        move = (df["close"] / df["close"].shift(1) - 1).abs()
        blocked = ((gap > cfg["max_abs_gap"]) | (move > cfg["max_abs_daily_return"])).fillna(False).to_numpy()
        natr = (_atr(df, 14) / df["close"]).to_numpy(dtype=float)
        self.market = Market(self.dates, *(df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close", "volume")),
                             natr, blocked)
        self.segs = {k: _segment(self.dates, v) for k, v in self.s["segments"].items()}
        budget = self.s["budget"]
        self.features, self.defs, self.skipped = build_features(df, self.s["search_space"], max_period=budget.get("max_period"))
        disc = slice(*self.segs["discovery"])
        trend_p = self.s["regimes"]["trend_sma"]
        sma = df["close"].rolling(trend_p, min_periods=trend_p).mean()
        trend = np.where(sma.isna(), "undefined", np.where(df["close"] > sma, "above_sma", "below_sma"))
        vol_median = float(np.nanmedian(natr[disc])) if np.isfinite(natr[disc]).any() else float("nan")
        vol = np.where(np.isnan(natr), "undefined", np.where(natr > vol_median, "high_vol", "low_vol"))
        self.regimes = {f"trend_sma{trend_p}": trend, "volatility_vs_discovery_median": vol}
        c = self.s["costs"]
        self.costs = CostModel(float(c["commission_bps"]), float(c["slippage_bps"]),
                               float(c["buy_statutory_bps"]), float(c["sell_statutory_bps"]))
        self.horizons = [h for h in budget["horizons"] if h in self.s["horizons"]]

    def _eval(self, h: Hypothesis, seg: str, horizon: int, **kw: Any) -> dict[str, Any]:
        b = self.s["budget"]
        mask = signal_mask(h, self.features)
        seed = int(h.hypothesis_id[:8], 16) ^ horizon
        return evaluate(self.market, mask, self.segs[seg], horizon, self.costs, target_atr=float(self.s["target_atr"]),
                        bootstrap=b["bootstrap_iterations"], random_draws=b["random_entry_draws"], seed=seed, **kw)

    def _passes_screen(self, r: Mapping[str, Any]) -> bool:
        c = self.s["criteria"]
        return (r["independent_events"] >= c["min_events_discovery"] and r["mean_net"] is not None
                and r["mean_net"] > 0 and r["excess_mean"] > 0 and r["p_value"] is not None
                and r["p_value"] <= c["screen_max_p"])

    def _evaluate_batch(self, name: str, hyps: Sequence[Hypothesis]) -> list[dict[str, Any]]:
        """Evaluate hypotheses on discovery in resumable chunks."""
        chunk = int(self.s["budget"]["checkpoint_every"])
        rows: list[dict[str, Any]] = []
        for start in range(0, len(hyps), chunk):
            path = self.dir / "stages" / f"{name}-chunks" / f"{start // chunk:05d}.json"
            cached = read_checkpoint(path)
            if cached is None:
                self._check_stop()
                part = []
                for h in hyps[start:start + chunk]:
                    for hz in self.horizons:
                        r = self._eval(h, "discovery", hz)
                        part.append({"hypothesis": h.to_dict(), **r})
                write_checkpoint(path, {"rows": part})
                cached = {"rows": part}
                self._status(name, "RUNNING", progress=f"{min(start + chunk, len(hyps))}/{len(hyps)} hypotheses")
            rows.extend(cached["rows"])
        return rows

    # ------------------------------------------------------------------ stages
    def _stage_data(self) -> dict[str, Any]:
        d = self.data
        summary = d.summary()
        out = {"symbol": self.symbol, "status": d.status, "issues": d.issues, "holdout_pin": self.pin.to_dict(),
               "bars": summary["bars"], "first": summary["first"], "last": summary["last"],
               "bars_sha256": summary["bars_sha256"], "segments_opened": summary["segments_opened"],
               "segments_withheld": summary["segments_withheld"]}
        if d.status == "ACCEPTED":
            self._prepare()
            out["blocked_sessions"] = [self.dates[i] for i in np.nonzero(self.market.blocked)[0]]
            out["segments"] = {k: {"span": self.s["segments"][k], "sessions": b - a,
                                   "first": self.dates[a] if b > a else None, "last": self.dates[b - 1] if b > a else None}
                               for k, (a, b) in self.segs.items()}
            out["features"] = len(self.features)
            out["feature_families"] = sorted({f.family for f in self.defs.values()})
            out["skipped_features"] = self.skipped
            out["horizons"] = self.horizons
        return out

    def _stage_screen(self) -> dict[str, Any]:
        b = self.s["budget"]
        disc = slice(*self.segs["discovery"])
        enumerated = single_conditions(self.defs, self.features, disc, self.s["threshold_quantiles"])
        sampled = budget_sample(enumerated, self.defs, int(b["max_single_candidates"]), int(self.s["seed"]))
        extra = [h for h in self.extra if all(c.feature in self.features for c in h.conditions)]
        missing = [h.to_dict() for h in self.extra if h not in extra]
        hyps = unique(sampled + extra)
        rows = self._evaluate_batch("screen", hyps)
        survivors = [r for r in rows if self._passes_screen(r)]
        survivors.sort(key=lambda r: (-(r["t_stat"] or 0), r["hypothesis"]["hypothesis_id"], r["horizon"]))
        return {"enumerated_single_conditions": len(enumerated), "tested_hypotheses": len(hyps),
                "file_hypotheses": len(extra), "file_hypotheses_unavailable_features": missing,
                "tests": len(rows), "budget_limited": len(enumerated) > len(sampled), "rows": rows,
                "survivors": [{"hypothesis_id": r["hypothesis"]["hypothesis_id"], "horizon": r["horizon"]} for r in survivors]}

    def _stage_refine(self, screen: Mapping[str, Any]) -> dict[str, Any]:
        b, c = self.s["budget"], self.s["criteria"]
        by_id = {r["hypothesis"]["hypothesis_id"]: r["hypothesis"] for r in screen["rows"]}
        top = screen["survivors"][: int(b["refine_top_k"])]
        disc = slice(*self.segs["discovery"])
        grids = self.s["search_space"]["indicators"]
        jobs: list[tuple[dict[str, Any], Hypothesis, list[Hypothesis]]] = []
        all_neigh: dict[str, Hypothesis] = {}
        for item in top:
            h = Hypothesis.from_dict(by_id[item["hypothesis_id"]], by_id[item["hypothesis_id"]]["origin"])
            ns = neighbours(h, self.defs, self.features, disc, grids, self.s["threshold_quantiles"])
            jobs.append((item, h, ns))
            for n in ns:
                all_neigh[n.hypothesis_id] = n
        neigh_rows = self._evaluate_batch("refine", [all_neigh[k] for k in sorted(all_neigh)])
        index = {(r["hypothesis"]["hypothesis_id"], r["horizon"]): r for r in neigh_rows}
        results = []
        for item, h, ns in jobs:
            vals = [index[(n.hypothesis_id, item["horizon"])] for n in ns if (n.hypothesis_id, item["horizon"]) in index]
            positive = [v for v in vals if v["excess_mean"] is not None and v["excess_mean"] > 0]
            frac = len(positive) / len(vals) if vals else None
            stable = frac is not None and len(vals) >= 2 and frac >= c["min_neighbour_positive_fraction"]
            results.append({**item, "neighbours": len(vals), "neighbour_positive_fraction": frac,
                            "stability": "STABLE" if stable else ("NO_NEIGHBOURS" if len(vals) < 2 else "UNSTABLE")})
        return {"tests": len(neigh_rows), "rows": neigh_rows, "results": results}

    def _stage_interact(self, screen: Mapping[str, Any], refine: Mapping[str, Any]) -> dict[str, Any]:
        b = self.s["budget"]
        by_id = {r["hypothesis"]["hypothesis_id"]: r["hypothesis"] for r in screen["rows"]}
        keep = [r for r in refine["results"] if r["stability"] != "UNSTABLE"]
        seen: list[Hypothesis] = []
        for r in keep:
            raw = by_id[r["hypothesis_id"]]
            h = Hypothesis.from_dict(raw, raw["origin"])
            if len(h.conditions) == 1 and h.hypothesis_id not in {x.hypothesis_id for x in seen}:
                seen.append(h)
        pairs = interactions(seen[: int(b["interaction_pool"])], self.defs, int(b["max_interactions"]))
        rows = self._evaluate_batch("interact", pairs)
        survivors = [r for r in rows if self._passes_screen(r)]
        survivors.sort(key=lambda r: (-(r["t_stat"] or 0), r["hypothesis"]["hypothesis_id"], r["horizon"]))
        return {"tests": len(rows), "rows": rows,
                "survivors": [{"hypothesis_id": r["hypothesis"]["hypothesis_id"], "horizon": r["horizon"]} for r in survivors]}

    def _discovery_index(self, *stages: Mapping[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
        idx: dict[tuple[str, int], dict[str, Any]] = {}
        for st in stages:
            for r in st["rows"]:
                idx[(r["hypothesis"]["hypothesis_id"], r["horizon"])] = r
        return idx

    def _finalists(self, screen: Mapping[str, Any], refine: Mapping[str, Any], interact: Mapping[str, Any]) -> list[tuple[str, int]]:
        stable = [(r["hypothesis_id"], r["horizon"]) for r in refine["results"] if r["stability"] != "UNSTABLE"]
        inter = [(r["hypothesis_id"], r["horizon"]) for r in interact["survivors"]]
        idx = self._discovery_index(screen, interact)
        pool = sorted(set(stable + inter), key=lambda k: (-(idx[k]["t_stat"] or 0), k))
        return pool[: int(self.s["budget"]["max_finalists"])]

    def _stage_validate(self, screen: Mapping[str, Any], refine: Mapping[str, Any], interact: Mapping[str, Any]) -> dict[str, Any]:
        c = self.s["criteria"]
        idx = self._discovery_index(screen, interact)
        total = screen["tests"] + refine["tests"] + interact["tests"]
        finalists = self._finalists(screen, refine, interact)
        rows = []
        for hid, hz in finalists:
            h = Hypothesis.from_dict(idx[(hid, hz)]["hypothesis"], idx[(hid, hz)]["hypothesis"]["origin"])
            r = self._eval(h, "validation", hz, regimes=self.regimes)
            passed = (r["independent_events"] >= c["min_events_validation"] and r["mean_net"] is not None
                      and r["mean_net"] > 0 and r["excess_mean"] > 0)
            rows.append({"hypothesis": h.to_dict(), "passed": passed, **r})
        all_p = [r["p_value"] for st in (screen, refine, interact) for r in st["rows"]]
        q_all = bh_adjust(all_p, total)
        qmap = {}
        k = 0
        for st in (screen, refine, interact):
            for r in st["rows"]:
                qmap[(r["hypothesis"]["hypothesis_id"], r["horizon"])] = q_all[k]
                k += 1
        disc = []
        for hid, hz in finalists:
            r = idx[(hid, hz)]
            disc.append({"hypothesis_id": hid, "horizon": hz, "discovery_q_bh": qmap.get((hid, hz)),
                         "discovery_p_bonferroni": bonferroni(r["p_value"], total)})
        return {"total_discovery_tests": total, "finalists": [list(f) for f in finalists], "rows": rows,
                "multiple_testing": disc}

    def _stage_test(self, validate: Mapping[str, Any]) -> dict[str, Any]:
        c = self.s["criteria"]
        rows = []
        for v in validate["rows"]:
            if not v["passed"]:
                continue
            h = Hypothesis.from_dict(v["hypothesis"], v["hypothesis"]["origin"])
            r = self._eval(h, "test", v["horizon"], regimes=self.regimes, keep_events=True)
            if r["independent_events"] < c["min_events_test"]:
                verdict = "TEST_INSUFFICIENT_EVENTS"
            elif r["mean_net"] > 0 and r["excess_mean"] > 0:
                verdict = "TEST_CONSISTENT"
            else:
                verdict = "TEST_INCONSISTENT"
            rows.append({"hypothesis": h.to_dict(), "verdict": verdict, **r})
        return {"rows": rows, "evaluated_once": True}

    # ------------------------------------------------------------------ driver
    def execute(self) -> dict[str, Any]:
        self._load()
        manifest_path = self.dir / "run-manifest.json"
        if manifest_path.exists() and read_json(manifest_path).get("state") == "COMPLETED":
            from .report import verify_outputs
            verify_outputs(self.dir)
            self.log(f"[{self.symbol}] reusing completed run {self.run_id[:12]}")
            return {"run_id": self.run_id, "symbol": self.symbol, "dir": str(self.dir), "reused": True,
                    "state": "COMPLETED"}
        try:
            data = self._stage("data", self._stage_data)
            if data["status"] != "ACCEPTED":
                self._status("data", "QUARANTINED", issues=data["issues"])
                return {"run_id": self.run_id, "symbol": self.symbol, "dir": str(self.dir), "reused": False,
                        "state": "QUARANTINED", "issues": data["issues"]}
            if not hasattr(self, "features"):
                self._prepare()
            screen = self._stage("screen", self._stage_screen)
            refine = self._stage("refine", lambda: self._stage_refine(screen))
            interact = self._stage("interact", lambda: self._stage_interact(screen, refine))
            validate = self._stage("validate", lambda: self._stage_validate(screen, refine, interact))
            test = self._stage("test", lambda: self._stage_test(validate))
            from .report import write_outputs
            self._stage("report", lambda: write_outputs(self, data, screen, refine, interact, validate, test))
            self._status("report", "COMPLETED")
            return {"run_id": self.run_id, "symbol": self.symbol, "dir": str(self.dir), "reused": False,
                    "state": "COMPLETED"}
        except StopRequested as stop:
            self._status("stopped", "STOPPED", stop_file=str(stop))
            return {"run_id": self.run_id, "symbol": self.symbol, "dir": str(self.dir), "state": "STOPPED"}
        except Exception as exc:
            self._status("error", "FAILED", error=f"{type(exc).__name__}: {exc}")
            raise


def classification() -> str:
    return CLASSIFICATION
