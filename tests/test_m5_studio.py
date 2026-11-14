"""Strategy Studio historical workflow: eligibility is server-side and cannot be forged."""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from m5_fixtures import build_world, settings_dict, strategy_config

from stock_gplm.m5.studio_api import HistoricalResearchService
from stock_gplm.v1.registry import StrategyRegistry
from stock_gplm.v1.studio import _HTML, StudioHandler


def _serve(registry: StrategyRegistry, research):
    handler = type("TestStudio", (StudioHandler,), {"registry": registry, "research": research})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _call(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": f"127.0.0.1:{port}"}
    payload = None
    if body is not None:
        payload, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    result = response.status, json.loads(response.read())
    connection.close()
    return result


@pytest.fixture()
def studio(tmp_path: Path):
    raw, evidence = build_world(tmp_path, listing=False)  # strict evidence deliberately incomplete
    registry = StrategyRegistry(tmp_path / "studio.sqlite3")
    server = _serve(registry, HistoricalResearchService(raw, evidence, tmp_path / "reports", registry))
    yield server.server_address[1], registry, tmp_path
    server.shutdown()
    server.server_close()


def _backtest_body(version, mode: str, **extra) -> dict:
    return {"strategy_id": version.strategy_id, "version": version.version, "symbols": ["ALPHA-EQ", "BETA-EQ"],
            "start": "2024-01-01", "end": "2024-09-30", "mode": mode, "settings": settings_dict(), **extra}


def test_client_cannot_forge_or_skip_eligibility(studio) -> None:
    port, registry, _ = studio
    request = {"symbol": "ALPHA-EQ", "start": "2024-01-01", "end": "2024-09-30", "mode": "STRICT_RESEARCH"}
    assert _call(port, "POST", "/api/m5/eligibility", request | {"status": "ELIGIBLE"})[0] == 400
    status, decision = _call(port, "POST", "/api/m5/eligibility", request)
    assert status == 200 and decision["status"] == "BLOCKED" and "PIT_MEMBERSHIP_MISSING" in decision["missing_evidence"]
    version = registry.save(strategy_config())
    for forged in ({"data_quality_eligible": True}, {"eligibility": "ELIGIBLE"}, {"skip_validation": True}):
        assert _call(port, "POST", "/api/m5/backtests", _backtest_body(version, "STRICT_RESEARCH", **forged))[0] == 400
    status, blocked = _call(port, "POST", "/api/m5/backtests", _backtest_body(version, "STRICT_RESEARCH"))
    assert status == 422 and {d["status"] for d in blocked["decisions"]} == {"BLOCKED"}


def test_diagnostic_workflow_runs_lists_exports_and_refuses_ranking(studio) -> None:
    port, registry, tmp_path = studio
    first = registry.save(strategy_config())
    status, run = _call(port, "POST", "/api/m5/backtests", _backtest_body(first, "DIAGNOSTIC_ONLY"))
    assert status == 201 and run["classification"] == "DIAGNOSTIC_ONLY_NON_INVESTABLE" and not run["investable"]
    edited = strategy_config(risk={"stop_loss": {"type": "percent", "value": 3},
                                   "take_profit": {"type": "percent", "value": 4}})
    status, saved = _call(port, "POST", "/api/strategies", {"format": "json", "document": json.dumps(edited)})
    assert status == 201 and saved["version"] == first.version + 1
    second_version = registry.get(saved["strategy_id"], saved["version"])
    status, second = _call(port, "POST", "/api/m5/backtests", _backtest_body(second_version, "DIAGNOSTIC_ONLY"))
    assert status == 201
    stored = json.loads((tmp_path / "reports" / run["run_id"] / "run.json").read_text())
    assert (stored["strategy_version"], stored["strategy_config_hash"]) == (first.version, first.config_hash)
    assert {r["run_id"] for r in _call(port, "GET", "/api/m5/runs")[1]["runs"]} == {run["run_id"], second["run_id"]}
    status, exported = _call(port, "GET", f"/api/m5/runs/{run['run_id']}")
    assert status == 200 and "Non-investable diagnostic" in exported["markdown"]
    status, comparison = _call(port, "POST", "/api/m5/compare", {"run_ids": [run["run_id"], second["run_id"]]})
    assert status == 200 and comparison["ranking_by_total_return"] is None
    assert _call(port, "GET", "/api/m5/runs/..%2f..%2fetc")[0] == 404
    assert _call(port, "POST", "/api/m5/backtests", _backtest_body(first, "DIAGNOSTIC_ONLY"))[0] == 409


def test_unconfigured_studio_reports_historical_research_unavailable(tmp_path: Path) -> None:
    server = _serve(StrategyRegistry(tmp_path / "r.sqlite3"), None)
    try:
        assert _call(server.server_address[1], "GET", "/api/m5/datasets")[0] == 503
    finally:
        server.shutdown()
        server.server_close()
    assert "Historical research (M5)" in _HTML and "hEligibility" in _HTML
