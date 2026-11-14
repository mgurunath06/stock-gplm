"""Regression tests for the V1 defects recorded in docs/m4-integration-inventory.md."""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from stock_gplm.v1.backtest import run_backtest
from stock_gplm.v1.execution import resting_exit
from stock_gplm.v1.registry import StrategyRegistry
from stock_gplm.v1.studio import StudioHandler

ROOT = Path(__file__).parents[1]


def _strategy(*, entry_close_above: float = 0.0, exit_close_above: float = 1e9,
              stop: float = 50.0, target: float = 50.0, max_hold: int = 100) -> dict:
    return {
        "schema_version": 1,
        "strategy": {"id": "m4-test", "name": "M4 test", "description": "Synthetic only.",
                     "enabled": True, "timeframe": "1d"},
        "universe": {"exchange": "NSE", "instrument_type": "EQ", "symbols": []},
        "indicators": [],
        "entry": {"operator": "GT", "left": {"field": "close"}, "right": {"constant": entry_close_above}},
        "exit": {"condition": {"operator": "GT", "left": {"field": "close"}, "right": {"constant": exit_close_above}},
                 "max_holding_sessions": max_hold},
        "risk": {"stop_loss": {"type": "percent", "value": stop},
                 "take_profit": {"type": "percent", "value": target}},
        "position_sizing": {"type": "fixed_fraction", "value": 1.0},
        "trading_costs": {"profile": "zero", "commission_bps": 0, "slippage_bps": 0},
    }


def _bars(rows: list[tuple[float, float, float, float]]) -> list[dict]:
    start = date(2024, 1, 1)
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "open": o, "high": h, "low": low, "close": c, "volume": 100.0}
        for i, (o, h, low, c) in enumerate(rows)
    ]


def test_exit_condition_fills_next_open_not_the_deciding_close() -> None:
    bars = _bars([(10, 10, 10, 10), (10, 11, 9.5, 11), (12, 13, 11.5, 13), (14, 14, 14, 14)])
    # Entry signal on every bar; exit signal when close > 12 (bar index 2).
    result = run_backtest(_strategy(exit_close_above=12), bars, data_quality_eligible=True)
    trade = result.trades[0]
    assert trade.entry_date == bars[1]["date"]
    assert trade.exit_reason == "EXIT_CONDITION"
    assert trade.exit_date == bars[3]["date"]
    assert trade.exit_price == pytest.approx(14.0)  # the next open, not the deciding close of 13


def test_gap_through_stop_fills_at_the_worse_open() -> None:
    bars = _bars([(10, 10, 10, 10), (10, 10.5, 9.8, 10), (7, 7.5, 6.5, 7)])
    result = run_backtest(_strategy(stop=10, target=50), bars, data_quality_eligible=True)
    trade = result.trades[0]
    assert trade.exit_reason == "STOP_LOSS_GAP"
    assert trade.exit_price == pytest.approx(7.0)
    assert trade.net_return == pytest.approx(-0.3)


def test_gap_up_target_is_capped_and_both_touched_assumes_stop() -> None:
    assert resting_exit({"open": 130, "high": 131, "low": 129}, 90.0, 120.0) == (120.0, "TAKE_PROFIT_GAP")
    assert resting_exit({"open": 100, "high": 125, "low": 85}, 90.0, 120.0) == (90.0, "STOP_LOSS")
    assert resting_exit({"open": 100, "high": 101, "low": 99}, 90.0, 120.0) is None


def test_max_holding_exit_executes_at_next_open() -> None:
    bars = _bars([(10, 10, 10, 10), (10, 10, 10, 10), (10, 10, 10, 10), (11, 11, 11, 11)])
    result = run_backtest(_strategy(max_hold=2), bars, data_quality_eligible=True)
    trade = result.trades[0]
    assert (trade.entry_date, trade.exit_date, trade.exit_reason) == (bars[1]["date"], bars[3]["date"], "MAX_HOLDING")


def test_registry_rows_are_append_only_and_backtests_idempotent(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    record = registry.save(_strategy())
    result = run_backtest(record.config, _bars([(10, 10, 10, 10)] * 4), strategy_version=1, data_quality_eligible=True)
    registry.record_backtest(result)
    registry.record_backtest(result)  # identical deterministic rerun
    with sqlite3.connect(tmp_path / "registry.sqlite3") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE strategy_versions SET canonical_json = '{}'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM backtest_results")
        assert connection.execute("SELECT COUNT(*) FROM backtest_results").fetchone()[0] == 1


def test_concurrent_saves_allocate_distinct_versions(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite3"
    StrategyRegistry(path)
    errors: list[BaseException] = []

    def save(threshold: float) -> None:
        try:
            StrategyRegistry(path).save(_strategy(entry_close_above=threshold))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(float(i),)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sorted(v.version for v in StrategyRegistry(path).list_versions()) == list(range(1, 9))


@pytest.fixture()
def studio(tmp_path: Path):
    handler = type("TestStudio", (StudioHandler,), {"registry": StrategyRegistry(tmp_path / "studio.sqlite3")})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _request(port: int, method: str, path: str, *, host: str | None = None, origin: str | None = None,
             body: dict | None = None) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if origin:
        headers["Origin"] = origin
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    status, data = response.status, json.loads(response.read() or b"{}")
    connection.close()
    return status, data


def test_studio_rejects_rebinding_hosts_and_cross_site_origins(studio: int) -> None:
    assert _request(studio, "GET", "/api/strategies")[0] == 200
    assert _request(studio, "GET", "/api/strategies", host="attacker.example:80")[0] == 403
    status, _ = _request(studio, "POST", "/api/validate", origin="http://attacker.example",
                         body={"format": "json", "document": "{}"})
    assert status == 403


def test_studio_backtest_result_is_labelled_unverified(studio: int) -> None:
    document = json.dumps(_strategy())
    status, saved = _request(studio, "POST", "/api/strategies", body={"format": "json", "document": document})
    assert status == 201
    candles = _bars([(10, 10, 10, 10)] * 4)
    body = {"strategy_id": saved["strategy_id"], "version": saved["version"], "candles": candles,
            "data_quality_eligible": True}
    status, result = _request(studio, "POST", "/api/backtests", body=body)
    assert status == 201
    assert result["data_classification"] == "CALLER_SUPPLIED_UNVERIFIED"
    # A deterministic rerun is idempotent rather than an unhandled database error.
    assert _request(studio, "POST", "/api/backtests", body=body)[0] == 201
