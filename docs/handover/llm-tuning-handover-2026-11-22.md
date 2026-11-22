# LLM strategy tuning handover, 2026-11-22 (session 7)

Read this first, then `strategy-revamp-handover-2026-11-22.md` (session 6). Branch `feature/m6-prd-v1`.

## 0. Working rules (owner; break none)

* **Never run programs, tests or LLM test calls yourself.** Write code, `py_compile` it, give ONE PowerShell command
  at a time; the owner runs it and pastes output. Only exception: the API health check (`stock_gplm.llm.health`).
  Reading result files is fine.
* PowerShell prelude:
  ```powershell
  cd H:\Guru\Learning\StockGLPM\app
  $env:PYTHONPATH='src'; $env:PYTHONIOENCODING='utf-8'; $py='E:\Guru\Learning\Alekhya\venv\Scripts\python.exe'
  ```
* Data: use the BEST source. NSE website / archives are allowed when best (owner corrected me today - there was
  never a "no NSE website" rule). Be polite (paced, resumable, no re-downloads).
* Commit only the files you changed (`git add <files>`; never `git commit -a` - `universe.txt` and
  `config/strategies/exits.json` are deliberately uncommitted, owner decides).
* Short answers, plain English, readable strategy names + ids. Owner is often on mobile.

## 1. Running now (started by owner ~11:20)

