"""Audit-first local state: one SQLite database (WAL) plus an append-only JSONL event mirror.

Design rules

* Every material action writes an ``events`` row. Rows form a SHA-256 hash chain
  (``prev_hash`` -> ``hash``), so an edited or deleted row is detected by :func:`verify_chain`.
* ``predictions`` and ``evaluations`` are append-only: triggers refuse UPDATE and DELETE. An
  original forecast can never be rewritten; its outcome is a separate, later row.
* Idempotency is enforced by unique keys (a prediction's natural key, a file's path and hash),
  so re-running a command with unchanged inputs inserts nothing new.
* Market data is never copied into the database: files are referenced by path and SHA-256.
* One writer at a time: :class:`ProcessLock` (an exclusive lock file with stale-owner
  detection that works on Windows and POSIX). Readers such as the dashboard need no lock.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import socket
import sqlite3
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence, TextIO

from stock_gplm import locking

SCHEMA_VERSION = 2
# v2 (M5.9A, additive): append-only ``model_approvals``. No existing table or row is changed; a v1
# database is upgraded in place by creating the new table and recording a SCHEMA_MIGRATED event.

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, command TEXT NOT NULL, args_json TEXT NOT NULL,
  started_utc TEXT NOT NULL, finished_utc TEXT, status TEXT NOT NULL, device TEXT, code_sha TEXT,
  config_sha TEXT, host TEXT, pid INTEGER, summary_json TEXT);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, run_id TEXT, kind TEXT NOT NULL,
  level TEXT NOT NULL, subject TEXT, payload_json TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS events_subject ON events(subject);
CREATE TABLE IF NOT EXISTS data_files (
  path TEXT PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, period TEXT NOT NULL,
  bytes INTEGER, mtime_ns INTEGER, sha256 TEXT, records INTEGER, decision TEXT NOT NULL, detail_json TEXT,
  first_seen_utc TEXT NOT NULL, last_checked_utc TEXT NOT NULL, run_id TEXT);
CREATE INDEX IF NOT EXISTS data_files_symbol ON data_files(symbol, timeframe);
CREATE TABLE IF NOT EXISTS data_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, period TEXT NOT NULL,
  code TEXT NOT NULL, severity TEXT NOT NULL, file TEXT NOT NULL, detail_json TEXT, first_seen_utc TEXT NOT NULL,
  run_id TEXT, UNIQUE(symbol, timeframe, period, code, file));
CREATE TABLE IF NOT EXISTS candle_cache (
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL, fingerprint TEXT NOT NULL, path TEXT NOT NULL, rows INTEGER,
  first TEXT, last TEXT, built_utc TEXT NOT NULL, run_id TEXT, summary_json TEXT, PRIMARY KEY(symbol, timeframe));
CREATE TABLE IF NOT EXISTS strategies (
  strategy_id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, origin TEXT NOT NULL,
  immutable INTEGER NOT NULL, active INTEGER NOT NULL, timeframe TEXT, scope_json TEXT NOT NULL, path TEXT NOT NULL,
  sha256 TEXT NOT NULL, parent_id TEXT, parent_version INTEGER, created_utc TEXT NOT NULL, provenance_json TEXT,
  PRIMARY KEY(strategy_id, version));
CREATE TABLE IF NOT EXISTS models (
  model_id TEXT PRIMARY KEY, lane_key TEXT NOT NULL, kind TEXT NOT NULL, horizon INTEGER NOT NULL,
  created_utc TEXT NOT NULL, train_end_label TEXT, train_rows INTEGER, path TEXT, sha256 TEXT, device TEXT,
  feature_config_sha TEXT, data_fingerprint TEXT, metrics_json TEXT, run_id TEXT);
CREATE INDEX IF NOT EXISTS models_lane ON models(lane_key, horizon, kind);
CREATE TABLE IF NOT EXISTS champions (
  lane_key TEXT NOT NULL, horizon INTEGER NOT NULL, model_kind TEXT NOT NULL, model_id TEXT, since_utc TEXT NOT NULL,
  reason TEXT NOT NULL, metrics_json TEXT, run_id TEXT, PRIMARY KEY(lane_key, horizon));
CREATE TABLE IF NOT EXISTS training_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, lane_key TEXT NOT NULL, model_kind TEXT NOT NULL,
  horizon INTEGER NOT NULL, step_label TEXT NOT NULL, epoch INTEGER NOT NULL, loss REAL, train_acc REAL,
  rows INTEGER, device TEXT, seconds REAL, ts_utc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS th_lane ON training_history(lane_key, horizon, model_kind);
CREATE TABLE IF NOT EXISTS predictions (
  prediction_id TEXT PRIMARY KEY, natural_key TEXT NOT NULL UNIQUE, mode TEXT NOT NULL, run_id TEXT NOT NULL,
  replay_id TEXT, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, lane_key TEXT NOT NULL, source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL, source_version TEXT, horizon_bars INTEGER NOT NULL, decision_label TEXT NOT NULL,
  decision_ns INTEGER NOT NULL, target_session TEXT, direction TEXT NOT NULL, prob_raw REAL, prob_calibrated REAL,
  calibration TEXT, actionable INTEGER NOT NULL, entry_ref REAL, stop REAL, target REAL, cost_bps REAL,
  data_fingerprint TEXT, feature_config_sha TEXT, created_utc TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pred_lane ON predictions(lane_key, decision_ns);
CREATE INDEX IF NOT EXISTS pred_symbol ON predictions(symbol, mode);
CREATE INDEX IF NOT EXISTS pred_replay ON predictions(replay_id, lane_key, source_kind, source_id, horizon_bars);
CREATE TABLE IF NOT EXISTS evaluations (
  prediction_id TEXT PRIMARY KEY REFERENCES predictions(prediction_id), evaluated_utc TEXT NOT NULL,
  run_id TEXT NOT NULL, outcome_label TEXT, outcome_ns INTEGER, realized_return REAL, up INTEGER,
  correct INTEGER, brier REAL, trade_net_return REAL, exit_reason TEXT, bars_held INTEGER, payload_json TEXT);
CREATE TABLE IF NOT EXISTS api_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, provider TEXT NOT NULL, model TEXT, endpoint TEXT,
  purpose TEXT NOT NULL, request_id TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
  cost_basis TEXT NOT NULL, latency_ms REAL, success INTEGER NOT NULL, error TEXT, run_id TEXT, subject TEXT);
CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY, run_id TEXT, payload_json TEXT NOT NULL,
  payload_sha TEXT NOT NULL, updated_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS config_snapshots (sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, content TEXT NOT NULL,
  first_seen_utc TEXT NOT NULL, run_id TEXT);
CREATE TABLE IF NOT EXISTS feature_snapshots (
  sha256 TEXT PRIMARY KEY, contract TEXT NOT NULL, content TEXT NOT NULL,
  created_utc TEXT NOT NULL, run_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS m56_catalogues (
  catalogue_sha256 TEXT PRIMARY KEY, source_path TEXT NOT NULL, contract TEXT NOT NULL,
  strategy_count INTEGER NOT NULL, manifest_path TEXT NOT NULL, created_utc TEXT NOT NULL, run_id TEXT);
CREATE TABLE IF NOT EXISTS m56_coverage (
  run_id TEXT NOT NULL, reference_id TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
  status TEXT NOT NULL, reason TEXT NOT NULL, requirements_json TEXT NOT NULL, strategy_sha256 TEXT NOT NULL,
  created_utc TEXT NOT NULL, PRIMARY KEY(run_id, reference_id, symbol, timeframe));
CREATE INDEX IF NOT EXISTS m56_coverage_status ON m56_coverage(run_id, status);
CREATE TABLE IF NOT EXISTS m56_results (
  result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
  classification TEXT NOT NULL, origin TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
  regime TEXT NOT NULL, evidence_stage TEXT NOT NULL, metrics_json TEXT NOT NULL, lineage_json TEXT NOT NULL,
  data_fingerprint TEXT NOT NULL, device TEXT NOT NULL, created_utc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS m56_results_board ON m56_results(symbol, timeframe, regime, classification);
CREATE TABLE IF NOT EXISTS m56_method_runs (
  method_run_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, method TEXT NOT NULL, symbol TEXT,
  timeframe TEXT, status TEXT NOT NULL, device TEXT NOT NULL, checkpoint_key TEXT,
  generated INTEGER NOT NULL, evaluated INTEGER NOT NULL, detail_json TEXT NOT NULL, created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_approvals (
  model_id TEXT NOT NULL, artifact_sha256 TEXT NOT NULL, approved_by TEXT NOT NULL, approved_utc TEXT NOT NULL,
  scope_json TEXT NOT NULL, evidence_json TEXT NOT NULL, run_id TEXT, PRIMARY KEY(model_id, artifact_sha256));
CREATE TRIGGER IF NOT EXISTS model_approvals_no_update BEFORE UPDATE ON model_approvals
  BEGIN SELECT RAISE(ABORT, 'model approvals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS model_approvals_no_delete BEFORE DELETE ON model_approvals
  BEGIN SELECT RAISE(ABORT, 'model approvals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_update BEFORE UPDATE ON predictions
  BEGIN SELECT RAISE(ABORT, 'predictions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_delete BEFORE DELETE ON predictions
  BEGIN SELECT RAISE(ABORT, 'predictions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS evaluations_no_update BEFORE UPDATE ON evaluations
  BEGIN SELECT RAISE(ABORT, 'evaluations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS evaluations_no_delete BEFORE DELETE ON evaluations
  BEGIN SELECT RAISE(ABORT, 'evaluations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS feature_snapshots_no_update BEFORE UPDATE ON feature_snapshots
  BEGIN SELECT RAISE(ABORT, 'feature snapshots are append-only'); END;
CREATE TRIGGER IF NOT EXISTS feature_snapshots_no_delete BEFORE DELETE ON feature_snapshots
  BEGIN SELECT RAISE(ABORT, 'feature snapshots are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS m56_coverage_no_update BEFORE UPDATE ON m56_coverage
  BEGIN SELECT RAISE(ABORT, 'M5.6 coverage is append-only'); END;
CREATE TRIGGER IF NOT EXISTS m56_coverage_no_delete BEFORE DELETE ON m56_coverage
  BEGIN SELECT RAISE(ABORT, 'M5.6 coverage is append-only'); END;
CREATE TRIGGER IF NOT EXISTS m56_results_no_update BEFORE UPDATE ON m56_results
  BEGIN SELECT RAISE(ABORT, 'M5.6 results are append-only'); END;
CREATE TRIGGER IF NOT EXISTS m56_results_no_delete BEFORE DELETE ON m56_results
  BEGIN SELECT RAISE(ABORT, 'M5.6 results are append-only'); END;
"""

