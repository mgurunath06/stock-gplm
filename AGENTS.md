# Stock GPLM project rules

- Scope: local M1 ingestion and M2/M2.1 historical/reference-data validation, plus
  M3.5 research-only daily model training (authorized by the user on 2026-11-02).
  No orders, live signals, remote synchronization, Oracle changes, or credential
  access. M3.5 training runs on local CPU from manifest-verified M3.1/M3.2 runs only,
  fits LightGBM only when every required daily gate passes, otherwise runs only the
  policy's exploratory baselines, and never scores a locked holdout twice. Minute
  fusion is not a prerequisite for daily-only training.
- M3.6 reference evidence enters only as files a person downloaded in a browser.
  NSE's Terms of Use prohibit automated collection, so never script NSE downloads or
  get around its access controls. Registered sources are immutable and hash-bound.
  The weekend rule needs a registered NSE schedule document. The locked holdout is
  scored only after the policy's predefined promotion criteria pass.
- M3.7 (authorized 2026-11-02) may fit LightGBM on blocked-gate daily data only as a
  separate `EXPLORATORY_BIASED` pipeline. It shares the 20-trial-per-horizon research
  ledger, never reads the locked holdout, writes no ranking, never edits gates and is
  never promoted. ETFs stay out of the equity universe.
- M3.8 diagnostics replay already-counted trials only, after verifying them against stored
  metrics. They leave the trial ledger byte-identical. Hypotheses are pre-registered and
  logged, with Bonferroni correction over every test. Promoting any of them into a model
  is a new trial.
- M5.2 controlled handover (authorized by the user on 2026-11-14) has four
  separately governed tiers. T1 metadata-only inventory is authorized. T2
  byte-level integrity plus metadata and publication-marker validation is
  authorized. T1 or T2 authorization never implies T3 or T4 authorization.
  T3 governed development-data preparation is **NOT AUTHORIZED**. T4 protected
  holdout operations are **PROHIBITED**. Because the independently governed
  holdout cutoff is unavailable, M5.2 must not decompress or decode any candle
  payload. It may use read-only commands over the existing Oracle minute tree and
  collection summary solely to capture hashes and provenance; it must not rerun a
  collector or download candle files. Local and Oracle raw files, metadata and
  markers remain immutable. Reconciliation work stops at an import and quarantine
  plan unless the user separately authorizes execution. No T1/T2 result authorizes
  data relocation, derived datasets, features, labels, splits, eligibility,
  training, scoring, rankings, trading, trust-root changes or holdout access.
- M5.5 (`stock_gplm.app`, the `gplm` application; authorized by the owner on 2026-11-15) is an
  independent research workflow under the `OWNER_AUTHORIZED_FULL_LOCAL` basis: it may read every
  locally available daily and minute candle, including 2026. M3.2 records and its split plan stay
  unchanged; its cutoff governs only M3.x experiments. The application issues research forecasts
  (REPLAY and LIVE, clearly separated) but never places orders or contacts a broker, never handles
  provider credentials, keeps predictions and evaluations append-only, never fills or repairs
  candles, and documents every data interpretation (see `docs/m55-data-investigation.md`).
- LLM assistance (`stock_gplm.llm`) is opt-in and advisory. Keys come only from
  environment variables or a secret store. Never send raw market datasets, private
  strategies or credentials to a remote provider without explicit approval. LLM
  output cannot change gates, splits, labels, metrics, rankings or risk rules.
- Use Python 3.12 and the workspace .venv. Keep dependencies minimal, code typed,
  interfaces documented, and tests synthetic and reproducible.
- Raw provider files, metadata, and publication markers are immutable. Open them
  only for reading. Never fill missing candles, silently repair values, or hide
  rejected records. Reports must live outside raw directories.
- Reject unpublished, corrupt, unsupported, or inconsistent inputs. A checksum
  proves byte integrity, not authenticity or market-data correctness.
- Keep Oracle publication integrity and provider-content quality as separate
  decisions. `UNVALIDATED_RAW` is metadata, never proof of candle quality.
- `ShoonyaDailyV1` is the only confirmed content schema. Other record shapes stay
  quarantined until a sanitized provider sample supports a versioned adapter.
- Real-data acceptance is limited to one explicitly supplied local triplet. Write
  reports outside raw directories and never overwrite prior acceptance evidence.
- M2 calendar, listing, alias, provider-outage, and corporate-action facts require
  explicit provenance. Missing evidence remains `UNKNOWN`; never infer weekends,
  holidays, IPO membership, outages, adjustments, or historical completeness.
- M2.1 reference payloads require a versioned, checksum-bound manifest. A current
  instrument master supplies current identifiers only; it cannot prove historical
  membership. Raw-candle acceptance, historical completeness, and training
  readiness are independent decisions.
- The observed Shoonya NSE master V1 schema is a single `NSE_symbols.txt` CSV in
  ZIP with `Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,`.
  The blank trailing column is real and ISIN is absent. Validate changes instead
  of coercing another layout into this contract.
- Never evaluate provider text as code. Bound compressed and decompressed input.
- Keep current documentation in docs/ and update it alongside implementation.
- Future research must use point-in-time universes, chronological horizon-specific
  splits, purging/embargo where needed, locked holdouts and trial budgets,
  realistic costs/slippage/liquidity, calibrated outcome probabilities, experiment
  provenance, and benchmark-relative out-of-sample evaluation. Never fabricate
  performance, candles, or missing-history explanations.
- Run `.venv/bin/python -m pytest` after changes. Distinguish synthetic evidence
  from real collector compatibility. Do not claim checks that were not executed.
