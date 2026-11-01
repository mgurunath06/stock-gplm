# M1 local validation

M1 reads local Oracle-style Shoonya daily publications without changing them. It
does not connect to Oracle, synchronize files, use credentials, train models, or
place trades.

## Run locally

```bash
python3.12 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pytest

.venv/bin/python -m stock_gplm.cli \
  '/local/data/daily/shoonya/NSE/RELIANCE-EQ/2026-10-01_2026-10-07.json.gz'
```

VS Code selects `${workspaceFolder}/.venv/bin/python`. JSON reports go to stdout
and status logs go to stderr. Store redirected reports outside the immutable raw
tree. Exit code 0 requires publication integrity and confirmed-schema content
quality to pass.

## Confirmed Oracle publication contract

The expected layout is:

```text
data/daily/shoonya/NSE/<SYMBOL>/
├── <START_DATE>_<END_DATE>.json.gz
├── <START_DATE>_<END_DATE>.meta.json
└── <START_DATE>_<END_DATE>.json.gz.done
```

Segment dates use ISO `YYYY-MM-DD`, must be valid dates, and start must not follow
end. The symbol is reported from the containing directory; M1 does not use that
directory name as proof of historical universe membership.

Publication integrity passes only when all confirmed checks pass:

- manifest `status` is exactly `COMPLETE`;
- manifest `file` equals the compressed filename;
- manifest `sha256` equals SHA-256 of the exact compressed bytes read;
- manifest `bytes` equals their length;
- decompression yields a nonempty JSON list;
- manifest `records` equals list length;
- metadata `records`, when present, equals list length; and
- metadata `sha256_compressed`, when present, equals the calculated digest.

Metadata must be a JSON object. `quality_status`, when present, must be a string
and is copied into the report. `UNVALIDATED_RAW` is expected and does not fail
publication integrity. It also gives no evidence that any candle is valid.
Metadata fields such as `source`, `symbol`, `from`, `to`, and `fetched_at_utc` are
preserved in the source but not interpreted in M1 because their exact types and
semantics have not been confirmed. The manifest does not authenticate metadata,
so changing an unrelated metadata field cannot be detected by its checksum.

The validator reads the compressed file once, hashes those in-memory bytes, and
decompresses the same bytes. Inputs are bounded at 16 MiB compressed, 32 MiB
expanded, 64 KiB per sidecar, and 100,000 records. Hash agreement establishes
internal publication consistency, not authenticity or market correctness.

## Separate content-quality stage

Content checks run only after publication integrity passes. `ShoonyaDailyV1` is
based on the sanitized `RELIANCE-EQ` record observed in
`2026-10-01_2026-10-07.json.gz`. Every list item must be a serialized JSON string
that decodes to an object with exactly the seven V1 fields below. `stat` is not a
V1 field and is not required. All field values must be strings.

| V1 field | Meaning | Validation and normalization |
| --- | --- | --- |
| `time` | trading date | exact uppercase `DD-MON-YYYY` |
| `into` | open | positive fixed-point decimal string |
| `inth` | high | positive fixed-point decimal string |
| `intl` | low | positive fixed-point decimal string |
| `intc` | close | positive fixed-point decimal string |
| `intv` | volume | nonnegative fixed-point string representing a whole number |
| `ssboe` | source Unix timestamp | unsigned integer string; UTC date must equal `time` |

The timestamp policy is
`ssboe_posix_seconds_utc_date_equals_trading_date`: `ssboe` is decoded as POSIX
seconds in UTC, and its UTC calendar date must equal `time`. For the observed
record, `1791331200` becomes `2026-10-07T00:00:00Z`, matching `07-OCT-2026`.
M1 does not claim that the epoch represents exchange open, close, or local
midnight; only its confirmed date consistency is used.

OHLC values are parsed with `decimal.Decimal`, retained exactly, and emitted as
strings in JSON reports. Scientific notation, signs, whitespace, NaN, and infinity
are rejected. Prices must be positive and satisfy
`low <= open/close <= high`. Volume must be integral after Decimal parsing;
`11880731.00` normalizes to integer `11880731`. Explicit zero volume is accepted.

Serialized rows are parsed only with strict `json.loads`; Python literals,
executable expressions, nonstandard JSON constants, and duplicate object keys are
rejected. Dictionary rows, missing or extra fields, non-string values, and other
shapes are treated as unrecognized schemas and receive
`QUARANTINE_UNVERIFIED_SCHEMA`.

Duplicate dates and chronology that changes direction fail content quality.
Ascending input remains ascending. Valid reverse-chronological input is reported
as `descending` and copied into an in-memory ascending normalized list. Raw source
bytes and order are untouched. Records are never repaired, filled, or silently
deduplicated. Returned dates must fall inside the filename range; the first and
last dates need not equal its boundaries.

The report has independent `publication_integrity` and `content_quality` sections.
Disposition is `REJECT_PUBLICATION`, `QUARANTINE_CONTENT`,
`QUARANTINE_UNVERIFIED_SCHEMA`, or `ACCEPT`. Only records that structurally match
the confirmed V1 schema and pass every configured check can produce `ACCEPT`.

## Explicitly unverified or unsupported

- Only one sanitized V1 record shape has been confirmed. Whether the provider may
  add fields or return alternate versions under other conditions remains unknown;
  those shapes are deliberately quarantined.
- `ssboe` is confirmed to match the UTC trading date for the observed record. Its
  broader provider semantics and behavior around boundary timestamps remain
  unverified.
- The filename range is not asserted to equal the first and last returned candle;
  it may describe a request window rather than observed sessions.
- No exchange calendar, holidays, expected sessions, listing/delisting history,
  symbol changes, corporate actions, tick size, currency, or adjustment status is
  validated.
- Missing segments and missing dates are classified as unassessed. They are never
  converted to zero-volume days.
- The current NSE instrument master is not used to prove historical membership.
- Cross-file overlap and duplicates are not checked in M1.
- No real Oracle file was read; tests establish synthetic behavior only.

## Synthetic evidence and project files

`tests/conftest.py` publishes deterministic fixtures with the confirmed names and
sidecar fields. Tests cover manifest and metadata checks, tampering, gzip/JSON
failures, serialized rows, resource limits, OHLCV, timestamps, duplicates,
chronology, immutability, quarantine, and CLI behavior. The executed example is
[synthetic-quality-report.json](examples/synthetic-quality-report.json).

```text
stock-gplm/
├── .env.example
├── .gitignore
├── .vscode/settings.json
├── AGENTS.md
├── README.md
├── pyproject.toml
├── requirements-dev.lock
├── config/m2/{instrument-inventory.json,nse-trading-calendar.json,reliance-quality-request.json}
├── docs/
│   ├── README.md
│   ├── architecture.md
│   ├── m1-ingestion.md
│   ├── m2-historical-quality.md
│   ├── real-data-acceptance.md
│   └── examples/synthetic-quality-report.json
├── src/stock_gplm/
│   ├── __init__.py
│   ├── acceptance.py
│   ├── acceptance_cli.py
│   ├── cli.py
│   ├── ingestion/{__init__.py,daily.py,shoonya_daily_v1.py}
│   └── m2/{__init__.py,cli.py,models.py,quality.py}
└── tests/{conftest.py,test_acceptance.py,test_daily.py,test_m2_models.py,test_m2_quality.py}
```

Runtime dependencies: none. Test versions are pinned in `requirements-dev.lock`.
The existing workspace has no Git repository, so M1 did not initialize or commit
one.
