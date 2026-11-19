"""M5.9B governance registries: exchange calendars, immutable model artifacts, strategy lifecycle and
the live-alert universe. All state lives in the existing audit database (schema v3, additive) in
append-only tables; every write also records a hash-chained ``events`` row.

Nothing here downloads, scrapes, infers or approves anything on its own:

* a calendar is an owner-supplied local CSV; every date in its coverage must be listed explicitly
  (no weekday inference); dates outside coverage are ``UNKNOWN``;
* a model artifact is an existing local ``DirectionModel`` file (``.npz``, loaded with
  ``allow_pickle=False``); registration copies its bytes into a read-only store under an id derived
  from its SHA-256, so any byte change is a different artifact; approval is an explicit owner action;
* strategy lifecycle events attach to the canonical ``strategies`` store (no new strategy registry);
* the live-alert universe is an owner-supplied membership file plus explicitly approved supplements.

Statuses (``ACTIVE``, ``APPROVED``, ``REVOKED``, ...) are derived from the append-only event rows,
never stored as mutable columns.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .db import AuditDB, jdump, utc_now

CALENDAR_SCHEMA = "stock-gplm.exchange-calendar.v1"
UNIVERSE_SCHEMA = "stock-gplm.live-universe.v1"
MODEL_ARTIFACT_CONTRACT = "stock-gplm.fixed-model-artifact.m59b.v1"
SUPPORTED_TIMEZONES = {"NSE": "Asia/Kolkata"}
DAY_STATUSES = ("OPEN", "SPECIAL", "PARTIAL", "CLOSED")
_STATUS_TO_SESSION = {"OPEN": "TRADING_SESSION", "SPECIAL": "SPECIAL_SESSION", "PARTIAL": "SPECIAL_SESSION",
                      "CLOSED": "CLOSED"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class GovernanceError(ValueError):
    """A registry operation was refused (invalid evidence, missing approval, illegal transition)."""


def _file_sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _need(value: str | None, what: str) -> str:
    if not value or not str(value).strip():
        raise GovernanceError(f"{what} is required")
    return str(value).strip()


def _refs(evidence: Sequence[str] | None) -> list[str]:
    return [str(e).strip() for e in (evidence or []) if str(e).strip()]


# =============================================================================== calendar registry
@dataclass
class CalendarParse:
    exchange: str | None
    timezone: str | None
    days: dict[str, dict[str, Any]]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.days)


def parse_calendar(text: str) -> CalendarParse:
    """Validate an owner calendar CSV (``exchange,date,status,open,close,timezone[,note]``).

    ``status`` is OPEN | SPECIAL | PARTIAL | CLOSED; sessions need ``open`` < ``close`` (HH:MM, exchange
    local time); CLOSED rows have no times. One exchange and one supported timezone per file. Every date
    between the first and last listed date must be listed exactly once: a gap, a duplicate or a conflicting
    row is an error (holidays are never inferred from weekdays)."""
    errors: list[str] = []
    days: dict[str, dict[str, Any]] = {}
    exchanges, zones = set(), set()
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    need = {"exchange", "date", "status", "open", "close", "timezone"}
    if not reader.fieldnames or not need <= {f.strip().lower() for f in reader.fieldnames}:
        return CalendarParse(None, None, {}, [f"header must contain {sorted(need)}"])
    for n, raw in enumerate(reader, start=2):
        r = {str(k).strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        ex, d, st, op, cl, tz = (r.get("exchange", "").upper(), r.get("date", ""), r.get("status", "").upper(),
                                 r.get("open", ""), r.get("close", ""), r.get("timezone", ""))
        try:
            date.fromisoformat(d)
            if len(d) != 10:
                raise ValueError
        except ValueError:
            errors.append(f"line {n}: bad date {d!r}")
            continue
        if st not in DAY_STATUSES:
            errors.append(f"line {n}: status {st!r} not in {DAY_STATUSES}")
            continue
        if st == "CLOSED":
            if op or cl:
                errors.append(f"line {n}: CLOSED day {d} must not have open/close times")
                continue
        else:
            if not (_HHMM.match(op) and _HHMM.match(cl)) or op >= cl:
                errors.append(f"line {n}: session {d} needs open < close as HH:MM (got {op!r}-{cl!r})")
                continue
        exchanges.add(ex)
        zones.add(tz)
        row = {"status": st, "open": op or None, "close": cl or None, "note": r.get("note") or None}
        if d in days:
            same = {k: days[d][k] for k in ("status", "open", "close")} == {k: row[k] for k in ("status", "open", "close")}
            errors.append(f"line {n}: {'duplicate' if same else 'conflicting'} entry for {d}")
            continue
        days[d] = row
    if len(exchanges) != 1:
        errors.append(f"exactly one exchange per file required (got {sorted(exchanges)})")
    if len(zones) != 1:
        errors.append(f"exactly one timezone per file required (got {sorted(zones)})")
    ex = next(iter(exchanges)) if len(exchanges) == 1 else None
    tz = next(iter(zones)) if len(zones) == 1 else None
    if ex is not None and tz is not None and SUPPORTED_TIMEZONES.get(ex) != tz:
        errors.append(f"exchange {ex} requires timezone {SUPPORTED_TIMEZONES.get(ex, '<unsupported exchange>')}, got {tz}")
    if days:
        d, end = date.fromisoformat(min(days)), date.fromisoformat(max(days))
        gaps = []
        while d <= end:
            if d.isoformat() not in days:
                gaps.append(d.isoformat())
            d += timedelta(days=1)
        if gaps:
            errors.append(f"coverage gap: {len(gaps)} unlisted date(s) inside coverage, first {gaps[:5]}")
        if not any(v["status"] != "CLOSED" for v in days.values()):
            errors.append("calendar lists no session")
    else:
        errors.append("no calendar rows")
    return CalendarParse(ex, tz, days, errors)


def register_calendar(db: AuditDB, path: Path, actor: str) -> dict[str, Any]:
    """Validate and register (not activate) a local calendar file. Idempotent per content hash."""
    actor = _need(actor, "registered_by")
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    parsed = parse_calendar(data.decode("utf-8"))
    if not parsed.ok:
        raise GovernanceError("calendar invalid: " + "; ".join(parsed.errors[:10]))
    cid = f"cal-{str(parsed.exchange).lower()}-{digest[:16]}"
    if db.one("SELECT 1 FROM calendar_registrations WHERE calendar_id=?", (cid,)):
        return {"calendar_id": cid, "created": False, **calendar_record(db, cid)}
    start, end = min(parsed.days), max(parsed.days)
    with db.tx() as c:
        c.execute("INSERT INTO calendar_registrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, parsed.exchange, Path(path).name, digest, utc_now(), start, end, CALENDAR_SCHEMA,
                   parsed.timezone, jdump(parsed.days), actor, db.run_id))
    db.event("CALENDAR_REGISTERED", {"calendar_id": cid, "exchange": parsed.exchange, "sha256": digest,
                                     "source_filename": Path(path).name, "coverage": [start, end],
                                     "registered_by": actor}, subject=cid)
    return {"calendar_id": cid, "created": True, **calendar_record(db, cid)}


def _active_calendar_ids(db: AuditDB) -> dict[str, str]:
    active: dict[str, str] = {}
    for e in db.q("SELECT calendar_id, exchange, action FROM calendar_events ORDER BY seq"):
        if e["action"] == "ACTIVATE":
            active[e["exchange"]] = e["calendar_id"]
        elif e["action"] == "DEACTIVATE" and active.get(e["exchange"]) == e["calendar_id"]:
            del active[e["exchange"]]
    return active


def calendar_record(db: AuditDB, calendar_id: str) -> dict[str, Any]:
    r = db.one("SELECT * FROM calendar_registrations WHERE calendar_id=?", (calendar_id,))
    if r is None:
        raise GovernanceError(f"unknown calendar {calendar_id}")
    days = json.loads(r.pop("days_json"))
    r["status"] = "ACTIVE" if _active_calendar_ids(db).get(r["exchange"]) == calendar_id else "REGISTERED"
    r["sessions"] = sum(1 for v in days.values() if v["status"] != "CLOSED")
    r["closed_days"] = sum(1 for v in days.values() if v["status"] == "CLOSED")
    r["special_or_partial"] = sorted(d for d, v in days.items() if v["status"] in ("SPECIAL", "PARTIAL"))
    r["events"] = db.q("SELECT seq, action, actor, note, ts_utc FROM calendar_events WHERE calendar_id=? ORDER BY seq",
                       (calendar_id,))
    return r


def list_calendars(db: AuditDB) -> list[dict[str, Any]]:
    return [calendar_record(db, r["calendar_id"]) for r in
            db.q("SELECT calendar_id FROM calendar_registrations ORDER BY registered_utc")]


def set_calendar_active(db: AuditDB, calendar_id: str, active: bool, actor: str, note: str | None = None) -> dict[str, Any]:
    """Explicit activation (supersedes the exchange's previous active calendar) or deactivation."""
    actor = _need(actor, "actor")
    rec = calendar_record(db, calendar_id)
    if not active and rec["status"] != "ACTIVE":
        raise GovernanceError(f"{calendar_id} is not active")
    if active and rec["status"] == "ACTIVE":
        raise GovernanceError(f"{calendar_id} is already active")
    action = "ACTIVATE" if active else "DEACTIVATE"
    with db.tx() as c:
        c.execute("INSERT INTO calendar_events (calendar_id, exchange, action, actor, note, ts_utc, run_id) "
                  "VALUES (?,?,?,?,?,?,?)", (calendar_id, rec["exchange"], action, actor, note, utc_now(), db.run_id))
    db.event(f"CALENDAR_{action}D", {"calendar_id": calendar_id, "exchange": rec["exchange"], "actor": actor,
                                     "note": note}, subject=calendar_id)
    return calendar_record(db, calendar_id)


def active_calendar(db: AuditDB | None, exchange: str = "NSE") -> Any:
    """The active registered calendar as a :class:`~.session_calendar.SessionCalendar`, or None."""
    if db is None:
        return None
    try:
        cid = _active_calendar_ids(db).get(exchange)
    except Exception:  # pre-v3 read-only database: no registry tables
        return None
    if cid is None:
        return None
    r = db.one("SELECT days_json, sha256 FROM calendar_registrations WHERE calendar_id=?", (cid,))
    from .session_calendar import SessionCalendar

    return SessionCalendar.from_registry(cid, json.loads(r["days_json"]), r["sha256"])


# =============================================================================== live-alert universe
def parse_universe(text: str) -> tuple[list[str], list[str]]:
    """Symbols from an owner file: a CSV with a ``Symbol`` column (e.g. an index constituent list) or
    one symbol per line. Returns (normalized symbols, errors). Duplicates are errors."""
    from .ingest import normalize_symbol

    text = text.lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    errors: list[str] = []
    raw: list[str] = []
    if lines and "," in lines[0]:
        reader = csv.DictReader(io.StringIO(text))
        col = next((f for f in (reader.fieldnames or []) if f and f.strip().lower() == "symbol"), None)
        if col is None:
            return [], ["CSV needs a 'Symbol' column"]
        raw = [(r.get(col) or "").strip() for r in reader]
    else:
        raw = [ln.strip() for ln in lines if not ln.lstrip().startswith("#")]
    out: list[str] = []
    for i, s in enumerate(raw, start=1):
        try:
            sym = normalize_symbol(s)
        except ValueError as exc:
            errors.append(f"entry {i}: {exc}")
            continue
        if sym in out:
            errors.append(f"entry {i}: duplicate symbol {sym}")
            continue
        out.append(sym)
    if not out:
        errors.append("no symbols")
    return out, errors


def register_universe(db: AuditDB, path: Path, name: str, membership_as_of: str, actor: str) -> dict[str, Any]:
    actor, name = _need(actor, "registered_by"), _need(name, "name").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
        raise GovernanceError(f"bad universe name {name!r}")
    date.fromisoformat(_need(membership_as_of, "membership_as_of"))
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    syms, errors = parse_universe(data.decode("utf-8"))
    if errors:
        raise GovernanceError("universe invalid: " + "; ".join(errors[:10]))
    uid = f"uni-{name}-{digest[:16]}"
    if db.one("SELECT 1 FROM universe_registrations WHERE universe_id=?", (uid,)):
        return {"universe_id": uid, "created": False, **universe_record(db, uid)}
    with db.tx() as c:
        c.execute("INSERT INTO universe_registrations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (uid, name, Path(path).name, digest, membership_as_of, jdump(sorted(syms)), len(syms),
                   UNIVERSE_SCHEMA, utc_now(), actor, db.run_id))
    db.event("UNIVERSE_REGISTERED", {"universe_id": uid, "sha256": digest, "symbols": len(syms),
                                     "membership_as_of": membership_as_of, "registered_by": actor}, subject=uid)
    return {"universe_id": uid, "created": True, **universe_record(db, uid)}


