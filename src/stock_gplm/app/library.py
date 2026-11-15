"""Permanent, versioned strategy library (``stock-gplm.strategy.v3``).

* Strategy definitions are JSON files, never source code. Discovered defaults are written once
  under ``<home>/strategies/defaults/<id>/v<N>.json``, made read-only and bound to their SHA-256 in
  the database; they are never modified. Editing produces a *new version* (a user copy under
  ``user/``), so the full version history stays on disk.
* Scope: symbol list or ``"*"`` (universe-wide), and timeframes. Activation is database state, not
  file content, so an immutable default can still be switched on or off.
* :func:`validate` is strict: known keys only, known operators, feature keys that resolve to a
  registered indicator output (optionally ``<tf>:`` prefixed) or a documented intraday feature, and
  finite numbers. Anything else is refused. Nothing in a strategy is ever executed as code.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..m54.indicators import REGISTRY, build_features
from ..m54.timeframes import Bars, align
from ..m54.unified import _intraday_extras
from .db import AuditDB, jdump, sha, utc_now

CONTRACT = "stock-gplm.strategy.v3"
OPS = ("gt", "lt", "cross_above", "cross_below", "is")
TIMEFRAMES = ("1m", "5m", "15m", "30m", "60m", "1d", "1w")
TOP_KEYS = {"contract", "strategy_id", "version", "name", "origin", "immutable", "description", "scope", "timeframe",
            "direction", "entry", "exit", "position_sizing", "costs_profile", "provenance", "evidence", "parent",
            "classification"}
INTRADAY_FEATURES = {"VWAP().session_distance", "VWAP().reclaim", "SESSION().minute_of_session", "SESSION().opening_gap",
                     "ORB(minutes=15).position", "ORB(minutes=15).breakout_up", "ORB(minutes=30).position",
                     "ORB(minutes=30).breakout_up"}
SUMMARY_FEATURES = {"INTRADAY().first_hour_return", "INTRADAY().last_hour_return", "INTRADAY().close_vs_vwap",
                    "INTRADAY().realized_vol", "INTRADAY().close_location", "INTRADAY().volume_unknown_share"}
_KEY = re.compile(r"^(?:(?P<tf>1m|5m|15m|30m|60m|1d|1w):)?(?P<ind>[A-Z_]+)\((?P<params>[^)]*)\)\.(?P<out>[a-z_0-9]+)$")


class StrategyError(ValueError):
    """A strategy document failed validation."""


def parse_feature(key: str) -> dict[str, Any]:
    if key in INTRADAY_FEATURES or key in SUMMARY_FEATURES:
        return {"tf": None, "indicator": key.split("(")[0], "params": {}, "output": key.split(".")[-1], "special": True}
    m = _KEY.fullmatch(key)
    if not m:
        raise StrategyError(f"feature {key!r} is not IND(k=v,...).output")
    ind = m["ind"]
    if ind not in REGISTRY:
        raise StrategyError(f"unknown indicator {ind}")
    if m["out"] not in REGISTRY[ind].outputs:
        raise StrategyError(f"{ind} has no output {m['out']}; outputs: {sorted(REGISTRY[ind].outputs)}")
    params: dict[str, Any] = {}
    if m["params"]:
        for part in m["params"].split(","):
            k, _, v = part.partition("=")
            if not re.fullmatch(r"[a-z_]+", k) or not v:
                raise StrategyError(f"bad parameter {part!r} in {key}")
            if re.fullmatch(r"-?\d+", v):
                params[k] = int(v)
            elif re.fullmatch(r"-?\d+\.\d*", v):
                params[k] = float(v)
            elif re.fullmatch(r"[A-Z]+", v):
                params[k] = v
            else:
                raise StrategyError(f"bad parameter value {v!r} in {key}")
    for k, v in params.items():
        if isinstance(v, int) and not 1 <= v <= 500:
            raise StrategyError(f"parameter {k}={v} outside 1..500")
    return {"tf": m["tf"], "indicator": ind, "params": params, "output": m["out"], "special": False}


def _num(v: Any, name: str, lo: float, hi: float, allow_none: bool = True) -> None:
    if v is None and allow_none:
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or not lo <= float(v) <= hi:
        raise StrategyError(f"{name} must be a number in [{lo}, {hi}]")


def validate(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalised copy or raise :class:`StrategyError`."""
    if not isinstance(doc, Mapping):
        raise StrategyError("strategy must be a JSON object")
    unknown = set(doc) - TOP_KEYS
    if unknown:
        raise StrategyError(f"unknown keys {sorted(unknown)}")
    d = json.loads(json.dumps(doc))
    d["contract"] = CONTRACT
    if not isinstance(d.get("name"), str) or not 1 <= len(d["name"]) <= 120:
        raise StrategyError("name must be a 1-120 character string")
    tf = d.get("timeframe")
    if tf not in TIMEFRAMES:
        raise StrategyError(f"timeframe must be one of {TIMEFRAMES}")
    if d.get("direction", "long") not in ("long", "short"):
        raise StrategyError("direction must be long or short")
    d.setdefault("direction", "long")
    scope = d.get("scope") or {"symbols": "*", "timeframes": [tf]}
    syms = scope.get("symbols", "*")
    if syms != "*" and (not isinstance(syms, list) or not all(isinstance(s, str) and re.fullmatch(r"[A-Z0-9&_.-]{1,40}", s)
                                                               for s in syms)):
        raise StrategyError("scope.symbols must be '*' or a list of symbols")
    if syms != "*":
        scope["symbols"] = [s if s.endswith("-EQ") else f"{s}-EQ" for s in syms]
    scope["timeframes"] = [t for t in scope.get("timeframes", [tf]) if t in TIMEFRAMES] or [tf]
    d["scope"] = scope
    entry = d.get("entry")
    if not isinstance(entry, Mapping) or entry.get("logic", "AND") != "AND":
        raise StrategyError("entry must be an object with logic AND")
    conds = entry.get("conditions")
    if not isinstance(conds, list) or not 1 <= len(conds) <= 6:
        raise StrategyError("entry.conditions must hold 1-6 conditions")
    clean = []
    for c in conds:
        if not isinstance(c, Mapping) or set(c) - {"feature", "op", "threshold", "label", "text", "timeframe", "family",
                                                    "indicator", "output", "params", "threshold_basis"}:
            raise StrategyError(f"bad condition {c!r}")
        if c.get("op") not in OPS:
            raise StrategyError(f"operator must be one of {OPS}")
        parse_feature(str(c.get("feature")))
        if c["op"] == "is":
            if c.get("threshold") not in (None,):
                raise StrategyError("'is' takes no threshold")
        else:
            _num(c.get("threshold"), "threshold", -1e9, 1e9, allow_none=False)
        clean.append({"feature": c["feature"], "op": c["op"], "threshold": c.get("threshold"),
                      "label": str(c.get("label", c.get("threshold_basis", "")))[:40]})
    d["entry"] = {"logic": "AND", "conditions": clean}
    ex = d.get("exit") or {}
    if set(ex) - {"max_hold_bars", "stop_atr", "target_atr", "trail_atr", "text", "atr", "order_of_checks"}:
        raise StrategyError("exit has unknown keys")
    _num(ex.get("max_hold_bars"), "exit.max_hold_bars", 1, 2000, allow_none=False)
    for k in ("stop_atr", "target_atr", "trail_atr"):
        _num(ex.get(k), f"exit.{k}", 0.1, 50)
    d["exit"] = {"max_hold_bars": int(ex["max_hold_bars"]), "stop_atr": ex.get("stop_atr"),
                 "target_atr": ex.get("target_atr"), "trail_atr": ex.get("trail_atr")}
    ps = d.get("position_sizing") or {"method": "fixed_fraction", "risk_per_trade_pct": 0.5}
    if ps.get("method") not in ("fixed_fraction", "fixed_notional", "equal_weight"):
        raise StrategyError("position_sizing.method must be fixed_fraction, fixed_notional or equal_weight")
    _num(ps.get("risk_per_trade_pct"), "position_sizing.risk_per_trade_pct", 0.01, 10)
    d["position_sizing"] = {k: ps[k] for k in ps if k in ("method", "risk_per_trade_pct", "notional", "note")}
    if d.get("costs_profile", "delivery" if tf in ("1d", "1w") else "intraday") not in ("delivery", "intraday"):
        raise StrategyError("costs_profile must be delivery or intraday")
    d.setdefault("costs_profile", "delivery" if tf in ("1d", "1w") else "intraday")
    d.setdefault("origin", "user")
    d.setdefault("description", "")
    d["classification"] = "RESEARCH_ONLY_NOT_INVESTMENT_ADVICE"
    for k in ("provenance", "evidence"):
        if k in d and not isinstance(d[k], Mapping):
            raise StrategyError(f"{k} must be an object")
    return d


