# Stock GPLM

Local Indian equity quantitative research platform. M1 provides read-only daily
publication validation using synthetic tests. No models, synchronization, or
trading are implemented. Start with [the documentation index](docs/README.md).

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python -m stock_gplm.cli \
  '/local/data/daily/shoonya/NSE/RELIANCE-EQ/2026-10-01_2026-10-07.json.gz'
```

For the read-only one-segment acceptance command and its `ACCEPT`, `QUARANTINE`,
and `REJECT` contract, see
[docs/real-data-acceptance.md](docs/real-data-acceptance.md).

M2 point-in-time inventory, explicit calendar, corporate-action metadata, and
cross-segment quality reporting are documented in
[docs/m2-historical-quality.md](docs/m2-historical-quality.md).

M2.1 adds checksum-bound, read-only reference-data import contracts and coverage
reporting. See [docs/m21-reference-data.md](docs/m21-reference-data.md). No
authoritative NSE calendar payload is currently available locally.

M2.2 validates the locally supplied real Shoonya NSE instrument master against
its observed provider schema. See [docs/m22-shoonya-master-validation.md](docs/m22-shoonya-master-validation.md).

M2.3 builds deterministic, checksum-bound, streamed inventories for explicitly
approved local OHLCV trees and reconciles every current EQ symbol, including
zero-artifact symbols. See [docs/m23-historical-inventory.md](docs/m23-historical-inventory.md).

M2.4 implements bounded, resumable, marker-first Oracle-to-WSL synchronization
through the fixed `sne-cloud` OpenSSH profile. No real transfer or scheduler was
executed. See [docs/m24-secure-sync.md](docs/m24-secure-sync.md).

M2.5 provides a synthetically tested Oracle deployment package for cache-first
Shoonya OAuth/TOTP renewal. The offline runtime and encrypted credentials are
deployed, but live authentication and scheduling remain disabled. See
[docs/m25-oracle-authentication.md](docs/m25-oracle-authentication.md).

M3 builds deterministic local descriptive datasets and exploratory SVG reports
from validated immutable publications while keeping missing reference evidence
explicitly incomplete. See [docs/m3-data-readiness.md](docs/m3-data-readiness.md).
M3.2 adds point-in-time feature, label and evaluation contracts without training any
model. See [docs/m32-research-framework.md](docs/m32-research-framework.md).

M3.4 adds separate immutable minute ingestion, quality checks, and causal fusion with
prior-session daily features. Coverage is partial and training stays `NOT_READY`. See
[docs/m34-minute-ingestion-fusion.md](docs/m34-minute-ingestion-fusion.md).

M3.5 adds research-only daily training on local CPU, with daily-only gates, a trial
ledger, a one-time locked holdout, a cost model and a calibrated, evidence-labelled
ranking. Reference evidence is still missing, so only exploratory baselines have run on
real data. See [docs/m35-daily-training.md](docs/m35-daily-training.md). Optional,
advisory LLM assistance is described in [docs/llm-assistance.md](docs/llm-assistance.md).

M3.6 adds an offline intake for NSE reference files that you download in a browser,
because NSE's terms prohibit automated collection. It also adds strict adapters, a
manifest-bound reference bundle with a counted gap report, ETF separation, and promotion
criteria that must pass before the locked holdout is scored. No NSE file has been
supplied yet, so every reference gate stays blocked. See
[docs/m36-reference-evidence.md](docs/m36-reference-evidence.md).
