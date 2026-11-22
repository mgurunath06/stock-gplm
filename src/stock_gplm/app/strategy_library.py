"""Declarative, self-expanding strategy library (``stock-gplm.strategy-def.v1``).

Strategies are data, never Python. A library is an ordered list of **roots**:

* the shipped defaults in ``config/strategies/library`` (catalogue translations, AI hypotheses,
  examples) — read-only at run time; and
* the writable root ``<home>/strategy-library`` holding user edits (``user/``), ML and AI
  discoveries (``discovered/``), ``state.json`` (enable/disable overrides) and ``.history/``.

Every ``*.json`` file below a root (dot-directories excluded) is read on every load, so a new or
edited file is picked up by the next run with no code change. A file holds one strategy or a bundle
``{"defaults": {...}, "exit_profiles": {...}, "strategies": [...]}``. When the same ``id`` exists in
several roots, the writable root wins (an editable copy overriding an immutable default);
``restore`` archives the copy and the default becomes effective again.

Versioning: each distinct canonical definition of an ``id`` gets a version number in
``.history/registry.json`` and an immutable snapshot ``.history/<id>/v<N>-<sha12>.json``. Snapshots
are never overwritten or deleted, so failed experiments and superseded definitions stay restorable.

Origins: ``CATALOGUE_ORIGINAL`` (the hash-bound source text, kept in ``original_text``),
``CATALOGUE_DERIVED``, ``ML_DISCOVERED``, ``AI_GENERATED``, ``USER_DEFINED``.
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import strategy_dsl as dsl
from .db import utc_now

CONTRACT = "stock-gplm.strategy-def.v1"
ORIGINS = ("CATALOGUE_ORIGINAL", "CATALOGUE_DERIVED", "ML_DISCOVERED", "AI_GENERATED", "USER_DEFINED")
TIMEFRAMES = ("1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo")  # 1mo: owner 2026-11-21 (monthly scans)
INTRADAY = ("1m", "5m", "15m", "30m", "60m")
BLOCK_STATUSES = ("BLOCKED_MISSING_DATA", "BLOCKED_IMPLEMENTATION", "BLOCKED_METHOD", "DEFERRED")
MAX_GRID = 64
_KNOWN = {"contract", "id", "version", "origin", "reference_id", "interpretation", "name", "category", "style",
          "description", "direction", "entry", "exit", "params", "applicability", "fidelity", "assumptions",
          "requires", "blocked", "enabled", "provenance", "evidence", "original_text", "parent", "tags", "benchmark",
          "method", "notes"}
DEFAULT_EXIT = {"max_hold_bars": 10, "stop_atr": None, "target_atr": None, "trail_atr": None, "when": None}
DEFAULT_PROFILE = {"max_hold_bars": 10, "stop_atr": 2.0, "target_atr": 3.0, "trail_atr": None, "when": None}


class StrategyConfigError(ValueError):
    pass


def canonical(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)


def sha(doc: Any) -> str:
    return hashlib.sha256(canonical(doc).encode()).hexdigest()


@dataclass
class StrategyDef:
    """One effective, validated strategy definition."""

    doc: dict[str, Any]
    path: str
    root: str
    entry_node: tuple | None = None
    exit_node: tuple | None = None
    version: int = 1
    sha256: str = ""
    enabled: bool = True
    needs: dict[str, bool] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.doc["id"]

    @property
    def origin(self) -> str:
        return self.doc["origin"]

    @property
    def direction(self) -> str:
        return self.doc.get("direction", "long")

    @property
    def blocked(self) -> dict[str, Any] | None:
        return self.doc.get("blocked")

    @property
    def reference_id(self) -> str:
        return self.doc.get("reference_id") or ""

    @property
    def timeframes(self) -> list[str]:
        return list((self.doc.get("applicability") or {}).get("timeframes") or TIMEFRAMES)

    def applies(self, symbol: str, timeframe: str, groups: Mapping[str, Sequence[str]] | None = None) -> bool:
        app = self.doc.get("applicability") or {}
        if timeframe not in self.timeframes:
            return False
        syms = app.get("symbols", "*")
        grp = app.get("groups") or []
        if syms in ("*", None, []) and not grp:
            return True
        base = symbol.upper()
        bare = base[:-3] if base.endswith("-EQ") else base
        names = set()
        if isinstance(syms, list):
            names |= {s.upper() for s in syms}
        for g in grp:
            names |= {s.upper() for s in (groups or {}).get(g, [])}
        return base in names or bare in names or (bare + "-EQ") in names

    def grid(self) -> list[dict[str, Any]]:
        params = self.doc.get("params") or {}
        combos: list[dict[str, Any]] = [{}]
        for name in sorted(params):
            spec = params[name]
            values = spec.get("grid") or [spec["default"]]
            combos = [{**c, name: v} for c in combos for v in values]
        if len(combos) > MAX_GRID:  # deterministic thinning, defaults always kept
            step = len(combos) / MAX_GRID
            keep = [combos[int(i * step)] for i in range(MAX_GRID)]
            d = self.defaults()
            combos = keep if d in keep else [d] + keep[:-1]
        return combos

    def defaults(self) -> dict[str, Any]:
        return {k: v["default"] for k, v in (self.doc.get("params") or {}).items()}

    def fingerprint(self) -> str:
        """Equivalence key used for deduplication (rule + exit + direction + timeframes)."""
        node = dsl.substitute(self.entry_node, self.defaults()) if self.entry_node else None
        raw_exit = self.doc.get("exit")
        ex = dict(raw_exit) if isinstance(raw_exit, dict) else {"profile": raw_exit}
        if self.exit_node is not None:
            ex["when"] = dsl.unparse(dsl.substitute(self.exit_node, self.defaults()))
        return sha({"entry": dsl.unparse(node) if node else None, "exit": ex, "dir": self.direction,
                    "tf": sorted(self.timeframes), "symbols": (self.doc.get("applicability") or {}).get("symbols", "*")})


def _merge_exit(raw: Any, profiles: Mapping[str, Any]) -> dict[str, Any]:
    if raw is None:
        raw = "default"
    if isinstance(raw, str):
        if raw not in profiles and raw != "default":
            raise StrategyConfigError(f"unknown exit profile {raw!r}")
        raw = profiles.get(raw, DEFAULT_PROFILE)
    if isinstance(raw, dict) and "profile" in raw:
        base = profiles.get(raw["profile"])
        if base is None:
            raise StrategyConfigError(f"unknown exit profile {raw['profile']!r}")
        raw = {**base, **{k: v for k, v in raw.items() if k != "profile"}}
    return {**DEFAULT_EXIT, **raw}


def normalize(raw: Mapping[str, Any], defaults: Mapping[str, Any] | None = None,
              profiles: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Apply bundle defaults and exit profiles; return a complete definition (not yet validated)."""
    doc = copy.deepcopy(dict(defaults or {}))
    for k, v in raw.items():
        if k == "applicability" and isinstance(v, dict):
            doc["applicability"] = {**(doc.get("applicability") or {}), **v}
        else:
            doc[k] = copy.deepcopy(v)
    doc.setdefault("contract", CONTRACT)
    doc.setdefault("direction", "long")
    doc.setdefault("params", {})
    doc.setdefault("assumptions", [])
    doc.setdefault("requires", ["OHLCV"])
    doc.setdefault("enabled", True)
    doc.setdefault("applicability", {"symbols": "*", "timeframes": ["1d"]})
    doc["applicability"].setdefault("symbols", "*")
    doc["applicability"].setdefault("regimes", ["ALL"])
    if not doc.get("blocked"):
        doc["exit"] = _merge_exit(doc.get("exit"), profiles or {})
    return doc