def _active_universe_id(db: AuditDB) -> str | None:
    active = None
    for e in db.q("SELECT universe_id, action FROM universe_events ORDER BY seq"):
        if e["action"] == "ACTIVATE":
            active = e["universe_id"]
        elif e["action"] == "DEACTIVATE" and active == e["universe_id"]:
            active = None
    return active


def universe_record(db: AuditDB, universe_id: str) -> dict[str, Any]:
    r = db.one("SELECT * FROM universe_registrations WHERE universe_id=?", (universe_id,))
    if r is None:
        raise GovernanceError(f"unknown universe {universe_id}")
    r["symbols"] = json.loads(r.pop("symbols_json"))
    r["status"] = "ACTIVE" if _active_universe_id(db) == universe_id else "REGISTERED"
    return r


def list_universes(db: AuditDB) -> dict[str, Any]:
    rows = []
    for r in db.q("SELECT universe_id FROM universe_registrations ORDER BY registered_utc"):
        rec = universe_record(db, r["universe_id"])
        rec["symbols"] = rec["symbols"][:10]
        rows.append(rec)
    return {"universes": rows, "active": _active_universe_id(db), "supplements": active_supplements(db)}


def set_universe_active(db: AuditDB, universe_id: str, active: bool, actor: str, note: str | None = None) -> dict[str, Any]:
    actor = _need(actor, "actor")
    rec = universe_record(db, universe_id)
    if not active and rec["status"] != "ACTIVE":
        raise GovernanceError(f"{universe_id} is not active")
    if active and rec["status"] == "ACTIVE":
        raise GovernanceError(f"{universe_id} is already active")
    action = "ACTIVATE" if active else "DEACTIVATE"
    with db.tx() as c:
        c.execute("INSERT INTO universe_events (universe_id, action, actor, note, ts_utc, run_id) VALUES (?,?,?,?,?,?)",
                  (universe_id, action, actor, note, utc_now(), db.run_id))
    db.event(f"UNIVERSE_{action}D", {"universe_id": universe_id, "actor": actor, "note": note}, subject=universe_id)
    return universe_record(db, universe_id)


