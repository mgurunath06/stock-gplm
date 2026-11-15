"""Synthetic smoke test of ``gplm research-matrix``: every catalogue entry gets a row; resume skips lanes."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from stock_gplm.app import cli
from stock_gplm.app.m56_catalogue import parse_catalogue
from stock_gplm.app.settings import DEFAULT_CONFIG, REPO
from stock_gplm.m54.fixture import make_fixture

CATALOGUE = REPO / "config" / "strategies" / "reference-300-strategies.txt"


def test_research_matrix_covers_catalogue_and_resumes(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    make_fixture(raw, ("AAA",), start="2021-01-01", end="2026-03-31", minute_from="2026-03-01",
                 cutoff="2026-03-02", seed=9)
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cfg["data"].update(daily_root=str(raw / "daily" / "NSE"), minute_roots=[str(raw / "minute" / "NSE")],
                       real_raw_tree=str(raw))
    cfg["m56"].update(development_end="2025-06-30", tree_max_candidates=4)
    cp = tmp_path / "c.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    out = tmp_path / "out"
    args = ["research-matrix", "--stocks", "AAA,MISSING", "--timeframes", "1d", "--methods", "tree",
            "--device", "cpu", "--name", "t", "--out", str(out), "--config", str(cp),
            "--home", str(tmp_path / "home"), "--quiet"]
    assert cli.main(args) == 0
    n_refs = len(parse_catalogue(CATALOGUE).references)
    db = sqlite3.connect(out / "matrix.sqlite")
    cat = dict(db.execute("SELECT symbol, COUNT(*) FROM matrix WHERE source='CATALOGUE' GROUP BY 1").fetchall())
    assert cat == {"AAA-EQ": n_refs, "MISSING-EQ": n_refs}  # one row per registered strategy, blocked or tested
    statuses = {r[0] for r in db.execute("SELECT status FROM matrix WHERE symbol='AAA-EQ' AND source='CATALOGUE'")}
    assert {"TESTED", "BLOCKED_IMPLEMENTATION", "BLOCKED_MISSING_DATA"} <= statuses
    assert db.execute("SELECT COUNT(*) FROM matrix WHERE status!='TESTED' AND classification!='NOT_TESTED'"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT MAX(data_end) FROM matrix WHERE symbol='AAA-EQ'").fetchone()[0] <= "2025-06-30"
    assert db.execute("SELECT COUNT(*) FROM matrix WHERE source='ML'").fetchone()[0] > 0
    total = db.execute("SELECT COUNT(*) FROM matrix").fetchone()[0]
    db.close()
    assert cli.main(args) == 0
    db = sqlite3.connect(out / "matrix.sqlite")
    assert db.execute("SELECT COUNT(*) FROM matrix").fetchone()[0] == total
    db.close()
    assert "executed on at least one lane" in (out / "summary.md").read_text(encoding="utf-8")
    assert (out / "ranked_validated.csv").is_file()
    for mode, want in (("catalogue", {"CATALOGUE"}), ("ml", {"ML"})):
        o2 = tmp_path / mode
        a2 = [x if x != str(out) else str(o2) for x in args] + ["--mode", mode]
        assert cli.main(a2) == 0
        db = sqlite3.connect(o2 / "matrix.sqlite")
        assert {r[0] for r in db.execute("SELECT DISTINCT source FROM matrix")} == want
        db.close()