def strategy_id_for(doc: Mapping[str, Any]) -> str:
    core = {"tf": doc["timeframe"], "dir": doc.get("direction", "long"), "entry": doc["entry"], "exit": doc["exit"],
            "scope": doc.get("scope")}
    return "st-" + sha(core)[:16]


class Library:
    def __init__(self, root: Path, db: AuditDB):
        self.root = Path(root)
        self.db = db

    def _path(self, sid: str, version: int, immutable: bool) -> Path:
        return self.root / ("defaults" if immutable else "user") / sid / f"v{version}.json"

    def add(self, doc: Mapping[str, Any], *, immutable: bool, origin: str, active: bool,
            parent: tuple[str, int] | None = None, provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
        d = validate(doc)
        d["origin"] = origin
        sid = d.get("strategy_id") if parent else None
        sid = sid or strategy_id_for(d)
        row = self.db.one("SELECT MAX(version) v FROM strategies WHERE strategy_id=?", (sid,))
        exists = row and row["v"] is not None
        if exists and parent is None:
            cur = self.get(sid)
            return {"strategy_id": sid, "version": cur["version"], "created": False, "path": cur["_path"]}
        version = (row["v"] + 1) if exists else 1
        d.update(strategy_id=sid, version=version, immutable=immutable,
                 parent={"strategy_id": parent[0], "version": parent[1]} if parent else None)
        if provenance:
            d["provenance"] = {**d.get("provenance", {}), **provenance}
        path = self._path(sid, version, immutable)
        if path.exists():
            raise StrategyError(f"{path} already exists; versions are never overwritten")
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(d, indent=2, sort_keys=True) + "\n"
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        if immutable:
            try:
                os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                pass
        digest = sha(text)
        with self.db.tx() as c:
            c.execute("INSERT INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, version, d["name"], origin, int(immutable), int(active), d["timeframe"], jdump(d["scope"]),
                       str(path), digest, parent[0] if parent else None, parent[1] if parent else None, utc_now(),
                       jdump(d.get("provenance", {}))))
        self.db.event("STRATEGY_VERSION_CREATED", {"strategy_id": sid, "version": version, "origin": origin,
                                                   "immutable": immutable, "active": active, "sha256": digest,
                                                   "path": str(path), "parent": d["parent"]}, subject=sid)
        return {"strategy_id": sid, "version": version, "created": True, "path": str(path)}

    def get(self, sid: str, version: int | None = None) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM strategies WHERE strategy_id=? " + ("AND version=? " if version else "")
                          + "ORDER BY version DESC LIMIT 1", (sid, version) if version else (sid,))
        if row is None:
            matches = self.db.q("SELECT DISTINCT strategy_id FROM strategies WHERE strategy_id LIKE ?", (f"{sid}%",))
            if len(matches) == 1:
                return self.get(matches[0]["strategy_id"], version)
            raise StrategyError(f"no strategy {sid}" + (f" v{version}" if version else ""))
        text = Path(row["path"]).read_text(encoding="utf-8")
        if sha(text) != row["sha256"]:
            raise StrategyError(f"{row['path']} does not match its recorded SHA-256 (edited outside GPLM)")
        d = json.loads(text)
        d["_path"], d["_active"], d["_sha256"] = row["path"], bool(row["active"]), row["sha256"]
        return d

    def list(self, symbol: str | None = None, timeframe: str | None = None, active_only: bool = False
             ) -> list[dict[str, Any]]:
        rows = self.db.q("SELECT s.* FROM strategies s JOIN (SELECT strategy_id, MAX(version) v FROM strategies "
                         "GROUP BY strategy_id) m ON s.strategy_id=m.strategy_id AND s.version=m.v ORDER BY s.created_utc")
        out = []
        for r in rows:
            scope = json.loads(r["scope_json"])
            if active_only and not r["active"]:
                continue
            if timeframe and timeframe not in scope.get("timeframes", [r["timeframe"]]):
                continue
            if symbol and scope.get("symbols") != "*" and symbol not in scope.get("symbols", []):
                continue
            out.append(r)
        return out

    def set_active(self, sid: str, active: bool) -> None:
        d = self.get(sid)
        with self.db.tx() as c:
            c.execute("UPDATE strategies SET active=? WHERE strategy_id=? AND version=?", (int(active), d["strategy_id"],
                                                                                         d["version"]))
        self.db.event("STRATEGY_ACTIVATED" if active else "STRATEGY_DEACTIVATED",
                      {"strategy_id": d["strategy_id"], "version": d["version"]}, subject=d["strategy_id"])

    def copy_for_edit(self, sid: str, changes: Mapping[str, Any] | None = None, name: str | None = None) -> dict[str, Any]:
        base = self.get(sid)
        doc = {k: v for k, v in base.items() if not k.startswith("_")}
        if changes:
            for k, v in changes.items():
                doc[k] = v
        if name:
            doc["name"] = name
        return self.add(doc, immutable=False, origin=f"user-edit-of:{base['strategy_id']}@v{base['version']}",
                        active=False, parent=(base["strategy_id"], base["version"]))

    def history(self, sid: str) -> list[dict[str, Any]]:
        d = self.get(sid)
        return self.db.q("SELECT strategy_id, version, name, origin, immutable, active, created_utc, sha256, parent_id, "
                         "parent_version, path FROM strategies WHERE strategy_id=? ORDER BY version", (d["strategy_id"],))


