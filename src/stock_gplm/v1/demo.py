"""Create a non-overwriting synthetic demonstration of both V1 strategies."""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .backtest import run_backtest
from .evaluator import evaluate_strategy
from .registry import StrategyRegistry


def synthetic_demo_candles() -> list[dict[str, Any]]:
    """Return artificial OHLCV designed to exercise distinct strategy rules."""
    rows: list[dict[str, Any]] = []
    start = date(2025, 1, 1)
    for index in range(300):
        close = 100.0 + index * 0.05
        volume = 1_000.0
        if index == 260:
            close = 114.5  # prior-20-session-high breakout with volume confirmation
            volume = 3_000.0
        elif index == 269:
            close = 108.0  # artificial one-session oversold displacement
        elif index == 270:
            close = 112.5  # rebound below MA10 while still above MA100
        rows.append(
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close - 0.1,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": volume,
            }
        )
    return rows


def run_demo(output_dir: Path, config_dir: Path) -> dict[str, Any]:
    """Create a registry and report; refuse to overwrite any prior evidence."""
    output_dir.mkdir(parents=True, exist_ok=False)
    registry = StrategyRegistry(output_dir / "strategy-registry.sqlite3")
    rows = synthetic_demo_candles()
    demonstrations: list[dict[str, Any]] = []
    for filename in ("swing-breakout-v1.json", "mean-reversion-v1.json"):
        config = json.loads((config_dir / filename).read_text(encoding="utf-8"))
        record = registry.save(config)
        evaluation = evaluate_strategy(record.config, rows)
        result = run_backtest(
            record.config,
            rows,
            strategy_version=record.version,
            data_quality_eligible=True,  # only the explicitly synthetic fixture above
        )
        registry.record_backtest(result)
        signal_dates = [date_value for date_value, signal in zip(evaluation.dates, evaluation.entry_signals) if signal]
        demonstrations.append(
            {
                "strategy_id": record.strategy_id,
                "strategy_version": record.version,
                "config_hash": record.config_hash,
                "entry_signal_dates": signal_dates,
                "trade_count": len(result.trades),
                "backtest_result_hash": result.result_hash,
                "trades": [
                    {
                        "entry_signal_date": trade.entry_signal_date,
                        "entry_date": trade.entry_date,
                        "exit_date": trade.exit_date,
                        "exit_reason": trade.exit_reason,
                        "net_return": trade.net_return,
                    }
                    for trade in result.trades
                ],
            }
        )
    report = {
        "schema_version": 1,
        "classification": "SYNTHETIC_ENGINEERING_DEMO_NOT_MARKET_EVIDENCE",
        "execution_policy": "completed_close_signal_next_session_open_v1",
        "candle_count": len(rows),
        "strategies": demonstrations,
    }
    (output_dir / "demo-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory outside raw-data trees")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config/v1/strategies"),
        help="directory containing the two shipped JSON definitions",
    )
    args = parser.parse_args()
    report = run_demo(args.output_dir, args.config_dir)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
