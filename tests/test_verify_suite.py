"""The correctness-verification suite runs end to end, is sensitive to planted defects, and writes its evidence."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from stock_gplm.verify import reference as R
from stock_gplm.verify import specparse as sp
from stock_gplm.verify.runner import run


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, Path]:
    out = tmp_path_factory.mktemp("verify")
    return run(out, quiet=True), out


def _section(rep: dict, prefix: str) -> dict:
    return next(s for s in rep["sections"] if s["section"].startswith(prefix))


def test_writes_auditable_outputs(report: tuple[dict, Path]) -> None:
    rep, out = report
    for f in ("VERIFY_REPORT.md", "verify-report.json", "strategies.csv", "indicators.csv", "primitives.csv",
              "engine-trades.csv", "ml-evolve-log.jsonl", "audit-manifest.json"):
        assert (out / f).is_file(), f
    assert rep["verdict"] in ("PASS", "WARN", "FAIL", "PASS_WITH_TRACKED_GAPS")
    assert rep["correctness_verdict"] != "FAIL" and rep["exit_code"] in (0, 2)
    man = json.loads((out / "audit-manifest.json").read_text(encoding="utf-8"))
    assert man["real_candles_read"] is False and man["inputs"]
    rows = list(csv.DictReader((out / "strategies.csv").open(encoding="utf-8")))
    assert len(rows) >= 340 and all(r["verdict"] for r in rows)
    assert sum(1 for line in (out / "ml-evolve-log.jsonl").open(encoding="utf-8")) > 40


def test_self_test_catches_planted_defects(report: tuple[dict, Path]) -> None:
    sec = _section(report[0], "0.")
    assert sec["verdict"] == "PASS", [c for c in sec["checks"] if c["verdict"] != "PASS"]


def test_engine_and_leakage_checks_pass(report: tuple[dict, Path]) -> None:
    sec = _section(report[0], "3.")
    assert sec["verdict"] == "PASS", [c["name"] for c in sec["checks"] if c["verdict"] != "PASS"]


def test_indicator_arithmetic_has_no_failures(report: tuple[dict, Path]) -> None:
    sec = _section(report[0], "2.")
    assert all(c["verdict"] != "FAIL" for c in sec["checks"]), [c["name"] for c in sec["checks"] if c["verdict"] == "FAIL"]


def test_reference_bh_and_t_test() -> None:
    assert R.bh([0.01, 0.011, 0.04], 3) == pytest.approx([0.0165, 0.0165, 0.04])
    t, p = R.t_one_sided_p([0.01, 0.02, -0.005, 0.015, 0.0])
    from scipy import stats

    assert p == pytest.approx(stats.ttest_1samp([0.01, 0.02, -0.005, 0.015, 0.0], 0, alternative="greater").pvalue)


def test_spec_parser_subset() -> None:
    cols = {k: [float(i % 7 + 10 + i * 0.1) for i in range(60)] for k in ("o", "h", "l", "c", "v")}
    sig = sp.signal("Close[t] > MAX(High, 5)[t-1] AND Volume > 0.5 * SMA(Volume, 5)", cols)
    assert len(sig) == 60
    with pytest.raises(sp.Unsupported):
        sp.parse("Close > Box_High (where Box_High held unbreached for >= 5 bars)")
    assert sp.spec_tokens("RSI(14) < 30 AND Close(9:30) > 1.5x Avg")["numbers"] == [14, 30, 1.5]
