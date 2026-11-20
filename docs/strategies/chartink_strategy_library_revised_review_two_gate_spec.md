# Chartink Strategy Library Review & Two-Gate Spec
20 November 2026 · NSE Cash & F&O Equities · Research Draft Pending Owner Approval

## 1. Executive Recommendation & Two-Gate Workflow

Prioritise a lean set of **four clearly specified starter strategies** for the first owner-run backtest:
1. **O2-004** (Daily MACD $\times$ signal below zero) — *Mean-reverting trend turn*
2. **O2-023** (Daily & 60m EMA50 reclaim in a downtrend) — *Pullback / reclaim*
3. **O2-028** (1h + daily stack breaking prior-day high) — *Momentum breakout*
4. **O2-035** (1h tight-base breakout) — *Volatility contraction breakout*

To avoid analysis paralysis while preserving statistical discipline, split governance into **two gates**:
* **Gate 1 (Pilot Run — Fast Edge Discovery):** Run the 4 starters using the concrete NSE defaults in Section 2 (liquidity floor, realistic cost bands, baseline + trailing exit templates, and ATR-normalized learner diagnostics). No requirement for historical delisting databases or Deflated Sharpe Ratios yet.
* **Gate 2 (Full Library & Promotion):** Apply full-universe validation, chronological walk-forward folds with purged boundaries, date-block resampling, Deflated Sharpe diagnostics, and 66-screenshot provenance reconciliation before promoting any rule to paper or live trading.

> *Scope note:* This review is based on the supplied scan inventory and three-stock smoke check. Source screenshots, draft JSON, strategy schema, and per-rule smoke outputs remain uninspected; all amendments below require code-level verification prior to execution.

---

## 2. Concrete NSE Defaults for Owner Sign-Off (Gate 1)

Freeze these parameters before launching the Gate 1 pilot:

* **Liquidity Floor (Proxy for Missing Market Cap):** 20-day Median Daily Turnover $\ge$ ₹15 Cr and $\text{Close} \ge$ ₹50 (evaluated on prior completed session).
* **NSE Short-Selling Rules:**
  * **Swing / Overnight Shorts** (O2-025, O2-026, O2-029, O2-041, O2-051, O2-012): Restrict strictly to the **F&O universe** (modelled as futures/perp equivalents). Do *not* model overnight cash shorts via SLBM.
  * **Intraday Shorts** (5m, 15m, 30m): Permitted on the liquidity-filtered cash universe with a mandatory EOD square-off at **15:15 IST**.
* **Intraday Session Hygiene:** Exclude entries during the opening auction spread distortion (**09:15–09:20 IST**) and block new intraday entries after **14:45 IST**.
* **Indicator Warm-Up:** Minimum $3 \times$ the longest EMA period (e.g., 600 completed bars for $\text{EMA}_{200}$).
* **Round-Trip Friction (Brokerage + STT + Exchange/SEBI Fees + GST + Slippage):**
  * Cash Delivery / Swing (1d, 1w): $0.25\%$–$0.35\%$
  * Cash Intraday (5m, 15m, 30m): $0.10\%$–$0.15\%$
  * F&O (Futures Shorts/Longs): $0.06\%$–$0.10\%$

---

## 3. Priority Corrections & Bug Fixes

