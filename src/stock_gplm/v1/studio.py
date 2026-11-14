"""Dependency-free local Strategy Studio and JSON API.

The server binds only to IPv4 loopback, and :func:`make_server` enforces that for
programmatic callers as well as the CLI. Each connection must come from a loopback
peer. Each request must also carry a loopback ``Host``. An ``Origin``, when
present, must be the Studio's own. A request with no ``Origin`` is accepted only
from a loopback peer whose ``Sec-Fetch-Site`` (if sent) is ``same-origin`` or
``none``. A missing ``Origin`` is never treated as proof of a trusted peer. The
Studio additionally requires a cryptographically random per-launch bearer token
on every API request. The token is stored in an owner-only local file and is
removed when the server closes. The Studio has no broker client, credential
support, remote data loader or order-routing endpoint.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .backtest import run_backtest
from .codec import dump_json, dump_yaml, load_document
from .registry import StrategyRegistry
from .schema import StrategyValidationError, validate_strategy

MAX_REQUEST_BYTES = 2 * 1024 * 1024
# The only accepted bind names, each mapped to the literal address actually bound.
LOOPBACK_BIND = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1"}


def is_loopback_address(address: object) -> bool:
    """True only for a literal loopback IP (IPv4-mapped IPv6 included); names are never resolved."""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Threading server that refuses any connection whose peer address is not loopback."""

    token_path: Path | None = None

    def verify_request(self, request: Any, client_address: Any) -> bool:  # noqa: D401
        return is_loopback_address(client_address[0] if client_address else None)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            if self.token_path is not None:
                try:
                    self.token_path.unlink()
                except FileNotFoundError:
                    pass