def active_supplements(db: AuditDB) -> dict[str, dict[str, Any]]:
    cur: dict[str, dict[str, Any]] = {}
    for e in db.q("SELECT * FROM universe_supplements ORDER BY seq"):
        if e["action"] == "ADD":
            cur[e["symbol"]] = {"seq": e["seq"], "approved_by": e["actor"], "reason": e["reason"], "since": e["ts_utc"]}
        else:
            cur.pop(e["symbol"], None)
    return cur


def supplement(db: AuditDB, symbol: str, add: bool, actor: str, reason: str) -> dict[str, Any]:
    """Explicitly approve (``add``) or revoke one supplementary live-alert symbol, with a reason."""
    from .ingest import normalize_symbol

    actor, reason = _need(actor, "approved_by"), _need(reason, "reason")
    sym = normalize_symbol(symbol)
    present = sym in active_supplements(db)
    if add and present:
        raise GovernanceError(f"{sym} is already an approved supplement")
    if not add and not present:
        raise GovernanceError(f"{sym} is not an approved supplement")
    action = "ADD" if add else "REVOKE"
    with db.tx() as c:
        c.execute("INSERT INTO universe_supplements (symbol, action, actor, reason, ts_utc, run_id) VALUES (?,?,?,?,?,?)",
                  (sym, action, actor, reason, utc_now(), db.run_id))
    db.event(f"UNIVERSE_SUPPLEMENT_{action}", {"symbol": sym, "actor": actor, "reason": reason}, subject=sym)
    return {"symbol": sym, "action": action, "active_supplements": sorted(active_supplements(db))}


