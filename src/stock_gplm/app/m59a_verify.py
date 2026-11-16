"""Read-only M5.9A verification of a GPLM database (owner-run).

``python -m stock_gplm.app.m59a_verify --db PATH`` prints JSON and exits 1 when a violation exists:

* ``stale_or_ungated_live``      v2 ``mode='LIVE'`` rows whose status is not LIVE, whose freshness is not
                                 FRESH, or that lack stop/target;
* ``actionable_without_contract`` actionable rows (any engine) missing stop or target;
* ``target_contract_mismatch``   rows whose stored target contract disagrees with horizon/target;
* ``m58_bars_held_mismatch``     M5.8 v4 reveals scored with bars held != horizon.

Legacy rows (pre-M5.9A) are counted for information, never modified.
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


def verify(db_path: Path) -> dict[str, Any]:
    uri = f"file:{urllib.parse.quote(Path(db_path).resolve().as_posix())}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        violations = {k: int(conn.execute(q).fetchone()[0]) for k, q in CHECKS.items()}
        info = {k: int(conn.execute(q).fetchone()[0]) for k, q in INFO.items()}
    finally:
        conn.close()
    return {"database": str(db_path), "ok": not any(violations.values()), "violations": violations, "info": info}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path)
    result = verify(ap.parse_args(argv).db)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
