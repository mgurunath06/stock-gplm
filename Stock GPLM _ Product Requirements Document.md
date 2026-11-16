# Stock GPLM — Product Requirements Document

Version 1

### TL;DR

Stock GPLM is an Indian equity decision-support platform that identifies and ranks explainable, cost-adjusted trading opportunities. The cloud collects and synchronizes licensed market data; the user’s PC performs research, learning and backtesting and generates real-time alerts. A reproducible 13 August 2026 replay tests chronological processing and research feedback, not profitability on its own.

## Product Objective Hierarchy

Stock GPLM’s primary objective is to identify and rank actionable, cost-adjusted trading opportunities across stocks and holding horizons. It must determine which strategies work best for each stock, sector, timeframe and market regime, while independently discovering additional strategies. Next-candle prediction, indicator calculation, chronological replay and online adaptation are supporting capabilities. Success is determined primarily through robust out-of-sample trading utility, calibration, stability, risk and capacity—not next-candle directional accuracy alone.

For the selected universe, the system must provide the best available opportunities and, for each applicable opportunity:

* Buy, hold, exit, sell an existing holding or no-trade recommendation.
* Entry or trigger zone, stop-loss, invalidation and targets.
* Expected gross and net return, probability, confidence and expected holding period.
* Applicable strategy and market regime.
* Relevant predictions and actual indicator values from every timeframe used.
* Historical evidence, limitations and reasons for abstention where applicable.

## Goals

### Business Goals

* Evaluate the supplied minimum library of 300 strategies and enable discovery beyond it.
* Establish a reproducible research-to-alert workflow for the licensed universe; coverage target TBD.
* Assess cost-adjusted out-of-sample utility, stability and risk against baselines; approval thresholds TBD.
* Track cloud data and local compute costs; budget TBD.

### User Goals

* Find and compare the strongest eligible opportunities across stocks and holding horizons.
* Understand each recommendation, its risk, supporting indicators and limitations.
* Research stock-specific strategies locally and inspect chronological feedback.
* Receive timely decision-support alerts without automatic order placement.

### Non-Goals

* Guaranteed profitability, guaranteed accuracy or personalized investment advice.
* Automated execution, derivatives or intraday short-selling in Version 1.
* Multi-broker consolidation, broker reconciliation or advanced portfolio optimization in Version 1.

## User Stories

* As a trader, I want ranked opportunities with entry, risk and cost information, so that I can decide which trades to investigate.
* As a trader, I want to see relevant timeframe forecasts and indicator values, so that I can understand agreement and conflict.
* As a researcher, I want to replay historical candles and inspect frozen predictions and subsequent feedback, so that I can check for leakage.
* As a researcher, I want to enter and discover strategies and compare their out-of-sample results, so that I can identify robust approaches.
* As an operator, I want to monitor feed freshness and pause alerts or restore a model checkpoint, so that I can respond to failures.

## Functional Requirements

### 1\. Universe, market data and cloud delivery (Priority: P0)

* Ingest and research the broad available point-in-time NSE and BSE cash-equity and ETF universe, subject to licensed data availability. Historical research must account for listing, delisting, symbol changes and eligibility at the relevant date. For historical index-based testing, use membership effective on that date wherever available; report unavailable membership data and its limitations.
* Initially restrict live alerts to the current Nifty 200 plus stocks explicitly approved by the user. The supplementary alert list is configurable without code changes. Research coverage is not restricted to the live-alert list.
* The cloud collects historical and live market data, minute and daily candles or suitable source events, symbol information, exchange calendars, corporate actions and licensed market context through authorized APIs. The exact provider and licenses remain open; Shoonya is a candidate, not an approved provider.
* Validate timestamps, OHLCV, missing bars, duplicates, outliers, halts and corrections. Record source, retrieval time, quality status and revisions; quarantine unsafe data. Preserve point-in-time adjustment information and original decision snapshots when corrections arrive.
* Synchronize historical and incremental data to the PC over an authenticated, encrypted connection. Resume interrupted transfers, avoid duplicate records and show gaps or stale feeds. On reconnect, catch up without presenting old signals as current; suppress affected live alerts while required data is stale or incomplete.

