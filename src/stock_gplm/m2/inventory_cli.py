"""Build a checksum-bound M2.3 inventory from explicitly approved local inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .inventory import run_inventory_v1
from .master_validation import (
    read_shoonya_nse_eq_inventory_v1,
    validate_shoonya_nse_master_v1,
)
from .reference_data import (
    import_corporate_actions_v1,
    import_historical_symbol_mappings_v1,
    import_listing_history_v1,
    import_nse_calendar_v1,
    import_provider_unavailability_v1,
)

CURRENT_EQ_COUNT = 2715


def _pair(payload: Path | None, manifest: Path | None, label: str) -> bool:
    if (payload is None) != (manifest is None):
        raise ValueError(f"{label} requires both payload and manifest")
    return payload is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True, type=Path)
    parser.add_argument("--raw-root", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--expected-eq-count", required=True, type=int)
    for option in (
        "calendar", "listing-history", "symbol-mapping", "corporate-actions",
        "provider-unavailability",
    ):
        parser.add_argument(f"--{option}-payload", type=Path)
        parser.add_argument(f"--{option}-manifest", type=Path)
    args = parser.parse_args()
    try:
        if args.expected_eq_count != CURRENT_EQ_COUNT:
            raise ValueError(
                f"expected EQ count must equal the approved current universe count {CURRENT_EQ_COUNT}"
            )
        supplied = {}
        reference_files: list[Path] = []
        for attribute, label in (
            ("calendar", "calendar"),
            ("listing_history", "listing history"),
            ("symbol_mapping", "symbol mapping"),
            ("corporate_actions", "corporate actions"),
            ("provider_unavailability", "provider unavailability"),
        ):
            payload = getattr(args, f"{attribute}_payload")
            manifest = getattr(args, f"{attribute}_manifest")
            supplied[attribute] = _pair(payload, manifest, label)
            if payload is not None and manifest is not None:
                reference_files.extend((payload, manifest))

        master_report = validate_shoonya_nse_master_v1(args.master)
        if master_report["decision"] != "ACCEPT":
            raise ValueError("M2.2 instrument-master validation rejected the source")
        master_sha256 = hashlib.sha256(args.master.read_bytes()).hexdigest()
        if master_sha256 != master_report["source_integrity"]["sha256"]:
            raise RuntimeError("instrument master changed after validation")
        eq_inventory = read_shoonya_nse_eq_inventory_v1(args.master)
        calendar_import = (
            import_nse_calendar_v1(args.calendar_payload, args.calendar_manifest)
            if supplied["calendar"] else None
        )
        listing = (
            import_listing_history_v1(
                args.listing_history_payload, args.listing_history_manifest
            ) if supplied["listing_history"] else None
        )
        mappings = (
            import_historical_symbol_mappings_v1(
                args.symbol_mapping_payload, args.symbol_mapping_manifest
            ) if supplied["symbol_mapping"] else None
        )
        actions = (
            import_corporate_actions_v1(
                args.corporate_actions_payload, args.corporate_actions_manifest
            ) if supplied["corporate_actions"] else None
        )
        outages = (
            import_provider_unavailability_v1(
                args.provider_unavailability_payload,
                args.provider_unavailability_manifest,
            ) if supplied["provider_unavailability"] else None
        )
        run_dir = run_inventory_v1(
            master_path=args.master,
            master_sha256=master_sha256,
            eq_inventory=eq_inventory,
            raw_root=args.raw_root,
            request_path=args.request,
            output_root=args.output_root,
            expected_eq_count=args.expected_eq_count,
            calendar=calendar_import.calendar if calendar_import else None,
            listing_history=listing,
            mappings=mappings,
            actions=actions,
            unavailability=outages,
            reference_files=tuple(reference_files),
        )
        summary = json.loads((run_dir / "inventory-summary.json").read_text(encoding="utf-8"))
    except (OSError, RuntimeError, TypeError, ValueError, FileExistsError, json.JSONDecodeError) as exc:
        print(f"M2.3 inventory error: {exc}", file=sys.stderr)
        return 3
    print(f"Run ID: {summary['run_id']}")
    print(f"Symbols reconciled: {summary['universe']['reconciled_eq_symbols']}")
    print(f"Zero-artifact symbols: {summary['universe']['zero_artifact_symbols']}")
    print(f"Publication gate: {summary['gates']['publication']}")
    print(f"Candle-content gate: {summary['gates']['candle_content']}")
    print(f"Historical-completeness gate: {summary['gates']['historical_completeness']}")
    print("Training-readiness gate: NOT_READY")
    print(f"Reports: {run_dir}")
    return 0 if all(
        summary["gates"][key] == "ACCEPT"
        for key in ("publication", "candle_content", "historical_completeness")
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
