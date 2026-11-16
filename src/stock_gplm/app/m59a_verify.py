"""Read-only M5.9A/M5.9B verification of a GPLM database (owner-run).

``python -m stock_gplm.app.m59a_verify --db PATH`` prints JSON and exits 1 when a violation exists:

* ``stale_or_ungated_live``      v2 ``mode='LIVE'`` rows whose status is not LIVE, whose freshness is not
                                 FRESH, or that lack stop/target;
* ``actionable_without_contract`` actionable rows (any engine) missing stop or target;
* ``target_contract_mismatch``   rows whose stored target contract disagrees with horizon/target;
* ``m58_bars_held_mismatch``     M5.8 v4 reveals scored with bars held != horizon.

M5.9B (``m59b_checks``) adds, for rows claiming the M5.9A+ contract: ``stale_live``,
``live_without_canonical_decision``, ``live_without_approved_model``, ``live_with_revoked_model``,
``live_with_altered_model`` (stored artifact re-hashed), ``strategy_live_without_approval``,
``live_outside_universe``, ``live_outside_calendar``, ``actionable_missing_fields`` (full opportunity
contract) and ``unknown_status_rows``; plus the shared-status and contract-version distributions.

The database is opened with ``mode=ro`` and nothing is repaired. Legacy rows (pre-M5.9A) are counted
and classified (``LEGACY_UNVERIFIED_LIVE`` etc.) for information, never modified.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any

CHECKS = {
    "stale_or_ungated_live": (
        "SELECT COUNT(*) FROM predictions WHERE mode='LIVE' AND json_extract(payload_json,'$.engine')='gplm-rolling.v2' "
        "AND (COALESCE(json_extract(payload_json,'$.status'),'')!='LIVE' "
        "OR COALESCE(json_extract(payload_json,'$.freshness.status'),'')!='FRESH' OR stop IS NULL OR target IS NULL)"),
    "actionable_without_contract": (
        "SELECT COUNT(*) FROM predictions WHERE actionable=1 AND (stop IS NULL OR target IS NULL) "
        "AND (json_extract(payload_json,'$.engine')='gplm-rolling.v2' OR source_version='stock-gplm.m58-prediction.v4')"),
    "target_contract_mismatch": (
        "SELECT COUNT(*) FROM predictions WHERE json_extract(payload_json,'$.target_contract') IS NOT NULL AND "
        "(json_extract(payload_json,'$.target_contract.declared_horizon')!=horizon_bars OR "
        "json_extract(payload_json,'$.target_contract.derived_target')!=target_session)"),
    "m58_bars_held_mismatch": (
        "SELECT COUNT(*) FROM predictions p JOIN evaluations e ON e.prediction_id=p.prediction_id "
        "WHERE p.source_version='stock-gplm.m58-prediction.v4' AND COALESCE(e.exit_reason,'')!='NOT_COMPARABLE' "
        "AND e.bars_held!=p.horizon_bars"),
}
INFO = {
    "legacy_live_rows": "SELECT COUNT(*) FROM predictions WHERE mode='LIVE' AND json_extract(payload_json,'$.status') IS NULL",
    "legacy_m58_rows": ("SELECT COUNT(*) FROM predictions WHERE mode='HISTORICAL_FROZEN' AND "
                        "source_version!='stock-gplm.m58-prediction.v4'"),
    "v2_live_rows": ("SELECT COUNT(*) FROM predictions WHERE mode='LIVE' AND "
                     "json_extract(payload_json,'$.engine')='gplm-rolling.v2'"),
    "not_comparable_evaluations": "SELECT COUNT(*) FROM evaluations WHERE exit_reason='NOT_COMPARABLE'",
}


# ----------------------------------------------------------------------------- M5.9B governance checks
M59B_KEYS = ("stale_live", "live_without_canonical_decision", "live_without_approved_model",
             "live_with_revoked_model", "live_with_altered_model", "strategy_live_without_approval",
             "live_outside_universe", "live_outside_calendar", "actionable_missing_fields", "unknown_status_rows")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _file_sha(path: str) -> str | None:
    import hashlib

    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def m59b_checks(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, Any], dict[str, list[str]]]:
    """Governance checks over stored rows (read-only). Rows that claim the M5.9A+ contract (``payload.status``
    present) are judged; legacy rows are counted and classified, never judged against rules they predate."""
    from .contracts import shared_status, validate_actionable_row

    v = {k: 0 for k in M59B_KEYS}
    examples: dict[str, list[str]] = {k: [] for k in M59B_KEYS}
    gov = all(_has_table(conn, t) for t in ("model_artifacts", "model_revocations", "strategy_lifecycle",
                                            "universe_registrations", "universe_supplements", "calendar_registrations"))

    def hit(key: str, pid: str) -> None:
        v[key] += 1
        if len(examples[key]) < 5:
            examples[key].append(pid)

    artifacts: dict[str, dict[str, Any]] = {}
    altered: list[str] = []
    if gov:
        for mid, digest, path in conn.execute("SELECT model_id, sha256, stored_path FROM model_artifacts"):
            actual = _file_sha(path)
            artifacts[mid] = {"sha256": digest, "intact": actual == digest}
            if actual != digest:
                altered.append(mid)
    legacy_actionable_missing = 0
    cols = ("prediction_id", "mode", "symbol", "source_kind", "source_id", "source_version", "horizon_bars",
            "decision_label", "target_session", "actionable", "stop", "target", "direction", "prob_raw",
            "prob_calibrated", "created_utc", "payload_json")
    for raw in conn.execute(f"SELECT {','.join(cols)} FROM predictions WHERE mode='LIVE' OR actionable=1"):
        row = dict(zip(cols, raw))
        payload = json.loads(row["payload_json"] or "{}")
        pid, created = row["prediction_id"], row["created_utc"]
        modern = payload.get("status") is not None
        if int(row["actionable"] or 0) and validate_actionable_row(row, payload):
            if modern:
                hit("actionable_missing_fields", pid)
            else:
                legacy_actionable_missing += 1
        if row["mode"] != "LIVE" or not modern:
            continue
        if (payload.get("freshness") or {}).get("status") != "FRESH":
            hit("stale_live", pid)
        dec = payload.get("live_decision") or {}
        ev = dec.get("evidence") or {}
        if not (dec.get("eligible") is True and dec.get("status") == "LIVE"):
            hit("live_without_canonical_decision", pid)
        strategy_driven = dec.get("prediction_class") == "STRATEGY_TRIGGERED" or row["source_kind"] in ("STRATEGY", "RULE")
        mid, digest = ev.get("model_id"), ev.get("artifact_sha256")
        if row["source_kind"] == "MODEL" or mid:
            appr = conn.execute("SELECT approved_utc FROM model_approvals WHERE model_id=? AND artifact_sha256=?",
                                (mid, digest)).fetchone() if mid and digest else None
            if not gov or mid not in artifacts or appr is None or str(appr[0]) > str(created):
                hit("live_without_approved_model", pid)
            if gov and mid:
                rev = conn.execute("SELECT revoked_utc FROM model_revocations WHERE model_id=? AND artifact_sha256=?",
                                   (mid, digest)).fetchone()
                if rev is not None and str(rev[0]) <= str(created):
                    hit("live_with_revoked_model", pid)
                if mid in artifacts and (not artifacts[mid]["intact"] or artifacts[mid]["sha256"] != digest):
                    hit("live_with_altered_model", pid)
        if strategy_driven:
            stage = None
            if gov and row["source_kind"] == "STRATEGY":
                for st, ts in conn.execute("SELECT stage, ts_utc FROM strategy_lifecycle WHERE strategy_id=? AND "
                                           "version=? ORDER BY seq", (row["source_id"], row["source_version"])):
                    if str(ts) <= str(created):
                        stage = st
            if stage != "APPROVED":
                hit("strategy_live_without_approval", pid)
        uid = ev.get("universe_id")
        member = False
        if gov and uid:
            u = conn.execute("SELECT symbols_json FROM universe_registrations WHERE universe_id=?", (uid,)).fetchone()
            member = bool(u) and row["symbol"] in json.loads(u[0])
            if u and not member:
                active = False
                for act, ts in conn.execute("SELECT action, ts_utc FROM universe_supplements WHERE symbol=? ORDER BY seq",
                                            (row["symbol"],)):
                    if str(ts) <= str(created):
                        active = act == "ADD"
                member = active
        if not member:
            hit("live_outside_universe", pid)
        cid = ev.get("calendar_id")
        covered = False
        if gov and cid:
            c = conn.execute("SELECT coverage_start, coverage_end FROM calendar_registrations WHERE calendar_id=?",
                             (cid,)).fetchone()
            dates = [str(row["decision_label"])[:10], str(row["target_session"] or "")[:10]]
            covered = bool(c) and all(c[0] <= d <= c[1] for d in dates)
        if not covered:
            hit("live_outside_calendar", pid)
    status_dist: dict[str, int] = {}
    for mode, st, n in conn.execute("SELECT mode, json_extract(payload_json,'$.status'), COUNT(*) FROM predictions "
                                    "GROUP BY 1, 2"):
        s = shared_status(mode, {"status": st} if st is not None else {})
        status_dist[s] = status_dist.get(s, 0) + int(n)
    v["unknown_status_rows"] = status_dist.get("UNKNOWN_STATUS", 0)
    contracts = {str(k): int(n) for k, n in conn.execute(
        "SELECT COALESCE(json_extract(payload_json,'$.output_contract'), json_extract(payload_json,'$.engine'), "
        "source_version, 'NONE'), COUNT(*) FROM predictions GROUP BY 1")}
    info = {"governance_tables_present": gov, "status_distribution": status_dist,
            "legacy_unverified_live": status_dist.get("LEGACY_UNVERIFIED_LIVE", 0),
            "contract_version_distribution": contracts, "altered_model_artifacts": altered,
            "legacy_actionable_rows_missing_fields": legacy_actionable_missing}
    return v, info, {k: x for k, x in examples.items() if x}


def verify(db_path: Path) -> dict[str, Any]:
    uri = f"file:{urllib.parse.quote(Path(db_path).resolve().as_posix())}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        violations = {k: int(conn.execute(q).fetchone()[0]) for k, q in CHECKS.items()}
        info: dict[str, Any] = {k: int(conn.execute(q).fetchone()[0]) for k, q in INFO.items()}
        v2, info2, examples = m59b_checks(conn)
    finally:
        conn.close()
    violations.update(v2)
    info.update(info2)
    return {"database": str(db_path), "ok": not any(violations.values()), "violations": violations, "info": info,
            "examples": examples}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path)
    result = verify(ap.parse_args(argv).db)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
