# M5.9B acceptance — calendar registration, approval controls and safe fixed-model LIVE forecasting

Date 2026-11-16. Branch `feature/m55-gplm-app`. Starting commit `3e1169b` (M5.9A). Evidence class:
**synthetic unit tests only (8 cases)**; no real data, training, replay, backtest, full suite or GPU job.
Technical reference: `docs/m59b-governance.md`. M5.9A questions: `docs/m59a-compliance-addendum.md`.
Test ledger: `docs/m59b-test-budget.md`.

## Acceptance criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Four pending M5.9A compliance questions resolved | Met | compliance addendum A–D (C was a real gap, now fixed) |
| M5.9A test count documented without rerunning | Met | compliance addendum E (139–167 recorded executions, 99 distinct; overlap explained) |
| Local calendar registered, validated, activated, audited | Met | test 1; `calendar_registrations`/`calendar_events` + events |
| Calendar absence / insufficient coverage fails closed | Met | test 1 (`CALENDAR_UNKNOWN`, no weekday fallback), test 7 |
| Immutable artifacts registered, approved, revoked | Met | tests 2, 3 |
| Approved fixed-model path never retrains / updates | Met | test 4 (fit, train_network, Platt fit patched to fail; artifact hash unchanged; deterministic) |
| Adaptive models remain SHADOW/RESEARCH | Met | gate `ADAPTIVE_MODEL_NOT_LIVE` (test 7); rolling passes no model evidence (static review) |
| Minimum strategy lifecycle exists and is enforced | Met | test 5 |
| Only approved strategies produce strategy-driven LIVE | Met | test 7 (unapproved, missing, retired → SHADOW); rolling looks up lifecycle |
| Versioned Nifty 200 / supplementary universe registered and activated | Met (synthetic file) | test 6 |
| Symbols outside the active universe cannot become LIVE | Met | tests 6, 7 |
| One canonical gate controls LIVE | Met | `contracts.live_decision`, used by fixed-model and rolling paths; DB guard (test 7) |
| Actionable output requires every mandatory field | Met | DB contract + action/freshness tightening (test 7) |
| Legacy records unchanged and identifiable | Met | additive v3 migration, `shared_status`, verifier info (test 8) |
| Verifier detects unsafe/legacy rows without modifying them | Met | test 8 (all 10 M5.9B classes; DB file hash unchanged) |
| ≤ 10 test cases executed | Met | 8 executed, 8 passed |
| No bulk processing / real-data research / replay / training / full suite / GPU | Met | — |
| Locally committed; nothing pushed, merged or deployed | Met | see handover |
| Working tree clean except documented pre-existing files | Met | `.git.wsl-backup`, the PRD markdown, `config/app/gplm.windows.json` (untracked, untouched) |

## Not verified here (owner)

* The full suite, including four M5.9A tests statically edited for intended contract changes (listed in
  the test ledger) and the rolling-engine tests reviewed statically.
* Any real calendar, Nifty 200 file, model artifact, approval or strategy evidence (none registered).
* CLI commands end to end against the owner database (only the library functions were tested).

## Owner commands (run locally; not run by Claude)

PowerShell from `H:\Guru\Learning\StockGLPM\app`. `$DB = "H:\Guru\Learning\StockGLPM\state\gplm.sqlite"`
(adjust to your home). Replace `"Owner Name"` with your identity (or set `governance.approver` in the config).
The first governance write migrates the database to schema v3 (additive); back it up first if you wish:
`.\windows\gplm.cmd audit --backup`.

1. Full test suite

   ```powershell
   $env:PYTHONPATH = "src"; .\.venv\Scripts\python.exe -m pytest -q
   ```

   Expect the 4 known `test_m54_unified.py` failures (`KeyError: '4h'`, pre-existing) and nothing else.

2. Calendar registration (your own browser-downloaded NSE holiday list converted to the CSV format in
   `docs/m59b-governance.md` §2, with every date — weekends and holidays as `CLOSED` — listed)

   ```powershell
   .\windows\gplm.cmd calendar validate --file H:\Guru\Learning\StockGLPM\reference\nse-calendar-2026.csv
   .\windows\gplm.cmd calendar register --file H:\Guru\Learning\StockGLPM\reference\nse-calendar-2026.csv --by "Owner Name"
   .\windows\gplm.cmd calendar list
   .\windows\gplm.cmd calendar activate --calendar-id cal-nse-<id> --by "Owner Name" --note "NSE circular <ref>"
   ```

3. Universe registration (current Nifty 200 constituent CSV you downloaded; must have a `Symbol` column)

   ```powershell
   .\windows\gplm.cmd universe register --file H:\Guru\Learning\StockGLPM\reference\ind_nifty200list.csv --name nifty200 --as-of 2026-11-13 --by "Owner Name"
   .\windows\gplm.cmd universe activate --universe-id uni-nifty200-<id> --by "Owner Name"
   .\windows\gplm.cmd universe supplement-add --stock IRCTC --reason "personal watch-list" --by "Owner Name"
   .\windows\gplm.cmd universe check --stock RELIANCE
   ```

4. Bounded model registration (freezes one model your rolling run already saved; nothing is trained)

   ```powershell
   .\windows\gplm.cmd audit --export models --format csv --out models.csv
   Import-Csv models.csv | Where-Object { $_.lane_key -eq 'RELIANCE-EQ|1d' -and $_.kind -eq 'logit' -and $_.horizon -eq '1' } |
     Sort-Object created_utc -Descending | Select-Object -First 3 model_id, train_end_label, sha256
   .\windows\gplm.cmd model register --from-model-id "<model_id from above>" --scope-symbols RELIANCE --evidence "<path to your OOS report>" --by "Owner Name"
   .\windows\gplm.cmd model inspect --model-id fm-<id>
   ```

5. Approval / revocation (only after you have reviewed real evidence)

   ```powershell
   .\windows\gplm.cmd model approve --model-id fm-<id> --evidence "<OOS report>" --evidence "<paper-trade log>" --expires 2027-02-01 --by "Owner Name"
   .\windows\gplm.cmd model revoke --model-id fm-<id> --reason "<why>" --by "Owner Name"
   .\windows\gplm.cmd strategies lifecycle <strategy_id> --version 1
   .\windows\gplm.cmd strategies advance <strategy_id> --version 1 --to VALIDATED --evidence "<ref>" --by "Owner Name"
   ```

6. Read-only verification

   ```powershell
   $env:PYTHONPATH = "src"; .\.venv\Scripts\python.exe -m stock_gplm.app.m59a_verify --db $DB
   ```

   Expect `ok: true` (exit 0); legacy rows appear under `info` (`legacy_unverified_live`, status and
   contract-version distributions) and are never modified.

7. One-stock fixed-model smoke

   ```powershell
   .\windows\gplm.cmd model predict --stock RELIANCE --model-id fm-<id> --as-of 2026-11-13
   ```

   Expect `SHADOW` with `MODEL_NOT_APPROVED` for an unapproved candidate; `REFUSED` if the artifact's
   feature schema or cost policy no longer matches the configuration; `LIVE` only with an active covering
   calendar, fresh data, RELIANCE in the active universe, an approved unexpired artifact and every action
   gate passing. Re-running the same command stores nothing new.
