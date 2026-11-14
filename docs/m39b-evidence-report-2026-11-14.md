# M3.9-B engineering evidence report — 2026-11-14

## Result

The isolated corporate-action engine is synthetically verified. The full local
suite passed: **599 passed in 33.38 seconds** using Python 3.12.16 and pytest
8.4.2. The M3.9-B-focused file contributes 34 synthetic and isolation tests.

This result proves implementation behavior only. No historical dataset was
modified or declared corrected, no model was trained, no research trial was
run, and no protected-holdout file was accessed.

## Supported calculations

- Forward stock splits, including one old share becoming two or five shares.
- Reverse splits, with reciprocal price/volume treatment.
- Bonus issues, including one-for-one and one-for-two.
- Multiple sequential events with strict pre-ex-date boundaries.
- Cash dividends as a separately selected total-return adjustment when both the
  cash amount and validated cum-dividend reference price exist. Price-return
  mode excludes the distribution.

Examples from synthetic fixtures:

- A 1-to-2 split maps pre-event close 100 and volume 10 to adjusted close 50
  and volume 20; the ex-date candle is unchanged.
- A 1-for-2 bonus maps pre-event close 100 and volume 10 to 66.66666667 and 15.
- A cash dividend of INR 10 with reference price INR 100 produces total-return
  factor 9/10, while the price-return factor remains 1.

## Validation findings

Tests cover invalid and missing ratios, missing ex-dates, zero/invalid prices,
bad OHLC bounds, negative volume, conflicting sources, duplicate events,
look-ahead evidence, future candles, future effective dates, adjusted/raw
mixing, stable repeated execution, and immutable inputs. Suspicious price gaps
remain unattributed.

Rights, mergers, demergers and symbol changes fail closed. `UNKNOWN` facts stay
unknown. M3.9 NSE MCP `REFERENCE_ONLY` evidence cannot be marked validated for
derivation. Training/research/MCP packages do not import M3.9-B, and M3.9-B has
no filesystem, network, training or command-line entry point.

## Unresolved blockers

- No real source licence is approved for canonical adjustment derivation.
- No checksum-bound real event bundle has been admitted.
- Rights and restructuring methodologies are not validated.
- Dividend reference-price sourcing and market-calendar alignment require a
  separately evidenced policy.

The machine-readable companion is
[`examples/m39b-corporate-action-report.json`](examples/m39b-corporate-action-report.json).