| Rule / Scan | Proposed Amendment | Acceptance Check |
|---|---|---|
| **O2-021 vs O2-023** · 201041 vs 201836 | Keep both IDs in the inventory for traceability, but **drop O2-021 from active runs** in favour of canonical O2-023 unless the daily 10% filter proves incremental discrimination. | Verify exact symbol/timestamp overlap on broad point-in-time data, including high-volatility regimes. |
| **O2-034** · 200900 branches 2 & 3 | Preserve original branch logic until equivalence is proven. Strict inequalities exclude exact equality and require defined Bollinger band values. | Unit-test high below, equal to, and above the upper band, plus missing/NaN indicator values. |
| **O2-041** · 201750 (Shooting Star) | Hold broken source; register repair as a separate F&O-only hypothesis: two green daily candles followed by a red candle opening above prior close (confirm if prior high was intended), small real body in lower 30% of range, upper wick $\ge 2 \times \text{body}$. Confirm via completed close below pattern low. | Lock wick, body, gap, and confirmation-window rules before running. Test zero-range candles and gap failures. |
| **O2-052** · 200943 / 201554 | Keep inclusive `AND` band ($0.99 \times \text{EMA}_{200} \le \text{Close} \le 1.01 \times \text{EMA}_{200}$ or $\pm 2\%$) strictly as a **Condition-Only** state feature. Count $1\%$ and $2\%$ widths as separate trials; optionally benchmark against $\lvert \text{Close} - \text{EMA}_{200} \rvert \le 0.5 \times \text{ATR}_{14}$. | Test exact boundaries, values just outside, and un-warmed EMA states. |
| **201411** | **Hold / Exclude from Gate 1.** Lower Bollinger Band ($<\text{SMA}_{20}$) is mathematically always true for positive standard deviation. Confirm if intent was an oversold gap-up (`prev_close < Lower_BB` AND `open > prev_open` AND $\text{RSI}_{14} < 50$) before drafting. | Document owner intent; if unconfirmed, leave retired to avoid noise. |
| **201803 / 201816** | **Drop the accidental 60m vs. 15m mismatch.** Draft and test *only* the corrected 60m $\text{EMA}_{100}$ vs. 60m $\text{EMA}_{200}$ rule to avoid inflating trial counts on a likely Chartink dropdown error. | Verify 60m bar timestamps and HTF alignment in the engine. |
| **200524 / 201344** | Reuse existing O1-004 / O1-003 aliases where active predicates match. For 201344, verify whether $\text{EMA}_{13} > 20$ adds incremental filtering on 60m before retiring. | Compare active predicates, cross conventions, and exact trigger sets against O1 baselines. |
| **202010** (Monthly EMA200) | **Retire from standalone testing.** 200 monthly bars ($\approx 16.7$ years before warm-up) introduces severe survivorship and long-listing coverage bias on NSE. | Log as unsupported due to history constraints. |
| **O2-020** ("9 GT 20 at $45^\circ$") | Chart angles are zoom- and scale-dependent. Replace $45^\circ$ with an **ATR-normalized slope**: $\frac{\text{EMA}_9(t) - \text{EMA}_9(t-3)}{3 \times \text{ATR}_{14}} \ge 0.25$ while $\text{EMA}_9 > \text{EMA}_{20}$. | Verify scale invariance across ₹100 and ₹10,000 stocks. |

---

## 4. Rule-by-Rule Research Queue

