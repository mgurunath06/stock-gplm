"""Dependency-free local Strategy Studio and JSON API.

The server binds to loopback by default. It has no broker client, credential
support, remote data loader, or order-routing endpoint.
"""
from __future__ import annotations

import argparse
import json
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
<script>
const out=x=>document.querySelector('#result').textContent=typeof x==='string'?x:JSON.stringify(x,null,2);
const byId=id=>document.getElementById(id);
let working=null;
async function api(path,options={}){let r=await fetch(path,options);let x=await r.json();if(!r.ok)throw new Error(x.error||r.statusText);return x}
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
refresh().catch(e=>out(e.message));
</script></body></html>"""


class StudioHandler(BaseHTTPRequestHandler):
    registry: StrategyRegistry

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
        path = urlparse(self.path).path
        try:
            if path == "/":
                payload = _HTML.encode("utf-8")
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
            self._json(404, {"error": "not found"})
        except (KeyError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
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
            self._json(404, {"error": "not found"})
        except (json.JSONDecodeError, KeyError, StrategyValidationError, TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(registry_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = type("BoundStudioHandler", (StudioHandler,), {"registry": StrategyRegistry(registry_path)})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Strategy Studio: http://{host}:{port} (local research only)")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="local SQLite registry path")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.registry, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
