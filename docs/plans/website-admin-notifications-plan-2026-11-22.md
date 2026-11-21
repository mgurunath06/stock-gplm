# Plan for 2026-11-22: website revamp, notifications, admin page, new strategy candidates

Status the night before: overnight chain running (Yahoo rebuild -> final 2026 walk-forward on all stocks and
timeframes -> 2010+ walk-forward). Results feed everything below.

## 1. Website (live_lab: stdlib HTTP server + static/index.html, live.html)

| Page | What it shows | Source |
|---|---|---|
| Picks (home) | Today's signals only from walk-forward ACTIVE combos: stock, TF, strategy, entry / stop / target from the learned exit, unseen record (recent avg, PF, trades), confidence | learning/current/active.json, exits.json, live bars |
| Stock | Per TF: which strategies work there, unseen record month by month, open setups, chart | unseen.csv, wf_trades.csv |
| Groups | Nifty 50 / Bank / 100 / 200 / 500, sector x TF: best strategies and picks | group_report, sector_tf.csv |
| Notifications | History of every alert with its outcome (target / stop / time) | notify.py events |
| Admin | See section 3 | |

Live lab restart needed anyway: it still runs the old library and ATR exits.

## 2. Notifications

Today: in-page only (notify.py: SETUP / OUTCOME / CONFLICT / BEHAVIOUR / SYSTEM). Add a push channel; owner picks:
Telegram bot (simplest, free, phone + desktop), ntfy.sh (no account), email (Gmail), browser push.
Rules: only walk-forward ACTIVE combos; one message per stock setup with entry / stop / target; outcome follow-up;
quiet hours; daily summary after close; login-expiry and data-gap warnings (SYSTEM).

## 3. Admin page (separate admin password)

* Strategy catalogue: all library strategies (id, Chartink name, rule text, TFs, side, exit type, status).
* "Where it works": heatmap of unseen results by sector x TF and index group, top stocks, recent vs older.
* Fine-tune per stock x TF (and per strategy): enable / disable, exit (max swing %, target x risk), ACTIVE
  thresholds (min trades, min PF, min confidence). Saved to config files the engine already reads
  (exits.json stocks.SYM.tf.strategies, confidence.json stocks.SYM.tf) plus a new overrides.json for
  enable / disable; every change logged (who, when, before / after).
* "Retest" button: queues a walk-forward for that stock x TF x strategy only (needs walk_forward --strategies,
  to build).
* Add strategy: paste rule (DSL) or Chartink text -> validation -> saved disabled until its walk-forward passes.

## 4. New strategy candidates from the web (to add to the library, judged by the walk-forward like the rest)

Evidence is mostly US and mostly backtests; nothing is assumed to work on NSE until the walk-forward says so.

| Candidate | Rule sketch | TF | Evidence |
|---|---|---|---|
| ORB "stocks in play" | 5-minute opening range breakout, only stocks whose first-5-minute volume is far above the same window's 14-day average; stop at the other side of the range, exit at the close | 5m | Zarattini, Barbon, Aziz 2024 (7,000 US stocks 2016-23, net of costs); QuantConnect replication; slippage at the open is the main risk |
| Short-term momentum vs reversal by turnover | Last-month winners keep winning among high-turnover stocks; last-month losers rebound among low-turnover stocks | 1d / 1w | CEPR DP15857 (short-term momentum) |
| Turn of the month | Long from the last trading day of a month to the 3rd day of the next | 1d | Long-running US evidence (S&P 500 1975-2024), described as stable |
| Internal bar strength | Buy when (close - low) / (high - low) < 0.2, exit when > 0.8 | 1d | Popular practitioner rule; no academic evidence found |
| RSI(2) pullback in an uptrend | Close above 200 SMA, RSI(2) < 10 -> buy; exit on close above 5 SMA | 1d | Practitioner (Connors); test only |

Caveat from the same research: published anomalies lose roughly a third of their return after publication, and
costs erase many of them.

## 5. Decisions needed from the owner

1. Push channel: Telegram / ntfy / email / browser.
2. Admin access: separate password on the same site, or PC-only.
3. Hosting: PC only (as today) or also reachable from the phone outside home.
4. Which candidates from section 4 to add first.