_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Stock GPLM Strategy Studio</title>
<style>
:root{font-family:system-ui,sans-serif;color:#172033;background:#f5f7fb}body{max-width:1180px;margin:auto;padding:24px}
h1{margin-bottom:4px}.safe{color:#0b6b4b;font-weight:650}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:white;padding:18px;border:1px solid #dce2eb;border-radius:10px}label{display:block;font-size:13px;font-weight:650;margin-top:10px}
input,textarea,select,button{box-sizing:border-box;width:100%;padding:9px;margin-top:4px}textarea{font-family:ui-monospace,monospace;min-height:310px}
button{cursor:pointer;background:#1e55d5;color:white;border:0;border-radius:6px;font-weight:650}.row{display:flex;gap:8px}.row>*{flex:1}
pre{white-space:pre-wrap;max-height:420px;overflow:auto;background:#101828;color:#d9e2f2;padding:12px;border-radius:6px}
@media(max-width:800px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>Strategy Studio</h1><p class="safe">Local research only · activation never enables live broker orders</p>
<div class="grid"><section class="card"><h2>Definition</h2>
<div class="row"><label>Import format<select id="format"><option>yaml</option><option>json</option></select></label>
<label>Saved version<select id="versions"><option value="">New / imported</option></select></label></div>
<label>Strategy YAML or JSON<textarea id="config" spellcheck="false"></textarea></label>
<div class="row"><button onclick="validateConfig()">Validate</button><button onclick="saveConfig()">Save immutable version</button></div>
<div class="row"><button onclick="cloneConfig()">Clone</button><button onclick="exportConfig()">Export</button></div>
<div class="row"><button onclick="activate()">Activate research config</button><button onclick="deactivate()">Deactivate</button></div>
<details><summary><strong>Structured editor and rule builder</strong></summary>
<button onclick="loadStructured()">Load validated document into form</button>
<div class="row"><label>ID<input id="sid"></label><label>Name<input id="sname"></label></div>
<label>Description<input id="sdesc"></label>
<div class="row"><label>Enabled<select id="senabled"><option value="true">true</option><option value="false">false</option></select></label>
<label>Maximum holding sessions<input id="maxhold" type="number" min="1"></label></div>
<div class="row"><label>Stop loss %<input id="stop" type="number" min="0" step="0.1"></label>
<label>Take profit %<input id="target" type="number" min="0" step="0.1"></label></div>
<button onclick="applyParameters()">Apply parameters</button>
<h3>Indicators</h3><div class="row"><label>ID<input id="iid" placeholder="ma20"></label>
<label>Type<select id="itype"><option>SMA</option><option>SMA_VOLUME</option><option>EMA</option><option>HIGHEST</option><option>LOWEST</option><option>PERCENT_CHANGE</option></select></label></div>
<div class="row"><label>Period<input id="iperiod" type="number" min="1" value="20"></label>
<label>Field<select id="ifield"><option>close</option><option>open</option><option>high</option><option>low</option><option>volume</option></select></label></div>
<button onclick="addIndicator()">Add indicator</button><div id="indicatorList"></div>
<h3>Comparison rule</h3><div class="row"><label>Target<select id="ruleTarget"><option value="entry">Entry (append)</option><option value="exit">Exit (replace)</option></select></label>
<label>Entry group<select id="entryGroup"><option>AND</option><option>OR</option></select></label></div>
<div class="row"><label>Left operand<input id="ruleLeft" placeholder="field:close"></label>
<label>Operator<select id="ruleOp"><option>GT</option><option>GTE</option><option>LT</option><option>LTE</option><option>EQ</option><option>CROSS_ABOVE</option><option>CROSS_BELOW</option></select></label></div>
<label>Right operand<input id="ruleRight" placeholder="indicator:ma20 or constant:100"></label>
<button onclick="addRule()">Add rule</button><div id="ruleList"></div>
<p>Operands use <code>field:close</code>, <code>indicator:ma20</code>, or <code>constant:1.5</code>. Use the document editor for nested NOT or arithmetic operands.</p>
</details>
</section><section class="card"><h2>Backtest</h2>
<p>Paste completed daily candles as a JSON array. Signals at a completed close enter no earlier than the next session open.</p>
<label>Candles JSON<textarea id="candles" spellcheck="false">[]</textarea></label>
<label><input id="syntheticEligible" type="checkbox"> I confirm these are synthetic demo candles eligible only for an engineering backtest.</label>
<button onclick="backtest()">Launch backtest</button><h2>Validation / result</h2><pre id="result">Ready.</pre></section></div>
<section class="card" style="margin-top:16px"><h2>Historical research (M5)</h2>
<p>Eligibility is decided on the server by the M5 adapter from manifest-verified local files. This page only displays its decisions.
<code>DIAGNOSTIC_ONLY</code> runs are non-investable and cannot be ranked, promoted or paper traded.</p>
<div class="row"><label>Mode<select id="hmode"><option>STRICT_RESEARCH</option><option>DIAGNOSTIC_ONLY</option></select></label>
<label>Symbols (comma separated, at most 25; the whole list must pass)<input id="hsymbols" placeholder="RELIANCE-EQ,TCS-EQ"></label></div>
<div class="row"><label>Data start<input id="hstart" type="date"></label><label>Data end<input id="hend" type="date"></label>
<label>Evaluation start<input id="hestart" type="date"></label><label>Evaluation end<input id="heend" type="date"></label></div>
<div class="row"><label>Initial capital (INR)<input id="hcapital" type="number" value="1000000"></label>
<label>Max open positions<input id="hmaxpos" type="number" value="10"></label>
<label>Max position fraction<input id="hposfrac" type="number" step="0.01" value="0.2"></label>
<label>Max gross exposure (no margin)<input id="hexposure" type="number" step="0.01" value="1"></label></div>
<div class="row"><label>Statutory buy bps<input id="hbuy" type="number" step="0.01" value="12.41"></label>
<label>Statutory sell bps<input id="hsell" type="number" step="0.01" value="10.91"></label>
<label>Cost basis note<input id="hbasis" value="ASSUMPTION: NSE delivery statutory charges per M3.5 policy; verify against current schedules"></label></div>
<div class="row"><button onclick="hDatasets()">Discover datasets</button><button onclick="hEligibility()">Check eligibility</button>
<button onclick="hBacktest()">Run historical backtest (selected version)</button></div>
<div class="row"><button onclick="hRuns()">List runs</button><label>Run IDs to compare or export<input id="hruns"></label>
<button onclick="hCompare()">Compare</button><button onclick="hExport()">Export report</button></div>
<pre id="hresult">Not run.</pre></section>
<script>
const AUTH_TOKEN=__STUDIO_AUTH_TOKEN_JSON__;
history.replaceState(null,'',location.pathname);
const out=x=>document.querySelector('#result').textContent=typeof x==='string'?x:JSON.stringify(x,null,2);
const byId=id=>document.getElementById(id);
let working=null;
async function api(path,options={}){options.headers={...(options.headers||{}),Authorization:'Bearer '+AUTH_TOKEN};let r=await fetch(path,options);let x=await r.json();if(!r.ok)throw new Error(x.error||r.statusText);return x}
async function refresh(){let x=await api('/api/strategies');let s=document.querySelector('#versions');s.innerHTML='<option value="">New / imported</option>';for(let v of x.strategies){let o=document.createElement('option');o.value=v.strategy_id+'@'+v.version;o.textContent=o.value+' · '+v.activation_status;s.appendChild(o)}}
document.querySelector('#versions').onchange=async e=>{if(!e.target.value)return;let [id,v]=e.target.value.split('@');let x=await api('/api/strategies/'+id+'/'+v);document.querySelector('#config').value=x.document;document.querySelector('#format').value='yaml';out(x.record)};
function payload(){return JSON.stringify({format:document.querySelector('#format').value,document:document.querySelector('#config').value})}
async function validateConfig(){try{out(await api('/api/validate',{method:'POST',headers:{'content-type':'application/json'},body:payload()}))}catch(e){out(e.message)}}
async function saveConfig(){try{out(await api('/api/strategies',{method:'POST',headers:{'content-type':'application/json'},body:payload()}));await refresh()}catch(e){out(e.message)}}
async function cloneConfig(){try{let x=await api('/api/validate',{method:'POST',headers:{'content-type':'application/json'},body:payload()});x.config.strategy.id=x.config.strategy.id+'-clone';x.config.strategy.name=x.config.strategy.name+' (Clone)';document.querySelector('#config').value=JSON.stringify(x.config,null,2);document.querySelector('#format').value='json';out('Clone prepared. Change its ID or rules, validate, then save.')}catch(e){out(e.message)}}
async function exportConfig(){try{let x=await api('/api/export',{method:'POST',headers:{'content-type':'application/json'},body:payload()});document.querySelector('#config').value=x.document;out('Exported canonical '+x.format)}catch(e){out(e.message)}}
function selected(){let x=document.querySelector('#versions').value;if(!x)throw new Error('Select a saved version');return x.split('@')}
async function activate(){try{let [id,v]=selected();out(await api('/api/strategies/'+id+'/'+v+'/activate',{method:'POST'}));await refresh()}catch(e){out(e.message)}}
async function deactivate(){try{let [id]=selected();out(await api('/api/strategies/'+id+'/deactivate',{method:'POST'}));await refresh()}catch(e){out(e.message)}}
async function backtest(){try{let [id,v]=selected();out(await api('/api/backtests',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({strategy_id:id,version:Number(v),candles:JSON.parse(document.querySelector('#candles').value),data_quality_eligible:document.querySelector('#syntheticEligible').checked})}))}catch(e){out(e.message)}}
function syncDocument(){document.querySelector('#config').value=JSON.stringify(working,null,2);document.querySelector('#format').value='json';renderStructured()}
async function loadStructured(){try{let x=await api('/api/validate',{method:'POST',headers:{'content-type':'application/json'},body:payload()});working=x.config;renderStructured();out('Validated document loaded into structured editor.')}catch(e){out(e.message)}}
function renderStructured(){if(!working)return;byId('sid').value=working.strategy.id;byId('sname').value=working.strategy.name;byId('sdesc').value=working.strategy.description;byId('senabled').value=String(working.strategy.enabled);byId('maxhold').value=working.exit.max_holding_sessions;byId('stop').value=working.risk.stop_loss.value;byId('target').value=working.risk.take_profit.value;
 byId('indicatorList').innerHTML=working.indicators.map((x,i)=>'<p><code>'+x.id+'</code> '+x.type+'('+x.field+', '+x.period+') <button onclick="removeIndicator('+i+')">Remove</button></p>').join('');
 let rules=working.entry.conditions||[working.entry];byId('ruleList').innerHTML=rules.map((x,i)=>'<p><code>'+JSON.stringify(x)+'</code> <button onclick="removeRule('+i+')">Remove</button></p>').join('')+'<p><strong>Exit:</strong> <code>'+JSON.stringify(working.exit.condition)+'</code></p>'}
function applyParameters(){try{if(!working)throw new Error('Load a validated document first');working.strategy.id=byId('sid').value;working.strategy.name=byId('sname').value;working.strategy.description=byId('sdesc').value;working.strategy.enabled=byId('senabled').value==='true';working.exit.max_holding_sessions=Number(byId('maxhold').value);working.risk.stop_loss={type:'percent',value:Number(byId('stop').value)};working.risk.take_profit={type:'percent',value:Number(byId('target').value)};syncDocument();out('Parameters applied. Validate before saving.')}catch(e){out(e.message)}}
function addIndicator(){try{if(!working)throw new Error('Load a validated document first');working.indicators.push({id:byId('iid').value,type:byId('itype').value,period:Number(byId('iperiod').value),field:byId('ifield').value});syncDocument();out('Indicator added. Validate dependencies before saving.')}catch(e){out(e.message)}}
function removeIndicator(i){working.indicators.splice(i,1);syncDocument();out('Indicator removed. Referencing rules must also be changed before validation.')}
function operand(text){let p=text.indexOf(':');if(p<1)throw new Error('Operand must use kind:value');let k=text.slice(0,p),v=text.slice(p+1);if(k==='constant')return {constant:Number(v)};if(k==='field')return {field:v,offset:0};if(k==='indicator')return {indicator:v,offset:0};throw new Error('Operand kind must be field, indicator, or constant')}
function addRule(){try{if(!working)throw new Error('Load a validated document first');let rule={operator:byId('ruleOp').value,left:operand(byId('ruleLeft').value),right:operand(byId('ruleRight').value)};if(byId('ruleTarget').value==='exit')working.exit.condition=rule;else{if(!['AND','OR'].includes(working.entry.operator))working.entry={operator:byId('entryGroup').value,conditions:[working.entry]};working.entry.operator=byId('entryGroup').value;working.entry.conditions.push(rule)}syncDocument();out('Rule applied. Validate before saving.')}catch(e){out(e.message)}}
function removeRule(i){if(!working.entry.conditions)throw new Error('Entry root is not a rule group');working.entry.conditions.splice(i,1);syncDocument();out('Entry rule removed. At least one rule is required.')}
const hout=x=>byId('hresult').textContent=typeof x==='string'?x:JSON.stringify(x,null,2);
const post=(path,body)=>api(path,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
const hsymbols=()=>byId('hsymbols').value.split(',').map(x=>x.trim());
async function hDatasets(){try{hout(await api('/api/m5/datasets'))}catch(e){hout(e.message)}}
async function hEligibility(){try{let rows=[];for(let s of hsymbols())rows.push(await post('/api/m5/eligibility',{symbol:s,start:byId('hstart').value,end:byId('hend').value,mode:byId('hmode').value}));hout(rows.map(d=>({symbol:d.request.symbol,status:d.status,investable:d.investable,failures:d.failures,evidence:d.evidence,warnings:d.warnings})))}catch(e){hout(e.message)}}
function hSettings(){return {contract:'stock-gplm.m5-backtest-settings.v1',evaluation_start:byId('hestart').value,evaluation_end:byId('heend').value,initial_capital:Number(byId('hcapital').value),max_open_positions:Number(byId('hmaxpos').value),max_position_fraction:Number(byId('hposfrac').value),max_gross_exposure:Number(byId('hexposure').value),buy_statutory_bps:Number(byId('hbuy').value),sell_statutory_bps:Number(byId('hsell').value),cost_basis:byId('hbasis').value}}
async function hBacktest(){try{let [id,v]=selected();hout('Running...');hout(await post('/api/m5/backtests',{strategy_id:id,version:Number(v),symbols:hsymbols(),start:byId('hstart').value,end:byId('hend').value,mode:byId('hmode').value,settings:hSettings()}))}catch(e){hout(e.message)}}
async function hRuns(){try{hout(await api('/api/m5/runs'))}catch(e){hout(e.message)}}
async function hCompare(){try{hout(await post('/api/m5/compare',{run_ids:byId('hruns').value.split(',').map(x=>x.trim()).filter(Boolean)}))}catch(e){hout(e.message)}}
async function hExport(){try{let id=byId('hruns').value.split(',')[0].trim();hout((await api('/api/m5/runs/'+id)).markdown)}catch(e){hout(e.message)}}
refresh().catch(e=>out(e.message));
</script></body></html>"""


class StudioHandler(BaseHTTPRequestHandler):
    registry: StrategyRegistry
    research: Any = None  # stock_gplm.m5.studio_api.HistoricalResearchService when configured
    auth_token: str = ""

    def _research(self) -> Any:
        if self.research is None:
            raise LookupError("historical research is not configured; start with --m5-evidence")
        return self.research

    def _trusted_origin(self) -> bool:
        """Reject remote peers, DNS rebinding and cross-site requests to the loopback API.

        The peer address is checked first; ``Host`` and ``Origin`` are defence in depth
        and are never sufficient on their own because a client can set them freely.
        """
        if not is_loopback_address(self.client_address[0] if self.client_address else None):
            return False
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin in {f"http://{host}" for host in allowed}
        return self.headers.get("Sec-Fetch-Site") in {None, "same-origin", "none"}

    def _authenticated(self) -> bool:
        value = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return bool(self.auth_token) and value.startswith(prefix) and hmac.compare_digest(
            value[len(prefix):], self.auth_token)

    def _bootstrap_authenticated(self) -> bool:
        supplied = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return bool(self.auth_token) and hmac.compare_digest(supplied, self.auth_token)

    def _json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if size < 1 or size > MAX_REQUEST_BYTES:
            raise ValueError("request body size is invalid")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def _document(self, body: dict[str, Any]):
        if set(body) != {"format", "document"} or not isinstance(body["document"], str):
            raise ValueError("body must contain only format and document")
        return validate_strategy(load_document(body["document"], str(body["format"])))

    def do_GET(self) -> None:  # noqa: N802
        if not self._trusted_origin():
            self._json(403, {"error": "untrusted Host or Origin"})
            return
        path = urlparse(self.path).path
        if path == "/":
            if not self._bootstrap_authenticated():
                self._json(403, {"error": "authentication required"})
                return
        elif not self._authenticated():
            self._json(403, {"error": "authentication required"})
            return
        try:
            if path == "/":
                payload = _HTML.replace("__STUDIO_AUTH_TOKEN_JSON__", json.dumps(self.auth_token)).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/api/strategies":
                self._json(200, {"strategies": [asdict(value) | {"config": None} for value in self.registry.list_versions()]})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "strategies"]:
                record = self.registry.get(parts[2], int(parts[3]))
                self._json(200, {"record": asdict(record), "document": dump_yaml(record.config)})
                return
            if path == "/api/m5/datasets":
                self._json(200, self._research().datasets())
                return
            if path == "/api/m5/runs":
                self._json(200, self._research().runs())
                return
            if len(parts) == 4 and parts[:3] == ["api", "m5", "runs"]:
                self._json(200, self._research().report(parts[3]))
                return
            self._json(404, {"error": "not found"})
        except LookupError as exc:
            self._json(503 if not isinstance(exc, KeyError) else 404, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        if not self._trusted_origin():
            self._json(403, {"error": "untrusted Host or Origin"})
            return
        if not self._authenticated():
            self._json(403, {"error": "authentication required"})
            return
        path = urlparse(self.path).path
        try:
            if path in {"/api/validate", "/api/export", "/api/strategies"}:
                strategy = self._document(self._body())
                if path == "/api/validate":
                    self._json(200, {"valid": True, "config_hash": strategy.config_hash, "config": strategy.config})
                elif path == "/api/export":
                    query = parse_qs(urlparse(self.path).query)
                    format_name = query.get("format", ["yaml"])[0]
                    document = dump_json(strategy.config) if format_name == "json" else dump_yaml(strategy.config)
                    self._json(200, {"format": format_name, "document": document, "config_hash": strategy.config_hash})
                else:
                    self._json(201, asdict(self.registry.save(strategy)))
                return
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[:2] == ["api", "strategies"] and parts[4] == "activate":
                self._json(200, asdict(self.registry.activate(parts[2], int(parts[3]))))
                return
            if len(parts) == 4 and parts[:2] == ["api", "strategies"] and parts[3] == "deactivate":
                self.registry.deactivate(parts[2])
                self._json(200, {"strategy_id": parts[2], "activation_status": "INACTIVE"})
                return
            if path == "/api/backtests":
                body = self._body()
                if set(body) != {"strategy_id", "version", "candles", "data_quality_eligible"}:
                    raise ValueError("backtest request fields are invalid")
                record = self.registry.get(str(body["strategy_id"]), int(body["version"]))
                result = run_backtest(record.config, body["candles"], strategy_version=record.version,
                                      data_quality_eligible=body["data_quality_eligible"] is True)
                self.registry.record_backtest(result)
                self._json(201, result.to_dict())
                return
            if path == "/api/m5/eligibility":
                self._json(200, self._research().eligibility(self._body()))
                return
            if path == "/api/m5/backtests":
                self._json(*self._research().backtest(self._body()))
                return
            if path == "/api/m5/compare":
                self._json(200, self._research().compare(self._body()))
                return
            self._json(404, {"error": "not found"})
        except FileExistsError as exc:
            self._json(409, {"error": str(exc)})
        except (json.JSONDecodeError, KeyError, StrategyValidationError, TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
        except LookupError as exc:
            self._json(503, {"error": str(exc)})
        except sqlite3.Error as exc:
            self._json(409, {"error": f"registry rejected the change: {exc}"})

    def log_message(self, format: str, *args: object) -> None:
        return


def _write_launch_token(path: Path, token: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def make_server(registry_path: Path, host: str = "127.0.0.1", port: int = 8765, *,
                m5_evidence: Path | None = None, raw_root: Path | None = None,
                m5_reports: Path | None = None, token_path: Path | None = None) -> LoopbackHTTPServer:
    """Build the Studio server; any non-loopback bind is refused before a socket is opened."""
    if not isinstance(host, str) or host not in LOOPBACK_BIND:
        raise ValueError(f"Strategy Studio binds only to {sorted(LOOPBACK_BIND)}; refused {host!r}")
    registry = StrategyRegistry(registry_path)
    os.chmod(Path(registry_path), 0o600)
    Path(registry_path).chmod(0o600)
    research = None
    if m5_evidence is not None:
        # Imported lazily so the V1 engine itself never depends on the M5 data layer.
        from stock_gplm.m5.evidence import load_reference_evidence
        from stock_gplm.m5.studio_api import HistoricalResearchService

        if raw_root is None or m5_reports is None:
            raise ValueError("--m5-evidence requires --raw-root and --m5-reports")
        Path(m5_reports).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(Path(m5_reports), 0o700)
        evidence = load_reference_evidence(m5_evidence)
        research = HistoricalResearchService(raw_root, evidence, m5_reports, registry)
    token = secrets.token_urlsafe(32)
    handler = type("BoundStudioHandler", (StudioHandler,), {
        "registry": registry, "research": research, "auth_token": token})
    server = LoopbackHTTPServer((LOOPBACK_BIND[host], port), handler)
    if not is_loopback_address(server.server_address[0]):
        server.server_close()
        raise ValueError(f"Strategy Studio bound to a non-loopback address {server.server_address[0]!r}")
    launch_token_path = Path(token_path) if token_path is not None else Path(registry_path).with_suffix(".studio-token")
    try:
        _write_launch_token(launch_token_path, token)
    except BaseException:
        server.server_close()
        raise
    server.token_path = launch_token_path
    return server


def serve(registry_path: Path, host: str = "127.0.0.1", port: int = 8765, *,
          m5_evidence: Path | None = None, raw_root: Path | None = None, m5_reports: Path | None = None,
          token_path: Path | None = None) -> None:
    """Serve the Studio on loopback only (see :func:`make_server`)."""
    server = make_server(registry_path, host, port, m5_evidence=m5_evidence, raw_root=raw_root,
                         m5_reports=m5_reports, token_path=token_path)
    print(f"Strategy Studio: http://{host}:{server.server_address[1]}/?token=<read-owner-only-token-file> "
          f"(token file: {server.token_path})")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="local SQLite registry path")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--m5-evidence", type=Path, help="stock-gplm.m5-evidence-paths.v1 file")
    parser.add_argument("--raw-root", type=Path, help="published Shoonya NSE daily root (read only)")
    parser.add_argument("--m5-reports", type=Path, help="new or existing directory for immutable run reports")
    parser.add_argument("--token-file", type=Path, help="owner-only per-launch token file")
    args = parser.parse_args()
    serve(args.registry, args.host, args.port, m5_evidence=args.m5_evidence, raw_root=args.raw_root,
          m5_reports=args.m5_reports, token_path=args.token_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
