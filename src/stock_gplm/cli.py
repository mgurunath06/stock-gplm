"""Local validation CLI: JSON on stdout, status on stderr, no raw-file writes."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from stock_gplm.ingestion import validate_daily


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path, help="local .json.gz publications")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    reports = [validate_daily(path) for path in args.files]
    for report in reports:
        logging.info(
            "%s: publication=%s content=%s disposition=%s",
            report.source,
            report.publication_integrity.status,
            report.content_quality.status,
            report.disposition,
        )
    print(json.dumps({"schema_version": 1, "reports": [r.to_dict() for r in reports]},
                     indent=2, allow_nan=False))
    return 0 if all(report.ready_for_normalization for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
