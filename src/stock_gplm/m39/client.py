"""Bounded, read-only client for NSE's official Streamable HTTP MCP servers.

Only the MCP initialize, tools/list and tools/call methods are implemented.  The
small protocol surface keeps the core project dependency-free while still using
the published MCP JSON-RPC wire protocol.  A transport can be injected for fully
offline tests.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import logging
import random
import ssl
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

PROTOCOL_VERSION = "2025-06-18"
ENDPOINTS = {
    "bhavcopy": "https://mcp.nseindia.in/bhavcopy/cm/mcp",
    "market": "https://mcp.nseindia.in/cmmkt/mcp",
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_TOOL_PAGES = 10

log = logging.getLogger(__name__)


class MCPError(RuntimeError):
    """Base error for a failed MCP operation."""


class MCPConnectionError(MCPError):
    """The HTTP connection failed or retries were exhausted."""


class MCPProtocolError(MCPError):
    """The peer returned malformed or unexpected MCP data."""


class MCPToolError(MCPError):
    """The MCP server rejected a tool call or reported tool failure."""


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class MCPTransport(Protocol):
    async def post(self, url: str, headers: Mapping[str, str], body: bytes,
                   connect_timeout: float, read_timeout: float) -> HTTPResponse: ...


class HTTPSConnectionTransport:
    """Standard-library HTTPS transport with distinct connect/read deadlines."""

    @staticmethod
    def _post(url: str, headers: Mapping[str, str], body: bytes,
              connect_timeout: float, read_timeout: float) -> HTTPResponse:
        parsed = urlparse(url)
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=connect_timeout,
            context=ssl.create_default_context())
        try:
            connection.connect()
            if connection.sock is None:
                raise MCPConnectionError("HTTPS connection has no socket")
            connection.sock.settimeout(read_timeout)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            connection.request("POST", path, body=body, headers=dict(headers))
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise MCPProtocolError(f"MCP response exceeds {MAX_RESPONSE_BYTES} bytes")
            return HTTPResponse(response.status, dict(response.getheaders()), data)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise MCPConnectionError(f"MCP connection failed: {type(exc).__name__}: {exc}") from exc
        finally:
            connection.close()

    async def post(self, url: str, headers: Mapping[str, str], body: bytes,
                   connect_timeout: float, read_timeout: float) -> HTTPResponse:
        return await asyncio.to_thread(
            self._post, url, headers, body, connect_timeout, read_timeout)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted), None)


def _decode_response(response: HTTPResponse) -> dict[str, Any] | None:
    if response.status == 202 and not response.body:
        return None
    if not response.body:
        raise MCPProtocolError(f"MCP returned HTTP {response.status} with an empty body")
    content_type = (_header(response.headers, "content-type") or "").lower()
    raw = response.body
    if "text/event-stream" in content_type:
        events = [line[5:].strip() for line in raw.decode("utf-8").splitlines() if line.startswith("data:")]
        if not events:
            raise MCPProtocolError("MCP event stream contained no data event")
        raw = events[-1].encode()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPProtocolError("MCP response is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise MCPProtocolError("MCP response is not a JSON-RPC 2.0 object")
    return value


def validate_arguments_against_schema(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate the top-level object subset used by discovered NSE tool schemas."""
    if schema.get("type") != "object" or not isinstance(schema.get("properties", {}), dict):
        raise MCPProtocolError("discovered tool inputSchema is not a supported object schema")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise MCPProtocolError("discovered tool inputSchema has malformed required fields")
    missing = sorted(set(required) - set(arguments))
    if missing:
        raise ValueError(f"tool arguments omit required fields: {missing}")
    extras = sorted(set(arguments) - set(properties))
    if schema.get("additionalProperties") is False and extras:
        raise ValueError(f"tool arguments contain unsupported fields: {extras}")
    types: dict[str, tuple[type, ...]] = {
        "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
        "object": (dict,), "array": (list,),
    }
    for key, value in arguments.items():
        definition = properties.get(key)
        if not isinstance(definition, dict) or not isinstance(definition.get("type"), str):
            raise MCPProtocolError(f"discovered schema for {key} has no supported type")
        expected = types.get(definition["type"])
        if expected is None:
            raise MCPProtocolError(f"discovered schema uses unsupported type {definition['type']}")
        if isinstance(value, bool) and definition["type"] in {"integer", "number"}:
            raise ValueError(f"tool argument {key} must be {definition['type']}")
        if not isinstance(value, expected):
            raise ValueError(f"tool argument {key} must be {definition['type']}")


