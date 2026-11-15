"""GPLM correctness-verification suite (``stock-gplm.verify.v1``).

Answers four audit questions with deterministic synthetic evidence, never real candles:

1. **Did the engine understand each catalogue strategy?** Every library interpretation is compared
   with its hash-bound original pseudo-code: structurally (indicators, parameters, thresholds,
   direction, timeframes) and, where the pseudo-code is machine-readable, semantically: an
   independent pseudo-code evaluator built on independent reference indicators must reproduce
   the DSL entry signal bar for bar.
2. **Are the indicators computed correctly?** RSI, MACD, EMA, SMA, ATR, ADX and others are checked
   against pure-Python reference implementations written without NumPy/pandas, on fixtures and edge
   cases (constant, monotonic, gapped, zero-volume, too-short series). Seeding conventions that
   differ from the textbook (TA-Lib style) are measured and reported, not hidden.
3. **Is the backtest honest?** Next-bar fills, stops, targets, trailing stops, gap fills, signal
   exits, costs, purging, non-overlap and future-data leakage (all DSL primitives are tested for
   causality by truncation and future perturbation).
4. **Is the statistics/validation pipeline sound, and is ML discovery real?** Segments and purge,
   discovery-only optimisation, the once-only validation read, Benjamini-Hochberg, gates, holdout
   refusal, genetic-programming novelty, persistence and reload of evolved and tree rules.

Run ``gplm verify`` (or ``windows\\verify.cmd``); see :mod:`stock_gplm.verify.runner`.
"""

VERIFY_CONTRACT = "stock-gplm.verify.v1"
