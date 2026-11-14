"""M5 eligibility, causality, execution and neutrality tests on synthetic fixtures only."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from m5_fixtures import (
    HOLDOUT,
    event_record,
    ROOT,
    build_world,
    calendar_registration,
    settings_dict,
    strategy_config,
    tree_hash,
    write_segment,
    write_symbol,
)
from m5_fixtures import waive_strict_authority  # noqa: F401 - fixture

# Synthetic fixtures: the strict authority gates are waived here and enforced in test_m511_authority.py.
pytestmark = pytest.mark.usefixtures("waive_strict_authority")

from stock_gplm.m39b import CorporateActionEvent, SourceEvidence
from stock_gplm.m5 import (
    BacktestSettings,
    DataEligibilityRequest,
    DecisionStatus,
    HistoricalDataAdapter,
    compare_runs,
    run_historical_backtest,
)
from stock_gplm.m5.evidence import load_registered_calendar
from stock_gplm.m5.run_store import make_replayer
from stock_gplm.m5.runner import ANALYTICAL_FILES, instrument_signals
from stock_gplm.v1.evaluator import evaluate_strategy
from stock_gplm.v1.portfolio import CostModel, InstrumentSignals, PortfolioSettings, ShareEvent, simulate_portfolio
from stock_gplm.v1.registry import StrategyRegistry
from stock_gplm.v1.schema import validate_strategy

STRICT, DIAG = "STRICT_RESEARCH", "DIAGNOSTIC_ONLY"


def assess(raw_root, evidence, symbol="ALPHA-EQ", mode=STRICT, start="2024-01-01", end="2024-09-30", **kw):
    return HistoricalDataAdapter(raw_root, evidence).assess(DataEligibilityRequest(symbol, start, end, mode, **kw))


def codes(decision) -> set[str]:
    return {f.code for f in decision.failures}


# ---------------------------------------------------------------- eligibility and provenance

def test_complete_evidence_is_strictly_eligible_and_withholds_holdout(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    decision, dataset = assess(raw, evidence)
    assert decision.status == DecisionStatus.ELIGIBLE and decision.investable
    assert dataset is not None and dataset.bars[-1].date < HOLDOUT
    assert all("holdout_rows_withheld" not in s for s in decision.segments)
    assert {e.component for e in decision.evidence if e.status == "VERIFIED"} >= {
        "publication_integrity", "calendar", "pit_membership", "alias_history", "corporate_actions", "identity"}


@pytest.mark.parametrize("mode", [STRICT, DIAG])
def test_tampered_payload_is_rejected_in_every_mode(tmp_path: Path, mode: str) -> None:
    raw, evidence = build_world(tmp_path)
    target = raw / "ALPHA-EQ" / "2024-01-01_2024-06-30.json.gz"
    target.write_bytes(target.read_bytes() + b"\0")
    decision, dataset = assess(raw, evidence, mode=mode)
    assert decision.status == DecisionStatus.REJECTED and dataset is None
    assert "SEGMENT_REJECTED" in codes(decision)


def test_missing_marker_rejected(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    (raw / "ALPHA-EQ" / "2024-01-01_2024-06-30.json.gz.done").unlink()
    decision, _ = assess(raw, evidence, mode=DIAG)
    assert decision.status == DecisionStatus.REJECTED and "UNPUBLISHED_SEGMENT" in codes(decision)


@pytest.mark.parametrize("override, code", [({"symbol": "OTHER-EQ"}, "METADATA_MISMATCH"),
                                            ({"source": "unknown"}, "METADATA_MISMATCH"),
                                            ({"quality_status": "QUARANTINED"}, "QUARANTINED_SOURCE")])
def test_invalid_or_quarantined_metadata_rejected(tmp_path: Path, override: dict, code: str) -> None:
    raw, evidence = build_world(tmp_path)
    rows = write_symbol(tmp_path / "scratch", "ALPHA-EQ")
    write_segment(raw, "ALPHA-EQ", "2024-01-01", "2024-06-30", [r for r in rows if r[0] <= "2024-06-30"],
                  meta_overrides=override)
    decision, _ = assess(raw, evidence, mode=DIAG)
    assert decision.status == DecisionStatus.REJECTED and code in codes(decision)


def test_symbol_token_mismatch_rejected_and_matching_token_verified(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    assert assess(raw, evidence, expected_token="1000")[0].status == DecisionStatus.ELIGIBLE
    decision, _ = assess(raw, evidence, mode=DIAG, expected_token="999")
    assert decision.status == DecisionStatus.REJECTED and "IDENTITY_MISMATCH" in codes(decision)


def test_missing_pit_membership_blocks_strict_and_is_recorded_in_diagnostic(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path, listing=False)
    strict, released = assess(raw, evidence)
    assert strict.status == DecisionStatus.BLOCKED and released is None
    diagnostic, dataset = assess(raw, evidence, mode=DIAG)
    assert diagnostic.status == DecisionStatus.DIAGNOSTIC_ALLOWED and dataset is not None
    record = diagnostic.to_dict()
    assert not record["investable"] and not record["ranking_eligible"] and not record["paper_trading_eligible"]
    assert "PIT_MEMBERSHIP_MISSING" in record["missing_evidence"]


def test_listing_contradicting_observations_is_integrity_failure(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    listing = evidence.listing
    late = replace(listing, records=tuple(replace(r, listing_date="2024-03-01") for r in listing.records))
    decision, _ = assess(raw, replace(evidence, listing=late), mode=DIAG)
    assert decision.status == DecisionStatus.REJECTED and "LISTING_CONTRADICTION" in codes(decision)


def test_unverified_alias_evidence_and_overlapping_alias_block(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path, aliases="PARTIAL")
    assert "ALIAS_EVIDENCE_MISSING" in codes(assess(raw, evidence)[0])
    raw2, evidence2 = build_world(tmp_path / "b", alias_records=[
        {"canonical_symbol": "ALPHA-EQ", "alias_symbol": "OLDALPHA-EQ", "valid_from": "2023-01-01",
         "valid_to": "2024-02-15", "isin": None}])
    decision = assess(raw2, evidence2)[0]
    assert decision.status == DecisionStatus.BLOCKED and "ALIAS_STITCHING_UNSUPPORTED" in codes(decision)


def test_calendar_missing_gap_and_contradiction(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path, calendar=False)
    assert "CALENDAR_EVIDENCE_MISSING" in codes(assess(raw, evidence)[0])
    raw2, evidence2 = build_world(tmp_path / "gap")
    write_symbol(raw2, "ALPHA-EQ", drop={"2024-02-07"})
    gap = assess(raw2, evidence2)[0]
    assert gap.status == DecisionStatus.BLOCKED and "MISSING_SESSIONS" in codes(gap)
    raw3, evidence3 = build_world(tmp_path / "closed")
    write_symbol(raw3, "ALPHA-EQ", extra_days=["2024-01-26"])  # a synthetic closure
    closed = assess(raw3, evidence3, mode=DIAG)[0]
    assert closed.status == DecisionStatus.REJECTED and "CALENDAR_CONTRADICTION" in codes(closed)


def test_calendar_registration_tamper_is_rejected(tmp_path: Path) -> None:
    document = calendar_registration()
    document["sessions"][10]["status"] = "MARKET_CLOSED"
    document["sessions"][10]["session_open"] = document["sessions"][10]["session_close"] = None
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="identifier"):
        load_registered_calendar(path)


@pytest.mark.parametrize("symbol, code", [("ALPHA-BE", "UNSUPPORTED_SERIES"), ("NIFTYBEES-EQ", "ETF_EXCLUDED"),
                                          ("ABSENT-EQ", "SYMBOL_NOT_FOUND")])
def test_unsupported_series_etf_and_unknown_symbol_rejected(tmp_path: Path, symbol: str, code: str) -> None:
    raw, evidence = build_world(tmp_path)
    decision = assess(raw, evidence, symbol=symbol, mode=DIAG)[0]
    assert decision.status == DecisionStatus.REJECTED and code in codes(decision)


def test_holdout_overlap_and_unverified_boundary_rejected(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    assert "HOLDOUT_OVERLAP" in codes(assess(raw, evidence, mode=DIAG, end=HOLDOUT)[0])
    assert "HOLDOUT_BOUNDARY_UNVERIFIED" in codes(assess(raw, replace(evidence, holdout=None), mode=DIAG)[0])


def test_contradictory_overlapping_segments_rejected(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    write_segment(raw, "ALPHA-EQ", "2024-03-01", "2024-03-31",
                  [("2024-03-04", "1.00", "2.00", "0.50", "1.50", 10)])
    decision = assess(raw, evidence, mode=DIAG)[0]
    assert decision.status == DecisionStatus.REJECTED and "CONTRADICTORY_OBSERVATION" in codes(decision)


def test_publication_gap_is_evidence_not_fabrication(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    for path in (raw / "ALPHA-EQ").glob("2024-07-01_2024-09-30*"):
        path.unlink()
    decision, dataset = assess(raw, evidence, mode=DIAG)
    assert "PUBLICATION_COVERAGE_GAP" in codes(decision)
    assert dataset is not None and dataset.bars[-1].date <= "2024-06-30"


def test_forged_decision_or_swapped_bars_fail_verification(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path, listing=False)
    decision, dataset = assess(raw, evidence, mode=DIAG)
    forged = replace(decision.request, mode=STRICT)
    with pytest.raises(ValueError):
        object.__setattr__(decision, "request", forged)
        decision.verify()
    _, good = assess(raw, evidence, mode=DIAG)
    object.__setattr__(good, "bars", good.bars[:-1] + (replace(good.bars[-1], close="1.00"),))
    with pytest.raises(ValueError, match="bars differ"):
        good.verify()


def test_adapter_and_backtest_leave_historical_files_unchanged(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    before = tree_hash(raw)
    datasets = [assess(raw, evidence, symbol=s)[1] for s in ("ALPHA-EQ", "BETA-EQ")]
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    run_historical_backtest(version, datasets, BacktestSettings.from_dict(settings_dict()), tmp_path / "out")
    assert tree_hash(raw) == before
    with pytest.raises(ValueError, match="outside raw"):
        run_historical_backtest(version, datasets, BacktestSettings.from_dict(settings_dict()), raw / "reports",
                                forbidden_roots=(raw,))


# ---------------------------------------------------------------- causality

def test_future_candles_never_change_earlier_signals(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    _, dataset = assess(raw, evidence)
    rows = [bar.evaluator_row() for bar in dataset.bars]
    for name in ("swing-breakout-v1.json", "mean-reversion-v1.json"):
        config = json.loads((ROOT / "config/v1/strategies" / name).read_text())
        full = evaluate_strategy(config, rows)
        for cut in (40, 90, 150):
            prefix = evaluate_strategy(config, rows[:cut])
            assert prefix.entry_signals == full.entry_signals[:cut]
            assert prefix.exit_signals == full.exit_signals[:cut]
            altered = rows[:cut] + [dict(r, close=r["close"] * 3, high=r["high"] * 3) for r in rows[cut:]]
            assert evaluate_strategy(config, altered).entry_signals[:cut] == full.entry_signals[:cut]


def test_warmup_produces_no_signal_before_lookback(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    _, dataset = assess(raw, evidence)
    result = evaluate_strategy(strategy_config(), [b.evaluator_row() for b in dataset.bars])
    assert not any(result.entry_signals[:12])  # the slow SMA needs 12 sessions plus one for a cross


def _event(ex_date: str, available: str, *, kind: str = "STOCK_SPLIT", new: int = 2, base: int = 1) -> CorporateActionEvent:
    source = SourceEvidence("synthetic-fixture", f"evt-{ex_date}", "APPROVED_FOR_DERIVATION", available,
                            "2024-10-02T00:00:00Z")
    return CorporateActionEvent(f"{kind.lower()}-{ex_date}", "ALPHA-EQ", kind, ex_date, source, "VALIDATED",
                                new_shares=new, base_shares=base)


def test_corporate_actions_apply_only_from_their_known_effective_date(tmp_path: Path) -> None:
    action = [{"action_type": "SPLIT", "effective_date": "2024-05-02", "adjustment_status": "UNADJUSTED",
               "details": {"ratio": "2:1"}}]
    raw, evidence = build_world(tmp_path / "none", ca_actions=action)
    assert "CORPORATE_ACTION_EVENT_UNRESOLVED" in codes(assess(raw, evidence)[0])
    raw, evidence = build_world(tmp_path / "known", ca_actions=action,
                                events=[event_record("2024-05-02", "2024-04-15T00:00:00Z")])
    decision, dataset = assess(raw, evidence)
    assert decision.status == DecisionStatus.ELIGIBLE and dataset.share_events[0]["price_factor"] == "1/2"
    assert decision.corporate_action_reconciliation[0]["outcome"] == "APPLIED"
    config = validate_strategy(strategy_config(entry={"operator": "GT", "left": {"field": "close"},
                                                      "right": {"constant": 100}}))
    raw_signals = evaluate_strategy(config, [b.evaluator_row() for b in dataset.bars])
    adjusted = instrument_signals(config, dataset)
    before = [i for i, b in enumerate(dataset.bars) if b.date < "2024-05-02"]
    # A level rule before the ex-date sees raw prices, never prices halved by the later split.
    assert [adjusted.entry_signals[i] for i in before] == [raw_signals.entry_signals[i] for i in before]
    assert any(raw_signals.entry_signals[i] for i in before)
    raw, evidence = build_world(tmp_path / "late", ca_actions=action,
                                events=[event_record("2024-05-02", "2024-05-20T00:00:00Z")])
    _, delayed = assess(raw, evidence)
    unknown = [i for i, b in enumerate(delayed.bars) if "2024-05-02" <= b.date < "2024-05-20"]
    assert [instrument_signals(config, delayed).entry_signals[i] for i in unknown] == [
        raw_signals.entry_signals[i] for i in unknown]


# ---------------------------------------------------------------- execution simulation

def _bars(rows):
    return tuple({"date": d, "open": o, "high": h, "low": low, "close": c, "volume": 100.0} for d, o, h, low, c in rows)


def _simulate(rows, entries, exits=None, *, config=None, settings=None, costs=None, events=(), extra=()):
    instrument = InstrumentSignals("ALPHA-EQ", _bars(rows), tuple(entries), tuple(exits or [False] * len(rows)),
                                   tuple(events))
    instruments = [instrument, *extra]
    sessions = sorted({b["date"] for i in instruments for b in i.bars})
    settings = settings or PortfolioSettings(10_000.0, 5, 1.0, 1.0, sessions[0], sessions[-1])
    return simulate_portfolio(validate_strategy(config or strategy_config(
        position_sizing={"type": "fixed_fraction", "value": 0.5})).config, instruments, sessions, settings,
        costs or CostModel(0, 0, 0, 0))


DAYS = [f"2024-01-{d:02d}" for d in range(1, 31)]


def test_entry_next_open_costs_and_slippage_are_exact() -> None:
    rows = [(DAYS[i], 100.0, 101.0, 99.0, 100.0) for i in range(5)]
    result = _simulate(rows, [True, False, False, False, False],
                       costs=CostModel(commission_bps=10, slippage_bps=20, buy_statutory_bps=5, sell_statutory_bps=7))
    order = result.orders[0]
    assert (order["date"], order["status"]) == (DAYS[1], "FILLED")
    assert order["fill_price"] == pytest.approx(100.2)
    quantity = 5_000 // (100.2 * 1.0015)
    assert order["quantity"] == quantity
    assert result.equity_curve[1]["cash"] == pytest.approx(10_000 - quantity * 100.2 * 1.0015)
    assert result.open_positions[0]["status"] == "OPEN_AT_RANGE_END_NOT_A_TRADE" and not result.trades


def test_gap_stop_target_and_ambiguity_in_portfolio() -> None:
    gap = _simulate([(DAYS[0], 100, 100, 100, 100), (DAYS[1], 100, 100, 100, 100), (DAYS[2], 90, 91, 89, 90)],
                    [True, False, False])
    assert gap.trades[0]["exit_reason"] == "STOP_LOSS_GAP" and gap.trades[0]["exit_fill_price"] == 90
    both = _simulate([(DAYS[0], 100, 100, 100, 100), (DAYS[1], 100, 100, 100, 100), (DAYS[2], 100, 120, 80, 100)],
                     [True, False, False])
    assert both.trades[0]["exit_reason"] == "STOP_LOSS" and both.trades[0]["exit_fill_price"] == pytest.approx(94)
    target = _simulate([(DAYS[0], 100, 100, 100, 100), (DAYS[1], 100, 100, 100, 100), (DAYS[2], 130, 131, 129, 130)],
                       [True, False, False])
    assert target.trades[0]["exit_reason"] == "TAKE_PROFIT_GAP" and target.trades[0]["exit_fill_price"] == pytest.approx(109)


def test_exit_signal_fills_at_next_open() -> None:
    rows = [(DAYS[i], 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i) for i in range(5)]
    result = _simulate(rows, [True, False, False, False, False], [False, False, True, False, False])
    trade = result.trades[0]
    assert (trade["exit_decision_date"], trade["exit_date"], trade["exit_fill_price"]) == (DAYS[2], DAYS[3], 103.0)


def test_cash_exposure_and_duplicate_position_limits() -> None:
    rows = [(DAYS[i], 100.0, 101.0, 99.0, 100.0) for i in range(6)]
    others = tuple(InstrumentSignals(s, _bars(rows), (True,) * 6, (False,) * 6) for s in ("BETA-EQ", "GAMMA-EQ"))
    result = _simulate(rows, [True] * 6, extra=others, config=strategy_config(
        position_sizing={"type": "fixed_fraction", "value": 0.8}))
    filled = {o["symbol"]: o["quantity"] for o in result.orders if o["status"] == "FILLED"}
    # ALPHA takes 80% of equity; BETA is trimmed to the remaining exposure room; GAMMA gets none.
    assert filled == {"ALPHA-EQ": 80.0, "BETA-EQ": 20.0}
    assert any(o["symbol"] == "GAMMA-EQ" and o["reason"] == "BUDGET_OR_EXPOSURE_BELOW_ONE_SHARE" for o in result.orders)
    assert all(p["cash"] >= 0 and p["gross_exposure"] <= 1.0 + 1e-9 for p in result.equity_curve)
    for symbol in ("ALPHA-EQ", "BETA-EQ"):  # repeated entry signals never open a duplicate position
        assert sum(1 for o in result.orders if o["symbol"] == symbol and o["side"] == "BUY") == 1


def test_missing_session_cancels_entry_without_fabricating_prices() -> None:
    rows = [(DAYS[0], 100, 100, 100, 100), (DAYS[2], 100, 100, 100, 100)]
    filler = InstrumentSignals("BETA-EQ", _bars([(DAYS[i], 50, 50, 50, 50) for i in range(3)]), (False,) * 3, (False,) * 3)
    result = _simulate(rows, [True, False], extra=(filler,))
    assert result.orders[0]["reason"] == "ENTRY_SESSION_MISSING" and not result.trades


def test_split_while_holding_rescales_quantity_not_value() -> None:
    rows = [(DAYS[0], 100, 100, 100, 100), (DAYS[1], 100, 100, 100, 100), (DAYS[2], 50, 50.5, 49.5, 50)]
    result = _simulate(rows, [True, False, False], events=[ShareEvent(DAYS[2], 0.5, "split")])
    position = result.open_positions[0]
    assert position["quantity"] == 100.0 and position["unrealized_pnl"] == pytest.approx(0.0)


# ---------------------------------------------------------------- runs, neutrality, determinism

def _run(tmp_path: Path, world: tuple, config: dict, mode: str = STRICT, out: str = "out") -> dict:
    raw, evidence = world
    datasets = [assess(raw, evidence, symbol=s, mode=mode)[1] for s in ("ALPHA-EQ", "BETA-EQ")]
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    validated = validate_strategy(config)
    version = registry.find(validated.strategy_id, validated.config_hash) or registry.save(validated)
    return run_historical_backtest(version, datasets, BacktestSettings.from_dict(settings_dict()), tmp_path / out)


def test_both_sample_strategies_share_one_generic_engine(tmp_path: Path) -> None:
    world = build_world(tmp_path)
    runs = [_run(tmp_path, world, json.loads((ROOT / "config/v1/strategies" / n).read_text()))
            for n in ("swing-breakout-v1.json", "mean-reversion-v1.json")]
    assert {r["simulator_version"] for r in runs} == {"stock-gplm.v1-portfolio-simulator.v1"}
    assert {r["execution"]["policy"] for r in runs} == {"completed_close_signal_next_session_open_v2"}
    assert runs[0]["strategy_config_hash"] != runs[1]["strategy_config_hash"]
    # Ranking needs replay verification against the adapter and registry (M5.1).
    unverified = compare_runs([tmp_path / "out" / r["run_id"] for r in runs])
    assert unverified["ranking_by_total_return"] is None
    raw, evidence = world
    comparison = compare_runs([tmp_path / "out" / r["run_id"] for r in runs], replay=make_replayer(
        HistoricalDataAdapter(raw, evidence), StrategyRegistry(tmp_path / "registry.sqlite3")))
    assert comparison["comparable"] and comparison["ranking_by_total_return"] is not None


def test_strategy_name_has_no_execution_semantics_and_config_changes_behaviour(tmp_path: Path) -> None:
    world = build_world(tmp_path)
    base = _run(tmp_path, world, strategy_config())
    renamed = strategy_config(strategy={"id": "swing-breakout-v1", "name": "Swing Breakout",
                                        "description": "Renamed only.", "enabled": True, "timeframe": "1d"})
    other = _run(tmp_path, world, renamed)
    trades = lambda r: json.loads((tmp_path / "out" / r["run_id"] / "trades.json").read_text())  # noqa: E731
    assert trades(base) == trades(other)
    changed = _run(tmp_path, world, strategy_config(risk={"stop_loss": {"type": "percent", "value": 1},
                                                   "take_profit": {"type": "percent", "value": 2}}))
    assert trades(changed) != trades(base)


def test_diagnostic_runs_cannot_be_ranked_or_promoted(tmp_path: Path) -> None:
    world = build_world(tmp_path, listing=False)
    run = _run(tmp_path, world, strategy_config(), mode=DIAG)
    assert run["classification"] == "DIAGNOSTIC_ONLY_NON_INVESTABLE"
    assert not any(run[k] for k in ("investable", "ranking_eligible", "promotion_eligible",
                                    "paper_trading_eligible", "ml_training_eligible"))
    second = _run(tmp_path, world, strategy_config(position_sizing={"type": "fixed_fraction", "value": 0.2}), mode=DIAG)
    comparison = compare_runs([tmp_path / "out" / r["run_id"] for r in (run, second)])
    assert comparison["ranking_by_total_return"] is None
    assert "only STRICT_RESEARCH runs may be ranked" in comparison["reasons"]
    assert "Non-investable diagnostic" in (tmp_path / "out" / run["run_id"] / "report.md").read_text()


def test_strict_run_refuses_diagnostic_datasets(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path, listing=False)
    _, dataset = assess(raw, evidence, mode=DIAG)
    object.__setattr__(dataset.decision.request, "mode", STRICT)
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    with pytest.raises(ValueError):
        run_historical_backtest(version, [dataset], BacktestSettings.from_dict(settings_dict()), tmp_path / "o")


def test_reruns_are_deterministic_and_never_overwrite(tmp_path: Path) -> None:
    world = build_world(tmp_path)
    first = _run(tmp_path, world, strategy_config(), out="a")
    second = _run(tmp_path, world, strategy_config(), out="b")
    assert first["run_id"] == second["run_id"]
    for name in ANALYTICAL_FILES:
        assert (tmp_path / "a" / first["run_id"] / name).read_bytes() == (tmp_path / "b" / first["run_id"] / name).read_bytes()
    manifest = json.loads((tmp_path / "a" / first["run_id"] / "run-manifest.json").read_text())
    for name, digest in manifest["file_sha256"].items():
        assert hashlib.sha256((tmp_path / "a" / first["run_id"] / name).read_bytes()).hexdigest() == digest
    with pytest.raises(FileExistsError):
        _run(tmp_path, world, strategy_config(), out="a")


def test_run_provenance_points_to_exact_immutable_version(tmp_path: Path) -> None:
    run = _run(tmp_path, build_world(tmp_path), strategy_config())
    stored = StrategyRegistry(tmp_path / "registry.sqlite3").get(run["strategy_id"], run["strategy_version"])
    assert stored.config_hash == run["strategy_config_hash"] == validate_strategy(run["strategy_config_snapshot"]).config_hash
    assert run["holdout_cutoff"] == HOLDOUT and "locked_holdout_accessed" not in run
    access = run["holdout_access"]
    assert access["partition_policy"] == "DECLARED_WINDOW_STRICTLY_BEFORE_CUTOFF_VERIFIED_BEFORE_OPEN"
    assert access["opened_segment_windows"] == [["2024-01-01", "2024-06-30"], ["2024-07-01", "2024-09-30"]]
    assert "protected_rows_decoded_during_source_validation" not in access
    assert access["protected_rows_released_to_execution"] == access["protected_rows_scored_or_trained"] == 0
    assert all(d["decision"]["status"] == "ELIGIBLE" for d in run["datasets"])


def test_m5_has_no_network_broker_credential_or_scheduler_dependency() -> None:
    probe = ("import sys, stock_gplm.m5, stock_gplm.m5.cli, stock_gplm.v1.studio;"
             "print(sorted(m for m in sys.modules if m.split('.')[0] in "
             "{'requests','urllib3','selenium','paramiko'} or m.startswith(("
             "'stock_gplm.m24','stock_gplm.m25','stock_gplm.m39.','stock_gplm.llm'))))")
    output = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                            env={"PYTHONPATH": str(ROOT / "src")}).stdout.strip()
    assert output == "[]"
    source = "\n".join(p.read_text() for p in (ROOT / "src/stock_gplm/m5").glob("*.py"))
    for forbidden in ("urlopen", "os.environ", "getenv", "systemctl", "crontab", "place_order", "eval(", "exec("):
        assert forbidden not in source


def test_unverified_provider_basis_blocks_strict_and_never_applies_adjustments(tmp_path: Path) -> None:
    action = [{"action_type": "BONUS", "effective_date": "2024-05-02", "adjustment_status": "UNKNOWN",
               "details": {"ratio": "1:1"}}]
    raw, evidence = build_world(tmp_path, ca_actions=action, provider_basis=None)
    event = _event("2024-05-02", "2024-04-15T00:00:00Z", kind="BONUS", new=1, base=1)
    adapter = HistoricalDataAdapter(raw, evidence)
    strict, _ = adapter.assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT), [event])
    assert strict.status == DecisionStatus.BLOCKED and "PROVIDER_PRICE_BASIS_UNVERIFIED" in codes(strict)
    _, dataset = adapter.assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", DIAG), [event])
    # A possibly back-adjusted provider series is never adjusted again by M3.9-B.
    assert dataset.basis == "PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED" and dataset.share_events == ()
    assert dataset.adjustment_events == ()


def test_equity_reconciles_with_realized_and_unrealized_pnl(tmp_path: Path) -> None:
    run = _run(tmp_path, build_world(tmp_path), strategy_config(position_sizing={"type": "fixed_fraction", "value": 0.4}))
    metrics = run["performance"]["metrics"]
    assert metrics["ending_equity"] - 1_000_000.0 == pytest.approx(
        metrics["net_pnl_realized"] + metrics["unrealized_pnl"], abs=1e-6)
