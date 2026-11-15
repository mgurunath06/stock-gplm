# M5.8 implementation checkpoint (2026-11-15)

Branch `feature/m55-gplm-app`; local only, not pushed or merged.

Commits:

- `700782d` session-aligned 4h and configurable intraday frames
- `2495f65` reusable calculation cache, coverage matrix and `feature-coverage` CLI
- `78100ee` independently checked quadratic fit and causal Weis wave volume
- `711b6c3` virgin CPR, Smart Money Index and FVG mitigation state
- `313707f` causal Stock DNA discovery and reporting
- `c1485c2` cutoff-frozen prediction, separate reveal and evaluation
- `dc2d04e` resumable chronological freeze-then-reveal walk-forward
- `5f5da31` independently checked causal DTW VCP distance

Verification performed (synthetic only):

- M5.8 feature-engine targeted tests: 5 passed.
- Stock DNA tests: 2 passed, including future-candle mutation isolation.
- Prediction/reveal/walk-forward tests: 3 passed, including append-only freeze and deterministic resume.
- Timeframe tests: 2 passed.
- Full calculation section: 125 rows, 1,483 expression x timeframe evaluations across all eight
  governed frames; no unexpected errors. Current counts: 25
  independent-reference, 57 computed, 28 composed, 8 not implemented, 2 partial, 5 need data.
- No full repository test suite, real-data calculation, training, research or backtest was run.

Untracked pre-existing files intentionally untouched: `.git.wsl-backup`,
`config/app/gplm.windows.json`.

The completion path is documented in `docs/m58-feature-computation.md`: request Stock DNA at the
2025-08-13 cutoff, freeze a 2025-08-14 prediction, then reveal it by id. Next safe increment is the
remaining OHLCV algorithm families, one independently verified family per commit.
