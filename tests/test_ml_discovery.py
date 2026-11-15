"""Synthetic smoke test of ``gplm ml-discover``: real fits, persisted outputs, resume, holdout window."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from stock_gplm.app import cli
from stock_gplm.app.settings import DEFAULT_CONFIG
from stock_gplm.m54.fixture import make_fixture


def test_ml_discover_runs_persists_and_resumes(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_fixture(raw, ("AAA",), start="2021-01-01", end="2026-03-31", minute_from="2026-03-01",
                 cutoff="2026-03-02", seed=5)
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cfg["data"].update(daily_root=str(raw / "daily" / "NSE"), minute_roots=[str(raw / "minute" / "NSE")],
                       real_raw_tree=str(raw))
    cfg["m56"].update(development_end="2025-06-30", tree_max_candidates=6, fdr_q=1.0)
    cp = tmp_path / "c.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    out = tmp_path / "out"
    args = ["ml-discover", "--stocks", "AAA", "--timeframes", "1d,15m", "--methods", "tree,screen",
            "--device", "cpu", "--config", str(cp), "--home", str(tmp_path / "home"), "--out", str(out), "--quiet"]
    assert cli.main(args) == 0
    db = sqlite3.connect(out / "results.sqlite")
    lanes = dict(db.execute("SELECT timeframe || '/' || method, status FROM lanes").fetchall())
    assert lanes["1d/tree"] == "COMPLETED"  # sklearn models were fitted on the lane
    assert lanes["15m/tree"] == "BLOCKED_MISSING_DATA"  # all minute rows lie after the development end
    rows = list(csv.DictReader((out / "results.csv").open(encoding="utf-8")))
    assert rows and all(r["data_end"] <= "2025-06-30" for r in rows)
    assert all(r["classification"] != "VALIDATED" for r in rows if r["method"] == "screen")
    assert all((out / r["definition_file"]).is_file() and (out / r["editable_file"]).is_file() for r in rows)
    n = db.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    db.close()
    assert (out / "summary.md").is_file()
    assert cli.main(args) == 0  # resume: every lane is reused, nothing duplicated
    db = sqlite3.connect(out / "results.sqlite")
    assert db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == n
    db.close()