class NSEMCPClient:
    """One read-only MCP session against an allowlisted official NSE endpoint."""

    def __init__(self, endpoint: str, *, connect_timeout: float = 5.0, read_timeout: float = 15.0,
                 retries: int = 2, transport: MCPTransport | None = None) -> None:
        url = ENDPOINTS.get(endpoint, endpoint)
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "mcp.nseindia.in" or parsed.path not in {
            "/bhavcopy/cm/mcp", "/cmmkt/mcp"
        }:
            raise ValueError("endpoint must be one of NSE's two registered HTTPS MCP endpoints")
        if connect_timeout <= 0 or read_timeout <= 0 or retries < 0 or retries > 5:
            raise ValueError("timeouts must be positive and retries must be between 0 and 5")
        self.endpoint = endpoint if endpoint in ENDPOINTS else next(k for k, v in ENDPOINTS.items() if v == url)
        self.url = url
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retries = retries
        self.transport = transport or HTTPSConnectionTransport()
        self.session_id: str | None = None
        self.server_info: dict[str, Any] | None = None
        self.protocol_version: str | None = None
        self._request_id = 0
        self._closed = False
        self._tools: dict[str, dict[str, Any]] = {}
        self.last_http_status: int | None = None
        self.last_content_type: str | None = None
        self.last_jsonrpc_error: dict[str, Any] | None = None
        self.last_mcp_is_error: bool | None = None

    async def __aenter__(self) -> "NSEMCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def _post(self, payload: dict[str, Any], *, notification: bool = False) -> dict[str, Any] | None:
        if self._closed:
            raise MCPConnectionError("MCP client is closed")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Accept-Encoding": "identity",
                   "User-Agent": "stock-gplm-m39/0.1 (research-reference-only)"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = self.protocol_version or PROTOCOL_VERSION
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise MCPProtocolError(f"MCP request exceeds {MAX_REQUEST_BYTES} bytes")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await asyncio.wait_for(
                    self.transport.post(
                        self.url, headers, encoded, self.connect_timeout, self.read_timeout),
                    timeout=self.connect_timeout + self.read_timeout,
                )
                self.last_http_status = response.status
                self.last_content_type = _header(response.headers, "content-type")
                if response.status in {429, 500, 502, 503, 504}:
                    raise MCPConnectionError(f"retryable MCP HTTP status {response.status}")
                if response.status >= 400:
                    raise MCPProtocolError(f"MCP returned HTTP {response.status}")
                session_id = _header(response.headers, "mcp-session-id")
                if session_id:
                    self.session_id = session_id
                return _decode_response(response)
            except (asyncio.TimeoutError, MCPConnectionError) as exc:
                last = exc
                if attempt == self.retries:
                    break
                delay = min(2.0, 0.25 * (2 ** attempt)) + random.uniform(0, 0.05)
                log.warning("NSE MCP request retry %d/%d after %s", attempt + 1, self.retries, type(exc).__name__)
                await asyncio.sleep(delay)
        raise MCPConnectionError(f"NSE MCP request failed after {self.retries + 1} attempts: {last}") from last

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.last_jsonrpc_error = None
        self._request_id += 1
        response = await self._post({"jsonrpc": "2.0", "id": self._request_id, "method": method,
                                     "params": params or {}})
        if response is None:
            raise MCPProtocolError(f"MCP {method} returned no response")
        if response.get("id") != self._request_id:
            raise MCPProtocolError(f"MCP {method} response id does not match request")
        if "error" in response:
            error = response["error"]
            self.last_jsonrpc_error = dict(error) if isinstance(error, dict) else {"malformed": True}
            message = error.get("message", "unknown error") if isinstance(error, dict) else "malformed error"
            code = error.get("code") if isinstance(error, dict) else None
            raise MCPToolError(f"MCP {method} failed with JSON-RPC code {code}: {message}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError(f"MCP {method} result must be an object")
        return result

    async def connect(self) -> dict[str, Any]:
        if self.server_info is not None:
            return self.server_info
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "stock-gplm-m39", "version": "0.1.0"},
        })
        version = result.get("protocolVersion")
        info = result.get("serverInfo")
        if not isinstance(version, str) or not isinstance(info, dict) or not isinstance(info.get("name"), str):
            raise MCPProtocolError("MCP initialize result lacks protocolVersion/serverInfo")
        self.protocol_version = version
        self.server_info = info
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)
        return info

    async def list_tools(self) -> list[dict[str, Any]]:
        if self.server_info is None:
            raise MCPConnectionError("connect must succeed before list_tools")
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise MCPProtocolError("MCP tools/list returned malformed tools")
            for item in page:
                if not isinstance(item.get("name"), str) or not isinstance(item.get("inputSchema"), dict):
                    raise MCPProtocolError("MCP tool lacks name or inputSchema")
            tools.extend(page)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                self._tools = {item["name"]: item for item in tools}
                return tools
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise MCPProtocolError("MCP tools/list returned an invalid cursor")
            cursor = next_cursor
        raise MCPProtocolError(f"MCP tools/list exceeded {MAX_TOOL_PAGES} pages")

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self.server_info is None:
            raise MCPConnectionError("connect must succeed before call_tool")
        if not name or not isinstance(arguments, Mapping):
            raise ValueError("tool name and argument object are required")
        tool = self._tools.get(name)
        if tool is None:
            raise MCPToolError(f"tool {name!r} was not dynamically discovered in this session")
        validate_arguments_against_schema(arguments, tool["inputSchema"])
        self.last_mcp_is_error = None
        result = await self._request("tools/call", {"name": name, "arguments": dict(arguments)})
        self.last_mcp_is_error = result.get("isError") is True
        if result.get("isError") is True:
            raise MCPToolError(f"NSE MCP tool {name} reported an error")
        content = result.get("content")
        if not isinstance(content, list):
            raise MCPProtocolError("MCP tools/call result lacks a content list")
        return result

    def diagnostics(self) -> dict[str, Any]:
        """Return bounded protocol metadata; response bodies and session IDs are excluded."""
        return {"http_status": self.last_http_status, "content_type": self.last_content_type,
                "jsonrpc_error": self.last_jsonrpc_error, "mcp_is_error": self.last_mcp_is_error}

    async def close(self) -> None:
        # Streamable HTTP does not require a request when ending a client session.
        self._closed = True
        self.session_id = None