def universe_membership(db: AuditDB | None, symbol: str) -> dict[str, Any]:
    """Live-alert eligibility of one symbol: requires an active registered universe; the symbol must be a
    member of it or an active approved supplement. Research is never restricted by this check."""
    from .ingest import normalize_symbol

    sym = normalize_symbol(symbol)
    if db is None:
        return {"symbol": sym, "eligible": False, "universe_id": None, "basis": None, "reason": "NO_DATABASE"}
    try:
        uid = _active_universe_id(db)
    except Exception:
        uid = None
    if uid is None:
        return {"symbol": sym, "eligible": False, "universe_id": None, "basis": None, "reason": "UNIVERSE_NOT_ACTIVE"}
    if sym in universe_record(db, uid)["symbols"]:
        return {"symbol": sym, "eligible": True, "universe_id": uid, "basis": "MEMBER", "reason": None}
    sup = active_supplements(db).get(sym)
    if sup:
        return {"symbol": sym, "eligible": True, "universe_id": uid, "basis": f"SUPPLEMENT:{sup['seq']}", "reason": None}
    return {"symbol": sym, "eligible": False, "universe_id": uid, "basis": None, "reason": "NOT_IN_LIVE_UNIVERSE"}


# =============================================================================== model artifact registry
MODEL_FAMILIES = ("logit", "mlp")


