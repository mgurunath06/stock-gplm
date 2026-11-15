"""One small end-to-end ``gplm research-matrix`` run on synthetic raw files (no real candles).

Used by the validation and ML sections as audit evidence of the real code path: catalogue load,
library, DSL, lane build, simulation, BH, gates, final segment, evolve + tree discovery, library save.
"""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from pathlib import Path
from typing import Any

DEV_END = "2025-06-30"
STRATEGIES = "C1-0*,C3-0*,C4-00*"


def run(work: Path) -> dict[str, Any]:
    from ..app import cli
    from ..app.settings import DEFAULT_CONFIG
    from ..m54.fixture import make_fixture

    work.mkdir(parents=True, exist_ok=True)
    raw = work / "raw"
    if not raw.exists():
        make_fixture(raw, ("AAA",), start="2021-01-01", end="2026-03-31", minute_from="2026-03-01",
                     cutoff="2026-03-02", seed=9)
    cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    cfg["data"].update(daily_root=str(raw / "daily" / "NSE"), minute_roots=[str(raw / "minute" / "NSE")],
                       real_raw_tree=str(raw))
    cfg["m56"].update(development_end=DEV_END, tree_max_candidates=4)
    cfg.setdefault("research", {}).pop("strategy_library_home", None)
    cp = work / "config.json"
    cp.write_text(json.dumps(cfg), encoding="utf-8")
    out, home = work / "research-out", work / "home"
    args = ["research-matrix", "--stocks", "AAA", "--timeframes", "1d", "--methods", "evolve,tree",
            "--budget", "quick", "--strategies", STRATEGIES, "--device", "cpu", "--name", "verify",
            "--out", str(out), "--config", str(cp), "--home", str(home), "--quiet", "--workers", "1"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code1 = cli.main(args)
        rows1 = _count(out)
        finals1 = _finals(out)
        code2 = cli.main(args)  # resume: must not re-read final segments or duplicate rows
    return {"args": args, "exit_codes": [code1, code2], "out": out, "home": home, "config": cp,
            "rows_first": rows1, "rows_second": _count(out), "finals_first": finals1, "finals_second": _finals(out),
            "log_tail": buf.getvalue()[-3000:], "dev_end": DEV_END}


def _count(out: Path) -> int:
    with contextlib.closing(sqlite3.connect(out / "matrix.sqlite")) as db:
        return int(db.execute("SELECT COUNT(*) FROM matrix").fetchone()[0])


def _finals(out: Path) -> list[tuple[Any, ...]]:
    with contextlib.closing(sqlite3.connect(out / "matrix.sqlite")) as db:
        return [tuple(r) for r in db.execute("SELECT strategy_id, symbol, timeframe, data_fingerprint FROM final_reads"
                                             " ORDER BY 1")]


def rows(out: Path) -> list[dict[str, Any]]:
    with contextlib.closing(sqlite3.connect(out / "matrix.sqlite")) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM matrix")]