1. **Tuning chain** `deploy/windows/tune_phase1.cmd`: health check -> local LLM (qwen3:14b) on 1d, 15m, 5m, 60m ->
   API (MWAPI claude-sonnet-5-5, StartupAPI fallback) same order. Log `app\reports\llm\tune_phase1.log`.
   Resumable: rerun the .cmd. Outputs per TF:
   * 1d: `H:\Guru\Learning\StockGLPM\state-long\reports\tuning\tune-1d\` (BASELINE.md, TUNING.md, candidates.json,
     state.json, llm.jsonl, prompts/, progress.log)
   * 15m/5m/60m: `H:\Guru\Learning\StockGLPM\state\reports\tuning\tune-<tf>\`
2. **NSE filings index** `python -m stock_gplm.app.nse_filings index` (~6 h, 2,378 stocks x 4 lists, 1.5 s pacing)
   -> `data\raw\nse-filings\<SYM>\*.json`, `data\derived\nse-filings\filings.csv`.

Owner was downloading **Qwen3.6-35B-A3B** for Ollama (MoE, ~3B active, fits with RAM offload). When ready: add
`--local-model <exact tag>` to the local lines of `tune_phase1.cmd` (or set `local.model` in
`config/tuning/tuning.json`). Exact tag unknown - ask.

## 2. LLM providers (all checked today)

`config/research/providers.json`, keys in `app\.env` (git-ignored; Windows mode check skipped in
`llm/envfile.py`). Health: `& $py -m stock_gplm.llm.health [--models] [--only x] [--model x=ID]`.

| Order | Provider | State |
|---|---|---|
| 1 | Ollama `qwen3:14b` (local) | OK, ~22-37 s per proposal |
| 2 | MWAPI `claude-sonnet-5-5` | OK |
| 2 | StartupAPI | key valid, lists models, but every chat = HTTP 503 (owner contacting them) |
| 3 | OpenRouter (last, costliest) | OK; `openrouter/auto` -> pick a fixed cheap model before using |

## 3. Tuning system (built today)

* `src/stock_gplm/app/tune.py` (orchestrator), `tune_eval.py` (persistent worker pool, each stock loaded once,
  same `simulate` + costs as walk_forward, entry features), `tune_llm.py` (prompts, provider calls, validation),
  `config/tuning/tuning.json`.
* Per TF: **baseline** (BASELINE.md, queue = highest TUNE win first; research-only shorts last) -> **local rounds**
  (3 x 6 variants) -> **exit sweep** (rr x max swing on best-win and best-profit entries) -> **api rounds** (2 x 5)
  -> sweep -> **gate**.
* Windows: 1d TUNE 2010..2024, CONFIRM 2025..; intraday TUNE Jan-Jul 2026, CONFIRM Aug-Nov 2026 (minute data
  only exists for 2026). CONFIRM is never shown to any LLM. Recency decay: 1d 500 trading days (half weight ~10
  months), intraday 100 (owner may change `decay`).
* Edits allowed: <= 2 per variant (AND filter clauses, light entry rewrite with similarity >= 0.75 and the
  liquidity floor kept, exit rr / max_swing_pct / lookback / max_hold). `bench()` allowed (Nifty 50 loaded),
  `ref_symbol`/`feature` denied.
* **Buckets** (owner: don't lose good non-80% strategies), on CONFIRM:
  * **A high win**: win_w >= 70%, PF_w >= 1.3, n_eff >= 8, beats the original's avg.
  * **B profitable**: PF_w >= 1.3, avg > 0, n_eff >= 8, beats the original, any win rate.
  * **B (original)**: untouched strategy already passes B -> `keep_original` in candidates.json.
  * **research only**: daily/weekly/monthly shorts (not executable in a cash account) - never A/B.
  * LLM asked for ~2/3 win-rate ideas, ~1/3 profit ideas; best-by-win and best-by-profit both tracked.
* Accepted variants -> `candidates.json` (strategy docs, ids `<parent>-T<n>`, tags `bucket:A|B`). NOT in the library
  until the owner approves.
* Spend cap $5 per TF (`api.max_usd`), logged in llm.jsonl.
* Expectation set with owner: ~100 trials per strategy -> some A/B passes will be luck; A/B = paper-trade
  candidates, not live.

Baseline facts (500 / 200 / 300 stocks): **no original strategy (0 of 91) is profitable on CONFIRM** (bucket B).
Daily strong in 2010-24 (top ~46% win, PF ~1.75) but 2025-26 ~32-38% win, PF ~1.0. Every intraday strategy
~-0.1%/trade (gross ~+0.1%, costs ~0.2% round trip eat it).

## 4. Other work today

* `stock_gplm.app.active_v2` (ACTIVE rule v2: n_eff >= 8, >= 2 profitable periods, PF cap 5; causal comparison).
  Result: cleans lists (long daily 4,160 -> 1,060) but no edge: 2026 PF 0.77, weekly negative under every rule.
  Daily: selecting on earlier UNSEEN trades (+0.77%, PF 1.21) beats walk-forward's train-rule selection (+0.27%).
* New strategies: `config/strategies/library/owner/web-candidates.json` (WEB-RSBreakout, WEB-ORB30, WEB-High52W,
  WEB-NR7, WEB-VWAPReclaim, WEB-VDUPullback, WEB-GapAndGo; house swing exit + spec trend exits as `when`).
  GN-StrongestKidInClass unblocked (official Nifty 50 from the NSE MCP is on disk 2024-01-01..; earlier = no signals).
  Library: 100 strategies, 0 invalid, 0 blocked.
* Official index history: `data\raw\nse-index\NSE` 153 indices, **only from 2024-01-01** (NSE MCP limit).
  NSE MCP has no fundamentals (36 tools listed by `stock_gplm.m39.cli discover`).
* Fundamentals: `stock_gplm.app.nse_filings` (probe/index/xbrl/summary; probe on RELIANCE OK: 130 quarterly,
  6 half-yearly, 39 annual, 23 shareholding rows, exact broadcast times) and `stock_gplm.app.nse_xbrl`
  (facts/build; first tag map, not run yet).
* Independent analyst review (pasted by owner) agreed: good research platform, no-trade signal set today. Its
  must-haves are in the to-do list below.

## 5. TO DO (priority order)

**LLM tuning (main track)**
1. Watch the chain; when 1d local finishes read `tune-1d\TUNING.md` + `progress.log`, report buckets A/B/rejected
   with readable names. Check prompts/ for bad proposals (e.g. price-unit thresholds) and tighten `tune_llm.py`.
2. Switch local model to Qwen3.6-35B-A3B once downloaded (exact tag from owner); compare proposal quality.
3. API phase runs after local; check MWAPI spend in llm.jsonl; StartupAPI still 503.
4. Optional final OpenRouter round only on the shortlist (`--phase final --final-model <id>`, owner picks model).
5. Owner approval flow for candidates.json -> library (new bundle file, readable names, parent kept).
6. Paper-trade A/B candidates FROZEN for a fixed period (no retuning) - live alerts observation only.
7. Lower `min_tune_trades` for the 6 skipped low-trade strategies (e.g. 15 min Volume Shockers 66% win, 93 trades)
   if owner wants them tried.

**Fundamentals (second track)**
8. After `nse_filings index` finishes: `nse_filings summary` -> estimate XBRL size/time -> `nse_filings xbrl
   --since 2016-01-01`.
9. Run `nse_xbrl facts --file <sample>` on 2-3 files (results Ind-AS, bank, shareholding) -> fix FIELD_TAGS /
   SHP tags -> `nse_xbrl build`.
10. Point-in-time fundamentals access for strategies (value usable only after `filed_at`), then the hybrid
    strategies (CAN SLIM+VCP, PEAD first - they need quarterly EPS + result dates).
11. Portfolio/rebalance backtester for pure fundamental strategies (Coffee Can, GARP, Magic Formula, Op-leverage);
    survivorship: universe is today's listings - add delisted where possible.
12. Order book: LLM extraction from NSE announcements (later).
13. Optional Yahoo fundamentals snapshot archiver (current P/E, market cap; dated snapshots build our own
    point-in-time history) - owner has not said yes yet.

**Analyst must-haves before any live trading**
14. Multiple-testing correction in selection; select at universe/portfolio level, per-stock only as a filter.
15. Portfolio simulation: sizing, simultaneous-signal limits, sector concentration, turnover, drawdown, capital.
16. Tradability enforced everywhere (cash longs, intraday cash shorts, overnight shorts only via modelled F&O).
17. Verify corporate actions (RPOWER, RAYMOND suspects), liquidity, real broker charges.
18. Months of untouched paper fills: PF comfortably > 1.2 after costs across regimes.

**Carried over from session 6**
19. Pending: per-TF recency decay in walk_forward (calendar-time fade for 1w/1mo) - owner undecided.
20. `walk_forward --strategies` filter; switch remaining research modules to `research_bars` (1w/1mo).
21. Commit decision for `universe.txt` / `exits.json`; leftover ETFs in universe (SBIEQLWT50, SBIMID150).
22. Monday before market: check cloud auto-login (scp of 96c60e6 unconfirmed).
23. niftyindices.com history 2010-2023 for index strategies (now allowed as best source) - write fetcher.

## 6. Commits this session

3546550 health check + Windows .env; 7350a5a tuning system; f4e4b4f label fix; 82dbe62 ACTIVE v2; 89ae79f buckets;
ba2e656 web strategies + Nifty 50 refs + research-only shorts + incremental baseline; e7420cc / dff9132 NSE filings
downloader; 77b4ded XBRL parser.