def cost_policy_version(costs_cfg: Mapping[str, Any]) -> str:
    """Version id of the configured cost policy (hash of the ``costs`` section)."""
    return "costs:" + hashlib.sha256(jdump(dict(costs_cfg)).encode("utf-8")).hexdigest()[:16]


def load_artifact(path: Path) -> Any:
    """Load and schema-check a fixed DirectionModel artifact. Raises GovernanceError when unreadable."""
    from .models import DirectionModel

    try:
        m = DirectionModel.load(Path(path))
    except Exception as exc:  # unreadable, truncated, pickled or foreign file
        raise GovernanceError(f"artifact unreadable: {type(exc).__name__}: {exc}") from exc
    if m.kind not in MODEL_FAMILIES:
        raise GovernanceError(f"artifact family {m.kind!r} is not a fixed-model family {MODEL_FAMILIES}")
    if m.prep is None or not m.params or not len(m.prep.columns):
        raise GovernanceError("artifact has no fitted preprocessing/parameters")
    if int(m.params[0].shape[0]) != int(len(m.prep.columns)) or len(m.params) % 2:
        raise GovernanceError("artifact parameters are inconsistent with its feature columns")
    return m


def register_model(db: AuditDB, source: Path, store_dir: Path, *, timeframe: str, horizon: int, feature_schema: str,
                   training_cutoff: str, cost_policy: str, actor: str, scope: Mapping[str, Any],
                   neutral_band_version: str | None = None, evidence: Sequence[str] | None = None,
                   code_contract_version: str = MODEL_ARTIFACT_CONTRACT) -> dict[str, Any]:
    """Register an existing fixed model file as a ``CANDIDATE``. Registration never approves."""
    from .contracts import NEUTRAL_BAND_VERSION

    actor = _need(actor, "registered_by")
    for v, what in ((timeframe, "timeframe"), (feature_schema, "feature_schema"), (training_cutoff, "training_cutoff"),
                    (cost_policy, "cost_policy_version")):
        _need(v, what)
    if timeframe != "1d":
        raise GovernanceError("M5.9B fixed-model execution supports the 1d lane only")
    if not scope or not (scope.get("shared") is True or scope.get("symbols")):
        raise GovernanceError("scope must be {'shared': true} or {'symbols': [...]} ")
    data = Path(source).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    store_dir.mkdir(parents=True, exist_ok=True)
    tmp = store_dir / f".check-{os.getpid()}.npz"
    tmp.write_bytes(data)
    try:
        model = load_artifact(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if int(model.horizon) != int(horizon):
        raise GovernanceError(f"artifact horizon {model.horizon} != declared horizon {horizon}")
    mid = f"fm-{digest[:24]}"
    existing = db.one("SELECT * FROM model_artifacts WHERE model_id=?", (mid,))
    if existing:
        return {"model_id": mid, "created": False, **model_record(db, mid)}
    dest = store_dir / f"{mid}.npz"
    if not dest.exists():
        part = dest.with_name(f".{dest.name}.{os.getpid()}")
        part.write_bytes(data)
        os.replace(part, dest)
        try:
            os.chmod(dest, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass
    if _file_sha(dest) != digest:
        raise GovernanceError(f"stored artifact {dest} does not match the registered digest")
    from .ingest import normalize_symbol

    sc = {"shared": True} if scope.get("shared") is True else \
        {"symbols": sorted({normalize_symbol(s) for s in scope["symbols"]})}
    with db.tx() as c:
        c.execute("INSERT INTO model_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (mid, digest, model.kind, jdump(sc), timeframe, int(horizon), feature_schema, training_cutoff,
                   neutral_band_version or NEUTRAL_BAND_VERSION, cost_policy, code_contract_version,
                   datetime.fromtimestamp(Path(source).stat().st_mtime, timezone.utc).isoformat(timespec="milliseconds"),
                   utc_now(), str(dest), str(Path(source)),
                   jdump(_refs(evidence)), actor, db.run_id))
    db.event("MODEL_ARTIFACT_REGISTERED", {"model_id": mid, "sha256": digest, "family": model.kind,
                                           "timeframe": timeframe, "horizon": int(horizon), "scope": sc,
                                           "registered_by": actor}, subject=mid)
    return {"model_id": mid, "created": True, **model_record(db, mid)}


def model_record(db: AuditDB, model_id: str, at_utc: str | None = None) -> dict[str, Any]:
    r = db.one("SELECT * FROM model_artifacts WHERE model_id=?", (model_id,))
    if r is None:
        raise GovernanceError(f"unknown model artifact {model_id}")
    r["scope"] = json.loads(r.pop("scope_json"))
    r["evidence"] = json.loads(r.pop("evidence_json"))
    appr = db.one("SELECT rowid AS rid, * FROM model_approvals WHERE model_id=? AND artifact_sha256=?",
                  (model_id, r["sha256"]))
    rev = db.one("SELECT * FROM model_revocations WHERE model_id=? AND artifact_sha256=?", (model_id, r["sha256"]))
    now = at_utc or utc_now()
    expires = json.loads(appr["scope_json"]).get("expires_utc") if appr else None
    if rev:
        r["state"] = "REVOKED"
    elif appr and expires and str(expires) <= now:
        r["state"] = "EXPIRED"
    elif appr:
        r["state"] = "APPROVED"
    else:
        r["state"] = "CANDIDATE"
    r["approval"] = ({"approval_id": f"approval:{appr['rid']}", "approved_by": appr["approved_by"],
                      "approved_utc": appr["approved_utc"], "scope": json.loads(appr["scope_json"]),
                      "evidence": json.loads(appr["evidence_json"])} if appr else None)
    r["revocation"] = rev
    return r


def verify_model_file(db: AuditDB, model_id: str) -> dict[str, Any]:
    """Re-hash and re-load the stored artifact (read-only)."""
    r = model_record(db, model_id)
    p = Path(r["stored_path"])
    if not p.is_file():
        return {"model_id": model_id, "file_present": False, "hash_verified": False, "schema_ok": False,
                "error": f"missing {p}"}
    ok_hash = _file_sha(p) == r["sha256"]
    try:
        load_artifact(p)
        schema_ok, err = True, None
    except GovernanceError as exc:
        schema_ok, err = False, str(exc)
    return {"model_id": model_id, "file_present": True, "hash_verified": ok_hash, "schema_ok": schema_ok, "error": err}


def list_models(db: AuditDB) -> list[dict[str, Any]]:
    return [model_record(db, r["model_id"]) for r in db.q("SELECT model_id FROM model_artifacts ORDER BY registered_utc")]


def _iso_utc(value: str) -> str:
    d = datetime.fromisoformat(value)
    d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    return d.isoformat(timespec="milliseconds")


def approve_model(db: AuditDB, model_id: str, approver: str, evidence: Sequence[str], *,
                  scope: Mapping[str, Any] | None = None, expires_utc: str | None = None) -> dict[str, Any]:
    """Explicit owner approval of one registered artifact. Refused for a missing, unreadable, altered or
    schema-incompatible file, a revoked artifact, or without approver and evidence references."""
    approver = _need(approver, "approver")
    refs = _refs(evidence)
    if not refs:
        raise GovernanceError("approval needs at least one evidence reference")
    rec = model_record(db, model_id)
    if rec["state"] == "REVOKED":
        raise GovernanceError(f"{model_id} is revoked; register a new artifact instead")
    if rec["state"] in ("APPROVED", "EXPIRED"):
        raise GovernanceError(f"{model_id} already has an approval ({rec['state']}); approvals are append-only")
    chk = verify_model_file(db, model_id)
    if not (chk["file_present"] and chk["hash_verified"] and chk["schema_ok"]):
        raise GovernanceError(f"artifact check failed: {chk}")
    exp = _iso_utc(expires_utc) if expires_utc else None
    if exp and exp <= utc_now():
        raise GovernanceError("expiry is in the past")
    sc = {"registry": "model_artifacts", "scope": dict(scope or rec["scope"]), "expires_utc": exp}
    with db.tx() as c:
        c.execute("INSERT INTO model_approvals VALUES (?,?,?,?,?,?,?)",
                  (model_id, rec["sha256"], approver, utc_now(), jdump(sc), jdump({"references": refs}), db.run_id))
    db.event("MODEL_APPROVED", {"model_id": model_id, "artifact_sha256": rec["sha256"], "approved_by": approver,
                                "evidence": refs, "expires_utc": exp}, subject=model_id)
    return model_record(db, model_id)


def revoke_model(db: AuditDB, model_id: str, actor: str, reason: str) -> dict[str, Any]:
    actor, reason = _need(actor, "revoked_by"), _need(reason, "reason")
    rec = model_record(db, model_id)
    if rec["state"] == "REVOKED":
        raise GovernanceError(f"{model_id} is already revoked")
    with db.tx() as c:
        c.execute("INSERT INTO model_revocations VALUES (?,?,?,?,?,?)",
                  (model_id, rec["sha256"], actor, utc_now(), reason, db.run_id))
    db.event("MODEL_REVOKED", {"model_id": model_id, "artifact_sha256": rec["sha256"], "revoked_by": actor,
                               "reason": reason}, subject=model_id, level="WARN")
    return model_record(db, model_id)


# =============================================================================== strategy lifecycle
STAGES = ("DRAFT", "VALIDATED", "BACKTESTED", "PAPER_TESTED", "APPROVED", "RETIRED")
_NEXT = {"DRAFT": "VALIDATED", "VALIDATED": "BACKTESTED", "BACKTESTED": "PAPER_TESTED", "PAPER_TESTED": "APPROVED"}
_EVIDENCE_STAGES = ("VALIDATED", "BACKTESTED", "PAPER_TESTED", "APPROVED")
USER_INPUT, ML_GENERATED = "USER_INPUT", "ML_GENERATED"
ORIGIN_MAP = {
    "USER": (USER_INPUT, "MANUALLY_AUTHORED"), "USER_DEFINED": (USER_INPUT, "MANUALLY_AUTHORED"),
    "REFERENCE": (USER_INPUT, "CATALOGUE"), "CATALOGUE_ORIGINAL": (USER_INPUT, "CATALOGUE"),
    "CATALOGUE_DERIVED": (USER_INPUT, "DERIVED"),
    "AI_GENERATED": (ML_GENERATED, "AI_ASSISTED"),
    "ML_DISCOVERED": (ML_GENERATED, "ML_DISCOVERED"), "DISCOVERED": (ML_GENERATED, "ML_DISCOVERED"),
}


def strategy_origin(origin: str | None, has_parent: bool = False) -> tuple[str, str]:
    """(top-level origin USER_INPUT | ML_GENERATED, subtype). Unknown origins are ``UNCLASSIFIED``."""
    raw = str(origin or "")
    if raw.lower().startswith("import:"):  # `gplm strategies import FILE`: an imported user configuration
        return USER_INPUT, "DERIVED" if has_parent else "MANUALLY_AUTHORED"
    top, sub = ORIGIN_MAP.get(raw.upper(), ("UNCLASSIFIED", raw or "UNKNOWN"))
    if has_parent and top == USER_INPUT and sub != "DERIVED":
        sub = "DERIVED"
    return top, sub


def strategy_state(db: AuditDB | None, strategy_id: str, version: int | str) -> dict[str, Any]:
    """Current lifecycle stage of one strategy version (no events -> DRAFT; legacy rows are not rewritten)."""
    out: dict[str, Any] = {"strategy_id": strategy_id, "version": str(version), "stage": None, "seq": None,
                           "exists": False}
    if db is None:
        return out
    try:
        v = int(version)
    except (TypeError, ValueError):
        return out
    row = db.one("SELECT origin, parent_id FROM strategies WHERE strategy_id=? AND version=?", (strategy_id, v))
    if row is None:
        return out
    top, sub = strategy_origin(row["origin"], row["parent_id"] is not None)
    out.update(exists=True, stage="DRAFT", origin=top, origin_subtype=sub, legacy_origin=row["origin"])
    try:
        ev = db.q("SELECT seq, stage, actor, evidence_json, note, ts_utc FROM strategy_lifecycle "
                  "WHERE strategy_id=? AND version=? ORDER BY seq", (strategy_id, v))
    except Exception:
        ev = []
    if ev:
        out.update(stage=ev[-1]["stage"], seq=ev[-1]["seq"])
    out["history"] = [{**e, "evidence": json.loads(e.pop("evidence_json"))} for e in ev]
    return out


def advance_strategy(db: AuditDB, strategy_id: str, version: int, to_stage: str, actor: str,
                     evidence: Sequence[str] | None = None, note: str | None = None,
                     paper_min_trades: int | None = None) -> dict[str, Any]:
    """Record one lifecycle transition. Allowed: DRAFT -> VALIDATED -> BACKTESTED -> PAPER_TESTED -> APPROVED,
    and any non-retired stage -> RETIRED (terminal). Evidence references are required for every stage from
    VALIDATED to APPROVED; APPROVED also requires recorded evidence for each preceding stage."""
    actor = _need(actor, "actor")
    to_stage = str(to_stage).upper()
    if to_stage not in STAGES or to_stage == "DRAFT":
        raise GovernanceError(f"target stage must be one of {STAGES[1:]}")
    cur = strategy_state(db, strategy_id, version)
    if not cur["exists"]:
        raise GovernanceError(f"strategy {strategy_id} v{version} is not in the canonical strategy store")
    stage = cur["stage"]
    if stage == "RETIRED":
        raise GovernanceError("RETIRED is terminal")
    refs = _refs(evidence)
    if to_stage == "RETIRED":
        _need(note, "retirement reason (--note)")
    elif _NEXT.get(stage) != to_stage:
        raise GovernanceError(f"illegal transition {stage} -> {to_stage}; next allowed is {_NEXT.get(stage)}")
    if to_stage == "PAPER_TESTED" and paper_min_trades:
        # M6.6: forward paper-trading evidence from the paper ledger is required (and recorded).
        from .paper import paper_gate, summary as paper_summary

        src = f"strategy:{strategy_id}@{int(version)}"
        lacking_paper = paper_gate(db, src, int(paper_min_trades))
        if lacking_paper:
            raise GovernanceError("PAPER_TESTED requires paper evidence: " + "; ".join(lacking_paper))
        ref = paper_summary(db, src)["evidence_ref"]
        if ref and ref not in refs:
            refs.append(ref)
    if to_stage in _EVIDENCE_STAGES and not refs:
        raise GovernanceError(f"{to_stage} requires evidence references")
    if to_stage == "APPROVED":
        if cur["origin"] == "UNCLASSIFIED":
            raise GovernanceError(f"origin {cur.get('legacy_origin')!r} is unclassified; cannot approve")
        have = {h["stage"] for h in cur["history"] if h["evidence"]}
        lacking = [s for s in ("VALIDATED", "BACKTESTED", "PAPER_TESTED") if s not in have]
        if lacking:
            raise GovernanceError(f"APPROVED requires evidence for {lacking}")
    with db.tx() as c:
        c.execute("INSERT INTO strategy_lifecycle (strategy_id, version, stage, actor, evidence_json, note, ts_utc, run_id) "
                  "VALUES (?,?,?,?,?,?,?,?)", (strategy_id, int(version), to_stage, actor, jdump(refs), note, utc_now(),
                                               db.run_id))
    db.event("STRATEGY_LIFECYCLE", {"strategy_id": strategy_id, "version": int(version), "from": stage,
                                    "to": to_stage, "actor": actor, "evidence": refs, "note": note},
             subject=strategy_id)
    return strategy_state(db, strategy_id, version)


__all__ = [n for n in dir() if not n.startswith("_") and n not in {"annotations"}]