| IDs | Proposed Treatment |
|---|---|
| O2-001, O2-002 | Keep as simple EMA-cross trend baselines. Benchmark against O1 crosses on identical fills/exits and measure same-day signal overlap. |
| O2-003 | Define "MACD turning up" and which line must be $< 0$. Note: buying $\text{RSI}_3 \ge 80$ inside a downtrend risks buying exhaustion—test with vs. without the $\text{RSI}_3$ filter. |
| **O2-004**, O2-005 | **Gate 1 Starter (O2-004 daily).** Evaluate weekly as a separate trial. Clarify if $< 0$ applies to MACD alone or both MACD and signal. Reclassify monthly **O2-005** as a **Condition-Only** macro regime filter (too sparse standalone). |
| O2-006, O2-007 | Reconstruct Entry A and Predictive MACD from source. Strictly audit **O2-007** for look-ahead or repainting values. Benchmark against O2-004. |
| O2-008, O2-009 | Keep 5 higher closes (O2-009) as baseline. For **O2-008** (5 days $\ge 4.9\%$), enforce F&O eligibility or $\text{High} \ne \text{Low}$ with volume expansion to filter out unexecutable 5% upper-circuit traps. |
| O2-010, O2-011, O2-012 | Keep rising closes and closes above prior highs distinct; measure volume filter lift. Restrict short mirror (O2-012) to F&O / intraday rules. |
| O2-013, O2-014, O2-015 | Defer to Gate 2 with completed bars and realistic 5m/15m friction ($0.10\%$–$0.15\%$). Benchmark breakout/streak with vs. without RSI/stack filters. |
| O2-016, O2-017 | Exploratory intraday queue. Replace or pair the fixed $5\%$ 15m bar rule with a volatility-normalized surge ($\text{Bar Return} \ge 2.5 \times \text{ATR}_{14}(\text{15m})$) so it scales across Nifty 50 and midcaps. |
| O2-018, O2-019 | For **O2-018**, ensure the 15-day max volume excludes the trigger bar; match elapsed session time if run intraday. Reclassify **O2-019** (low-volume strength) as a **Condition-Only** pre-breakout/VCP state rather than a standalone entry. |
| O2-020 | Run using the ATR-normalized slope specification from Section 3. |
| O2-021, O2-022, **O2-023**, O2-024, O2-025 | **Gate 1 Starter (O2-023 on 1d & 60m).** Drop O2-021. Measure downtrend filter lift vs. raw EMA50 reclaim. For **O2-024**, signal on Heikin-Ashi but execute fills/stops strictly on **real OHLC**. Restrict **O2-025** short to F&O. |
| O2-026, O2-027 | Count each EMA anchor in short cross (O2-026, F&O only) as a distinct trial. Keep 30m $\text{EMA}_{100} \times \text{EMA}_{200}$ (O2-027) in low-frequency queue. |
| **O2-028**, O2-029 | **Gate 1 Starter (O2-028).** Measure incremental lift of daily/hourly EMA stack over raw prior-day high break. Define handling of opening gaps and repeated crosses. Evaluate **O2-029** short on F&O only. |
| O2-030, O2-031 | Use completed 5m/15m bars. Emit only fresh stack transitions (not every bar while stack persists). |
| O2-032 | Run 1d and 1w independently. Define how long the below-$\text{EMA}_{200}$ state remains valid and the exact recovery trigger. |
| O2-033, O2-034 | Isolate $\text{SMA}_{20}$ cross first; measure incremental d/w/m RSI lift and apply Section 3 branch check to O2-034. |
| **O2-035** | **Gate 1 Starter.** Require completed base ending *prior* to breakout bar. Lock one objective tightness metric (e.g., $N$-bar range $\le 1.5 \times \text{ATR}_{14}$) and benchmark against breakout without tightness filter. |
| O2-036, O2-037 | Define freshness window for completed monthly RSI in O2-036. For **O2-037** (1h RSI divergence), timestamp pivots strictly at **confirmation** (no look-ahead) and benchmark against price-pivot alone. |
| O2-038, O2-039, O2-040, O2-041 | Drop standalone hammer (**O2-038**, too noisy) or use only as control for O2-039/O2-040. Normalise wick/body to candle range with a tick-aware minimum range. Timestamp O2-040 at confirmation close. Hold O2-041 pending Section 3 sign-off. |
| O2-042, O2-043, O2-044 | Emit first qualifying event per uninterrupted 5-candle streak. Segment **Opening Drive** (09:20–09:45 IST, momentum hypothesis) from **Mid-day Climax** (10:00–14:30 IST, mean-reversion hypothesis). Define "strong day" for O2-044 as prior completed session. |
| O2-045, O2-046 | Require completed Supertrend flip; signal on Heikin-Ashi where specified, but fill and manage risk on **real OHLC**. |
| O2-047, O2-048, O2-049 | Test touch + rejection/reclaim vs. raw touch. Reconstruct Entry B (O2-049) before locking pullback thresholds. |
| O2-050, O2-051 | Keep $\text{Close} \times \text{EMA}_{200}$ as trend baselines on completed bars; define gap-through entries explicitly. Restrict O2-051 short to F&O. |
| O2-052..057 (+ O2-005, O2-019) | **Condition-Only Building Blocks.** Feed exclusively into the learner as regime/context filters. Include trigger-only controls, test predeclared pairs before triples, and penalise overlapping labels. |

---

## 5. Decisions on the Six Owner Questions

1. **Unclear Direction (200813, 200920, 201312, 201703/201713, 201723, 201908, 201926):**
   * **Decision:** Reject per-stock $\times$ timeframe direction selection (guaranteed overfitting). Recover source intent first; where ambiguous, predeclare continuation vs. reversal hypotheses and evaluate **at the portfolio level** (strategy $\times$ timeframe $\times$ universe) in-sample only, then validate out-of-sample.