### 2\. Candles, indicators and multi-timeframe behavior (Priority: P0)

* Support configurable 1, 3, 5, 15, 30 and 60 minutes, 4 hours, daily, weekly and monthly candles. Use the exchange calendar and local session time. The four-hour session anchor and final partial trading session must follow one documented, consistent aggregation policy; the exact policy is an implementation decision.
* Use completed candles and information available at the prediction timestamp. Other timeframe values are taken from their latest available completed bars; partial weekly and monthly bars must not appear as completed bars.
* Every timeframe produces an independent forecast. Other timeframes can supply context and supporting evidence. Timeframes need not agree: conflicts may affect confidence, risk or explanation but do not automatically suppress a valid opportunity. Reports display the relevant prediction and calculated indicator values for every timeframe used.
* Indicator families support configurable parameters. The following are standard defaults, not fixed limits:
  * Price and volume: OHLCV, turnover where available, typical price, returns over 1, 3, 5, 10 and 20 bars, gaps, range, body and wicks.
  * Trend: EMA 5, 9, 10, 20, 21, 50, 100 and 200; SMA 20, 50, 100 and 200; average distance and slope; MACD 12,26,9; ADX 14 with +DI and -DI; Supertrend 10,3.
  * Momentum: RSI 14 and slope, stochastic 14,3,3, ROC 5, 10 and 20, Williams %R 14, persistence and divergence candidates.
  * Volatility: true range, ATR 14 and ATR percentage, rolling standard deviation, Bollinger Bands 20,2, bandwidth and %B, realized volatility and compression state.
  * Volume and flow: volume SMA and EMA 20, relative volume, volume z-score, OBV, MFI 14, CMF 20, accumulation/distribution and price-volume divergence.
  * VWAP: session VWAP for intraday bars and day-, week-, month- or event-anchored VWAP where valid underlying price-volume data exists. Mark unavailable where it does not.
  * Structure and context: swing levels, pivots, support/resistance, Donchian 20 and 55, breakouts, relative strength versus market and sector, beta, correlation, regime and liquidity.
* Store each calculated value by symbol, timeframe and timestamp, with its actual parameters, input revision, calculation version and quality. Reports show the value and parameters used. Insufficient history produces unavailable, not zero; dependent calculations must not pretend otherwise.

### 3\. Opportunity horizons and learning targets (Priority: P0)

* Support intraday; 1–7 trading days; 7–15 trading days; 15–30 trading days; 1–3 months; 3–6 months; and 6–12 months. Each horizon must have a documented label-maturity rule, cost assumption and evaluation method; detailed mathematical definitions belong in a later model specification.
* Create independent forecasts for each timeframe and horizon. Learning targets include forward gross and net return, direction or no-trade, path risk such as maximum favorable and adverse excursion, target/stop or time-exit outcomes, holding period, strategy outcomes, regime and opportunity-ranking relevance. Outcomes remain pending until their applicable maturity rule is met.
* Compare models and strategies against simple appropriate baselines. Record sample sizes and separate stock-specific evidence from evidence learned across the wider universe.

### 4\. 13 August 2026 benchmark and next-candle replay (Priority: P0)

* Maintain 13 August 2026 as a permanent, reproducible benchmark for chronological processing, prediction freezing, outcome revelation, feedback, versioning, checkpointing and leakage prevention. It is not the main proof of model or strategy quality; a single day must never support a reliable profitability claim.
* For every eligible candle associated with the date, forecast the next candle using only data available before the target outcome. Record at least direction up/down/neutral, probability or uncertainty, expected close return or range, decision time, inputs and model version. Use a documented neutral band; do not imply an exact candle can be guaranteed.
* For intraday and daily candles, include eligible bars closing on the session date. For the first intraday target, use the last eligible completed bar from the prior session. For weekly and monthly candles, include the candle containing 13 August, freeze its forecast at the preceding completed bar and score it only when the target candle actually closes. Later outcomes cannot affect earlier decisions. Confirm session and data availability; do not silently replace the date.
* Freeze each prediction before its target outcome is available. When a valid target closes, record the realized outcome, correctness and forecast error; apply the research feedback according to the online-learning policy. Pending or invalid outcomes cannot update a model.
* Process simultaneous matured outcomes preferably as one event-time batch to avoid arbitrary ordering. Freeze relevant predictions before revealing their outcomes, and document a reproducible event-time ordering.
* Show separate frozen-model and adaptive research results. The adaptive run is not an untouched holdout. Main performance evidence must come from longer purged walk-forward evaluations with embargoes, untouched holdouts and paper trading. Report missing data and sample limitations, especially for weekly and monthly bars.

