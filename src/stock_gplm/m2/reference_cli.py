"""Import local M2.1 reference bundles and emit normalized coverage reports."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .coverage import build_reference_coverage_report
from .models import HorizonRequest
from .reference_data import (
    import_corporate_actions_v1,
    import_historical_symbol_mappings_v1,
    import_listing_history_v1,
    import_nse_calendar_v1,
    import_shoonya_instrument_master_v1,
    merge_historical_symbol_mappings,
    merge_listing_history,
)


def _load_request(path: Path) -> tuple[list[str], list[HorizonRequest]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"schema_version", "symbols", "horizons"}:
        raise ValueError("coverage request must contain schema_version,symbols,horizons")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported coverage-request schema_version")
    symbols = value["symbols"]
    if not isinstance(symbols, list) or not symbols or any(
        not isinstance(item, str) or not item for item in symbols
    ):
        raise ValueError("coverage request symbols must be a nonempty string array")
    if len(symbols) != len(set(symbols)):
        raise ValueError("coverage request contains duplicate symbols")
    horizons_value = value["horizons"]
    if not isinstance(horizons_value, list):
        raise ValueError("coverage request horizons must be an array")
    return symbols, [HorizonRequest.from_dict(item) for item in horizons_value]


def _write_new(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar-payload", required=True, type=Path)
    parser.add_argument("--calendar-manifest", required=True, type=Path)
    parser.add_argument("--instrument-payload", required=True, type=Path)
    parser.add_argument("--instrument-manifest", required=True, type=Path)
    parser.add_argument("--symbol-mapping-payload", type=Path)
    parser.add_argument("--symbol-mapping-manifest", type=Path)
    parser.add_argument("--corporate-actions-payload", type=Path)
    parser.add_argument("--corporate-actions-manifest", type=Path)
    parser.add_argument("--listing-history-payload", type=Path)
    parser.add_argument("--listing-history-manifest", type=Path)
    parser.add_argument("--coverage-request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    optional_pairs = (
        (args.symbol_mapping_payload, args.symbol_mapping_manifest, "symbol mapping"),
        (args.corporate_actions_payload, args.corporate_actions_manifest, "corporate actions"),
        (args.listing_history_payload, args.listing_history_manifest, "listing history"),
    )
    try:
        for payload, manifest, label in optional_pairs:
            if (payload is None) != (manifest is None):
                raise ValueError(f"{label} requires both payload and manifest")
        calendar = import_nse_calendar_v1(args.calendar_payload, args.calendar_manifest)
        master = import_shoonya_instrument_master_v1(
            args.instrument_payload, args.instrument_manifest
        )
        inventory = master.inventory
        mappings = None
        if args.symbol_mapping_payload:
            mappings = import_historical_symbol_mappings_v1(
                args.symbol_mapping_payload, args.symbol_mapping_manifest
            )
            inventory = merge_historical_symbol_mappings(inventory, mappings)
        listing = None
        if args.listing_history_payload:
            listing = import_listing_history_v1(
                args.listing_history_payload, args.listing_history_manifest
            )
            inventory = merge_listing_history(inventory, listing)
        actions = None
        if args.corporate_actions_payload:
            actions = import_corporate_actions_v1(
                args.corporate_actions_payload, args.corporate_actions_manifest
            )
        symbols, horizons = _load_request(args.coverage_request)
        sources = [calendar.manifest, master.manifest]
        if mappings:
            sources.append(mappings.manifest)
        if listing:
            sources.append(listing.manifest)
        if actions:
            sources.append(actions.manifest)
        coverage = build_reference_coverage_report(
            symbols=symbols,
            horizons=horizons,
            inventory=inventory,
            calendar=calendar.calendar,
            corporate_action_catalogs=list(actions.catalogs) if actions else [],
            sources=sources,
        )

        output_dir = args.output_dir.resolve()
        source_files = [
            args.calendar_payload, args.calendar_manifest,
            args.instrument_payload, args.instrument_manifest,
        ]
        if args.symbol_mapping_payload:
            source_files.extend([args.symbol_mapping_payload, args.symbol_mapping_manifest])
        if args.corporate_actions_payload:
            source_files.extend([args.corporate_actions_payload, args.corporate_actions_manifest])
        if args.listing_history_payload:
            source_files.extend([args.listing_history_payload, args.listing_history_manifest])
        for source_file in source_files:
            source_dir = source_file.resolve().parent
            if output_dir == source_dir or source_dir in output_dir.parents:
                raise ValueError("output directory must be outside reference source directories")
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs = {
            "instrument-inventory.json": inventory.to_dict(),
            "nse-trading-calendar.json": calendar.calendar.to_dict(),
            "reference-coverage.json": coverage.to_dict(),
            "reference-manifests.json": {
                "schema_version": 1,
                "sources": [item.to_dict() for item in sources],
            },
            "historical-symbol-mappings.json": {
                "schema_version": 1,
                "records": [asdict(item) for item in mappings.records] if mappings else [],
                "coverage_status": (
                    mappings.manifest.coverage_status if mappings else "UNKNOWN"
                ),
            },
            "listing-history.json": {
                "schema_version": 1,
                "records": [asdict(item) for item in listing.records] if listing else [],
                "coverage_status": listing.manifest.coverage_status if listing else "UNKNOWN",
            },
            "corporate-actions.json": {
                "schema_version": 1,
                "catalogs": [asdict(item) for item in actions.catalogs] if actions else [],
                "coverage_status": actions.manifest.coverage_status if actions else "UNKNOWN",
            },
        }
        if any((output_dir / name).exists() for name in outputs):
            raise FileExistsError("M2.1 report already exists; refusing to overwrite")
        for name, value in outputs.items():
            _write_new(output_dir / name, value)
    except (FileExistsError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"M2.1 reference-data error: {exc}", file=sys.stderr)
        return 3

    print(f"Reference coverage: {coverage.decision}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Coverage report: {output_dir / 'reference-coverage.json'}")
    return 0 if coverage.decision == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
