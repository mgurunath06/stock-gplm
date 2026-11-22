"""Interactive candle chart of one stock for the owner to mark their own trades (entry, SL, square-off, reason, comment).

    $env:PYTHONPATH="src"; python -m stock_gplm.app.owner_chart --symbol RELIANCE --tf 15m

Writes <reports>/tuning/owner-strategy-<tf>-<SYM>/chart.html (open it in the browser; needs internet once for the
chart library). Shows candles + EMA 13/20/200, the EMA crosses (small dots) and the owner's rows of owner_trades.csv -
NOT the program's trades (owner 2026-11-22: mark without seeing them). Click a candle -> menu there: Buy / Short / Skip,
then SL candle, then square off with reason buttons + comment; Undo / Redo (Ctrl+Z / Ctrl+Y); x deletes a trade.
Kept in the browser; Export writes owner_trades-<SYM>-<tf>.csv
in the owner_trades.csv format - put it in config/research/ and owner_strategy checks it trade by trade.
Times = TradingView candle START (IST).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import numpy as np

from .owner_strategy import REPO, SPEC, OwnerStrategy, owner_rows  # noqa: F401
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
    candles = [[T[i], r2(m.open[i]), r2(m.high[i]), r2(m.low[i]), r2(m.close[i]), int(m.volume[i])] for i in range(a, n)]
    emas = {}
    for p in (13, 20, 200):
        e = _ema_of(m, p)
        emas[str(p)] = [[T[i], r2(e[i])] for i in range(a, n) if np.isfinite(e[i])]
    from .owner_ml import _rsi  # owner 2026-11-22: show RSI(3) (own panel; follows replay like the EMAs)
    rsi = _rsi(np.asarray(m.close, dtype=float), 3)
    emas["rsi3"] = [[T[i], round(float(rsi[i]), 1)] for i in range(max(a, 3), n) if np.isfinite(rsi[i])]
    signals = []  # EMA crosses only - the program's trades are not shown (owner marks without seeing them)
    for side, arr in o.sig.items():
        signals += [[T[int(t)], side, o.kind[side].get(int(t), "")] for t in arr]
    rows = owner_rows(REPO / o.spec["check"]["owner_trades"], o.sym, o.tf)  # the original file only
    mine = []
    for r in rows:
        if r["signal"] and o.find(r["signal"]) is not None:
            mine.append({"time": _epoch(r["signal"]), "side": r["side"] or "long", "decision": r["decision"],
                         "exit": _epoch(r["exit"]) if r["exit"] and o.find(r["exit"]) is not None else None,
                         "why": r["why"]})
    from .strategy_engine import Exit
    rules = {side: Exit.of(o.exit_doc(side)).text(None, side == "short") for side in o.sig}
    return {"symbol": o.sym, "tf": o.tf, "candles": candles, "emas": emas, "signals": signals,
            "mine": mine, "rules": rules}


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 :root { --bg:#131722; --panel:#1e222d; --text:#d1d4dc; --muted:#8a8e99; --line:#2a2e39; --long:#26a69a; --short:#ef5350;
         --me:#ff9800; --file:#2962ff; }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--bg); color:var(--text); font:14px/1.45 system-ui, Segoe UI, sans-serif; }
 header { padding:8px 14px; display:flex; gap:16px; align-items:center; flex-wrap:wrap; border-bottom:1px solid var(--line); }
 header b { font-size:15px; }
 .leg span { margin-right:12px; white-space:nowrap; color:var(--muted); }
 .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:4px; vertical-align:middle; }
 body { display:flex; flex-direction:column; height:100vh; }
 #nav { display:flex; gap:4px; align-items:center; flex-wrap:wrap; padding:4px 10px; border-bottom:1px solid var(--line); }
 #nav input[type=date] { background:#131722; color:var(--text); border:1px solid #3a3f4b; border-radius:6px; padding:5px; font:inherit; color-scheme:dark; }
 #wrap { display:flex; flex:1; min-height:0; }
 #chart { flex:1; min-width:0; position:relative; }
 aside { width:360px; background:var(--panel); border-left:1px solid var(--line); padding:12px; overflow:auto; }
 button { background:#2a2e39; color:var(--text); border:1px solid #3a3f4b; border-radius:6px; padding:7px 11px;
          cursor:pointer; margin:3px 4px 3px 0; font:inherit; }
 button:hover { background:#363a45; } button:disabled { opacity:.35; cursor:default; }
 .big { font-size:15px; padding:9px 14px; }
 .bl { background:#1b3d39; border-color:var(--long); } .bs { background:#4a2222; border-color:var(--short); }
 .bm { background:#4a3415; border-color:var(--me); }
 .chip { border-radius:16px; padding:5px 11px; font-size:13px; }
 .chip.on { background:var(--me); color:#131722; border-color:var(--me); font-weight:600; }
 input[type=text] { width:100%; background:#131722; color:var(--text); border:1px solid #3a3f4b; border-radius:6px;
                    padding:7px 9px; font:inherit; }
 h3 { margin:14px 0 6px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
 .muted { color:var(--muted); } .pos { color:var(--long); } .neg { color:var(--short); }
 /* popup menu at the clicked candle */
 #pop { position:absolute; z-index:10; display:none; width:300px; background:var(--panel); border:1px solid var(--me);
        border-radius:10px; padding:12px; box-shadow:0 8px 28px rgba(0,0,0,.55); }
 #pop .t { font-weight:600; margin-bottom:6px; }
 #pop .x { position:absolute; right:8px; top:6px; background:none; border:none; font-size:18px; color:var(--muted); }
 .card { background:#131722; border:1px solid var(--line); border-radius:8px; padding:8px 10px; margin:6px 0; }
 .card.open { border-color:var(--me); }
 .card .row { display:flex; justify-content:space-between; gap:6px; align-items:center; }
 .card .del { background:none; border:none; color:var(--muted); padding:2px 6px; font-size:16px; margin:0; }
 .card .del:hover { color:var(--short); }
 .card input { margin-top:5px; padding:5px 7px; font-size:13px; }
 .lnk { color:#90caf9; cursor:pointer; text-decoration:underline; }
 #toast { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); background:#363a45; padding:8px 14px;
          border-radius:6px; display:none; z-index:20; }
 @media (max-width: 800px) { body { height:auto; } #wrap { flex-direction:column; height:auto; } #chart { height:70vh; } aside { width:auto; } }
</style></head><body>
<header><b>__TITLE__</b>
 <span class="leg"><span><span class="dot" style="background:#f5c542"></span>EMA13 x EMA20 / EMA200 cross</span>
 <span><span class="dot" style="background:var(--me)"></span>your trades</span>
 <span><span class="dot" style="background:var(--file)"></span>your trades file</span>
 <span>EMA 13 / 20 / 200 = blue / orange / white &middot; volume in the middle &middot; RSI(3) purple, bottom panel (30 / 70)</span></span>
 <label class="muted"><input type="checkbox" id="showSig" checked onchange="draw()"> show crosses</label>
</header>
<nav id="nav">
 <button onclick="moveTo(0)" title="Home">&#9198; Start</button>
 <button class="navB" data-d="&#9664;&#9664; " data-e="" onclick="step(-BIG)" title="PageUp"></button>
 <button class="navS" data-d="&#9664; " data-e="" onclick="step(-SMALL)" title="Shift + Left"></button>
 <button class="bm" onclick="cross(-1)" title="Left arrow">&#9664; Prev cross</button>
 <input type="date" onchange="goDate(this.value)" title="go to date">
 <b id="where" style="min-width:130px;text-align:center"></b>
 <button class="bm" onclick="cross(1)" title="Right arrow">Next cross &#9654;</button>
 <button class="navS" data-d="" data-e=" &#9654;" onclick="step(SMALL)" title="Shift + Right"></button>
 <button class="navB" data-d="" data-e=" &#9654;&#9654;" onclick="step(BIG)" title="PageDown"></button>
 <button onclick="moveTo(N - 1)" title="End">Latest &#9197;</button>
 <span class="muted" style="font-size:12px">Keys: &larr; &rarr; cross &middot; Shift+&larr;&rarr; day &middot; PgUp/PgDn week &middot; Home/End</span>
</nav>
<nav id="rbar" style="border-bottom:1px solid var(--line)">
 <button class="big bm" id="rStart" onclick="startReplay()" title="hide everything after the candle in the middle of the screen">&#9654; Replay from here (no peeking)</button>
 <span id="rOn" style="display:none">
  <b style="color:#ff9800">REPLAY</b> <span class="muted">now:</span> <b id="rNow"></b>
  <button class="big bm" onclick="rStep(1)" title="Right arrow / Space">Next candle &#9654;</button>
  <button onclick="rStep(4)" title="Shift + Right">+4 &#9654;&#9654;</button>
  <button onclick="rNextCross()" title="C">To next cross &#9197;</button>
  <button onclick="rStep(-1)" title="Left arrow">&#9664; Back</button>
  <button onclick="stopReplay()">Exit replay</button>
  <span class="muted" style="font-size:12px">Keys: &rarr; / Space next candle &middot; Shift+&rarr; +4 &middot; C next cross &middot; &larr; back.
   Enter / square off only on the newest candle; SL on any candle you can see.</span>
 </span>
</nav>
<div id="wrap"><div id="chart"><div id="pop"></div></div>
<aside>
 <div style="font-size:15px"><b>Click any candle</b> - a menu opens there.</div>
 <div class="muted">Buy / Short at the entry candle, then click the candle for the SL, then the candle to square off.
  Scroll / drag to move, mouse wheel to zoom.</div>
 <div id="saved" style="color:#26a69a;margin-top:6px;font-weight:600">&#10003; Saved</div>
 <div style="margin-top:8px"><button class="big" id="undoB" onclick="undo()">&#8630; Undo</button><button class="big" id="redoB" onclick="redo()">Redo &#8631;</button>
  <span class="muted">Ctrl+Z / Ctrl+Y</span></div>
 <h3>Your trades (<span id="nm">0</span>)</h3>
 <div id="list"></div>
 <div style="margin-top:8px"><button class="big bm" onclick="exportCsv()">Export CSV</button></div>
 <div class="muted">Everything you click or type is saved in this browser at once (watch the green tick). Export gives owner_trades-SYMBOL-TF.csv - put it in config\research.</div>
</aside></div>
<div id="toast"></div>
<script>
const D = __DATA__;
const KEY = 'owner-trades-v3-' + D.symbol + '-' + D.tf;
const REASONS = ['Target EMA 200', 'EMA 20 breach', 'Stop hit', 'Trailing stop', 'End of day', 'Gut feel', 'Other'];
let T = [];  // your trades: {id, side: long|short|skip, entry, sl, exit, reason, why, exitWhy}
try { T = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { T = []; }
let past = [], future = [];
function snap() { past.push(JSON.stringify(T)); if (past.length > 200) past.shift(); future = []; }
function store() {  // write to the browser + show the tick; no redraw (keeps the cursor in the box you type in)
  let ok = true;
  try { localStorage.setItem(KEY, JSON.stringify(T)); localStorage.setItem(KEY + '-draft', draft); } catch (e) { ok = false; }
  const el = document.getElementById('saved');
  el.innerHTML = ok ? '&#10003; Saved ' + new Date().toTimeString().slice(0, 8) : '&#9888; NOT saved - use Export CSV now';
  el.style.color = ok ? '#26a69a' : '#ef5350';
}
function save() { store(); draw(); }
let draft = '';  // comment typed in the menu before Buy / Short / Skip - kept even if the menu is closed
try { draft = localStorage.getItem(KEY + '-draft') || ''; } catch (e) {}
function undo() { if (!past.length) return toast('Nothing to undo'); future.push(JSON.stringify(T)); T = JSON.parse(past.pop()); save(); closePop(); toast('Undone'); }
function redo() { if (!future.length) return; past.push(JSON.stringify(T)); T = JSON.parse(future.pop()); save(); closePop(); }
function toast(s) { const t = document.getElementById('toast'); t.textContent = s; t.style.display = 'block';
  clearTimeout(toast.h); toast.h = setTimeout(() => t.style.display = 'none', 1400); }
const fmt = t => new Date(t * 1000).toISOString().slice(0, D.tf === '1d' ? 10 : 16).replace('T', ' ');
const esc = s => String(s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const chartEl = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartEl, {
  layout: { background: { color: '#131722' }, textColor: '#d1d4dc' },
  grid: { vertLines: { color: '#1f2330' }, horzLines: { color: '#1f2330' } },
  timeScale: { timeVisible: D.tf !== '1d', secondsVisible: false, rightOffset: 5 },
  crosshair: { mode: 0 }, autoSize: true });
const cs = chart.addCandlestickSeries({ upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
  wickUpColor: '#26a69a', wickDownColor: '#ef5350' });
cs.setData(D.candles.map(c => ({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] })));
const EC = { '13': '#2962ff', '20': '#ff9800', '200': '#e0e0e0', 'rsi3': '#ce93d8' };
const ES = {};
// price in the top 75%, RSI(3) in its own panel at the bottom (own scale 0..100, 30 / 70 lines)
cs.priceScale().applyOptions({ scaleMargins: { top: 0.04, bottom: 0.40 } });
// volume bars in the middle band (own scale), coloured with the candle; follows replay
const vbar = c => ({ time: c[0], value: c[5], color: c[4] >= c[1] ? 'rgba(38,166,154,0.55)' : 'rgba(239,83,80,0.55)' });
const VS = chart.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false,
  priceLineVisible: false });
chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.62, bottom: 0.22 } });
VS.setData(D.candles.map(vbar));
for (const p in D.emas) {
  const R = p === 'rsi3';
  ES[p] = chart.addLineSeries({ color: EC[p], lineWidth: p === '200' ? 2 : 1, priceLineVisible: false,
    lastValueVisible: R, crosshairMarkerVisible: R, priceScaleId: R ? 'rsi' : 'right', title: R ? 'RSI(3)' : '',
    autoscaleInfoProvider: R ? () => ({ priceRange: { minValue: 0, maxValue: 100 } }) : undefined });
  ES[p].setData(D.emas[p].map(x => ({ time: x[0], value: x[1] })));
  if (R) {
    chart.priceScale('rsi').applyOptions({ scaleMargins: { top: 0.80, bottom: 0.02 } });
    ES[p].createPriceLine({ price: 70, color: '#787b86', lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: '' });
    ES[p].createPriceLine({ price: 30, color: '#787b86', lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: '' });
  }
}
const emaAt = {}; for (const p in D.emas) { emaAt[p] = {}; D.emas[p].forEach(x => emaAt[p][x[0]] = x[1]); }
const byTime = {}, idx = {}; D.candles.forEach((c, i) => { byTime[c[0]] = c; idx[c[0]] = i; });
const e200 = {}, e20 = {}; D.emas['200'].forEach(x => e200[x[0]] = x[1]); D.emas['20'].forEach(x => e20[x[0]] = x[1]);
const sigAt = {}; D.signals.forEach(s => (sigAt[s[0]] = sigAt[s[0]] || []).push(s));

const entryPx = x => { const i = idx[x.entry]; return i != null && i + 1 < D.candles.length ? D.candles[i + 1][1] : null; };
const slPx = x => { const c = byTime[x.sl]; return c ? (x.side === 'short' ? c[2] : c[3]) : null; };
function result(x) {
  if (!x.exit || x.side === 'skip') return null;
  const e = entryPx(x), c = byTime[x.exit]; if (!e || !c) return null;
  let p = c[4];  // square-off price: the SL if it was hit on that candle, else the candle close
  const s = slPx(x); if (x.reason === 'Stop hit' && s != null) p = s;
  return Math.round(10000 * (x.side === 'short' ? (e - p) / e : (p - e) / e)) / 100;
}
const openTrade = () => T.find(x => x.side !== 'skip' && (!x.sl || !x.exit));
function guessReason(x, t) {
  const c = byTime[t], S = x.side === 'short', s = slPx(x), a = e200[t], b = e20[t], e = entryPx(x), ai = e200[x.entry];
  if (s != null && (S ? c[2] >= s : c[3] <= s)) return 'Stop hit';
  if (a && ai && e && (S ? e > ai : e < ai) && (S ? c[3] <= a * 1.001 : c[2] >= a * 0.999)) return 'Target EMA 200';
  if (b && (S ? c[2] >= b : c[3] <= b)) return 'EMA 20 breach';
  if (D.tf !== '1d' && fmt(t).slice(11) >= '15:00') return 'End of day';
  return 'Other';
}

// ---------------------------------------------------------------- popup menu at the clicked candle
const pop = document.getElementById('pop');
let popT = null, popReason = null;
function closePop() { pop.style.display = 'none'; popT = null; }
chart.subscribeClick(p => {
  if (!p.time || !p.point) return closePop();
  popT = p.time;
  const c = byTime[p.time], o = openTrade();
  const newest = RP == null || p.time === D.candles[RP][0];  // replay: enter / square off only on the newest candle
  const sig = (sigAt[p.time] || []).map(s => `${s[1]} cross ${s[2].replace('-milestone', '')}`).join(', ');
  let h = `<button class="x" onclick="closePop()">&times;</button><div class="t">${fmt(p.time)}</div>` +
    `<div class="muted" style="font-size:12px">O ${c[1]} H ${c[2]} L ${c[3]} C ${c[4]}${sig ? '<br>' + sig : ''}</div>`;
  const here = T.filter(x => [x.entry, x.sl, x.exit].includes(p.time));
  if (o && !o.sl && p.time !== o.entry) {
    h += `<div style="margin-top:8px">Your open <b>${o.side}</b> from ${fmt(o.entry)}:</div>` +
      `<button class="big bm" onclick="setSL()">SL here (${o.side === 'short' ? 'high ' + c[2] : 'low ' + c[3]})</button>`;
  } else if (o && o.sl && !o.exit && p.time > o.entry && !newest) {
    h += `<div style="margin-top:8px;color:#ff9800">Replay: square off only on the newest candle (${fmt(D.candles[RP][0])}). ` +
      `Press &rarr; to move on.</div>`;
  } else if (o && o.sl && !o.exit && p.time > o.entry) {
    popReason = guessReason(o, p.time);
    h += `<div style="margin-top:8px">Square off your <b>${o.side}</b> from ${fmt(o.entry)} here - why?</div><div id="chips">` +
      REASONS.map(r => `<button class="chip${r === popReason ? ' on' : ''}" onclick="pickReason(this,'${r}')">${r}</button>`).join('') +
      `</div><input type="text" id="pw" placeholder="why you exit (optional)" value="${esc(o.exitWhy)}"><button class="big bm" style="margin-top:6px" onclick="setExit()">Square off here</button>`;
  } else if (!newest) {
    h += `<div style="margin-top:8px;color:#ff9800">Replay: enter only on the newest candle (${fmt(D.candles[RP][0])}). ` +
      `Press &rarr; to move on.</div>`;
  } else {
    h += `<div style="margin-top:8px"><button class="big bl" onclick="newTrade('long')">Buy here</button>` +
      `<button class="big bs" onclick="newTrade('short')">Short here</button><button class="big" onclick="newTrade('skip')">Skip</button></div>` +
      `<input type="text" id="pw" placeholder="why? (optional) e.g. broke PDH, too far from EMA 200" value="${esc(draft)}">`;
  }
  if (here.length) h += `<div style="margin-top:8px"><button onclick="removeHere()">Remove my mark on this candle</button></div>`;
  if (o) h += `<div class="muted" style="margin-top:6px;font-size:12px">Esc = close. Undo = Ctrl+Z.</div>`;
  pop.innerHTML = h;
  pop.style.display = 'block';
  const W = chartEl.clientWidth, H = chartEl.clientHeight;
  pop.style.left = Math.min(Math.max(8, p.point.x + 14), W - 310) + 'px';
  pop.style.top = Math.min(Math.max(8, p.point.y - 40), H - pop.offsetHeight - 8) + 'px';
  const inp = document.getElementById('pw');
  if (inp) inp.oninput = () => {  // every keystroke is stored
    if (o && o.sl && !o.exit && p.time > o.entry) o.exitWhy = inp.value; else draft = inp.value;
    store();
  };
  if (inp) { inp.focus(); inp.onkeydown = e => { if (e.key === 'Enter') { const b = pop.querySelector('.big.bm,.big.bl'); if (b) b.click(); } }; }
});
function pickReason(el, r) { popReason = r; document.querySelectorAll('#chips .chip').forEach(b => b.classList.remove('on')); el.classList.add('on'); }
const pw = () => (document.getElementById('pw') || {}).value ? document.getElementById('pw').value.trim() : '';
function newTrade(side) {
  const o = openTrade();
  if (o && side !== 'skip') { toast('Finish or remove your open trade first'); return; }
  snap(); T.push({ id: Date.now(), side, entry: popT, sl: null, exit: null, reason: '', why: pw(), exitWhy: '',
    mode: RP == null ? 'hindsight' : 'replay' });
  draft = '';
  save(); closePop(); toast(side === 'skip' ? 'Skip saved' : 'Entry saved - now click the SL candle');
}
function setSL() { const o = openTrade(); snap(); o.sl = popT; save(); closePop(); toast('SL saved - now click the square-off candle'); }
function setExit() { const o = openTrade(); snap(); o.exit = popT; if (RP == null) o.mode = 'hindsight'; o.reason = popReason || 'Other'; o.exitWhy = pw(); save(); closePop();
  toast('Trade saved: ' + (result(o) ?? '') + '%'); }
function removeHere() {
  snap();
  T.forEach(x => { if (x.exit === popT) { x.exit = null; x.reason = ''; x.exitWhy = ''; } else if (x.sl === popT) { x.sl = null; } });
  T = T.filter(x => x.entry !== popT);
  save(); closePop();
}
function delTrade(id) { snap(); T = T.filter(x => x.id !== id); save(); toast('Trade deleted (Undo to bring it back)'); }
let typing = null;  // one undo step per box you type in, not one per letter
function editNote(id, field, v) {
  const x = T.find(y => y.id === id); if (!x) return;
  if (typing !== id + field) { snap(); typing = id + field; }
  x[field] = v; store();
}
function go(t) { const i = idx[t]; chart.timeScale().setVisibleLogicalRange({ from: i - 80, to: i + 40 }); }

// ---------------------------------------------------------------- drawing
let slLine = null;
function draw() {
  const mk = [];
  if (document.getElementById('showSig').checked)
    D.signals.forEach(s => mk.push({ time: s[0], position: s[1] === 'long' ? 'belowBar' : 'aboveBar', color: '#f5c542', shape: 'circle', size: 0.4 }));
  D.mine.forEach(m => mk.push({ time: m.time, position: 'belowBar', color: m.decision === 'skip' ? '#9e9e9e' : '#2962ff',
    shape: m.decision === 'skip' ? 'circle' : 'arrowUp', text: m.decision === 'skip' ? 'file: skip' : 'file' }));
  T.forEach(x => {
    const S = x.side === 'short';
    if (x.side === 'skip') { mk.push({ time: x.entry, position: 'belowBar', color: '#9e9e9e', shape: 'circle', text: 'skip' }); return; }
    mk.push({ time: x.entry, position: S ? 'aboveBar' : 'belowBar', color: '#ff9800', shape: S ? 'arrowDown' : 'arrowUp', text: S ? 'SHORT' : 'BUY' });
    if (x.sl) mk.push({ time: x.sl, position: S ? 'aboveBar' : 'belowBar', color: '#ef5350', shape: 'circle', size: 0.7, text: 'SL' });
    if (x.exit) { const r = result(x); mk.push({ time: x.exit, position: S ? 'belowBar' : 'aboveBar', color: (r || 0) > 0 ? '#26a69a' : '#ef5350',
      shape: 'square', size: 0.8, text: 'OUT ' + (r != null ? (r > 0 ? '+' : '') + r + '%' : '') }); }
  });
  const lim = RP == null ? Infinity : D.candles[RP][0];
  mk.sort((a, b) => a.time - b.time);
  cs.setMarkers(mk.filter(m => m.time <= lim));
  const o = openTrade();
  if (slLine) { cs.removePriceLine(slLine); slLine = null; }
  if (o && o.sl) slLine = cs.createPriceLine({ price: slPx(o), color: '#ef5350', lineStyle: 2, title: 'your SL' });
  document.getElementById('undoB').disabled = !past.length;
  document.getElementById('redoB').disabled = !future.length;
  const L = T.slice().sort((a, b) => b.entry - a.entry);
  document.getElementById('nm').textContent = L.length;
  document.getElementById('list').innerHTML = L.map(x => {
    const r = result(x), open = x.side !== 'skip' && (!x.sl || !x.exit);
    const next = !x.sl ? 'click the SL candle' : 'click the square-off candle';
    return `<div class="card${open ? ' open' : ''}"><div class="row"><span><span class="lnk" onclick="go(${x.entry})">${fmt(x.entry)}</span> ` +
      `<b class="${x.side === 'long' ? 'pos' : x.side === 'short' ? 'neg' : 'muted'}">${x.side === 'long' ? 'BUY' : x.side.toUpperCase()}</b>` +
      `${x.mode === 'replay' ? ' <span title="marked in replay - no peeking" style="color:#26a69a;font-size:11px">REPLAY</span>' : ''}</span>` +
      `<span>${r != null && (RP == null || x.exit <= D.candles[RP][0]) ? `<b class="${r > 0 ? 'pos' : 'neg'}">${r > 0 ? '+' : ''}${r}%</b>` : ''}` +
      `<button class="del" title="delete" onclick="delTrade(${x.id})">&times;</button></span></div>` +
      (x.side === 'skip' ? '' : `<div class="muted" style="font-size:12px">SL ${x.sl ? fmt(x.sl).slice(5) + ' @ ' + slPx(x) : '-'} &middot; ` +
        `out ${x.exit ? fmt(x.exit).slice(5) + ' (' + esc(x.reason) + ')' : '-'}${open ? ` &middot; <span style="color:#ff9800">next: ${next}</span>` : ''}</div>`) +
      `<input type="text" value="${esc(x.why)}" placeholder="why you ${x.side === 'skip' ? 'skip' : 'enter'}..." oninput="editNote(${x.id},'why',this.value)" onblur="typing=null">` +
      (x.exit ? `<input type="text" value="${esc(x.exitWhy)}" placeholder="why you exit..." oninput="editNote(${x.id},'exitWhy',this.value)" onblur="typing=null">` : '') +
      `</div>`;
  }).join('') || '<div class="muted">None yet.</div>';
}
function exportCsv() {
  const q = s => '"' + String(s || '').replace(/"/g, '""') + '"';
  const dt = t => { if (!t) return ''; const s = fmt(t); return s.slice(8, 10) + '-' + s.slice(5, 7) + '-' + s.slice(0, 4) + s.slice(10); };
  const rows = T.slice().sort((a, b) => a.entry - b.entry).map(x => [D.symbol, D.tf,
    x.side === 'skip' ? (((sigAt[x.entry] || [])[0] || [0, 'long'])[1]) : x.side, dt(x.entry), x.side === 'skip' ? 'skip' : 'take',
    dt(x.sl), dt(x.exit), q(x.reason), q([x.why, x.exitWhy].filter(Boolean).join(' | exit: ')), x.mode || 'hindsight'].join(','));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([['symbol,tf,side,signal_candle,decision,stop_candle,exit_candle,exit_reason,why,mode'].concat(rows).join('\n') + '\n'], { type: 'text/csv' }));
  a.download = `owner_trades-${D.symbol}-${D.tf}.csv`; a.click();
}
document.addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
  else if ((e.ctrlKey || e.metaKey) && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); redo(); }
  else if (k === 'escape') closePop();
  if (e.target.tagName === 'INPUT') return;  // arrows move the cursor while you type
  if (RP != null) {  // replay keys
    if (k === 'arrowright' || k === ' ') { e.preventDefault(); return rStep(e.shiftKey ? 4 : 1); }
    if (k === 'arrowleft') { e.preventDefault(); return rStep(-1); }
    if (k === 'c') { e.preventDefault(); return rNextCross(); }
  }
  const nav = { arrowleft: () => cross(-1), arrowright: () => cross(1), pageup: () => step(-BIG), pagedown: () => step(BIG),
    home: () => moveTo(0), end: () => moveTo(D.candles.length - 1) };
  if (e.shiftKey && k === 'arrowleft') { e.preventDefault(); step(-SMALL); }
  else if (e.shiftKey && k === 'arrowright') { e.preventDefault(); step(SMALL); }
  else if (nav[k]) { e.preventDefault(); nav[k](); }
});

// ---------------------------------------------------------------- navigation
const N = D.candles.length, ts = chart.timeScale();
const dayStart = [];  // index of each session's first candle
D.candles.forEach((c, i) => { if (!i || fmt(c[0]).slice(0, 10) !== fmt(D.candles[i - 1][0]).slice(0, 10)) dayStart.push(i); });
const DAILY = D.tf === '1d';
const SMALL = DAILY ? 21 : 1, BIG = DAILY ? 250 : 5;  // intraday: 1 day / 1 week (sessions); daily: 1 month / 1 year (candles)
document.querySelectorAll('.navS').forEach(b => b.textContent = b.dataset.d + (DAILY ? 'Month' : 'Day') + b.dataset.e);
document.querySelectorAll('.navB').forEach(b => b.textContent = b.dataset.d + (DAILY ? 'Year' : 'Week') + b.dataset.e);
const crossIdx = [...new Set(D.signals.map(s => idx[s[0]]))].filter(i => i != null).sort((a, b) => a - b);
function centre() { const r = ts.getVisibleLogicalRange(); return r ? Math.round((r.from + r.to) / 2) : N - 1; }
function width() { const r = ts.getVisibleLogicalRange(); return r ? Math.max(40, r.to - r.from) : 200; }
function moveTo(i, mark) {
  i = Math.max(0, Math.min(N - 1, i)); const w = width();
  ts.setVisibleLogicalRange({ from: i - w / 2, to: i + w / 2 });
  document.getElementById('where').textContent = fmt(D.candles[i][0]);
  if (mark) { try { chart.setCrosshairPosition(D.candles[i][4], D.candles[i][0], cs); } catch (e) {} }
}
function step(n) {  // n sessions (intraday) or n candles (daily)
  const c = centre();
  if (DAILY) return moveTo(c + n);
  let d = 0; while (d + 1 < dayStart.length && dayStart[d + 1] <= c) d++;
  const off = c - dayStart[d], d2 = Math.max(0, Math.min(dayStart.length - 1, d + n));
  const end = d2 + 1 < dayStart.length ? dayStart[d2 + 1] - 1 : N - 1;
  moveTo(Math.min(dayStart[d2] + off, end));  // same time of day, n sessions away
}
function cross(dir) {
  const c = centre();
  const i = dir > 0 ? crossIdx.find(x => x > c + 1) : [...crossIdx].reverse().find(x => x < c - 1);
  if (i == null) return toast(dir > 0 ? 'No later cross' : 'No earlier cross');
  moveTo(i, true);
}
function goDate(v) {
  if (!v) return;
  const t = Date.parse(v + 'T00:00:00Z') / 1000;
  const i = D.candles.findIndex(c => c[0] >= t);
  moveTo(i < 0 ? N - 1 : i, true);
}
// ---------------------------------------------------------------- replay (no peeking)
const RKEY = KEY + '-replay';
let RP = null;  // index of the newest visible candle
const bar = c => ({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] });
function showUpTo(i) {
  const t = D.candles[i][0];
  cs.setData(D.candles.slice(0, i + 1).map(bar));
  VS.setData(D.candles.slice(0, i + 1).map(vbar));
  for (const p in ES) ES[p].setData(D.emas[p].filter(x => x[0] <= t).map(x => ({ time: x[0], value: x[1] })));
}
function rShow() {
  document.getElementById('rNow').textContent = fmt(D.candles[RP][0]);
  try { localStorage.setItem(RKEY, String(RP)); } catch (e) {}
  draw(); ts.scrollToRealTime();
}
function startReplay(at) {
  RP = Math.max(250, Math.min(N - 2, at != null ? at : centre()));
  showUpTo(RP);
  document.getElementById('rStart').style.display = 'none'; document.getElementById('rOn').style.display = '';
  closePop(); rShow(); toast('Replay: everything after ' + fmt(D.candles[RP][0]) + ' is hidden');
}
function stopReplay() {
  RP = null; try { localStorage.removeItem(RKEY); } catch (e) {}
  cs.setData(D.candles.map(bar));
  VS.setData(D.candles.map(vbar));
  for (const p in ES) ES[p].setData(D.emas[p].map(x => ({ time: x[0], value: x[1] })));
  document.getElementById('rStart').style.display = ''; document.getElementById('rOn').style.display = 'none';
  closePop(); draw();
}
function rStep(k) {
  if (RP == null) return;
  if (k < 0) { RP = Math.max(250, RP + k); showUpTo(RP); return rShow(); }
  for (let j = 0; j < k && RP < N - 1; j++) {
    RP++; const c = D.candles[RP];
    cs.update(bar(c)); VS.update(vbar(c));
    for (const p in ES) if (emaAt[p][c[0]] != null) ES[p].update({ time: c[0], value: emaAt[p][c[0]] });
  }
  closePop(); rShow();
}
function rNextCross() {
  if (RP == null) return;
  const i = crossIdx.find(x => x > RP);
  if (i == null) return toast('No later cross');
  rStep(i - RP);
}
ts.subscribeVisibleLogicalRangeChange(() => { const c = centre(); if (D.candles[c]) document.getElementById('where').textContent = fmt(D.candles[c][0]); });
draw();
ts.setVisibleLogicalRange({ from: N - (DAILY ? 250 : 150), to: N + 5 });
try { const r = localStorage.getItem(RKEY); if (r != null) startReplay(+r); } catch (e) {}  // resume where you stopped
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
        title = f"{o.sym} {o.tf} - mark your trades"
        out = o.du.root / "chart.html"
        out.write_text(PAGE.replace("__TITLE__", title).replace("__DATA__", json.dumps(data, separators=(",", ":"))),
                       encoding="utf-8")
        o.log(f"{len(data['candles']):,} candles, {len(data['mine'])} rows of your trades file -> {out}")
        o.log("open it in the browser: click a candle -> Buy / Short / Skip, then the SL candle, then square off; Export CSV")
        return 0
    finally:
        o.du.close()


if __name__ == "__main__":
    sys.exit(main())