### 5\. Hybrid models and online learning (Priority: P0)

* Support a shared base model conditioned by stock, sector, timeframe and market regime; stock-specific models where evidence is sufficient; and stock-specific calibration or residual layers where useful. Fall back to the shared model when stock-specific evidence is inadequate. A small individual-stock sample must not block research; clearly report the strength of stock-specific evidence.
* In research mode, enable immediate per-candle learning after valid outcomes mature. Correct and incorrect outcomes may both be used. Retain the frozen forecast, outcome, update record and model versions; never revise an earlier forecast to match its outcome.
* Live alerts initially use an approved model version. Research updates may run in shadow alongside it. Automatic adaptive updates to live alerts are outside Version 1 unless separately tested, paper-traded and explicitly approved.
* Retain checkpoints, rollback and a static approved fallback. Pause updates or affected alerts on invalid data or update failures and provide a manual disable control. Model and strategy approval requires out-of-sample and paper-trading evidence, with a recorded decision.
* Use point-in-time inputs and matured labels, reproducible datasets and purged walk-forward evaluation. Avoid future corporate-action knowledge and unavailable cross-timeframe features. Measure calibration, ranking, net expectancy, downside risk, coverage, abstention and drift by stock, strategy, timeframe and regime.

### 6\. Strategy origins, lifecycle and backtesting (Priority: P0)

There are exactly two strategy origins; origin is separate from lifecycle status.

* ML-generated strategies: independently discover stock-, sector-, timeframe-, regime- and selected-stock-group strategies, as well as strategies across the wider universe. Outputs may be rules, rankings, feature combinations, models or ensembles. They must be explainable, versioned, evaluated out of sample and paper-tested before approval. Record discovery scope and candidate counts to help assess overfitting and multiple testing.
* User-input strategies: include the supplied minimum library of 300, additional user-entered strategies, imported configurations and future UI-created strategies. A strategy may apply to one stock, a stock list, Nifty 200 or another index, a sector or industry, a filtered subset, one or more timeframes and one or more regimes. Store strategies as editable configuration or create them through a UI; do not hard-code them. Validate entries, exits, parameters and risk rules and reject ambiguous or leakage-prone definitions. The 300 are a mandatory minimum to evaluate, not a ceiling; the actual library remains a dependency.
* Lifecycle: Draft → Validated → Backtested → Paper Tested → Approved → Retired. Neither origin can issue approved live alerts merely because it exists or performs well in one test.
* Backtests account for brokerage, taxes and exchange charges, spread, slippage, liquidity and gap-through entries or stops. When stop and target are both touched in one candle, use a conservative assumption or mark the sequence indeterminate. Clearly distinguish simulated from actual fills; do not imply simulated fills occurred.

### 7\. Real-time opportunities and basic risk controls (Priority: P0)

* On the PC, evaluate completed candles using approved model and strategy versions. Rank opportunities across the eligible alert universe, generate configurable notifications and support candidate, armed, triggered, cancelled, expired and exit events. Deduplicate alerts and preserve their original snapshots.
* Recommendations are buy, hold, exit, sell an existing holding or no-trade. Alerts do not place orders. Abstain or suppress affected alerts when evidence is weak, expected net value is negative, data is stale or incomplete, or applicable risk and liquidity checks fail.
* Each opportunity includes identity, timestamps and expiry, action, rank, horizon, strategy and model versions, trigger or entry zone, stop-loss and invalidation, targets, expected gross and net return, probability and confidence, expected holding period, market regime, supporting timeframe forecasts and indicator values, historical evidence, limitations and data freshness. Include the reason for no-trade or rejection where relevant.
* Optionally accept current holdings, available capital, existing position size, basic stock and sector exposure and simple user-configured risk limits. Use available inputs to avoid obviously conflicting or excessive alerts; mark missing inputs rather than assume no exposure. Advanced portfolio optimization and broker reconciliation are later-version work.

