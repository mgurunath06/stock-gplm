# M5.5 acceptance evidence — 2026-11-15

Branch `feature/m55-gplm-app` in `/home/mgurunath/projects/stock-gplm-m54`. Machine: WSL2 (Ubuntu-26.04-H) on
the owner's i7-14700K / RTX 5070 PC, Python 3.12.16, NumPy 2.5.4, pandas 2.3.3, SciPy 1.18.1, **no PyTorch**.
Evidence files are in `var/acceptance/` (state directory, not committed); the database is `var/gplm/gplm.sqlite`.

**Overall: PARTIAL.** Every item passes on CPU with real data except native-Windows GPU execution, which could
not be run from WSL without installing PyTorch (not authorized). The application and the verification
script are ready for the owner (`windows\install.ps1 -Gpu`, then `windows\gpu-verify.cmd`).

## Acceptance checklist

| # | Requirement | Result | Evidence |
| --- | --- | --- | --- |
| 1 | Full automated test suite | PASS — 957 passed, 3 skipped (GPU test without CUDA; two tests needing local files that are absent) | `17-full-test-suite.txt` |
| 2 | Synthetic integration test | PASS — `tests/test_app_gplm.py` (19 tests: ingest, closing-phase adapter, causality, kernels, rule detection, lookahead guard, resume equality, intraday lane, append-only + hash chain, lock, library, AI governance, daily idempotency + dashboard, crash/resume, incremental session) | pytest |
| 3 | Real daily-candle execution | PASS — RELIANCE, HDFCBANK, ICICIBANK daily 2022-10-31 → 2026-11-13 (989 bars each; 979 official, 10 minute-derived) | `03`, `06`, `07` |
| 4 | Real minute-candle execution incl. 2026 | PASS — RELIANCE 213 sessions 2026-01-01 → 2026-11-13 (79,875 bars) used for 1m/5m/15m/30m/60m discovery lanes and the 15m walk-forward lane; HDFCBANK/ICICIBANK 10 sessions each feed minute-derived days | `04`, `06` |
| 5 | Genuine multi-timeframe discovery | PASS — RELIANCE deep: all seven lanes (1m … 1w), 216,130 discovery tests, cross-timeframe confirmations | `04`, engine `report.md` |
| 6 | Rolling replay 2026-08-13 → latest completed session | PASS — 63 sessions to 2026-11-13, four lanes, 10,237 predictions, 9,314 evaluations, 26,741 training-history rows, 3,213 saved model states | `06`, `07`, `14-final-numbers.txt` |
| 7 | Accuracy and strategy-performance reporting | PASS — HTML/CSV/JSON report and dashboard; results below | `reports/latest/` |
| 8 | Daily incremental execution | PASS — real: replay stopped at 50 sessions, `gplm daily` processed only the remaining 13 per lane and issued live forecasts; synthetic: one new minute file → 1 file parsed, 1 step per lane | `07`, test |
| 9 | Repeat idempotency | PASS — second `gplm daily`: 0 predictions, 0 evaluations, 0 fits, 0 changed files, 1.4 s | `08-daily-repeat.txt` |
| 10 | Interruption / resumption | PASS — SIGINT after 30 s (run recorded `INTERRUPTED`), resume continued from checkpoints; synthetic crash test shows resumed == uninterrupted predictions | `05`, `06`, tests |
| 11 | Audit reconstruction of a sample prediction | PASS — prediction `a1f606d94c19bed1511c4b47` traced to input files and hashes, candle fingerprint, feature-config hash, model artifact (hash re-verified), training step, run, config snapshot, evaluation, hash chain | `10-audit-trace-sample.json` |
| 12 | API-disabled execution | PASS — AI disabled and refresh disabled: `api_calls` table empty after every run | dashboard `/api/api_calls` |
| 13 | CPU execution | PASS — every run above used `--device auto` → CPU (NumPy) with the reason recorded | `DEVICE_SELECTED` events |
| 14 | Windows installation and launch path | DELIVERED, not executed on Windows here — `windows/install.ps1`, `gplm.cmd`, `schedule-daily.ps1`; code paths made Windows-safe (no `resource`, no fork, ctypes lock check, URI-quoted SQLite paths, BOM-tolerant config) | files |
| 15 | Native Windows GPU verification | **NOT RUN (external blocker)** — `gplm gpu-test` here reports `CPU_ONLY` honestly; `windows/gpu-verify.cmd` produces the evidence on Windows | `02-gpu-test.txt` |

