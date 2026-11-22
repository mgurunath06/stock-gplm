"""Interactive candle chart of one stock with the owner strategy's trades - for the owner to mark entries / skips.

    $env:PYTHONPATH="src"; python -m stock_gplm.app.owner_chart --symbol RELIANCE --tf 15m

Writes <reports>/tuning/owner-strategy-<tf>-<SYM>/chart.html (open it in the browser; needs internet once for the
chart library). Shows candles + EMA 13/20/200, every signal (small dots), every trade the program took with the default
exit of config/research/owner_strategy.json (arrow at the entry, square at the exit with the result), and the owner's
rows of owner_trades.csv. Click a candle -> "I'd go long / short here", "I'd skip this signal", "clear"; marks are kept
in the browser and exported with the Export button as owner_marks-<SYM>-<tf>.csv (put it in config/research/).
Times = TradingView candle START (IST).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .owner_strategy import REPO, SPEC, OwnerStrategy, owner_rows
from .tune import CONFIG


def _epoch(text: str) -> int:
    """Candle start text (IST) -> seconds, shown as-is by the chart (treated as UTC so the axis reads IST)."""
    fmt = "%Y-%m-%d %H:%M" if len(text) > 10 else "%Y-%m-%d"
    return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp())


def build(o: OwnerStrategy) -> dict:
    from .strategy_engine import _ema_of
    m, n = o.m, len(o.m)
    a = max(0, o.a0 - 250)  # some history before the first signal for context
    T = [_epoch(o.tv(i)) for i in range(n)]
    r2 = lambda x: round(float(x), 2)  # noqa: E731
    candles = [[T[i], r2(m.open[i]), r2(m.high[i]), r2(m.low[i]), r2(m.close[i])] for i in range(a, n)]
    emas = {}
    for p in (13, 20, 200):
        e = _ema_of(m, p)
        emas[str(p)] = [[T[i], r2(e[i])] for i in range(a, n) if np.isfinite(e[i])]
    signals, trades = [], []
    for side, arr in o.sig.items():
        signals += [[T[int(t)], side, o.kind[side].get(int(t), "")] for t in arr]
        for x in o.sim(side, arr):
            t, e = int(x["t"]), int(x["exit_index"])
            st = o.stop_of(side, t)
            trades.append({"side": side, "signal": T[t], "signal_txt": o.tv(t), "trigger": o.kind[side].get(t, ""),
                           "entry": r2(x["entry_price"]), "stop": r2(st[0]) if st else None,
                           "stop_txt": o.tv(st[1]) if st else "", "exit": T[e], "exit_txt": o.tv(e),
                           "exit_price": r2(x["exit_price"]), "how": x["exit_reason"],
                           "pct": round(100 * float(x["net_return"]), 2)})
    rows = owner_rows(REPO / o.spec["check"]["owner_trades"], o.sym, o.tf)
    mine = []
    for r in rows:
        if r["signal"] and o.find(r["signal"]) is not None:
            mine.append({"time": _epoch(r["signal"]), "side": r["side"] or "long", "decision": r["decision"],
                         "exit": _epoch(r["exit"]) if r["exit"] and o.find(r["exit"]) is not None else None,
                         "why": r["why"]})
    from .strategy_engine import Exit
    rules = {side: Exit.of(o.exit_doc(side)).text(None, side == "short") for side in o.sig}
    return {"symbol": o.sym, "tf": o.tf, "candles": candles, "emas": emas, "signals": signals, "trades": trades,
            "mine": mine, "rules": rules}


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 :root { --bg:#131722; --panel:#1e222d; --text:#d1d4dc; --muted:#787b86; --line:#2a2e39; --long:#26a69a; --short:#ef5350;
         --mine:#2962ff; --skip:#9e9e9e; }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--bg); color:var(--text); font:13px/1.4 system-ui, Segoe UI, sans-serif; }
 header { padding:8px 14px; display:flex; gap:14px; align-items:center; flex-wrap:wrap; border-bottom:1px solid var(--line); }
 header b { font-size:15px; }
 .leg span { margin-right:12px; white-space:nowrap; }
 .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:4px; vertical-align:middle; }
 #wrap { display:flex; height:calc(100vh - 46px); }
 #chart { flex:1; min-width:0; }
 aside { width:330px; background:var(--panel); border-left:1px solid var(--line); padding:12px; overflow:auto; }
 button { background:#2a2e39; color:var(--text); border:1px solid #363a45; border-radius:4px; padding:6px 9px;
          cursor:pointer; margin:3px 3px 3px 0; font:inherit; }
 button:hover { background:#363a45; }
 button.l { border-color:var(--long); } button.s { border-color:var(--short); } button.k { border-color:var(--skip); }
 h3 { margin:14px 0 6px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
 .box { background:#131722; border:1px solid var(--line); border-radius:4px; padding:8px; margin:6px 0; }
 textarea { width:100%; background:#131722; color:var(--text); border:1px solid var(--line); border-radius:4px; }
 table { width:100%; border-collapse:collapse; font-size:12px; }
 td { padding:2px 4px; border-bottom:1px solid var(--line); cursor:pointer; }
 .pos { color:var(--long); } .neg { color:var(--short); } .muted { color:var(--muted); }
 @media (max-width: 800px) { #wrap { flex-direction:column; height:auto; } #chart { height:70vh; } aside { width:auto; } }
</style></head><body>
<header><b>__TITLE__</b>
 <span class="leg"><span><span class="dot" style="background:#f5c542"></span>signal</span>
 <span><span class="dot" style="background:var(--long)"></span>our long</span>
 <span><span class="dot" style="background:var(--short)"></span>our short</span>
 <span><span class="dot" style="background:var(--mine)"></span>your trades file</span>
 <span><span class="dot" style="background:#ff9800"></span>your marks</span>
 <span class="muted">EMA 13 / 20 / 200 = blue / orange / white</span></span>
 <span id="stats" class="muted"></span>
</header>
<div id="wrap"><div id="chart"></div>
<aside>
 <div class="muted">Click a candle, then mark it. Scroll / drag to move, wheel to zoom.</div>
 <h3>Selected candle</h3><div id="sel" class="box muted">none</div>
 <div><button class="l" onclick="mark('long')">I'd go LONG here</button><button class="s" onclick="mark('short')">I'd go SHORT here</button></div>
 <div><button class="k" onclick="mark('skip')">I'd SKIP this signal</button><button onclick="mark(null)">Clear mark</button></div>
 <textarea id="note" rows="2" placeholder="why (optional) - e.g. no PDH break, too far from EMA 200"></textarea>
 <h3>Your marks (<span id="nm">0</span>)</h3>
 <div><button onclick="exportCsv()">Export CSV</button><button onclick="if(confirm('Delete all marks?')){marks={};save();draw();}">Clear all</button></div>
 <table id="mk"></table>
 <h3>Our trades</h3><table id="tr"></table>
 <h3>Exit rules</h3><div id="rules" class="box muted"></div>
</aside></div>
<script>
const D = __DATA__;
const KEY = 'owner-marks-' + D.symbol + '-' + D.tf;
let marks = {};
try { marks = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { marks = {}; }
function save() { try { localStorage.setItem(KEY, JSON.stringify(marks)); } catch (e) {} }
const fmt = t => new Date(t * 1000).toISOString().slice(0, D.tf === '1d' ? 10 : 16).replace('T', ' ');
const chart = LightweightCharts.createChart(document.getElementById('chart'), {
  layout: { background: { color: '#131722' }, textColor: '#d1d4dc' },
  grid: { vertLines: { color: '#1f2330' }, horzLines: { color: '#1f2330' } },
  timeScale: { timeVisible: D.tf !== '1d', secondsVisible: false, rightOffset: 5 },
  crosshair: { mode: 0 }, autoSize: true });
const cs = chart.addCandlestickSeries({ upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
  wickUpColor: '#26a69a', wickDownColor: '#ef5350' });
cs.setData(D.candles.map(c => ({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] })));
const EC = { '13': '#2962ff', '20': '#ff9800', '200': '#e0e0e0' };
for (const p in D.emas) chart.addLineSeries({ color: EC[p], lineWidth: p === '200' ? 2 : 1, priceLineVisible: false,
  lastValueVisible: false, crosshairMarkerVisible: false }).setData(D.emas[p].map(x => ({ time: x[0], value: x[1] })));
const byTime = {}; D.candles.forEach(c => byTime[c[0]] = c);
const sigAt = {}; D.signals.forEach(s => (sigAt[s[0]] = sigAt[s[0]] || []).push(s));
const trAt = {}; D.trades.forEach(t => (trAt[t.signal] = trAt[t.signal] || []).push(t));
const mineAt = {}; D.mine.forEach(m => mineAt[m.time] = m);
let selected = null, stopLine = null;
function draw() {
  const mk = [];
  const traded = new Set(D.trades.map(t => t.signal + t.side));
  D.signals.forEach(s => { if (!traded.has(s[0] + s[1])) mk.push({ time: s[0], position: s[1] === 'long' ? 'belowBar' : 'aboveBar',
    color: '#f5c542', shape: 'circle', size: 0.4 }); });
  D.trades.forEach(t => {
    const L = t.side === 'long';
    mk.push({ time: t.signal, position: L ? 'belowBar' : 'aboveBar', color: L ? '#26a69a' : '#ef5350',
      shape: L ? 'arrowUp' : 'arrowDown', text: (L ? 'L' : 'S') });
    mk.push({ time: t.exit, position: L ? 'aboveBar' : 'belowBar', color: t.pct > 0 ? '#26a69a' : '#ef5350',
      shape: 'square', size: 0.6, text: (t.pct > 0 ? '+' : '') + t.pct + '%' });
  });
  D.mine.forEach(m => mk.push({ time: m.time, position: 'belowBar', color: m.decision === 'skip' ? '#9e9e9e' : '#2962ff',
    shape: m.decision === 'skip' ? 'circle' : 'arrowUp', text: m.decision === 'skip' ? 'you: skip' : 'you' }));
  Object.entries(marks).forEach(([t, m]) => mk.push({ time: +t, position: m.mark === 'short' ? 'aboveBar' : 'belowBar',
    color: '#ff9800', shape: m.mark === 'long' ? 'arrowUp' : m.mark === 'short' ? 'arrowDown' : 'circle',
    text: m.mark === 'skip' ? 'skip' : m.mark }));
  mk.sort((a, b) => a.time - b.time);
  cs.setMarkers(mk);
  const keys = Object.keys(marks).map(Number).sort((a, b) => a - b);
  document.getElementById('nm').textContent = keys.length;
  document.getElementById('mk').innerHTML = keys.map(t => `<tr onclick="go(${t})"><td>${fmt(t)}</td><td>${marks[t].mark}</td>` +
    `<td class="muted">${(marks[t].note || '').replace(/</g, '&lt;')}</td></tr>`).join('');
}
function select(t) {
  selected = t; const c = byTime[t]; if (!c) return;
  let h = `<b>${fmt(t)}</b><br>O ${c[1]} H ${c[2]} L ${c[3]} C ${c[4]}`;
  (sigAt[t] || []).forEach(s => h += `<br>signal: <b>${s[1]}</b> ${s[2]}`);
  (trAt[t] || []).forEach(x => h += `<br>our ${x.side}: entry ${x.entry}, stop ${x.stop} (${x.stop_txt}), exit ${x.exit_txt} ` +
    `${x.how} <span class="${x.pct > 0 ? 'pos' : 'neg'}">${x.pct}%</span>`);
  if (mineAt[t]) h += `<br>your file: ${mineAt[t].decision} - ${mineAt[t].why}`;
  if (marks[t]) h += `<br>your mark: <b>${marks[t].mark}</b> ${marks[t].note || ''}`;
  document.getElementById('sel').innerHTML = h;
  document.getElementById('note').value = marks[t] ? (marks[t].note || '') : '';
  if (stopLine) { cs.removePriceLine(stopLine); stopLine = null; }
  const tr = (trAt[t] || [])[0];
  if (tr && tr.stop) stopLine = cs.createPriceLine({ price: tr.stop, color: '#ef5350', lineStyle: 2, title: 'our stop' });
}
function go(t) { const i = D.candles.findIndex(c => c[0] === t);
  chart.timeScale().setVisibleLogicalRange({ from: i - 80, to: i + 40 }); select(t); }
chart.subscribeClick(p => { if (p.time) select(p.time); });
function mark(kind) {
  if (selected == null) return;
  if (kind) marks[selected] = { mark: kind, note: document.getElementById('note').value.trim() };
  else delete marks[selected];
  save(); draw(); select(selected);
}
function exportCsv() {
  const q = s => '"' + String(s || '').replace(/"/g, '""') + '"';
  const keys = Object.keys(marks).map(Number).sort((a, b) => a - b);
  const lines = ['symbol,tf,candle,mark,signal_here,our_trade,note'].concat(keys.map(t => [D.symbol, D.tf, fmt(t), marks[t].mark,
    (sigAt[t] || []).map(s => s[1] + ' ' + s[2]).join(' / '), (trAt[t] || []).map(x => x.side + ' ' + x.pct + '%').join(' / '),
    q(marks[t].note)].join(',')));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([lines.join('\n') + '\n'], { type: 'text/csv' }));
  a.download = `owner_marks-${D.symbol}-${D.tf}.csv`; a.click();
}
document.getElementById('tr').innerHTML = D.trades.slice().sort((a, b) => b.signal - a.signal).map(t =>
  `<tr onclick="go(${t.signal})"><td>${t.signal_txt}</td><td>${t.side}</td><td class="${t.pct > 0 ? 'pos' : 'neg'}">${t.pct}%</td>` +
  `<td class="muted">${t.how}</td></tr>`).join('');
document.getElementById('rules').innerHTML = Object.entries(D.rules).map(([s, r]) => `<b>${s}</b>: ${r}`).join('<br><br>');
const w = D.trades.filter(t => t.pct > 0).length;
document.getElementById('stats').textContent = `${D.trades.length} trades, ${w} won (${Math.round(100 * w / Math.max(1, D.trades.length))}%), ` +
  `total ${D.trades.reduce((s, t) => s + t.pct, 0).toFixed(1)}%`;
draw();
const n = D.candles.length; chart.timeScale().setVisibleLogicalRange({ from: n - 300, to: n + 5 });
</script></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--tf", default=None)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    a.no_check = True
    o = OwnerStrategy(a.symbol, a)
    try:
        if not o.load():
            return 1
        data = build(o)
        title = f"{o.sym} {o.tf} - owner strategy trades"
        out = o.du.root / "chart.html"
        out.write_text(PAGE.replace("__TITLE__", title).replace("__DATA__", json.dumps(data, separators=(",", ":"))),
                       encoding="utf-8")
        o.log(f"{len(data['candles']):,} candles, {len(data['trades'])} trades, {len(data['mine'])} of your rows -> {out}")
        o.log("open it in the browser: click candles to mark long / short / skip, then Export CSV")
        return 0
    finally:
        o.du.close()


if __name__ == "__main__":
    sys.exit(main())
