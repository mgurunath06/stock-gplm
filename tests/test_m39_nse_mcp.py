"""Synthetic-only tests for the M3.9 NSE MCP reference adapter."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from stock_gplm.m39.client import (
    HTTPResponse, MCPConnectionError, MCPProtocolError, MCPToolError, NSEMCPClient,
    validate_arguments_against_schema,
)
from stock_gplm.m39.comparison import (
    Observation, compare_observations, normalize_corporate_actions, normalize_nse_history,
    load_shoonya_segment,
)
from stock_gplm.m39.policy import (
    assert_reference_only_destination, validate_sample_request, validate_tool_arguments,
)


def response(value, *, status=200, headers=None):
    return HTTPResponse(status, headers or {"Content-Type": "application/json"},
                        json.dumps(value).encode() if value is not None else b"")


class ScriptedTransport:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = []

    async def post(self, url, headers, body, connect_timeout, read_timeout):
        self.calls.append((url, headers, json.loads(body), connect_timeout, read_timeout))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def initialization(session="test-session"):
    return response({"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
        "serverInfo": {"name": "synthetic-nse", "version": "1"},
    }}, headers={"Content-Type": "application/json", "Mcp-Session-Id": session})


def notification():
    return response(None, status=202)


def synthetic_tool(name="history"):
    return {"name": name, "description": "synthetic", "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}


def run(coro):
    return asyncio.run(coro)


def test_successful_initialization_and_schema_discovery():
    tool = synthetic_tool()
    transport = ScriptedTransport(initialization(), notification(),
                                  response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [tool]}}))

    async def exercise():
        client = NSEMCPClient("bhavcopy", transport=transport)
        assert await client.connect() == {"name": "synthetic-nse", "version": "1"}
        assert await client.list_tools() == [tool]
        await client.close()

    run(exercise())
    assert transport.calls[1][1]["Mcp-Session-Id"] == "test-session"
    assert transport.calls[2][2]["method"] == "tools/list"


def test_connection_failure_and_retry_exhaustion():
    transport = ScriptedTransport(MCPConnectionError("no route"), MCPConnectionError("still no route"))
    client = NSEMCPClient("market", retries=1, transport=transport)
    with pytest.raises(MCPConnectionError, match="2 attempts"):
        run(client.connect())
    assert len(transport.calls) == 2


def test_timeout_is_bounded_and_reported():
    class SlowTransport:
        async def post(self, *args):
            await asyncio.sleep(0.05)
            raise AssertionError("wait_for failed")

    client = NSEMCPClient("bhavcopy", connect_timeout=0.001, read_timeout=0.001, retries=0,
                          transport=SlowTransport())
    with pytest.raises(MCPConnectionError, match="1 attempts"):
        run(client.connect())


def test_invalid_tool_request_is_structured_error():
    transport = ScriptedTransport(
        initialization(), notification(),
        response({"jsonrpc": "2.0", "id": 2, "result": {"tools": [synthetic_tool("bad")]}}),
        response({"jsonrpc": "2.0", "id": 3,
                  "error": {"code": -32602, "message": "unknown tool"}}))

    async def exercise():
        client = NSEMCPClient("bhavcopy", transport=transport)
        await client.connect()
        await client.list_tools()
        with pytest.raises(MCPToolError, match="code -32602: unknown tool"):
            await client.call_tool("bad", {"symbol": "INFY"})
        assert client.diagnostics()["jsonrpc_error"]["code"] == -32602

    run(exercise())


@pytest.mark.parametrize("result", [
    {"tools": "not-a-list"},
    {"tools": [{"name": "missing-schema"}]},
])
def test_malformed_schema_discovery_is_rejected(result):
    transport = ScriptedTransport(initialization(), notification(),
                                  response({"jsonrpc": "2.0", "id": 2, "result": result}))

    async def exercise():
        client = NSEMCPClient("bhavcopy", transport=transport)
        await client.connect()
        with pytest.raises(MCPProtocolError):
            await client.list_tools()

    run(exercise())


def test_discovered_schema_is_enforced_before_network_call():
    schema = synthetic_tool()["inputSchema"]
    with pytest.raises(ValueError, match="required"):
        validate_arguments_against_schema({}, schema)
    with pytest.raises(ValueError, match="unsupported"):
        validate_arguments_against_schema({"symbol": "INFY", "extra": "x"}, schema)
    with pytest.raises(ValueError, match="must be string"):
        validate_arguments_against_schema({"symbol": 1}, schema)
    validate_arguments_against_schema({"symbol": "INFY"}, schema)


def row(day="2026-03-02", symbol="INFY", close="101", volume=1000):
    return {"symbol": symbol, "date": day, "open": "100", "high": "102", "low": "99",
            "close": close, "volume": volume}


def observation(day="2026-03-02", close="101", volume=1000, source="NSE"):
    return Observation("INFY", day, Decimal("100"), Decimal("102"), Decimal("99"), Decimal(close),
                       volume, source)


def test_empty_history_and_symbol_mismatch_are_rejected():
    with pytest.raises(ValueError, match="empty"):
        normalize_nse_history({"data": []}, requested_symbol="INFY", source="NSE")
    with pytest.raises(ValueError, match="symbol mismatch"):
        normalize_nse_history({"data": [row(symbol="TCS")]}, requested_symbol="INFY", source="NSE")


def test_corporate_actions_keep_unknown_adjustment_and_detect_mismatch():
    actions = normalize_corporate_actions({"actions": [{"symbol": "INFY", "exDate": "2026-03-02",
                                                         "type": "SPLIT", "description": "synthetic"}]},
                                          requested_symbol="INFY")
    assert actions[0]["adjustment_status"] == "UNKNOWN" and actions[0]["adjustment_factor"] is None
    with pytest.raises(ValueError, match="symbol mismatch"):
        normalize_corporate_actions({"actions": [{"symbol": "TCS", "exDate": "2026-03-02"}]},
                                    requested_symbol="INFY")
    assert normalize_corporate_actions({"actions": []}, requested_symbol="INFY") == []


def test_duplicates_tolerances_and_corporate_action_flag_are_explicit():
    nse = [observation(), observation()]
    shoonya = [observation(close="101.005", volume=1001, source="SHOONYA")]
    result = compare_observations(nse, shoonya, price_abs_tolerance=Decimal("0.01"), volume_tolerance=1,
                                  corporate_actions=[{"ex_date": "2026-03-02", "action_type": "SPLIT"}])
    assert result["duplicates"]["nse"] == ["2026-03-02"]
    assert result["counts"]["mismatched_dates"] == 0
    assert result["rows"][0]["corporate_action_flag"] == "SPLIT"
    assert result["rows"][0]["price_basis"] == {"nse": "UNADJUSTED", "shoonya": "UNADJUSTED"}
    assert result["provenance"]["nse_sources"] == ["NSE"]
    assert result["provenance"]["shoonya_sources"] == ["SHOONYA"]


def test_missing_dates_are_not_filled_or_explained():
    result = compare_observations([observation()], [observation(day="2026-03-03", source="SHOONYA")])
    assert result["counts"]["compared"] == 0
    assert result["missing_from_nse"] == ["2026-03-03"]
    assert result["missing_from_shoonya"] == ["2026-03-02"]


def test_training_ingestion_and_lookahead_are_blocked(tmp_path):
    for part in ("datasets", "features", "training", "backtests"):
        with pytest.raises(ValueError, match="cannot enter"):
            assert_reference_only_destination(tmp_path / part / "nse")
    with pytest.raises(PermissionError, match="disabled"):
        validate_sample_request("INFY", "2026-03-01", "2026-03-03", allow_sample_fetch=False)
    with pytest.raises(ValueError, match="locked holdout"):
        validate_sample_request("INFY", "2026-04-28", "2026-04-29", allow_sample_fetch=True)
    with pytest.raises(ValueError, match="exactly one month"):
        validate_tool_arguments({"symbol": "INFY", "months": 3, "endDate": "2026-03-03"},
                                symbol="INFY", start="2026-03-01", end="2026-03-03")
    # The filename gate runs before validate_daily can open any bytes.
    with pytest.raises(ValueError, match="locked-holdout"):
        load_shoonya_segment(tmp_path / "INFY-EQ" / "2026-01-01_2026-10-29.json.gz",
                             symbol="INFY", start="2026-03-01", end="2026-03-03")
    protected = tmp_path / "raw"
    protected.mkdir()
    alias = tmp_path / "safe-reports"
    alias.symlink_to(protected, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot enter"):
        assert_reference_only_destination(alias / "m39")


def test_training_and_backtest_modules_do_not_import_m39():
    source = Path(__file__).parents[1] / "src" / "stock_gplm"
    consumers = [source / name for name in ("m3", "m35", "m37", "m38")]
    for module in consumers:
        for path in module.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "stock_gplm.m39" not in text and "from .m39" not in text and "import m39" not in text


def test_m39_report_contract_is_rejected_by_research_and_training_loaders(tmp_path):
    from stock_gplm.m3.research import load_readiness_run
    from stock_gplm.m35.data import _manifest

    (tmp_path / "run-manifest.json").write_text(json.dumps({
        "contract": "stock-gplm.m39-nse-mcp-report.v1", "outputs": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="not an M3.1 readiness run"):
        load_readiness_run(tmp_path)
    with pytest.raises(ValueError, match="is not a stock-gplm.m3-readiness-run.v1 run"):
        _manifest(tmp_path, "stock-gplm.m3-readiness-run.v1", ())


def test_only_registered_endpoints_are_accepted():
    with pytest.raises(ValueError, match="registered"):
        NSEMCPClient("https://example.com/mcp")