def validate(doc: Mapping[str, Any]) -> tuple[tuple | None, tuple | None, dict[str, bool]]:
    """Check one normalised definition. Returns parsed entry/exit ASTs and data needs."""
    for k in ("id", "name", "origin"):
        if not doc.get(k):
            raise StrategyConfigError(f"missing {k}")
    unknown = set(doc) - _KNOWN
    if unknown:
        raise StrategyConfigError(f"unknown fields {sorted(unknown)}")
    if doc["origin"] not in ORIGINS:
        raise StrategyConfigError(f"origin must be one of {ORIGINS}")
    if doc.get("direction") not in ("long", "short"):
        raise StrategyConfigError("direction must be long or short")
    tfs = (doc.get("applicability") or {}).get("timeframes") or []
    bad = set(tfs) - set(TIMEFRAMES)
    if bad or not tfs:
        raise StrategyConfigError(f"applicability.timeframes invalid: {sorted(bad) or 'empty'}")
    if doc.get("blocked"):
        b = doc["blocked"]
        if b.get("status") not in BLOCK_STATUSES or not b.get("reason"):
            raise StrategyConfigError(f"blocked needs status in {BLOCK_STATUSES} and a reason")
        return None, None, {}
    params = doc.get("params") or {}
    for name, spec in params.items():
        if not isinstance(spec, dict) or "default" not in spec:
            raise StrategyConfigError(f"param {name} needs a default")
        grid = spec.get("grid") or []
        if not isinstance(grid, list) or len(grid) > 40:
            raise StrategyConfigError(f"param {name} grid must be a list of <= 40 values")
    entry = doc.get("entry")
    if not isinstance(entry, str):
        raise StrategyConfigError("entry must be an expression string")
    try:
        enode = dsl.parse(entry)
        ex = doc.get("exit") or {}
        xnode = dsl.parse(ex["when"]) if ex.get("when") else None
    except dsl.DSLError as exc:
        raise StrategyConfigError(f"expression error: {exc}") from exc
    used = dsl.params_used(enode) | (dsl.params_used(xnode) if xnode else set())
    missing = used - {k.lower() for k in params}
    if missing:
        raise StrategyConfigError(f"parameters used but not declared: {sorted(missing)}")
    ex = doc["exit"]
    hold = ex.get("max_hold_bars")
    if not isinstance(hold, int) or not 1 <= hold <= 2000:
        raise StrategyConfigError("exit.max_hold_bars must be an integer 1..2000")
    for k in ("stop_atr", "target_atr", "trail_atr"):
        v = ex.get(k)
        if v is not None and not (isinstance(v, (int, float)) and 0 < v <= 50):
            raise StrategyConfigError(f"exit.{k} must be null or in (0, 50]")
    for k in ("stop_pct", "target_pct"):  # owner 2026-11-22: fixed % stop / target (0.01 = 1%)
        v = ex.get(k)
        if v is not None and not (isinstance(v, (int, float)) and 0 < v <= 0.5):
            raise StrategyConfigError(f"exit.{k} must be null or in (0, 0.5]")
    if not isinstance(ex.get("overnight", False), bool):
        raise StrategyConfigError("exit.overnight must be true or false")
    tema = ex.get("target_ema")
    if tema is not None and not (isinstance(tema, int) and 2 <= tema <= 500):
        raise StrategyConfigError("exit.target_ema must be null or an integer 2..500")
    mrr = ex.get("min_rr")
    if mrr is not None and not (isinstance(mrr, (int, float)) and 0 <= mrr <= 20):
        raise StrategyConfigError("exit.min_rr must be null or 0..20")
    sw = ex.get("swing_stop")
    if sw is not None:
        if not isinstance(sw, dict) or set(sw) - {"buffer", "rr", "lookback", "max_swing_pct", "strength"}:
            raise StrategyConfigError("exit.swing_stop must be {buffer, rr, lookback, max_swing_pct, strength}")
        if not 1 <= int(sw.get("strength", 2)) <= 20:
            raise StrategyConfigError("exit.swing_stop.strength must be 1..20")
        if not 0 <= float(sw.get("buffer", 0.002)) <= 0.05 or not 0.2 <= float(sw.get("rr", 2.0)) <= 10 \
                or not 3 <= int(sw.get("lookback", 20)) <= 200:
            raise StrategyConfigError("exit.swing_stop: buffer 0..0.05, rr 0.2..10, lookback 3..200")
    pb = ex.get("pullback_stop")  # owner 2026-11-22: stop beyond the lowest low of the pullback
    if pb is not None:
        if not isinstance(pb, dict) or set(pb) - {"fast", "slow", "buffer", "lookback"}:
            raise StrategyConfigError("exit.pullback_stop must be {fast, slow, buffer, lookback}")
        if not 2 <= int(pb.get("fast", 13)) < int(pb.get("slow", 20)) <= 500 \
                or not 0 <= float(pb.get("buffer", 0.0)) <= 0.05 or not 5 <= int(pb.get("lookback", 400)) <= 5000:
            raise StrategyConfigError("exit.pullback_stop: 2 <= fast < slow <= 500, buffer 0..0.05, lookback 5..5000")
    rg = ex.get("ema_regime")  # owner 2026-11-22: below EMA anchor -> target it; above -> exit on EMA trail breach
    if rg is not None:
        if not isinstance(rg, dict) or set(rg) - {"anchor", "trail", "target", "breach_fill"}:
            raise StrategyConfigError("exit.ema_regime must be {anchor, trail, target, breach_fill}")
        if not 2 <= int(rg.get("trail", 20)) < int(rg.get("anchor", 200)) <= 500 \
                or rg.get("target", "live") not in ("live", "fixed") \
                or rg.get("breach_fill", "close") not in ("close", "next_open"):
            raise StrategyConfigError("exit.ema_regime: 2 <= trail < anchor <= 500, target live|fixed, "
                                      "breach_fill close|next_open")
    needs = dsl.requirements(enode)
    if xnode is not None:
        for k, v in dsl.requirements(xnode).items():
            needs[k] = needs.get(k, False) or v
    if needs.get("intraday") and not set(tfs) <= set(INTRADAY):
        raise StrategyConfigError("uses intraday-only functions but applicability includes daily/weekly timeframes")
    return enode, xnode, needs


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        for f in sorted(filenames):
            if f.endswith(".json") and not f.startswith((".", "_")) and f != "state.json":
                out.append(Path(dirpath) / f)
    return out


