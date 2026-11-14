"""Non-overwriting structured and human-readable M3.9 reports."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .policy import assert_reference_only_destination


def git_identity() -> dict[str, str]:
    def value(*args: str) -> str:
        result = subprocess.run(("git", *args), check=True, capture_output=True, text=True)
        return result.stdout.strip()
    return {"branch": value("branch", "--show-current"), "commit": value("rev-parse", "HEAD")}


def environment() -> dict[str, str]:
    # Hostnames can identify private infrastructure and are not needed for reproducibility.
    return {"python": platform.python_version(), "platform": platform.platform()}


def _markdown(summary: Mapping[str, Any]) -> str:
    lines = ["# Stock GPLM M3.9 NSE MCP validation", "",
             f"- Executed: `{summary['executed_at_utc']}`",
             f"- Git: `{summary['git']['branch']}` at `{summary['git']['commit']}`",
             f"- Mode: `{summary['mode']}`", "",
             "## Endpoint results", ""]
    for endpoint in summary.get("endpoints", []):
        lines.extend([f"### {endpoint['name']}", "",
                      f"- URL: `{endpoint['url']}`",
                      f"- Initialization: `{endpoint['initialization']}`",
                      f"- Protocol: `{endpoint.get('protocol_version', 'UNKNOWN')}`",
                      f"- Server: `{endpoint.get('server_name', 'UNKNOWN')}`",
                      f"- Tools discovered: `{len(endpoint.get('tools', []))}`"])
        if endpoint.get("error"):
            lines.append(f"- Error: `{endpoint['error']}`")
        lines.extend(["", "| Tool | Description | Input schema |", "|---|---|---|"])
        for tool in endpoint.get("tools", []):
            description = str(tool.get("description", "")).replace("|", "\\|").replace("\n", " ")
            schema = json.dumps(tool.get("inputSchema", {}), sort_keys=True).replace("|", "\\|")
            lines.append(f"| `{tool['name']}` | {description} | `{schema}` |")
        lines.append("")
    if summary.get("sample") is not None:
        sample = summary["sample"]
        lines.extend(["## Controlled sample", "", f"- Symbol: `{sample['symbol']}`",
                      f"- Interval: `{sample['start']}` through `{sample['end']}`",
                      f"- Tool: `{sample['tool']}`",
                      f"- Normalized NSE observations: `{sample['nse_observations']}`",
                      f"- Corporate-action request: `{sample.get('corporate_actions', {}).get('status', 'NOT_REQUESTED')}`",
                      f"- Raw MCP payload retained: `false`", ""])
        comparison = sample.get("comparison")
        if comparison:
            lines.extend([f"- Compared dates: `{comparison['counts']['compared']}`",
                          f"- Mismatched dates: `{comparison['counts']['mismatched_dates']}`",
                          f"- Missing from NSE: `{len(comparison['missing_from_nse'])}`",
                          f"- Missing from Shoonya: `{len(comparison['missing_from_shoonya'])}`", ""])
    if summary.get("corporate_action_retry") is not None:
        retry = summary["corporate_action_retry"]
        protocol = retry.get("protocol", {})
        lines.extend(["## Corporate-action retry", "",
                      f"- Symbol: `{retry['symbol']}`",
                      f"- Interval: `{retry['start']}` through `{retry['end']}`",
                      f"- Status: `{retry['status']}`",
                      f"- Classification: `{retry.get('classification', 'UNKNOWN')}`",
                      f"- Coverage: `{retry.get('coverage', 'UNKNOWN')}`",
                      f"- HTTP status: `{protocol.get('http_status', 'UNKNOWN')}`",
                      f"- JSON-RPC error: `{json.dumps(protocol.get('jsonrpc_error'))}`",
                      f"- MCP isError: `{protocol.get('mcp_is_error', 'UNKNOWN')}`"])
        if "application_error_code" in retry:
            lines.append(f"- Application error code: `{retry['application_error_code']}`")
        if retry.get("error"):
            lines.append(f"- Application error: `{retry['error']}`")
        lines.extend(["- Raw MCP payload retained: `false`", ""])
    lines.extend(["## Licence and research integrity", "",
                  "NSE MCP data is used only for informational, educational and research reference validation. "
                  "It is not licensed here for commercial use, model training/fine-tuning, production trading, "
                  "feature stores, labels or backtests.", "",
                  "Prices are treated as unadjusted. Corporate-action flags do not prove an adjustment factor "
                  "or explain an observed discrepancy. No source is modified and no raw MCP response is retained.", "",
                  "## Recommendation", "",
                  str(summary.get("recommendation", "Keep integration discovery-only pending review.")), ""])
    return "\n".join(lines)


def write_report(output_root: Path, summary: dict[str, Any]) -> Path:
    """Publish a new report directory atomically; an existing run is never overwritten."""
    output_root = Path(output_root)
    assert_reference_only_destination(output_root)
    summary = dict(summary)
    summary.setdefault("contract", "stock-gplm.m39-nse-mcp-report.v1")
    summary.setdefault("executed_at_utc", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    summary.setdefault("git", git_identity())
    summary.setdefault("environment", environment())
    identity = hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    timestamp = summary["executed_at_utc"].replace(":", "").replace("-", "")[:15]
    target = output_root / f"{timestamp}-{identity}"
    output_root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"report already exists: {target}")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=output_root))
    try:
        structured = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        (staging / "summary.json").write_text(structured, encoding="utf-8")
        (staging / "report.md").write_text(_markdown(summary), encoding="utf-8")
        os.rename(staging, target)
    except BaseException:
        for child in staging.iterdir() if staging.exists() else ():
            child.unlink()
        if staging.exists():
            staging.rmdir()
        raise
    return target