Also: `gplm doctor` OK (`01`), dashboard endpoints all HTTP 200 on real data (`15-dashboard-probe.txt`),
`gplm audit --verify`: 923 chained events OK, SQLite quick_check ok (`11`). Universe ingestion of all 2,394
local symbols (33,539 files) in 4 min 9 s (`16-universe-quality.txt`, `docs/m55-data-investigation.md`).

## Rolling walk-forward results (sequential out-of-sample, 2026-08-13 → 2026-11-13)

Label: net return after round-trip cost assumptions > 0. "maj" is the hindsight majority rate of the evaluated
window; "eff n" ≈ evaluated / horizon for overlapping daily labels.

| Lane | h | n | eff n | baseline acc | logit acc | mlp acc | baseline Brier | logit Brier | mlp Brier | train acc (logit / mlp) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| RELIANCE 1d | 1 | 61 | 61 | 0.623 | 0.623 | 0.607 | 0.240 | 0.249 | 0.262 | 0.72 / 0.90 |
| RELIANCE 1d | 5 | 57 | 11 | 0.684 | 0.421 | 0.491 | 0.266 | 0.340 | 0.379 | 0.71 / 0.94 |
| RELIANCE 1d | 20 | 42 | 2 | 0.810 | 0.167 | 0.333 | 0.214 | 0.453 | 0.526 | 0.82 / 0.98 |
| RELIANCE 15m | 4 | 1,323 | — | 0.764 | 0.764 | 0.764 | 0.182 | 0.183 | 0.183 | 0.77 / 0.79 |
| RELIANCE 15m | 8 | 1,071 | — | 0.700 | 0.714 | 0.700 | 0.209 | 0.217 | 0.251 | 0.73 / 0.82 |
| HDFCBANK 1d | 1 | 61 | 61 | 0.623 | 0.639 | 0.639 | 0.236 | 0.239 | 0.262 | 0.74 / 0.91 |
| HDFCBANK 1d | 5 | 57 | 11 | 0.579 | 0.474 | 0.281 | 0.257 | 0.320 | 0.574 | 0.73 / 0.92 |
| ICICIBANK 1d | 1 | 61 | 61 | 0.672 | 0.672 | 0.639 | 0.237 | 0.232 | 0.241 | 0.69 / 0.86 |
| ICICIBANK 1d | 5 | 57 | 11 | 0.561 | 0.474 | 0.386 | 0.270 | 0.298 | 0.465 | 0.77 / 0.93 |

(10- and 60-session horizons are in `reports/latest/accuracy.csv`; 120 and 250 sessions were skipped as
`INSUFFICIENT_HISTORY` — fewer than 12 independent labels.)

**Finding: no model beats the base-rate baseline out of sample.** Training accuracy (0.69–0.98) far exceeds
out-of-sample accuracy — overfitting — and the high 15-minute accuracy is the majority class (most one-hour
moves do not clear ~20 bps of costs). Under the paired champion test no challenger was ever promoted, so
**no replay prediction was actionable and all three stocks are NO TRADE for the session after 2026-11-13.**

Disclosure: the first real replay used a margin-only champion rule; it promoted models on noise and produced
9 actionable replay trades (3 wins, 6 losses, mean −0.33% net). The paired one-sided t-test was added after
seeing that, so this replay is not an independent validation of the new rule.