@dataclass
class LoadResult:
    strategies: dict[str, StrategyDef]
    invalid: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    shadowed: list[dict[str, Any]]

    def summary(self) -> dict[str, Any]:
        by_origin: dict[str, int] = {}
        for s in self.strategies.values():
            by_origin[s.origin] = by_origin.get(s.origin, 0) + 1
        return {"strategies": len(self.strategies), "by_origin": by_origin, "invalid": len(self.invalid),
                "new_or_changed": len(self.changes), "overridden_defaults": len(self.shadowed),
                "enabled": sum(s.enabled for s in self.strategies.values()),
                "blocked": sum(1 for s in self.strategies.values() if s.blocked)}


class StrategyLibrary:
    def __init__(self, roots: Sequence[Path], writable: Path):
        self.writable = Path(writable)
        self.roots = [Path(r) for r in roots if Path(r).resolve() != self.writable.resolve()] + [self.writable]
        self.history = self.writable / ".history"

    # ---- persistence helpers
    def _registry(self) -> dict[str, Any]:
        p = self.history / "registry.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    def _save_json(self, path: Path, doc: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def state(self) -> dict[str, Any]:
        p = self.writable / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"disabled": [], "enabled": []}

    # ---- loading
    def load(self, record: bool = True) -> LoadResult:
        found: dict[str, list[tuple[int, dict[str, Any], str, str]]] = {}
        invalid: list[dict[str, Any]] = []
        for prio, root in enumerate(self.roots):
            for path in _iter_files(root):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    invalid.append({"path": str(path), "id": None, "error": f"unreadable JSON: {exc}"})
                    continue
                items: list[tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any]]]
                if isinstance(raw, dict) and "strategies" in raw:
                    items = [(s, raw.get("defaults") or {}, raw.get("exit_profiles") or {}) for s in raw["strategies"]]
                elif isinstance(raw, dict):
                    items = [(raw, {}, raw.pop("exit_profiles", {}) if isinstance(raw, dict) else {})]
                else:
                    invalid.append({"path": str(path), "id": None, "error": "file must be an object"})
                    continue
                for s, d, prof in items:
                    try:
                        doc = normalize(s, d, prof)
                    except StrategyConfigError as exc:
                        invalid.append({"path": str(path), "id": s.get("id"), "error": str(exc)})
                        continue
                    found.setdefault(str(doc.get("id")), []).append((prio, doc, str(path), str(root)))
        strategies: dict[str, StrategyDef] = {}
        shadowed: list[dict[str, Any]] = []
        st = self.state()
        disabled, enabled = set(st.get("disabled", [])), set(st.get("enabled", []))
        for sid, cands in found.items():
            cands.sort(key=lambda x: x[0])
            top = cands[-1][0]
            same = [c for c in cands if c[0] == top]
            if len(same) > 1:
                invalid.append({"path": ", ".join(c[2] for c in same), "id": sid,
                                "error": "duplicate id inside one library root"})
                continue
            prio, doc, path, root = same[0]
            for c in cands[:-1]:
                shadowed.append({"id": sid, "default_path": c[2], "override_path": path})
            try:
                enode, xnode, needs = validate(doc)
            except StrategyConfigError as exc:
                invalid.append({"path": path, "id": sid, "error": str(exc)})
                continue
            en = bool(doc.get("enabled", True))
            if sid in disabled:
                en = False
            if sid in enabled:
                en = True
            strategies[sid] = StrategyDef(doc, path, root, enode, xnode, int(doc.get("version") or 1), sha(doc), en, needs)
        changes = self._record_versions(strategies) if record else []
        return LoadResult(strategies, invalid, changes, shadowed)

    def _record_versions(self, strategies: Mapping[str, StrategyDef]) -> list[dict[str, Any]]:
        try:
            reg = self._registry()
        except (OSError, ValueError):
            reg = {}
        changes = []
        dirty = False
        for sid, s in strategies.items():
            entry = reg.setdefault(sid, {"versions": []})
            known = {v["sha256"]: v["version"] for v in entry["versions"]}
            if s.sha256 in known:
                s.version = max(s.version, known[s.sha256])
                continue
            ver = max([v["version"] for v in entry["versions"]] + [s.version - 1, 0]) + 1
            s.version = ver
            snap = self.history / _safe(sid) / f"v{ver}-{s.sha256[:12]}.json"
            if not snap.exists():  # immutable snapshot
                self._save_json(snap, s.doc)
            entry["versions"].append({"version": ver, "sha256": s.sha256, "path": s.path, "origin": s.origin,
                                      "recorded_utc": utc_now(), "snapshot": str(snap)})
            changes.append({"id": sid, "version": ver, "kind": "NEW" if ver == 1 else "UPDATED", "path": s.path})
            dirty = True
        if dirty:
            self._save_json(self.history / "registry.json", reg)
        return changes

    # ---- mutation (writable root only; defaults are never modified)
    def add_file(self, source: Path, subdir: str = "user") -> list[str]:
        raw = json.loads(Path(source).read_text(encoding="utf-8-sig"))
        items = raw["strategies"] if isinstance(raw, dict) and "strategies" in raw else [raw]
        for s in items:
            doc = normalize(s, raw.get("defaults") if isinstance(raw, dict) else None,
                            raw.get("exit_profiles") if isinstance(raw, dict) else None)
            validate(doc)
        dest = self.writable / subdir / Path(source).name
        if dest.exists() and dest.read_bytes() != Path(source).read_bytes():
            raise StrategyConfigError(f"{dest} exists with different content; choose another file name")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return [str(s.get("id")) for s in items]

    def save(self, doc: Mapping[str, Any], subdir: str) -> Path:
        d = normalize(doc)
        validate(d)
        path = self.writable / subdir / f"{_safe(d['id'])}.json"
        self._save_json(path, d)
        return path

    def save_discovered(self, doc: Mapping[str, Any]) -> tuple[str, bool]:
        """Persist an ML/AI discovery; deduplicate equivalent rules. Returns (id, created)."""
        d = normalize(doc)
        enode, xnode, needs = validate(d)
        probe = StrategyDef(d, "", str(self.writable), enode, xnode, 1, sha(d), True, needs)
        fp = probe.fingerprint()
        idx_path = self.writable / "discovered" / "_index.json"
        idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.is_file() else {}
        if fp in idx:
            log = self.writable / "discovered" / "_rediscoveries.jsonl"
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"existing_id": idx[fp], "candidate_id": d["id"], "utc": utc_now(),
                                     "provenance": d.get("provenance")}, default=str) + "\n")
            return idx[fp], False
        sub = "ml" if d["origin"] == "ML_DISCOVERED" else "ai"
        self.save(d, f"discovered/{sub}")
        idx[fp] = d["id"]
        self._save_json(idx_path, idx)
        return d["id"], True

    def update_evidence(self, sid: str, evidence: Mapping[str, Any]) -> None:
        """Attach results to a discovered strategy (writable root only). A new version is recorded."""
        for p in (self.writable / "discovered").rglob(f"{_safe(sid)}.json"):
            doc = json.loads(p.read_text(encoding="utf-8"))
            doc["evidence"] = {**(doc.get("evidence") or {}), **evidence}
            self._save_json(p, doc)

    def set_enabled(self, sid: str, flag: bool) -> None:
        st = self.state()
        dis, en = set(st.get("disabled", [])), set(st.get("enabled", []))
        (en if flag else dis).add(sid)
        (dis if flag else en).discard(sid)
        self._save_json(self.writable / "state.json", {"disabled": sorted(dis), "enabled": sorted(en)})

    def copy_for_edit(self, sid: str, changes: Mapping[str, Any] | None = None) -> Path:
        cur = self.load(record=False).strategies.get(sid)
        if cur is None:
            raise StrategyConfigError(f"unknown strategy {sid}")
        doc = copy.deepcopy(cur.doc)
        doc["parent"] = {"id": sid, "version": cur.version, "origin": cur.origin, "sha256": cur.sha256,
                         "path": cur.path}
        doc["origin"] = "USER_DEFINED"
        doc["version"] = cur.version + 1
        for k, v in (changes or {}).items():
            _set_path(doc, k, v)
        validate(normalize(doc))
        cur_path = Path(cur.path)
        if cur_path.is_file() and self.writable.resolve() in cur_path.resolve().parents:
            # already an editable copy: update it in place (the previous version stays in .history)
            raw = json.loads(cur_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict) and "strategies" not in raw:
                self._save_json(cur_path, normalize(doc))
                return cur_path
        return self.save(doc, "user")

    def restore(self, sid: str) -> list[str]:
        """Archive writable-root copies of ``sid`` so the shipped default is effective again."""
        moved = []
        for p in list((self.writable / "user").rglob("*.json")) if (self.writable / "user").is_dir() else []:
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(raw, dict) and raw.get("id") == sid:
                dest = self.history / "archived" / f"{p.stem}-{utc_now().replace(':', '')}.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), dest)
                moved.append(str(dest))
        st = self.state()
        for k in ("disabled", "enabled"):
            if sid in st.get(k, []):
                st[k].remove(sid)
        self._save_json(self.writable / "state.json", st)
        return moved

    def history_of(self, sid: str) -> list[dict[str, Any]]:
        return self._registry().get(sid, {}).get("versions", [])


def _safe(sid: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in sid)


def _set_path(doc: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cur = doc
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def select(strategies: Mapping[str, StrategyDef], patterns: Sequence[str] | None) -> dict[str, StrategyDef]:
    if not patterns:
        return dict(strategies)
    out = {}
    for sid, s in strategies.items():
        keys = (sid, s.reference_id, s.origin)
        if any(fnmatch.fnmatchcase(k, p) for p in patterns for k in keys if k):
            out[sid] = s
    return out


__all__ = ["CONTRACT", "ORIGINS", "LoadResult", "StrategyConfigError", "StrategyDef", "StrategyLibrary", "normalize",
           "select", "sha", "validate"]