2. **3-Minute $\rightarrow$ 5m Mapping:**
   * **Decision:** Approved. Label 201250, 201301, 201827, and 202059 as 3m-origin scans running on 5m bars. Sub-5m NSE data rarely survives intraday brokerage and slippage.
3. **Market Cap / Nifty Segment:**
   * **Decision:** Run discovery on the **liquidity-filtered cash universe** (20d median turnover $\ge$ ₹15 Cr, $\text{Close} \ge$ ₹50) for longs and intraday shorts, and restrict swing shorts strictly to **F&O**. Keep Nifty 50/100/200/500 tags for attribution.
4. **Condition-Only Scans (O2-052..057 + O2-005, O2-019):**
   * **Decision:** Use exclusively as learner regime features / building blocks, never as standalone entries. Always benchmark against the trigger alone.
5. **Scan 201411 (Always-True Bollinger Condition):**
   * **Decision:** Exclude from Gate 1. Only draft if owner confirms the oversold gap-up intent (`prev_close < Lower_BB` AND `open > prev_open` AND $\text{RSI}_{14} < 50$).
6. **Exits & Learner Labels:**
   * **Decision (Exits):** Keep `10 bars / 1.5x ATR stop / 2.5x ATR target` as the **Reproduction Baseline**, but pair it with **Archetype Exits** so trend/breakout rules are not prematurely truncated:
     * *Trend / Breakout (O2-004, O2-028, O2-035, etc.):* Trailing $2.0 \times \text{ATR}_{14}$ Chandelier stop, **uncapped target**, 25-bar time stop.
     * *Mean Reversion / Pullback (O2-023, hammers, RSI extremes):* Fixed $1.5 \times \text{ATR}_{14}$ stop, $2.0 \times \text{ATR}_{14}$ target (or $\text{EMA}_{20}$ touch), 6–8 bar time stop.
     * *Intraday Momentum (5m/15m):* Fixed $1.2 \times \text{ATR}_{14}$ stop, $2.5 \times \text{ATR}_{14}$ target, 10–12 bar time stop **or 15:15 IST hard square-off**.
   * **Decision (Up-Move Learner Label):** Keep `1d: +3% within 5 days before -1.5%` for legacy continuity, **and** add an **ATR-normalized triple-barrier label** ($+2.0 \times \text{ATR}_{14}$ before $-1.0 \times \text{ATR}_{14}$ within 5 bars) as the primary evaluation metric to eliminate high-beta selection bias.

---

## 6. Backtest & Validation Protocol (Gate 1 vs. Gate 2)

### Gate 1 — Mandatory for Pilot Run (4 Starters)
1. **Timing & Look-Ahead Hygiene:** Join higher-timeframe (HTF) inputs strictly on **last completed** bar timestamps (`1d`, `1w`, `1mo`) to prevent Chartink-style intraday HTF repainting. Verify session boundaries and shortened/holiday sessions.
2. **Execution Realism:** Fill at the next eligible bar open after signal confirmation. Model gap-through stops at actual open prices. When a single bar touches both stop and target, default to a **conservative stop-first** fill. Compute $\text{ATR}_{14}$ strictly from pre-entry data. Apply Section 2 cost bands.
3. **Baseline Reporting:** Report unique signal dates/symbols, trade count ($N \ge 100$ minimum for inference), net expectancy (bps), win rate, profit factor, max drawdown, and symbol/sector concentration.

### Gate 2 — Mandatory for Full Library & Live Promotion
1. **Selection & Overfitting Controls:** Maintain a pre-run trial register counting all timeframes, directions, parameter widths, exit templates, and learner pairs. Use chronological walk-forward folds with **purged outcome windows** at fold boundaries and a strictly untouched holdout set. Compute dependence-aware confidence intervals (date-block resampling) and the **Deflated Sharpe Ratio** where inputs support it.
2. **Data Integrity:** Incorporate point-in-time index constituents, delisted symbols, and split/bonus-adjusted corporate actions consistently.
3. **Full Provenance Reconciliation:** Produce a 66-row audit table mapping every screenshot/branch to its source ID, draft rule ID, timeframe, O1 alias or merge status, and final disposition before promoting any strategy from draft to production.