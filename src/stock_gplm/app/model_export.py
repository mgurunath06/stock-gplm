"""M5.10 — freeze rolling champion fits into immutable candidate artifacts (``gplm model export``).

For every lane whose rolling champion is a fitted model (never the base-rate baseline), the stored
model file is registered through :func:`governance.register_model` as a ``CANDIDATE`` artifact scoped
to that one symbol. Registration is idempotent (the artifact id is derived from the file hash) and
never approves anything: approval stays a separate owner action with evidence.

``approved_only`` restricts the export to rolling fits that already carry an M5.9A owner approval
(``model_approvals``) for the exact file hash.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

from . import governance as G

EXPORT_CONTRACT = "stock-gplm.model-export.m510.v1"


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def export_champions(app: Any, actor: str, *, symbols: Sequence[str] | None = None, timeframe: str = "1d",
                     horizons: Sequence[int] | None = None, approved_only: bool = False) -> dict[str, Any]:
    db = app.db
    rows = db.q("SELECT c.lane_key, c.horizon, c.model_kind, c.model_id, c.reason, m.path, m.sha256, "
                "m.feature_config_sha, m.train_end_label FROM champions c LEFT JOIN models m ON m.model_id=c.model_id "
                "ORDER BY c.lane_key, c.horizon")
    want = {s.upper() for s in symbols} if symbols else None
    out: list[dict[str, Any]] = []
    for r in rows:
        sym, tf = str(r["lane_key"]).split("|")[0], str(r["lane_key"]).split("|")[-1]
        base = {"lane_key": r["lane_key"], "horizon": r["horizon"], "model_kind": r["model_kind"],
                "source_model_id": r["model_id"]}
        if tf != timeframe or (want is not None and sym not in want) or \
                (horizons and int(r["horizon"]) not in {int(h) for h in horizons}):
            continue
        if not r["model_id"] or r["model_kind"] not in G.MODEL_FAMILIES:
            out.append({**base, "status": "SKIPPED", "reason": "champion is not a fitted fixed-model family"})
            continue
        path = Path(r["path"] or "")
        digest = _sha(path)
        if digest is None:
            out.append({**base, "status": "SKIPPED", "reason": f"model file missing: {path}"})
            continue
        if r["sha256"] and r["sha256"] != digest:
            out.append({**base, "status": "SKIPPED", "reason": "model file hash differs from the stored row"})
            continue
        if approved_only and db.one("SELECT 1 FROM model_approvals WHERE model_id=? AND artifact_sha256=?",
                                    (r["model_id"], digest)) is None:
            out.append({**base, "status": "SKIPPED", "reason": "no M5.9A owner approval for this exact fit"})
            continue
        try:
            res = G.register_model(db, path, app.s.home / "model-artifacts", timeframe=tf, horizon=int(r["horizon"]),
                                   feature_schema=r["feature_config_sha"] or "", training_cutoff=r["train_end_label"] or "",
                                   cost_policy=G.cost_policy_version(app.s.section("costs")), actor=actor,
                                   scope={"symbols": [sym]},
                                   evidence=[f"rolling-champion:{r['lane_key']}|h{r['horizon']}:{r['reason']}",
                                             f"source-model:{r['model_id']}"])
        except G.GovernanceError as exc:
            out.append({**base, "status": "REFUSED", "reason": str(exc)})
            continue
        out.append({**base, "status": "REGISTERED" if res["created"] else "ALREADY_REGISTERED",
                    "artifact_model_id": res["model_id"], "state": res.get("state")})
    counts: dict[str, int] = {}
    for o in out:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    db.event("MODEL_EXPORT", {"contract": EXPORT_CONTRACT, "counts": counts, "approved_only": approved_only,
                              "timeframe": timeframe}, subject="model-export")
    return {"contract": EXPORT_CONTRACT, "counts": counts, "lanes": out,
            "next_step": "inspect each candidate with `gplm model inspect --model-id ID`; approve only with "
                         "out-of-sample and paper-trading evidence"}


__all__ = ["EXPORT_CONTRACT", "export_champions"]
