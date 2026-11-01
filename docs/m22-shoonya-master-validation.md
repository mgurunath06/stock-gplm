# M2.2 real Shoonya NSE instrument-master validation

M2.2 validates the explicitly supplied local file at
`data/reference/source/shoonya/NSE_symbols.txt.zip`. The validator reads the ZIP
in memory and writes only to a separate reports directory. It never extracts into,
renames, touches, or rewrites the source.

## Confirmed V1 schema

The real ZIP contains one unencrypted member named `NSE_symbols.txt`. It is UTF-8
CSV with this exact eight-column header:

```text
Exchange,Token,LotSize,Symbol,TradingSymbol,Instrument,TickSize,
```

The eighth column has a blank name and is empty in all records. There is no ISIN
column. The versioned `stock-gplm.shoonya-nse-instrument-master.v1` validator
rejects another header instead of guessing a mapping.

Validation includes bounded archive/member sizes, compression ratio, safe member
path, encryption state, UTF-8 decoding, row width, required values, NSE exchange,
numeric token, positive integral lot size, finite nonnegative tick size, unique
tokens and trading symbols, and EQ-specific symbol and positive-tick rules.

## Real validation result

```text
Decision: ACCEPT
SHA-256: 129b0eb840d854bceb181da6f0dca67fc5f25a355f6f1325677b95f005f4aee9
ZIP bytes: 100944
Rows: 10029
EQ instruments: 2715
Unique EQ base symbols: 2715
Unique EQ trading symbols: 2715
Unique EQ tokens: 2715
Available EQ ISINs: 0
Unique all-series base symbols: 10013
Unique all-series trading symbols: 10029
Unique all-series tokens: 10029
Invalid identifiers: 0
Duplicate tokens: 0
Duplicate trading symbols: 0
Duplicate EQ base symbols: 0
```

Fifteen base `Symbol` values are reused across different series in the full
master. These are explicitly listed in the machine report. They are not token or
trading-symbol collisions, and none creates an EQ duplicate, so they are reported
as series-level reuse rather than rejected.

Run the validator locally:

```bash
.venv/bin/python -m stock_gplm.m2.master_cli \
  data/reference/source/shoonya/NSE_symbols.txt.zip \
  --output-dir reports/reference/shoonya
```

The command refuses to overwrite existing reports and refuses output inside the
source directory. It produces:

- `NSE_symbols.validation.json`: complete machine-readable evidence;
- `NSE_symbols.validation.txt`: concise human summary.

## Provenance and research boundary

The report records the source name, supplied path, SHA-256, validation timestamp,
and observed filesystem timestamp. The filesystem timestamp is explicitly marked
as not being acquisition evidence. The source does not supply a publication date,
acquisition timestamp, or effective date, so those fields remain `null`.

Therefore the file-validation decision is `ACCEPT`, while reference readiness is
`INCOMPLETE` and historical membership is `NOT_ESTABLISHED`. Current membership,
absence from this snapshot, or a current Shoonya token must never be used as proof
of historical NSE universe membership.