### 8\. Daily operation and reporting (Priority: P0)

* After market close: ingest final candles and corrections, update features, mature outcomes and evaluate prior predictions.
* Before market open: refresh the eligible alert universe, licensed market context, events and basic risk state.
* During market hours: process completed candles and issue candidate, armed, triggered, cancelled, expired and exit alerts as applicable.
* After a signal matures: record its outcome and use valid matured evidence for governed research learning. Preserve simulated versus actual fill provenance.
* Show data quality, synchronization status, replay results, strategy performance, model versions, alert history, indicator evidence and no-trade reasons. Long-running collection, feature, training, backtest and evaluation jobs checkpoint at logical intervals and resume without repeating completed work.

## User Experience

### Entry Point & First-Time User Experience

* Connect the local application to the cloud data service and configure credentials securely. Show feed access and licensing status.
* Select research universe and timeframes; configure the supplementary live-alert stock list, simple risk limits, cost assumptions and notifications. Clearly distinguish the broad research universe from the restricted live-alert universe.
* Start backfill and show coverage, gaps, adjustment status and indicator warm-up requirements.

### Core Experience

* Run a local research job or the permanent 13 August benchmark. Show timeframe boundaries, frozen predictions, outcome maturity, feedback and checkpoint status; weekly and monthly outcomes remain pending until their target candles close.
* Compare frozen and adaptive benchmark runs, then review longer walk-forward, untouched-holdout and paper-trading results. Show costs, sample sizes and limitations rather than presenting a one-day result as proof.
* Create or import user-input strategies and inspect ML-generated candidates. Show origin separately from lifecycle status and require the prescribed steps before approval.
* Review ranked opportunities with action, entry, exits, risk, expected returns and evidence. Display relevant predictions and actual indicator parameters and values for each timeframe, including conflicts.
* Enable alerts only for approved strategies and model versions. Review matured outcomes, pause research updates, disable alerts or restore a checkpoint when necessary.

### Advanced Features & Edge Cases

* Stale or disconnected feeds show a warning and suppress affected live alerts; recovery catches up without replaying obsolete alerts as current.
* Missing history, unavailable membership data, uncertain fills, corporate-action corrections and long-horizon outcomes are clearly labeled. Invalid or pending outcomes do not train the model.
* A stock with limited history can use the shared model, with its limited stock-specific evidence disclosed.

### UI/UX Highlights

* Show exchange-local timestamps, candle timeframe, closed-bar status and research, paper or live-alert mode. Distinguish forecast, realized result and correction.
* Use clear text in addition to color, accessible contrast and keyboard-accessible controls; make risk and stale-data warnings prominent.

## Narrative

A trader follows Indian equities but has difficulty comparing opportunities across stocks and holding periods. Charts show many indicators, yet a favorable-looking pattern may not survive costs or work consistently in the current market regime.

The trader connects the local application to the cloud data service and selects the research universe. The cloud supplies licensed history and live data, while the PC builds completed candles, calculates indicators and evaluates strategies. The trader also configures a supplementary alert list and basic risk limits. Research can cover the broader available universe, while live alerts remain limited to the current Nifty 200 and approved additions.

The trader runs the permanent 13 August 2026 benchmark. The application freezes forecasts before outcomes are known, reveals each valid outcome at maturity and records research updates. Weekly and monthly candles remain pending until their actual close. Frozen and adaptive results show whether the processing and feedback are reproducible; they do not claim that one day establishes profitability.

The trader then reviews longer out-of-sample and paper-trading evidence. A candidate strategy advances through validation, backtesting and paper testing before approval. During a later session, an approved model identifies an opportunity. Its alert shows a trigger, stop, targets, expected net return, confidence and the indicator values behind each relevant timeframe. If the timeframes disagree, the explanation describes the conflict rather than automatically dismissing it. If the feed becomes stale, affected alerts stop. The trader keeps the final decision and can inspect the evidence behind it.