Machine-generated rules (re-searched every 10 sessions, BH-corrected) had small out-of-sample samples
(1–10 trades each), mostly negative; none was FDR-selected, so none was actionable. Library templates on the
RELIANCE 15m lane: VWAP reclaim 46 trades, 28% wins, −0.21% mean; opening-range breakout 29 trades, 34% wins,
−0.13% mean. 66 signals of discovered strategies were skipped because they fell inside the data the strategy
was discovered on.

## Discovery results (chronological discovery / validation / test inside each lane)

| Symbol | Budget | Lanes run | Tests | Tiers |
| --- | --- | --- | ---: | --- |
| RELIANCE | deep | 1m 5m 15m 30m 60m 1d 1w | 216,130 | 2 ROBUST_CANDIDATE, 2 PROMISING_NOT_SIGNIFICANT, 4 VALIDATION_ONLY, 7 REJECTED_TEST, 128 REJECTED_VALIDATION, 5 EXPLORATORY |
| RELIANCE | standard | 5m 15m 30m 60m 1d 1w | — | 2 PROMISING_NOT_SIGNIFICANT, 1 VALIDATION_ONLY, 4 REJECTED_TEST, 57 REJECTED_VALIDATION |
| HDFCBANK | standard | 1d 1w (intraday: 10 sessions < 40) | — | 24 REJECTED_VALIDATION |
| ICICIBANK | standard | 1d 1w (intraday: 10 sessions < 40) | — | 2 PROMISING_NOT_SIGNIFICANT, 7 VALIDATION_ONLY, 4 REJECTED_TEST, 11 REJECTED_VALIDATION |

The batched broad screen found no FDR survivor on any lane (min q = 1.0); a few rules passed later
validation/test (ICICIBANK 1d/1w `SCREEN_VALIDATED`).

Sample strategies with their actual numbers (net of cost assumptions):

1. **RELIANCE 1d, ROBUST_CANDIDATE** — `ATR(14)/close > 0.02228` (top 5% volatility) AND weekly RSI(14) < 70;
   exit: 5-bar time exit, stop 3×ATR, target 1.5×ATR, trailing 2.5×ATR. Discovery 8 trades +1.43% mean
   (87.5% wins), validation 5 trades +1.31%, test 17 trades **+0.29%** (53% wins, profit factor 1.20, max
   drawdown −9.2%); BH q = 0.039 over 216,130 tests. Small samples, and the deep budget was a second search
   after the standard one — treat it as a hypothesis to watch in live forecasts, not a proven edge.
2. **RELIANCE 5m, PROMISING_NOT_SIGNIFICANT** — RSI(25) crosses below 70 AND 60-minute RSI(14) < 70; 24-bar exit,
   3×ATR stop. Discovery 20 trades +0.06%, validation 7 trades +0.11%, test 6 trades +0.01%; q = 0.162.
3. **ICICIBANK 1d, SCREEN_VALIDATED** — SMA(100) slope < 0.00051 AND weekly ADX DI-spread < −1.85; discovery 14
   trades +1.51%, validation 8 trades +0.79%, test 12 trades +0.49%; not FDR-significant (q = 1.0).

## Remaining external blockers

1. **Native-Windows GPU execution** — needs the owner to run `windows\install.ps1 -Gpu` (downloads the CUDA 12.8
   PyTorch wheel, about 3 GB) and `windows\gpu-verify.cmd` on Windows.
2. **Authoritative session documentation** — the 15:20 closing-phase rule is an observed interpretation; an NSE
   document would make it authoritative. No holiday calendar: next-session dates are next-weekday estimates.
3. **Corporate actions** — no local evidence; large gaps are flagged, never adjusted.
4. **Minute history** — only RELIANCE has a long 2026 minute history; other symbols have 10 sessions, so
   their intraday lanes are honestly reported as insufficient.