def from_m54(s: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    """Convert a ``stock-gplm.m54-strategy.v2`` record into a v3 document (evidence kept verbatim)."""
    conds = s["entry"]["conditions"]
    tf = s.get("timeframe") or s.get("lane") or next((c.get("timeframe") for c in conds if ":" not in c["feature"]), "1d")
    ex = s["exit"]
    return {"name": s.get("name") or f"{symbol} {tf} " + " AND ".join(c.get("text", c["feature"]) for c in conds)[:100],
            "timeframe": tf, "direction": s.get("direction", "long"),
            "scope": {"symbols": [symbol], "timeframes": [tf]},
            "entry": {"logic": "AND", "conditions": [{"feature": c["feature"], "op": c["op"], "threshold": c.get("threshold"),
                                                      "label": c.get("threshold_basis", "")} for c in conds]},
            "exit": {"max_hold_bars": ex["max_hold_bars"], "stop_atr": ex.get("stop_atr"), "target_atr": ex.get("target_atr"),
                     "trail_atr": ex.get("trail_atr")},
            "evidence": s.get("evidence", {}), "description": s.get("tier", "")}


# ------------------------------------------------------------------ evaluation on a lane
def strategy_features(doc: Mapping[str, Any], bars: Mapping[str, Bars], base_tf: str) -> dict[str, np.ndarray]:
    """Compute exactly the features a strategy references on the base lane (causally aligned HTF)."""
    base = bars[base_tf]
    f = base.frame.reset_index(drop=True)
    end_ns = f["end_ns"].to_numpy(dtype=np.int64)
    out: dict[str, np.ndarray] = {}
    need: dict[str | None, dict[str, dict[str, list[Any]]]] = {}
    special: list[str] = []
    for c in doc["entry"]["conditions"]:
        p = parse_feature(c["feature"])
        if p["special"]:
            special.append(c["feature"])
            continue
        tf = p["tf"] if p["tf"] and p["tf"] != base_tf else None
        spec = need.setdefault(tf, {}).setdefault(p["indicator"], {})
        for k, v in p["params"].items():
            if v not in spec.setdefault(k, []):
                spec[k].append(v)
    for tf, inds in need.items():
        space = {"indicators": {i: {"params": prm} for i, prm in inds.items()}, "levels": {}}
        src = bars[tf] if tf else base
        if not len(src):
            continue
        sf = src.frame.reset_index(drop=True)
        arrays, _, _ = build_features(sf[["open", "high", "low", "close", "volume"]], space)
        for k, v in arrays.items():
            out[f"{tf}:{k}" if tf else k] = align(end_ns, sf["avail_ns"].to_numpy(dtype=np.int64), v) if tf else v
            if tf is None:
                out[f"{base_tf}:{k}"] = v
    if special and base.intraday:
        ea, _ = _intraday_extras(f, int(base_tf.rstrip("m")))
        out.update(ea)
    return out


def strategy_mask(doc: Mapping[str, Any], features: Mapping[str, np.ndarray], n: int) -> np.ndarray:
    from ..m54.hypotheses import Hypothesis, signal_mask

    h = Hypothesis.from_dict({"conditions": doc["entry"]["conditions"]}, origin="library")
    if any(c.feature not in features for c in h.conditions):
        return np.zeros(n, dtype=bool)
    return signal_mask(h, features)


def seed_templates(lib: Library, template_dir: Path) -> list[dict[str, Any]]:
    """Register every manually authored template under ``config/app/strategies`` (idempotent)."""
    out = []
    for p in sorted(Path(template_dir).glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
        out.append(lib.add(doc, immutable=True, origin=f"template:{p.name}", active=bool(doc.pop("active", True))
                           if "active" in doc else True, provenance={"template_file": p.name}))
    return out


def scoped(doc: Mapping[str, Any], symbol: str, timeframe: str) -> bool:
    sc = doc.get("scope", {})
    return (sc.get("symbols") == "*" or symbol in sc.get("symbols", [])) and timeframe in sc.get("timeframes", [doc["timeframe"]])


def summarize(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"strategy_id": r["strategy_id"], "version": r["version"], "name": r["name"], "timeframe": r["timeframe"],
             "origin": r["origin"], "immutable": bool(r["immutable"]), "active": bool(r["active"]),
             "scope": json.loads(r["scope_json"])} for r in rows]
