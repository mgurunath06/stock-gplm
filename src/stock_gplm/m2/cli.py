"""Generate M2 inventory and historical-quality reports from explicit local inputs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .models import (
    CorporateActionCatalog,
    HorizonRequest,
    InstrumentInventory,
    NSETradingCalendar,
    ProviderUnavailable,
)
from .quality import analyze_history

EXIT_CODES = {"ACCEPT": 0, "QUARANTINE": 1, "REJECT": 2}


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_new(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--quality-config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("segments", nargs="+", type=Path)
    args = parser.parse_args()

    try:
        inventory = InstrumentInventory.load(args.inventory)
        calendar = NSETradingCalendar.load(args.calendar)
        config = _load_object(args.quality_config)
        if type(config.get("schema_version")) is not int or config.get("schema_version") != 1:
            raise ValueError("unsupported quality-config schema_version")
        horizons = [HorizonRequest.from_dict(item) for item in config.get("horizons", [])]
        catalog_value = config.get("corporate_action_catalog")
        catalog = (
            CorporateActionCatalog.from_dict(catalog_value)
            if isinstance(catalog_value, dict) else None
        )
        unavailable = [
            ProviderUnavailable.from_dict(item)
            for item in config.get("provider_unavailable", [])
        ]
        report = analyze_history(
            symbol=args.symbol,
            segment_paths=args.segments,
            inventory=inventory,
            calendar=calendar,
            horizons=horizons,
            corporate_action_catalog=catalog,
            provider_unavailable=unavailable,
        )
        output_dir = args.output_dir.resolve()
        for segment in args.segments:
            raw_dir = segment.resolve().parent
            if output_dir == raw_dir or raw_dir in output_dir.parents:
                raise ValueError("output directory must be outside every raw segment directory")
        output_dir.mkdir(parents=True, exist_ok=True)
        inventory_path = output_dir / f"{args.symbol}.inventory.json"
        quality_path = output_dir / f"{args.symbol}.historical-quality.json"
        if inventory_path.exists() or quality_path.exists():
            raise FileExistsError("M2 report already exists; refusing to overwrite")
        _write_new(inventory_path, inventory.to_dict())
        _write_new(quality_path, report.to_dict())
    except (FileExistsError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"M2 quality error: {exc}", file=sys.stderr)
        return 3

    print(f"Decision: {report.decision}")
    print(f"Symbol: {report.symbol}")
    print(f"Reasons: {', '.join(report.reasons)}")
    print(f"Inventory report: {inventory_path}")
    print(f"Quality report: {quality_path}")
    return EXIT_CODES[report.decision]


if __name__ == "__main__":
    raise SystemExit(main())