## Success Metrics

### User-Centric Metrics

* Explainability and evidence coverage: proportion of alerts containing the required rationale, limitations, relevant timeframe forecasts, actual indicator parameters and values, and risk fields; target TBD.
* Opportunity-ranking quality: compare ranked opportunities with matured, cost-adjusted outcomes across the eligible universe and horizons; threshold TBD.

### Business Metrics

* Strategy coverage and robustness: at least 300 supplied user-input strategies imported and evaluated, with stability across time and regimes assessed for all approved strategies. Approval thresholds TBD.
* Trading utility and risk: measure cost-adjusted out-of-sample return or expectancy, drawdown, downside risk and capacity, with separate paper-trading evidence; thresholds TBD by horizon and strategy.

### Technical Metrics

* Benchmark integrity: verify chronological correctness, frozen predictions, correct outcome maturity, update sequencing, reproducibility, checkpoint recovery and absence of leakage for all eligible 13 August benchmark cases.
* Model quality and coverage: measure probability calibration, coverage and abstention, plus stability across time and regimes on walk-forward and untouched holdouts; thresholds TBD. Monitor feed freshness and suppression of affected stale-data alerts; latency target TBD.

Numerical model and trading thresholds are decisions to establish before live-alert approval; benchmark integrity is distinct from model quality.

### Tracking Plan

* Record data coverage, missing bars, corrections, synchronization and freshness.
* Record run configuration, prediction freeze, outcome maturity, event-time update, checkpoint, rollback and model version.
* Record strategy origin, lifecycle changes, evaluation evidence and approval; track alert eligibility, rejection, trigger, notification and matured outcome.
* Report cost-adjusted results, ranking, calibration, drawdown, stability, coverage and abstention by stock, timeframe, strategy and regime with sample counts.

## Technical Considerations

### Technical Needs

* Cloud: collect and synchronize licensed market data. PC: perform substantial feature generation, training, research, backtesting, evaluation and real-time alert generation.
* Maintain versioned records of source data, candles, indicator values and parameters, datasets, predictions, outcomes, strategies, checkpoints and signal events sufficient to reproduce decisions.
* Developer agents build, integrate, unit-test and run bounded smoke tests. They must not run full-scale research, bulk backtests or lengthy ML workloads. The user runs full workloads locally using commands produced by the program.
* Long-running work checkpoints at logical intervals so future sessions can resume without repeating completed work.

### Integration Points

* Licensed market-data and reference APIs, cloud-to-PC synchronization and user-selected alert channels. Provider selection, entitlements, redistribution rights and rate limits remain open.
* Broker order placement and automated execution are outside Version 1.

### Data Storage & Privacy

* Protect credentials outside code, encrypt data in transit and restrict access to data and model artifacts. Maintain checksums, backups and audit records appropriate to the deployment.
* Preserve source, raw and corrected data and adjustment provenance. Store frozen predictions and simulated versus actual fills distinctly; corrections must not silently overwrite prior decisions.

### Scalability & Performance

* Actual universe coverage, feed latency, PC capacity and storage requirements depend on provider access and hardware; targets are TBD. Benchmark locally before live-alert approval.
* Resume interrupted synchronization and workloads; expose stale feeds or processing delays and suppress affected alerts rather than issuing unsafe signals.

### Potential Challenges

* A single benchmark day cannot validate long-term profitability, particularly for weekly and monthly candles. Longer independent evaluation and paper testing are essential.
* Historical membership, delistings, corporate actions, data gaps and corrections can introduce survivorship bias or leakage. Show limitations when historical evidence is unavailable.
* Four-hour candle boundaries and final partial-session treatment require a consistent documented policy. Some VWAP calculations require unavailable or unlicensed underlying data.
* Open decisions: market-data provider and license; historical coverage and index membership availability; four-hour aggregation policy; cost and risk assumptions; research update algorithm; holdout dates; model and strategy approval thresholds; PC capacity, retention, latency and alert channels.