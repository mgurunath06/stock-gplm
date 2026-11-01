# One-segment real-data acceptance

This workflow evaluates exactly one already-local Shoonya publication triplet. It
does not connect to Oracle, copy files, discover other symbols, import a universe,
train models, or trade.

## Required local triplet

Preserve these names and their directory exactly:

```text
/home/mgurunath/projects/stock-gplm/data/raw/shoonya/NSE/RELIANCE-EQ/
├── 2026-10-01_2026-10-07.json.gz
├── 2026-10-01_2026-10-07.meta.json
└── 2026-10-01_2026-10-07.json.gz.done
```

The files are not currently present in the workspace. Once they arrive through a
separately authorized process, run this exact command from any directory:

```bash
/home/mgurunath/projects/stock-gplm/.venv/bin/python \
  -m stock_gplm.acceptance_cli \
  /home/mgurunath/projects/stock-gplm/data/raw/shoonya/NSE/RELIANCE-EQ/2026-10-01_2026-10-07.json.gz \
  --output-dir /home/mgurunath/projects/stock-gplm/reports/acceptance
```

The command accepts one `.json.gz` path only. It derives the adjacent metadata and
manifest names, fingerprints all three before validation, runs publication and
content checks, then fingerprints all three again. Any observed source change
during the run forces `REJECT`. The workflow never writes in the raw segment
directory and refuses an output directory inside it.

Two new files are created without overwriting existing reports:

```text
reports/acceptance/
├── RELIANCE-EQ_2026-10-01_2026-10-07.acceptance.json
└── RELIANCE-EQ_2026-10-01_2026-10-07.acceptance.txt
```

The JSON report includes each source path, byte count, SHA-256 before and after,
publication-integrity results, metadata checks, schema, record count, input
chronology, normalized candles, content issues, decision, and reasons. The text
file contains the decision and a concise review summary. The same summary and
artifact paths are printed to the terminal.

## Decision contract

| Decision | Meaning | Exit code |
| --- | --- | ---: |
| `ACCEPT` | Triplet is unchanged, publication integrity passes, and every record passes confirmed `ShoonyaDailyV1` checks | 0 |
| `QUARANTINE` | Publication is intact, but content fails or uses an unrecognized schema | 1 |
| `REJECT` | A source file is missing, publication integrity fails, or a triplet fingerprint changes during the run | 2 |

Artifact-path errors, including an existing report or output inside the raw
directory, return 3. CLI usage errors use the standard argparse exit behavior.

`ACCEPT` is acceptance of this one segment for downstream normalization. It is
not evidence of a complete historical universe, exchange-calendar completeness,
corporate-action adjustment, or model suitability. `UNVALIDATED_RAW` metadata
does not itself establish content quality; the independent V1 checks do.

## Checks performed

- exact Oracle filename and triplet convention;
- compressed-file SHA-256, byte length, manifest status/name/count;
- optional metadata record count and compressed digest;
- strict gzip and JSON decoding;
- serialized `ShoonyaDailyV1` structure and string types;
- exact Decimal OHLC parsing and whole-number volume;
- UTC `ssboe` date consistency;
- segment-date bounds, duplicates, and chronological direction; and
- ascending in-memory normalization after valid reverse ordering.

Raw files are opened read-only. No missing candle is generated, and a missing
segment is never interpreted as a zero-volume session.
