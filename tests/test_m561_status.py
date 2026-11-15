"""M5.6.1 regression tests: VALIDATED needs every gate; catalogue beyond 300 is explicit."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_gplm.app.m56_catalogue import CatalogueError, CataloguePolicy, load_catalogue, parse_catalogue
from stock_gplm.app.m56_status import GatePolicy, derive, derive_rows, leaderboard
from stock_gplm.app.settings import REPO

POLICY = GatePolicy(min_trades=20, fdr_q=0.05, max_drawdown=0.30, walk_forward_folds=4)
GOOD_FOLDS = [{"fold": i, "n": 8, "mean_excess": 0.004} for i in range(1, 5)]
GOOD_VAL = {"effective_n": 40, "q_value": 0.01, "mean_net": 0.005, "max_drawdown": -0.10, "folds": GOOD_FOLDS}
GOOD_TEST = {"effective_n": 25, "mean_net": 0.004, "max_drawdown": -0.08}


def _final(val: dict, test: dict) -> dict:
    return {"strategy_id": "s", "symbol": "AAA-EQ", "timeframe": "1d", "regime": "ALL", "data_fingerprint": "f",
            "evidence_stage": "TEST_ONCE", "classification": "VALIDATED",
            "metrics": {**test, "validation_evidence": val}, "lineage": {}}


def test_all_gates_passing_is_the_only_route_to_validated() -> None:
    assert derive(_final(GOOD_VAL, GOOD_TEST), POLICY)["classification"] == "VALIDATED"


@pytest.mark.parametrize("val,test,gate", [
    ({**GOOD_VAL, "effective_n": 19}, GOOD_TEST, "min_trades"),
    (GOOD_VAL, {**GOOD_TEST, "effective_n": 5}, "min_trades"),
    ({**GOOD_VAL, "q_value": 0.2}, GOOD_TEST, "multiple_testing"),
    ({**GOOD_VAL, "q_value": None}, GOOD_TEST, "multiple_testing"),
    ({**GOOD_VAL, "max_drawdown": -0.45}, GOOD_TEST, "drawdown"),
    (GOOD_VAL, {**GOOD_TEST, "max_drawdown": -0.31}, "drawdown"),
    ({**GOOD_VAL, "folds": GOOD_FOLDS[:1]}, GOOD_TEST, "fold_stability"),
])
def test_any_failed_gate_prevents_validated(val: dict, test: dict, gate: str) -> None:
    st = derive(_final(val, test), POLICY)
    assert st["classification"] != "VALIDATED" and st["eligibility"] == "NOT_ELIGIBLE"
    assert st["label_corrected"] and any(gate in r for r in st["reasons"])


def test_broad_screen_validated_label_is_corrected_in_leaderboard() -> None:
    # Shape of the real RELIANCE st-a9b9fff00bd2d143 row: SCREEN_VALIDATED, q_bh 1.0, 6/5 trades.
    row = {"result_id": "r1", "strategy_id": "st-a9b9fff00bd2d143", "symbol": "RELIANCE-EQ", "timeframe": "1d",
           "regime": "ALL", "evidence_stage": "BROAD_SCREEN", "classification": "VALIDATED",
           "data_fingerprint": "f", "metrics_json": json.dumps({
               "tier": "SCREEN_VALIDATED", "in_sample": {"q_bh": 1.0},
               "validation": {"trades": 6, "mean_net": 0.011}, "test": {"trades": 5, "mean_net": 0.005,
                                                                     "scored": True}}),
           "lineage_json": "{}"}
    st = derive_rows([row], POLICY)[0]["status"]
    assert st["classification"] == "DISCOVERED" and st["recorded_classification"] == "VALIDATED"
    assert st["discovery_status"] == "SCREEN_VALIDATED" and st["validation_status"] == "INSUFFICIENT_EVIDENCE"
    board = leaderboard([row], POLICY)
    assert board[0]["status"] == "NO VALIDATED STRATEGY" and board[0]["corrected_labels"] == 1


def test_legacy_final_row_joins_its_validation_row() -> None:
    val_row = {**_final(GOOD_VAL, GOOD_TEST), "evidence_stage": "VALIDATION", "classification": "EXPERIMENTAL",
               "metrics": GOOD_VAL}
    legacy = {**_final(GOOD_VAL, GOOD_TEST), "metrics": GOOD_TEST}
    assert derive_rows([val_row, legacy], POLICY)[1]["status"]["classification"] == "VALIDATED"
    assert derive_rows([legacy], POLICY)[0]["status"]["classification"] == "EXPERIMENTAL"


CATALOGUE = REPO / "config" / "strategies" / "reference-300-strategies.txt"


def test_catalogue_beyond_300_keeps_original_digests() -> None:
    parsed = parse_catalogue(CATALOGUE)
    rep = parsed.report()
    assert rep["status"] == "CLEAN" and rep["base_imported"] == 300 and rep["imported"] >= 300
    base = [r for r in parsed.references if r.category_number <= 6]
    # Pinned from the committed 300-entry text (HEAD 5759bf0): definitions stay byte-identical.
    assert base[0].digest == "7002972a1a145f7e998eeb7f00ee21cfab61acdfcb3c3aaa9a0f7fec067b56ed"
    ext = [r for r in parsed.references if r.category_number > 6]
    assert all(r.reference_id.startswith(f"REF-C{r.category_number}-") and r.ordinal > 300 for r in ext)


def test_malformed_and_duplicate_entries_are_reported_not_dropped(tmp_path: Path) -> None:
    text = CATALOGUE.read_text(encoding="utf-8") + (
        "\n### Category 99: Test\n\n341. **Ok:** text\n   - **Candidate logic:** `x > 1`\n   - **Mystery:** y\n"
        "341. **Dup:** again\n7. **Low number:** wrong\n342 broken line\n")
    p = tmp_path / "c.txt"
    p.write_text(text, encoding="utf-8")
    rep = parse_catalogue(p).report()
    assert rep["duplicate"] == 1 and rep["rejected"] == 1 and rep["unsupported_field"] == 1
    assert rep["status"] == "ISSUES"
    with pytest.raises(CatalogueError):
        load_catalogue(p)
    assert load_catalogue(p, CataloguePolicy(on_issue="quarantine")).references[-1].reference_id == "REF-C99-341"
