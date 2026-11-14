"""Immutable SQLite strategy registry and provenance records."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .backtest import BacktestResult
from .schema import ValidatedStrategy, validate_strategy


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    version: int
    config_hash: str
    created_at: str
    updated_at: str
    validation_status: str
    activation_status: str
    config: dict[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StrategyRegistry:
    """Local registry; activation selects research config and never routes orders."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS strategy_versions (
                    strategy_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    config_hash TEXT NOT NULL CHECK(length(config_hash) = 64),
                    canonical_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    validation_status TEXT NOT NULL CHECK(validation_status = 'VALID'),
                    PRIMARY KEY(strategy_id, version),
                    UNIQUE(strategy_id, config_hash)
                );
                CREATE TABLE IF NOT EXISTS active_strategies (
                    strategy_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    activated_at TEXT NOT NULL,
                    FOREIGN KEY(strategy_id, version) REFERENCES strategy_versions(strategy_id, version)
                );
                CREATE TABLE IF NOT EXISTS backtest_results (
                    result_hash TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version INTEGER NOT NULL,
                    config_hash TEXT NOT NULL,
                    data_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(strategy_id, strategy_version) REFERENCES strategy_versions(strategy_id, version)
                );
                CREATE TABLE IF NOT EXISTS paper_intents (
                    intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    strategy_version INTEGER NOT NULL,
                    config_hash TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    signal_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status = 'SIMULATED_NOT_ROUTABLE'),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(strategy_id, strategy_version) REFERENCES strategy_versions(strategy_id, version)
                );
                CREATE TRIGGER IF NOT EXISTS strategy_versions_no_update BEFORE UPDATE ON strategy_versions
                BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS strategy_versions_no_delete BEFORE DELETE ON strategy_versions
                BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS backtest_results_no_update BEFORE UPDATE ON backtest_results
                BEGIN SELECT RAISE(ABORT, 'backtest results are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS backtest_results_no_delete BEFORE DELETE ON backtest_results
                BEGIN SELECT RAISE(ABORT, 'backtest results are immutable'); END;
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def save(self, strategy: Mapping[str, Any] | ValidatedStrategy) -> StrategyVersion:
        validated = strategy if isinstance(strategy, ValidatedStrategy) else validate_strategy(strategy)
        timestamp = _now()
        with self._connect() as connection:
            # Serialize version allocation so concurrent saves cannot race MAX(version) + 1.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT version FROM strategy_versions WHERE strategy_id = ? AND config_hash = ?",
                (validated.strategy_id, validated.config_hash),
            ).fetchone()
            if existing is not None:
                raise ValueError("this exact strategy configuration is already registered and immutable")
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM strategy_versions WHERE strategy_id = ?",
                (validated.strategy_id,),
            ).fetchone()
            version = int(row["next_version"])
            connection.execute(
                "INSERT INTO strategy_versions VALUES (?, ?, ?, ?, ?, ?, 'VALID')",
                (validated.strategy_id, version, validated.config_hash, validated.canonical_json, timestamp, timestamp),
            )
        return self.get(validated.strategy_id, version)

    def get(self, strategy_id: str, version: int) -> StrategyVersion:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT v.*, CASE WHEN a.version = v.version THEN 'ACTIVE' ELSE 'INACTIVE' END AS activation_status
                   FROM strategy_versions v LEFT JOIN active_strategies a ON a.strategy_id = v.strategy_id
                   WHERE v.strategy_id = ? AND v.version = ?""",
                (strategy_id, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown strategy version {strategy_id}@{version}")
        return StrategyVersion(
            strategy_id=row["strategy_id"], version=row["version"], config_hash=row["config_hash"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            validation_status=row["validation_status"], activation_status=row["activation_status"],
            config=json.loads(row["canonical_json"]),
        )

    def list_versions(self) -> list[StrategyVersion]:
        with self._connect() as connection:
            keys = connection.execute(
                "SELECT strategy_id, version FROM strategy_versions ORDER BY strategy_id, version DESC"
            ).fetchall()
        return [self.get(row["strategy_id"], row["version"]) for row in keys]

    def activate(self, strategy_id: str, version: int) -> StrategyVersion:
        record = self.get(strategy_id, version)
        if not record.config["strategy"]["enabled"]:
            raise ValueError("a disabled configuration cannot be activated")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO active_strategies(strategy_id, version, activated_at) VALUES (?, ?, ?)
                   ON CONFLICT(strategy_id) DO UPDATE SET version = excluded.version, activated_at = excluded.activated_at""",
                (strategy_id, version, _now()),
            )
        return self.get(strategy_id, version)

    def deactivate(self, strategy_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM active_strategies WHERE strategy_id = ?", (strategy_id,))

    def record_backtest(self, result: BacktestResult) -> None:
        if result.strategy_version is None:
            raise ValueError("registered backtests require a strategy version")
        record = self.get(result.strategy_id, result.strategy_version)
        if record.config_hash != result.strategy_hash:
            raise ValueError("backtest strategy hash does not match immutable registry version")
        payload = json.dumps(result.to_dict(), sort_keys=True, allow_nan=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT result_json FROM backtest_results WHERE result_hash = ?", (result.result_hash,)
            ).fetchone()
            if existing is not None:
                # Deterministic reruns are idempotent; a differing payload under one hash is corruption.
                if existing["result_json"] != payload:
                    raise ValueError("stored backtest differs from a result with the same hash")
                return
            connection.execute(
                "INSERT INTO backtest_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                (result.result_hash, result.strategy_id, result.strategy_version, result.strategy_hash,
                 result.data_hash, payload, _now()),
            )

    def create_paper_intent(
        self, strategy_id: str, version: int, *, symbol: str, side: str, quantity: float, signal_at: str
    ) -> int:
        record = self.get(strategy_id, version)
        if record.activation_status != "ACTIVE":
            raise ValueError("paper intents require the exact active strategy version")
        if side not in {"BUY", "SELL"} or not symbol or quantity <= 0:
            raise ValueError("invalid paper intent")
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO paper_intents(strategy_id, strategy_version, config_hash, symbol, side, quantity,
                   signal_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'SIMULATED_NOT_ROUTABLE', ?)""",
                (strategy_id, version, record.config_hash, symbol, side, quantity, signal_at, _now()),
            )
            return int(cursor.lastrowid)

    def paper_intent(self, intent_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM paper_intents WHERE intent_id = ?", (intent_id,)).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return dict(row)