GENESIS = "0" * 64


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _clean(value: Any) -> Any:
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "item") and callable(value.item):  # numpy scalar
        return _clean(value.item())
    if isinstance(value, Path):
        return str(value)
    return value


def jdump(value: Any) -> str:
    return json.dumps(_clean(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha(value: Any) -> str:
    return hashlib.sha256((value if isinstance(value, str) else jdump(value)).encode("utf-8")).hexdigest()


class LockBusy(RuntimeError):
    """Another GPLM process holds the writer lock."""


class ProcessLock:
    """Persistent kernel lock, released automatically on process death.

    Acquisition waits up to timeout_seconds (default: fail immediately). Metadata
    left by an interrupted owner is reported only after exclusive acquisition.
    Never unlink this file: all contenders must lock the same file identity.
    """

    def __init__(self, path: Path, timeout_seconds: float = 0):
        self.timeout_seconds = max(0, timeout_seconds)
        self.path = Path(path)
        self.held = False
        self._stream: TextIO | None = None
        self.stale_takeover: dict[str, Any] | None = None

    def acquire(self, purpose: str) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        info = {"pid": os.getpid(), "host": socket.gethostname(), "purpose": purpose, "since_utc": utc_now()}
        if self.held:
            raise LockBusy(f"lock already acquired: {self.path}")
        if self.path.is_symlink():
            raise LockBusy(f"writer lock must not be a symlink: {self.path}")
        stream = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                locking.flock(stream.fileno(), locking.LOCK_EX | locking.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise LockBusy(f"another GPLM process holds {self.path}")
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            except BaseException:
                stream.close()
                raise
        try:
            stream.seek(0)
            try:
                owner = json.loads(stream.read())
            except ValueError:
                owner = {}
            if owner:
                self.stale_takeover = owner
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(info))
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        self.held = True
        return self

    def release(self) -> None:
        if self.held:
            assert self._stream is not None
            try:
                self._stream.seek(0)
                self._stream.truncate()
                self._stream.flush()
                locking.flock(self._stream.fileno(), locking.LOCK_UN)
            finally:
                self._stream.close()
                self.held = False
                self._stream = None

    def __enter__(self) -> "ProcessLock":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


class AuditDB:
    """Thin, explicit wrapper. All writes go through methods that also record an event."""

    def __init__(self, path: Path, events_dir: Path | None = None, run_id: str | None = None, readonly: bool = False):
        self.path = Path(path)
        self.events_dir = Path(events_dir) if events_dir else self.path.parent / "events"
        self.readonly = readonly
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(f"no GPLM database at {self.path}; run `gplm ingest` or `gplm daily` first")
            self.conn = sqlite3.connect(f"file:{urllib.parse.quote(self.path.resolve().as_posix())}?mode=ro", uri=True, timeout=30,
                                        check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if cur is None:
                self.conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(cur[0]) == 1 and SCHEMA_VERSION == 2:
                # additive migration: the v2 table was created above by the idempotent schema script
                self.conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
                self._migrated_from = 1
            elif int(cur[0]) != SCHEMA_VERSION:
                raise RuntimeError(f"database schema {cur[0]} != supported {SCHEMA_VERSION}")
            self.conn.commit()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.run_id = run_id
        if getattr(self, "_migrated_from", None):
            self.event("SCHEMA_MIGRATED", {"from": self._migrated_from, "to": SCHEMA_VERSION,
                                           "change": "added append-only model_approvals (M5.9A); no rows rewritten"})

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def q(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r is not None else None

    # ------------------------------------------------------------------ events (hash chain)
    def event(self, kind: str, payload: Mapping[str, Any] | None = None, *, subject: str | None = None,
              level: str = "INFO", run_id: str | None = None) -> int:
        body = jdump(payload or {})
        ts = utc_now()
        rid = run_id if run_id is not None else self.run_id
        with self.tx() as c:
            prev = c.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev[0] if prev else GENESIS
            digest = sha(f"{prev_hash}|{ts}|{rid}|{kind}|{level}|{subject}|{body}")
            cur = c.execute("INSERT INTO events (ts_utc, run_id, kind, level, subject, payload_json, prev_hash, hash) "
                            "VALUES (?,?,?,?,?,?,?,?)", (ts, rid, kind, level, subject, body, prev_hash, digest))
            seq = int(cur.lastrowid or 0)
        self._mirror({"seq": seq, "ts_utc": ts, "run_id": rid, "kind": kind, "level": level, "subject": subject,
                      "payload": json.loads(body), "hash": digest})
        return seq

    def _mirror(self, record: Mapping[str, Any]) -> None:
        self.events_dir.mkdir(parents=True, exist_ok=True)
        day = str(record["ts_utc"])[:10]
        with (self.events_dir / f"events-{day}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(jdump(record) + "\n")

    def verify_chain(self) -> dict[str, Any]:
        prev = GENESIS
        n = 0
        for r in self.conn.execute("SELECT * FROM events ORDER BY seq"):
            r = dict(r)
            expect = sha(f"{prev}|{r['ts_utc']}|{r['run_id']}|{r['kind']}|{r['level']}|{r['subject']}|{r['payload_json']}")
            if r["prev_hash"] != prev or r["hash"] != expect:
                return {"ok": False, "events_checked": n, "broken_at_seq": r["seq"]}
            prev = r["hash"]
            n += 1
        return {"ok": True, "events_checked": n, "head": prev}

    # ------------------------------------------------------------------ runs
    def start_run(self, kind: str, command: str, args: Mapping[str, Any], *, device: str | None, code_sha: str,
                  config_sha: str) -> str:
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{kind}-{uuid.uuid4().hex[:8]}"
        with self.tx() as c:
            c.execute("INSERT INTO runs (run_id, kind, command, args_json, started_utc, status, device, code_sha, "
                      "config_sha, host, pid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (run_id, kind, command, jdump(args), utc_now(), "RUNNING", device, code_sha, config_sha,
                       socket.gethostname(), os.getpid()))
        self.run_id = run_id
        self.event("RUN_STARTED", {"kind": kind, "command": command, "args": args, "device": device,
                                   "code_sha": code_sha, "config_sha": config_sha}, subject=run_id)
        return run_id

    def finish_run(self, status: str, summary: Mapping[str, Any]) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET finished_utc=?, status=?, summary_json=? WHERE run_id=?",
                      (utc_now(), status, jdump(summary), self.run_id))
        self.event("RUN_FINISHED", {"status": status, **summary}, subject=self.run_id,
                   level="INFO" if status == "COMPLETED" else ("WARN" if status == "PARTIAL" else "ERROR"))

    def snapshot_config(self, path: Path, content: str) -> str:
        digest = sha(content)
        with self.tx() as c:
            new = c.execute("INSERT OR IGNORE INTO config_snapshots VALUES (?,?,?,?,?)",
                            (digest, str(path), content, utc_now(), self.run_id)).rowcount
        if new:
            self.event("CONFIG_SNAPSHOT", {"path": str(path), "sha256": digest}, subject=str(path))
        return digest

    # ------------------------------------------------------------------ checkpoints
    def put_checkpoint(self, key: str, payload: Mapping[str, Any]) -> None:
        body = jdump(payload)
        with self.tx() as c:
            c.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET run_id=excluded.run_id, "
                      "payload_json=excluded.payload_json, payload_sha=excluded.payload_sha, updated_utc=excluded.updated_utc",
                      (key, self.run_id, body, sha(body), utc_now()))

    def get_checkpoint(self, key: str) -> dict[str, Any] | None:
        r = self.one("SELECT * FROM checkpoints WHERE key=?", (key,))
        if r is None:
            return None
        if sha(r["payload_json"]) != r["payload_sha"]:
            raise ValueError(f"checkpoint {key} failed its integrity check")
        return json.loads(r["payload_json"])

    # ------------------------------------------------------------------ predictions / evaluations
    def add_feature_snapshot(self, snapshot: Mapping[str, Any]) -> str:
        """Store one immutable snapshot by content hash and return its digest."""
        values = dict(snapshot)
        digest = str(values.pop("snapshot_sha256", "") or sha(values))
        body = jdump({**values, "snapshot_sha256": digest})
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO feature_snapshots VALUES (?,?,?,?,?)",
                      (digest, str(snapshot["contract"]), body, utc_now(), self.run_id))
        return digest

    def get_feature_snapshot(self, digest: str) -> dict[str, Any] | None:
        row = self.one("SELECT content FROM feature_snapshots WHERE sha256=?", (digest,))
        return json.loads(row["content"]) if row is not None else None

    def add_prediction(self, row: Mapping[str, Any]) -> bool:
        """Insert one forecast. Returns False (and inserts nothing) when its natural key exists."""
        cols = ["prediction_id", "natural_key", "mode", "run_id", "replay_id", "symbol", "timeframe", "lane_key",
                "source_kind", "source_id", "source_version", "horizon_bars", "decision_label", "decision_ns",
                "target_session", "direction", "prob_raw", "prob_calibrated", "calibration", "actionable", "entry_ref",
                "stop", "target", "cost_bps", "data_fingerprint", "feature_config_sha", "created_utc", "payload_json"]
        values = dict(row)
        values.setdefault("run_id", self.run_id)
        values.setdefault("created_utc", utc_now())
        values["payload_json"] = jdump(values.get("payload_json") or values.get("payload") or {})
        values.setdefault("prediction_id", sha(values["natural_key"])[:24])
        if int(values.get("actionable") or 0):
            from .contracts import validate_actionable_row

            missing = validate_actionable_row(values, json.loads(values["payload_json"]))
            if missing:
                raise ValueError(f"actionable prediction rejected: missing mandatory fields {missing}")
        with self.tx() as c:
            n = c.execute(f"INSERT OR IGNORE INTO predictions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [_clean(values.get(k)) for k in cols]).rowcount
        return bool(n)

    def model_approval(self, model_id: str | None, artifact_sha256: str | None) -> dict[str, Any] | None:
        """Recorded approval of one immutable model artifact, or None. Fail-closed: a model kind such as
        ``logit``, a null id or a digest that does not match the recorded artifact is never approved."""
        if not model_id or not artifact_sha256:
            return None
        row = self.one("SELECT * FROM model_approvals WHERE model_id=? AND artifact_sha256=?",
                       (model_id, artifact_sha256))
        if row is None:
            return None
        art = self.one("SELECT sha256 FROM models WHERE model_id=?", (model_id,))
        return row if art is not None and art["sha256"] == artifact_sha256 else None

    def record_model_approval(self, model_id: str, artifact_sha256: str, approved_by: str,
                              evidence: Mapping[str, Any], scope: Mapping[str, Any] | None = None) -> bool:
        """Append an owner approval for one stored, immutable model artifact. Evidence is required; the
        program never calls this on its own."""
        if not approved_by.strip() or not evidence:
            raise ValueError("approval needs an approver and recorded evidence")
        art = self.one("SELECT sha256 FROM models WHERE model_id=?", (model_id,))
        if art is None or art["sha256"] != artifact_sha256:
            raise ValueError(f"model artifact {model_id} with sha256 {artifact_sha256} is not a stored artifact")
        with self.tx() as c:
            n = c.execute("INSERT OR IGNORE INTO model_approvals VALUES (?,?,?,?,?,?,?)",
                          (model_id, artifact_sha256, approved_by, utc_now(), jdump(scope or {}), jdump(evidence),
                           self.run_id)).rowcount
        if n:
            self.event("MODEL_APPROVED", {"model_id": model_id, "artifact_sha256": artifact_sha256,
                                          "approved_by": approved_by}, subject=model_id)
        return bool(n)

    def add_evaluation(self, row: Mapping[str, Any]) -> bool:
        cols = ["prediction_id", "evaluated_utc", "run_id", "outcome_label", "outcome_ns", "realized_return", "up",
                "correct", "brier", "trade_net_return", "exit_reason", "bars_held", "payload_json"]
        values = dict(row)
        values.setdefault("run_id", self.run_id)
        values.setdefault("evaluated_utc", utc_now())
        values["payload_json"] = jdump(values.get("payload_json") or {})
        with self.tx() as c:
            n = c.execute(f"INSERT OR IGNORE INTO evaluations ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [_clean(values.get(k)) for k in cols]).rowcount
        return bool(n)

    # ------------------------------------------------------------------ export / backup
    def export_table(self, table: str, fmt: str, where: str = "", params: Sequence[Any] = ()) -> str:
        allowed = {"runs", "events", "data_files", "data_issues", "candle_cache", "strategies", "models", "champions",
                   "training_history", "predictions", "evaluations", "api_calls", "checkpoints", "m56_catalogues",
                   "m56_coverage", "m56_results", "m56_method_runs", "feature_snapshots", "model_approvals"}
        if table not in allowed:
            raise ValueError(f"unknown table {table}")
        rows = self.q(f"SELECT * FROM {table} {where}", params)
        if fmt == "json":
            return json.dumps(_clean(rows), indent=1, sort_keys=True)
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        return buf.getvalue()

    def backup(self, dest_dir: Path, keep: int = 10) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"gplm-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite"
        target = sqlite3.connect(dest)
        with target:
            self.conn.backup(target)
        target.close()
        backups = sorted(dest_dir.glob("gplm-*.sqlite"))
        for old in backups[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        return dest


def wait_for(predicate: Any, timeout: float = 5.0) -> bool:  # small helper for tests
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def iter_rows(db: AuditDB, sql: str, params: Iterable[Any] = ()) -> Iterator[dict[str, Any]]:
    for r in db.conn.execute(sql, tuple(params)):
        yield dict(r)
