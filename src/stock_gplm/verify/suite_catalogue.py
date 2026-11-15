"""Section 1: every catalogue interpretation against its original pseudo-code."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from ..app import strategy_dsl as dsl
from ..app.m56_catalogue import load_catalogue
from ..app.strategy_library import StrategyLibrary
from . import fixtures as fx
from . import specparse as sp
from .core import Check, Section
from .suite_indicators import cols

REPO = Path(__file__).resolve().parents[3]
CATALOGUE = REPO / "config" / "strategies" / "reference-300-strategies.txt"
LIBRARY = REPO / "config" / "strategies" / "library"
WARMUP = 260  # longest lookback in the subset (52-week max + lag)
VERDICTS = ("VERIFIED_SEMANTIC", "VERIFIED_STRUCTURAL", "APPROXIMATION", "REVIEW", "MISMATCH", "BLOCKED", "DEFERRED")


def _dsl_numbers(s: Any) -> list[float]:
    text = s.doc.get("entry") or ""
    for k, v in s.defaults().items():
        text = text.replace("$" + k, str(v))
    nums = [float(x) for x in re.findall(r"(?<![A-Za-z_])\d+(?:\.\d+)?", text)]
    for spec in (s.doc.get("params") or {}).values():
        nums += [float(v) for v in (spec.get("grid") or []) + [spec.get("default")] if isinstance(v, (int, float))]
    return nums


_SHORT = re.compile(r"\(short\b|\bshort\)|\bshort/|\bshort rule|\bbearish\b|death cross|\bshorting\b|"
                    r"\bshort sell", re.I)


def spec_direction(r: Any) -> str:
    """Direction read from the original name, explanation and rule (\"Short-term\" is not a short trade)."""
    text = " ".join((r.name, r.explanation, r.rule_text, r.trailing_text))
    return "short" if _SHORT.search(text) else "long"


def semantic(rule: str, entry: str, params: dict[str, Any], frames: list[Any]) -> dict[str, Any]:
    """Evaluate the original rule independently and the DSL entry; compare bar by bar after warm-up."""
    try:
        sp.parse(rule)
    except sp.Unsupported as exc:
        return {"semantic": "NOT_MACHINE_READABLE", "reason": str(exc)}
    tot = {"bars": 0, "spec_true": 0, "dsl_true": 0, "both": 0, "mismatch": 0, "mismatch_examples": []}
    for k, f in enumerate(frames):
        try:
            a = np.array(sp.signal(rule, cols(f)), bool)
        except sp.Unsupported as exc:
            return {"semantic": "NOT_MACHINE_READABLE", "reason": str(exc)}
        try:
            b = dsl.signal(entry, fx.ctx(f), params)
        except (dsl.DSLError, dsl.MissingData) as exc:
            return {"semantic": "DSL_NOT_EVALUABLE", "reason": str(exc)}
        a, b = a[WARMUP:], b[WARMUP:]
        tot["bars"] += a.size
        tot["spec_true"] += int(a.sum())
        tot["dsl_true"] += int(b.sum())
        tot["both"] += int((a & b).sum())
        mis = np.nonzero(a != b)[0]
        tot["mismatch"] += int(mis.size)
        for i in mis[:3]:
            tot["mismatch_examples"].append({"fixture": k, "bar": int(i + WARMUP), "spec": bool(a[i]), "dsl": bool(b[i])})
    union = tot["spec_true"] + tot["dsl_true"] - tot["both"]
    tot["jaccard"] = tot["both"] / union if union else 1.0
    if tot["spec_true"] == 0 and tot["dsl_true"] == 0:
        tot["semantic"] = "NOT_EXERCISED"
    elif tot["mismatch"] == 0:
        tot["semantic"] = "IDENTICAL"
    else:
        tot["semantic"] = "DIFFERENT"
    return tot


def run(sec: Section, ev: dict[str, Any], work: Path) -> None:
    refs = {r.reference_id: r for r in load_catalogue(CATALOGUE).references}
    loaded = StrategyLibrary([LIBRARY], work / "catalogue-library-readonly").load(record=False)
    lib = [s for s in loaded.strategies.values() if s.reference_id]
    frames = [fx.ohlcv(1500, fx.SEED), fx.ohlcv(1500, fx.SEED + 1, drift=0.0008), fx.ohlcv(1500, fx.SEED + 2, vol=0.025)]
    rows: list[dict[str, Any]] = []
    ev["strategy_rows"] = rows

    def coverage(c: Check) -> None:
        have = {s.reference_id for s in lib}
        missing = sorted(set(refs) - have)
        orphan = sorted({s.id for s in lib if s.reference_id not in refs})
        c.evidence.update(references=len(refs), interpretations=len(lib), invalid=loaded.invalid,
                          missing_references=missing, orphan_interpretations=orphan)
        c.expect(not loaded.invalid, f"{len(loaded.invalid)} library entries fail validation")
        c.expect(not missing, f"{len(missing)} catalogue references have no interpretation: {missing[:10]}")
        c.expect(not orphan, f"interpretations point at unknown references: {orphan[:10]}")
        c.note(f"{len(refs)} original references, {len(lib)} interpretations in the shipped library")
    sec.run("catalogue_coverage", "Every original catalogue reference has at least one library interpretation and "
            "every interpretation points at a real reference", coverage)

    def audit(c: Check) -> None:
        counts: dict[str, int] = {}
        for s in sorted(lib, key=lambda x: (refs.get(x.reference_id).ordinal if x.reference_id in refs else 0, x.id)):
            r = refs.get(s.reference_id)
            if r is None:
                continue
            row = _audit_one(s, r, frames)
            rows.append(row)
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        c.evidence["counts"] = counts
        mism = [r for r in rows if r["verdict"] == "MISMATCH"]
        rev = [r for r in rows if r["verdict"] == "REVIEW"]
        for r in mism:
            c.fail(f"{r['strategy_id']} ({r['name']}): {r['reasons']}")
        for r in rev[:60]:
            c.warn(f"{r['strategy_id']} ({r['name']}): {r['reasons']}")
        dirs = [f"{r['strategy_id']} {r['name']} ({r['direction_lib']})" for r in rows if r.get("direction_note")]
        c.evidence["direction_judgement_calls"] = dirs
        if dirs:
            c.note(f"{len(dirs)} direction judgement call(s) where prose keywords and the interpretation differ; "
                   f"listed for human review in strategies.csv (direction_note): " + ", ".join(dirs[:15]))
        if len(rev) > 60:
            c.warn(f"... {len(rev) - 60} more REVIEW rows in strategies.csv")
        c.note("verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    def m56_direction(c: Check) -> None:
        bad = [{"reference_id": r.reference_id, "name": r.name, "m56_direction": r.direction,
                "text_direction": spec_direction(r)} for r in refs.values() if r.direction != spec_direction(r)]
        c.evidence["disagreements"] = bad
        if bad:
            c.warn(f"{len(bad)} reference(s) where the M5.6 catalogue parser's keyword direction differs from the "
                   f"text (it treats any ' short' substring, e.g. 'Short-term', as a short trade): "
                   + ", ".join(f"{b['reference_id']} {b['name']}" for b in bad[:12]))
        c.note("M5.7 uses the library's explicit direction field; the M5.6 field is metadata only")
    sec.run("reference_direction_metadata", "The catalogue parser's direction metadata matches the original text",
            m56_direction)

    sec.run("interpretation_audit", "Each JSON interpretation carries the indicators, parameters, thresholds and "
            "direction of its original pseudo-code; machine-readable rules reproduce bar for bar", audit)


def _audit_one(s: Any, r: Any, frames: list[Any]) -> dict[str, Any]:
    d = s.doc
    row: dict[str, Any] = {"reference_id": r.reference_id, "strategy_id": s.id,
                           "interpretation": d.get("interpretation", "A"), "name": r.name,
                           "original_rule": r.rule_text, "dsl_entry": d.get("entry", ""),
                           "dsl_exit": d.get("exit"), "direction_spec": r.direction, "direction_lib": s.direction,
                           "timeframes_spec": ",".join(r.supported_timeframes), "timeframes_lib": ",".join(s.timeframes),
                           "fidelity": d.get("fidelity", ""), "assumptions": "; ".join(d.get("assumptions") or []),
                           "definition_sha256": s.sha256}
    reasons: list[str] = []
    if s.blocked:
        st = s.blocked["status"]
        row.update(verdict="DEFERRED" if st == "DEFERRED" else "BLOCKED", block_status=st,
                   block_reason=s.blocked["reason"], semantic="", reasons=s.blocked["reason"])
        if not s.blocked.get("reason"):
            row.update(verdict="MISMATCH", reasons="blocked without a reason")
        return row
    want_dir = spec_direction(r)
    row["direction_note"] = "" if want_dir == s.direction == r.direction else (
        f"prose keywords suggest {want_dir}, M5.6 parser says {r.direction}, interpretation is {s.direction} "
        "(judgement call; not used for the verdict)")
    extra_tf = set(s.timeframes) - set(r.supported_timeframes)
    tok =sp.spec_tokens(r.rule_text)
    used = dsl.functions_used(s.entry_node) | (dsl.functions_used(s.exit_node) if s.exit_node is not None else set())
    missing_fam = [f for f in tok["families"] if not set(sp.SYNONYMS[f]) & used]
    pool = _dsl_numbers(s)
    missing_num = [v for v in tok["numbers"] if not sp.number_present(v, pool)]
    row.update(spec_families=",".join(tok["families"]), missing_families=",".join(missing_fam),
               spec_numbers=",".join(f"{v:g}" for v in tok["numbers"]),
               missing_numbers=",".join(f"{v:g}" for v in missing_num))
    sem: dict[str, Any] = {"semantic": "NOT_RUN"}
    if "1d" in s.timeframes and not s.needs.get("benchmark") and not s.needs.get("features"):
        sem = semantic(r.rule_text, d.get("entry", ""), s.defaults(), frames)
    row.update(semantic=sem.get("semantic"), semantic_reason=sem.get("reason", ""),
               compared_bars=sem.get("bars"), spec_signals=sem.get("spec_true"), dsl_signals=sem.get("dsl_true"),
               mismatched_bars=sem.get("mismatch"), signal_jaccard=sem.get("jaccard"),
               mismatch_examples=sem.get("mismatch_examples"))
    exact = (d.get("fidelity") or "EXACT") == "EXACT"
    if sem.get("semantic") == "IDENTICAL":
        verdict = "VERIFIED_SEMANTIC"
    elif sem.get("semantic") == "DIFFERENT":
        j = sem["jaccard"]
        msg = (f"independent evaluation of the original rule disagrees on {sem['mismatch']} of {sem['bars']} bars "
               f"(signals {sem['spec_true']} original vs {sem['dsl_true']} interpretation, Jaccard {j:.3f})")
        reasons.append(msg)
        verdict = "MISMATCH" if exact else "APPROXIMATION"
    else:
        if missing_fam:
            reasons.append(f"original uses {missing_fam} but the interpretation has no matching function")
        if missing_num:
            reasons.append(f"original constants {[f'{v:g}' for v in missing_num]} not found in the interpretation")
        if not exact:
            verdict = "APPROXIMATION"
        elif missing_fam or missing_num:
            verdict = "REVIEW"
        else:
            verdict = "VERIFIED_STRUCTURAL"
        if sem.get("semantic") == "NOT_EXERCISED":
            reasons.append("machine-readable, but neither form fired on the fixtures")
    if verdict == "APPROXIMATION" and row["assumptions"]:
        reasons.append("declared: " + row["assumptions"])
    if extra_tf:
        reasons.append(f"also enabled on {sorted(extra_tf)}, which the M5.6 parser did not list for the original "
                       f"(note only)")
    row.update(verdict=verdict, reasons="; ".join(reasons))
    return row
