"""Discover official NSE MCP tools or run one explicitly permitted reference sample."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from .client import ENDPOINTS, MCPError, NSEMCPClient
from .comparison import (
    compare_observations, load_shoonya_segment, normalize_corporate_actions, normalize_nse_history, tool_payload,
)
from .policy import validate_sample_request, validate_tool_arguments
from .report import write_report

DEFAULT_OUTPUT = Path("reports/m39_nse_mcp")


def _endpoint(client: NSEMCPClient, tools: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "bhavcopy", "url": ENDPOINTS["bhavcopy"], "initialization": "PASS",
            "protocol_version": client.protocol_version, "server_name": client.server_info["name"],
            "tools": tools}


def _payload_error_classification(message: str) -> str:
    lowered = message.lower()
    if any(term in lowered for term in ("unauthorized", "forbidden", "permission", "licen")):
        return "ACCESS_OR_PERMISSION_ERROR"
    if "try again" in lowered or "could not be fetched" in lowered:
        return "TRANSIENT_SERVER_DATA_FETCH_FAILURE"
    return "APPLICATION_PAYLOAD_ERROR"


async def _call_corporate_actions(client: NSEMCPClient, tools: list[dict[str, Any]], *,
                                  symbol: str, start: str, end: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tool = next((item for item in tools if item["name"] == "get_corporate_actions"), None)
    arguments = {"symbol": symbol, "fromDate": start, "toDate": end}
    base: dict[str, Any] = {"tool": "get_corporate_actions", "arguments": arguments,
                            "coverage": "UNKNOWN", "raw_payload_retained": False}
    if tool is None:
        return base | {"status": "FAILED", "classification": "TOOL_NOT_DISCOVERED"}, []
    base["input_schema"] = tool["inputSchema"]
    try:
        result = await client.call_tool("get_corporate_actions", arguments)
    except MCPError as exc:
        return base | {"status": "FAILED", "classification": "PROTOCOL_OR_MCP_ERROR",
                       "error_type": type(exc).__name__, "error": str(exc),
                       "protocol": client.diagnostics()}, []
    protocol = client.diagnostics()
    try:
        payload = tool_payload(result)
    except ValueError as exc:
        return base | {"status": "FAILED", "classification": "MALFORMED_TOOL_CONTENT",
                       "error": str(exc), "protocol": protocol}, []
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        message = payload["error"]
        application_code = payload.get("errorCode", payload.get("code"))
        if not isinstance(application_code, (str, int, float)) or isinstance(application_code, bool):
            application_code = None
        return base | {"status": "FAILED", "classification": _payload_error_classification(message),
                       "application_error_code": application_code,
                       "error": message, "protocol": protocol}, []
    try:
        actions = normalize_corporate_actions(payload, requested_symbol=symbol)
    except ValueError as exc:
        return base | {"status": "FAILED", "classification": "UNSUPPORTED_RESPONSE_SCHEMA",
                       "error": str(exc), "protocol": protocol}, []
    return base | {"status": "PASS_RESPONSE", "classification": "EXPLICIT_ACTION_RECORDS",
                   "observations": len(actions), "protocol": protocol}, actions


async def _discover_one(name: str, args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "url": ENDPOINTS[name], "initialization": "FAILED", "tools": []}
    try:
        async with NSEMCPClient(name, connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
                                retries=args.retries) as client:
            result.update(initialization="PASS", protocol_version=client.protocol_version,
                          server_name=client.server_info["name"], tools=await client.list_tools())
    except MCPError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


async def _discover(args: argparse.Namespace) -> int:
    names = tuple(ENDPOINTS) if args.endpoint == "both" else (args.endpoint,)
    endpoints = [await _discover_one(name, args) for name in names]
    summary = {"mode": "DISCOVERY_ONLY", "endpoints": endpoints,
               "recommendation": "Keep NSE MCP isolated as reference-only; do not ingest it into research datasets."}
    report = write_report(args.output_root, summary)
    print(json.dumps({"report": str(report), "endpoints": [
        {"name": item["name"], "initialization": item["initialization"], "tools": len(item["tools"])}
        for item in endpoints]}, indent=2))
    return 0 if all(item["initialization"] == "PASS" for item in endpoints) else 2


async def _sample(args: argparse.Namespace) -> int:
    validate_sample_request(args.symbol, args.start, args.end, allow_sample_fetch=args.allow_sample_fetch)
    try:
        arguments = json.loads(args.arguments_json)
    except json.JSONDecodeError as exc:
        raise ValueError("--arguments-json must be valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("--arguments-json must contain an object")
    validate_tool_arguments(arguments, symbol=args.symbol, start=args.start, end=args.end)
    async with NSEMCPClient("bhavcopy", connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
                            retries=args.retries) as client:
        tools = await client.list_tools()
        matching = [tool for tool in tools if tool["name"] == args.tool]
        if len(matching) != 1:
            raise ValueError("requested tool is not present in the dynamically discovered schema")
        response = await client.call_tool(args.tool, arguments)
        observations = normalize_nse_history(tool_payload(response), requested_symbol=args.symbol,
                                             source=f"NSE_MCP:{ENDPOINTS['bhavcopy']}:{args.tool}")
        if any(not args.start <= row.trading_date <= args.end for row in observations):
            raise ValueError("NSE MCP returned an observation outside the declared sample interval")
        actions: list[dict[str, Any]] = []
        action_request: dict[str, Any] = {"status": "NOT_REQUESTED", "coverage": "UNKNOWN"}
        if args.include_corporate_actions:
            action_request, actions = await _call_corporate_actions(
                client, tools, symbol=args.symbol, start=args.start, end=args.end)
        comparison = None
        if args.shoonya_segment:
            shoonya = load_shoonya_segment(args.shoonya_segment, symbol=args.symbol,
                                           start=args.start, end=args.end)
            comparison = compare_observations(
                observations, shoonya, price_abs_tolerance=Decimal(args.price_abs_tolerance),
                price_rel_tolerance=Decimal(args.price_rel_tolerance), volume_tolerance=args.volume_tolerance,
                corporate_actions=actions)
        endpoint = _endpoint(client, tools)
    summary = {"mode": "EXPLICIT_SAMPLE_FETCH", "endpoints": [endpoint],
               "sample": {"symbol": args.symbol, "start": args.start, "end": args.end, "tool": args.tool,
                          "tool_input_schema": matching[0]["inputSchema"],
                          "historical_request": {"status": "PASS", "observations": len(observations)},
                          "nse_observations": len(observations),
                          "data_freshness": {"first_trading_date": min(row.trading_date for row in observations),
                                             "last_trading_date": max(row.trading_date for row in observations),
                                             "source_timestamps_present": sum(
                                                 row.source_timestamp is not None for row in observations)},
                          "corporate_actions": action_request,
                          "comparison": comparison},
               "recommendation": "Treat differences as diagnostics only; wider integration needs licence review."}
    report = write_report(args.output_root, summary)
    print(json.dumps({"report": str(report), "nse_observations": len(observations),
                      "compared": comparison["counts"]["compared"] if comparison else 0}, indent=2))
    return 0


async def _corporate_actions(args: argparse.Namespace) -> int:
    validate_sample_request(args.symbol, args.start, args.end, allow_sample_fetch=args.allow_sample_fetch)
    async with NSEMCPClient("bhavcopy", connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
                            retries=args.retries) as client:
        tools = await client.list_tools()
        request, _actions = await _call_corporate_actions(
            client, tools, symbol=args.symbol, start=args.start, end=args.end)
        endpoint = _endpoint(client, tools)
    summary = {"mode": "EXPLICIT_CORPORATE_ACTION_RETRY", "endpoints": [endpoint],
               "corporate_action_retry": {"symbol": args.symbol, "start": args.start, "end": args.end,
                                          **request},
               "recommendation": "Coverage remains UNKNOWN unless explicit records and licensing are validated."}
    report = write_report(args.output_root, summary)
    print(json.dumps({"report": str(report), "status": request["status"],
                      "classification": request.get("classification"),
                      "protocol": request.get("protocol")}, indent=2))
    return 0 if request["status"] == "PASS_RESPONSE" else 2


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--read-timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover", help="initialize and list tools; never call a data tool")
    discover.add_argument("--endpoint", choices=("both", *ENDPOINTS), default="both")
    _common(discover)
    sample = commands.add_parser("sample", help="make one bounded historical call after explicit opt-in")
    sample.add_argument("--allow-sample-fetch", action="store_true")
    sample.add_argument("--symbol", required=True, choices=("RELIANCE", "INFY", "TCS"))
    sample.add_argument("--start", required=True)
    sample.add_argument("--end", required=True)
    sample.add_argument("--tool", required=True)
    sample.add_argument("--arguments-json", required=True,
                        help="arguments copied from the discovered tool schema")
    sample.add_argument("--shoonya-segment", type=Path,
                        help="read-only accepted ShoonyaDailyV1 segment ending before the locked holdout")
    sample.add_argument("--include-corporate-actions", action="store_true",
                        help="also make one same-symbol/same-window corporate-action call")
    sample.add_argument("--price-abs-tolerance", default="0.01")
    sample.add_argument("--price-rel-tolerance", default="0.0001")
    sample.add_argument("--volume-tolerance", type=int, default=0)
    _common(sample)
    actions = commands.add_parser(
        "corporate-actions", help="make one bounded corporate-action call without fetching prices")
    actions.add_argument("--allow-sample-fetch", action="store_true")
    actions.add_argument("--symbol", required=True, choices=("RELIANCE", "INFY", "TCS"))
    actions.add_argument("--start", required=True)
    actions.add_argument("--end", required=True)
    _common(actions)
    args = parser.parse_args(argv)
    try:
        operation = {"discover": _discover, "sample": _sample, "corporate-actions": _corporate_actions}
        return asyncio.run(operation[args.command](args))
    except (MCPError, OSError, PermissionError, ValueError) as exc:
        print(f"M3.9 NSE MCP error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
