"""M5.9B governance tests: calendar registry, model-artifact registry and approval, fixed-model execution,
strategy lifecycle, live-alert universe, the canonical LIVE gate and the read-only verifier.

Synthetic, tiny fixtures only (a 3-week calendar, a 2-feature logistic artifact, a 4-bar lane). No real data,
no training, no replay. Each test function is a single executed case (no parametrization)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from stock_gplm.app import contracts as C
from stock_gplm.app import governance as G
from stock_gplm.app import models as models_mod
from stock_gplm.app.db import AuditDB, jdump, sha, utc_now
from stock_gplm.app.fixed_model import predict_fixed
from stock_gplm.app.models import DirectionModel, Prep
from stock_gplm.app.session_calendar import expected_completed_session, load_registered_calendar
from stock_gplm.app.targets import derive_target
from stock_gplm.m54.candles import ist_ns

IST = ZoneInfo("Asia/Kolkata")
PRED = {"stop_atr": 1.5, "target_atr": 2.5, "min_probability": 0.56}
NO_CONFIG = SimpleNamespace(section=lambda name: {})  # settings without a legacy M2 calendar


# ----------------------------------------------------------------------------- fixtures (tiny, synthetic)
def _calendar_rows() -> list[str]:
    """NSE 2026-03-02..2026-03-22: weekdays open, 2026-03-10 holiday, 2026-03-20 partial (close 13:00)."""
    rows, d = [], date(2026, 3, 2)
    while d <= date(2026, 3, 22):
        if d.weekday() >= 5 or d.isoformat() == "2026-03-10":
            rows.append(f"NSE,{d.isoformat()},CLOSED,,,Asia/Kolkata,{'holiday' if d.weekday() < 5 else 'weekend'}")
        elif d.isoformat() == "2026-03-20":
            rows.append(f"NSE,{d.isoformat()},PARTIAL,09:15,13:00,Asia/Kolkata,partial")
        else:
            rows.append(f"NSE,{d.isoformat()},OPEN,09:15,15:30,Asia/Kolkata,")
        d += timedelta(days=1)
    return rows


def _csv(rows: list[str]) -> str:
    return "exchange,date,status,open,close,timezone,note\n" + "\n".join(rows) + "\n"


def _make_model(path: Path, w: tuple[float, float] = (0.8, -0.3), b: float = 0.1, horizon: int = 1) -> Path:
    m = DirectionModel("logit", horizon, prep=Prep(np.array([0, 1]), np.zeros(2), np.ones(2)),
                       params=[np.array([[w[0]], [w[1]]]), np.array([b])], train_rows=100, trained_label="2026-03-12")
    m.save(path)
    return path


def _register(db: AuditDB, src: Path, store: Path, **kw):
    args = dict(timeframe="1d", horizon=1, feature_schema="fs-test", training_cutoff="2026-03-12",
                cost_policy="costs:test", actor="owner", scope={"symbols": ["AAA"]})
    args.update(kw)
    return G.register_model(db, src, store, **args)


def _lane() -> SimpleNamespace:
    sessions = ["2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19"]
    ends = np.array([ist_ns(s, "15:30") for s in sessions], dtype=np.int64)
    frame = pd.DataFrame({"session": sessions, "end_ns": ends, "incomplete": [False] * 4})
    market = SimpleNamespace(close=np.array([100.0, 101.0, 102.0, 103.0]), natr=np.full(4, 0.02), end_ns=ends,
                             sessions=np.array(sessions), no_signal=np.zeros(4, dtype=bool))
    return SimpleNamespace(symbol="AAA-EQ", timeframe="1d", key="AAA-EQ|1d", names=["f0", "f1"],
                           X=np.array([[0.0, 0.0], [0.5, 0.0], [0.2, 0.1], [1.0, 0.0]]), feature_config_sha="fs-test",
                           data_fingerprint="dfp", costs=SimpleNamespace(buy=0.002, sell=0.002), market=market,
                           bars=SimpleNamespace(frame=frame), label=lambda i: sessions[i])


def _world(tmp_path: Path) -> dict:
    db = AuditDB(tmp_path / "g.sqlite", tmp_path / "ev", run_id="t")
    (tmp_path / "nse.csv").write_text(_csv(_calendar_rows()), encoding="utf-8")
    cid = G.register_calendar(db, tmp_path / "nse.csv", "owner")["calendar_id"]
    G.set_calendar_active(db, cid, True, "owner")
    (tmp_path / "n200.txt").write_text("AAA\nBBB\n", encoding="utf-8")
    uid = G.register_universe(db, tmp_path / "n200.txt", "nifty200", "2026-03-01", "owner")["universe_id"]
    G.set_universe_active(db, uid, True, "owner")
    mid = _register(db, _make_model(tmp_path / "m.npz"), tmp_path / "store")["model_id"]
    G.approve_model(db, mid, "owner", ["reports/oos-aaa-2026-03.md"])
    return {"db": db, "cid": cid, "uid": uid, "mid": mid, "lane": _lane(), "cal": G.active_calendar(db)}


def _predict(w: dict, model_id: str | None = None) -> dict:
    db = w["db"]
    return predict_fixed(db, w["lane"], model_id or w["mid"],
                         freshness=C.evaluate_freshness("2026-03-19", "2026-03-19", calendar_confirmed=True),
                         calendar=w["cal"], universe=G.universe_membership(db, "AAA"), pred_cfg=PRED,
                         cost_policy="costs:test")


def _insert_raw(db: AuditDB, row: dict) -> None:
    """Store a row directly (simulates legacy / tampered rows that bypass AuditDB.add_prediction)."""
    values = dict(row)
    payload = values.pop("payload", {})
    values.setdefault("prediction_id", sha(values["natural_key"])[:24])
    values.setdefault("run_id", "t")
    values.setdefault("created_utc", utc_now())
    values["payload_json"] = jdump(payload)
    cols = list(values)
    db.conn.execute(f"INSERT INTO predictions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [values[c] for c in cols])
    db.conn.commit()


# ----------------------------------------------------------------------------- 1. calendar registry
def test_calendar_registration_holiday_week_coverage_and_activation(tmp_path: Path) -> None:
    rows = _calendar_rows()
    assert G.parse_calendar(_csv(rows)).ok
    bad = {"duplicate": rows + [rows[0]], "conflicting": rows + [rows[1].replace("OPEN", "CLOSED", 1)
                                                                  .replace("09:15,15:30", ",")],
           "coverage gap": rows[:5] + rows[6:], "requires timezone": [r.replace("Asia/Kolkata", "UTC") for r in rows]}
    for needle, variant in bad.items():
        errs = G.parse_calendar(_csv(variant)).errors
        assert any(needle in e for e in errs), (needle, errs)
    db = AuditDB(tmp_path / "g.sqlite", tmp_path / "ev", run_id="t")
    path = tmp_path / "nse-2026-03.csv"
    path.write_text(_csv(rows), encoding="utf-8")
    reg = G.register_calendar(db, path, "owner")
    cid = reg["calendar_id"]
    assert reg["created"] and reg["status"] == "REGISTERED" and reg["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (reg["coverage_start"], reg["coverage_end"]) == ("2026-03-02", "2026-03-22")
    assert reg["special_or_partial"] == ["2026-03-20"] and reg["source_filename"] == "nse-2026-03.csv"
    assert not G.register_calendar(db, path, "owner")["created"]
    # registration is not activation; no calendar -> no weekday fallback
    assert load_registered_calendar(NO_CONFIG, db) is None
    G.set_calendar_active(db, cid, True, "owner", "owner-downloaded NSE holiday list")
    cal = load_registered_calendar(NO_CONFIG, db)
    assert cal is not None and cal.calendar_id == cid
    # holiday-shortened week (Tue 2026-03-10 closed) and next-session targets
    assert cal.week_sessions("2026-03-11") == ["2026-03-09", "2026-03-11", "2026-03-12", "2026-03-13"]
    t = derive_target("1d", "2026-03-09", ist_ns("2026-03-09", "15:30"), 1, calendar=cal)
    assert t["target"] == "2026-03-11" and t["derivation"] == "REGISTERED_CALENDAR"
    wk = derive_target("1w", "2026-03-06", ist_ns("2026-03-06", "15:30"), 1, calendar=cal)
    assert wk["target"].startswith("2026-03-13T")
    # registered partial-session close governs freshness
    assert expected_completed_session(cal, None, datetime(2026, 3, 20, 14, 0, tzinfo=IST)) == ("2026-03-20", True)
    assert expected_completed_session(cal, None, datetime(2026, 3, 20, 12, 0, tzinfo=IST)) == ("2026-03-19", True)
    # outside coverage -> CALENDAR_UNKNOWN, never a guessed weekday
    assert expected_completed_session(cal, "2026-04-02") == (None, False)
    assert C.evaluate_freshness("2026-03-20", None, calendar_confirmed=False).status == C.CALENDAR_UNKNOWN
    beyond = derive_target("1d", "2026-03-20", ist_ns("2026-03-20", "15:30"), 1, calendar=cal)
    assert beyond["status"] == "UNAVAILABLE" and beyond["reason"] == C.CALENDAR_UNKNOWN
    assert cal.covers("2026-03-19", "2026-03-20") and not cal.covers("2026-03-20", "2026-03-23")
    # explicit deactivation, audited; append-only registry
    with pytest.raises(G.GovernanceError):
        G.set_calendar_active(db, cid, True, "owner")
    G.set_calendar_active(db, cid, False, "owner")
    assert load_registered_calendar(NO_CONFIG, db) is None
    assert [e["action"] for e in G.calendar_record(db, cid)["events"]] == ["ACTIVATE", "DEACTIVATE"]
    assert db.one("SELECT COUNT(*) n FROM events WHERE kind LIKE 'CALENDAR_%'")["n"] == 3
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("UPDATE calendar_registrations SET coverage_end='2027-01-01'")
    with pytest.raises(G.GovernanceError):
        G.set_calendar_active(db, cid, True, "")
    db.close()


# ----------------------------------------------------------------------------- 2. model registry
def test_model_registration_is_immutable_and_hash_bound(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "g.sqlite", tmp_path / "ev", run_id="t")
    store = tmp_path / "store"
    src = _make_model(tmp_path / "a.npz")
    rec = _register(db, src, store, evidence=["reports/oos.md"])
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    assert rec["created"] and rec["model_id"] == f"fm-{digest[:24]}" and rec["sha256"] == digest
    assert rec["state"] == "CANDIDATE" and rec["approval"] is None  # registration never approves
    assert db.model_approval(rec["model_id"], digest) is None
    for k in ("family", "scope", "timeframe", "horizon", "feature_schema", "training_cutoff", "neutral_band_version",
              "cost_policy_version", "code_contract_version", "created_utc", "registered_utc", "evidence"):
        assert rec[k] not in (None, "", []), k
    assert rec["family"] == "logit" and rec["scope"] == {"symbols": ["AAA-EQ"]}
    stored = Path(rec["stored_path"])
    assert stored.read_bytes() == src.read_bytes() and not os.access(stored, os.W_OK)
    again = _register(db, src, store)
    assert not again["created"] and again["model_id"] == rec["model_id"]
    other = _register(db, _make_model(tmp_path / "b.npz", w=(0.81, -0.3)), store)
    assert other["model_id"] != rec["model_id"]  # any byte change is a different artifact
    (tmp_path / "junk.npz").write_bytes(b"not a model")
    for kw, src_path in (({}, tmp_path / "junk.npz"), ({"horizon": 5}, src), ({"timeframe": "15m"}, src),
                         ({"scope": {}}, src), ({"actor": " "}, src), ({"feature_schema": ""}, src)):
        with pytest.raises(G.GovernanceError):
            _register(db, src_path, store, **kw)
    assert len(G.list_models(db)) == 2
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("UPDATE model_artifacts SET sha256='x'")
    db.close()


# ----------------------------------------------------------------------------- 3. approval / revocation / expiry
def test_model_approval_revocation_and_expiry_are_explicit_and_append_only(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "g.sqlite", tmp_path / "ev", run_id="t")
    store = tmp_path / "store"
    m1 = _register(db, _make_model(tmp_path / "1.npz"), store)
    with pytest.raises(G.GovernanceError):
        G.approve_model(db, m1["model_id"], "owner", [])  # evidence required
    with pytest.raises(G.GovernanceError):
        G.approve_model(db, m1["model_id"], " ", ["r"])  # approver required
    with pytest.raises(G.GovernanceError):
        G.approve_model(db, "fm-missing", "owner", ["r"])
    ok = G.approve_model(db, m1["model_id"], "owner", ["reports/oos.md", "reports/paper.md"])
    assert ok["state"] == "APPROVED" and ok["approval"]["approved_by"] == "owner"
    appr = db.model_approval(m1["model_id"], m1["sha256"])
    assert appr is not None and appr["approval_id"] == ok["approval"]["approval_id"]
    with pytest.raises(G.GovernanceError):
        G.approve_model(db, m1["model_id"], "owner", ["again"])
    # altered stored artifact cannot be approved
    m3 = _register(db, _make_model(tmp_path / "3.npz", w=(0.5, 0.5)), store)
    p3 = Path(m3["stored_path"])
    os.chmod(p3, stat.S_IWRITE | stat.S_IREAD)
    p3.write_bytes(p3.read_bytes() + b"tamper")
    assert not G.verify_model_file(db, m3["model_id"])["hash_verified"]
    with pytest.raises(G.GovernanceError):
        G.approve_model(db, m3["model_id"], "owner", ["r"])
    # expiry
    m2 = _register(db, _make_model(tmp_path / "2.npz", w=(0.2, 0.2)), store)
    with pytest.raises(G.GovernanceError):
        G.approve_model(db, m2["model_id"], "owner", ["r"], expires_utc="2000-01-01")
    G.approve_model(db, m2["model_id"], "owner", ["r"], expires_utc="2099-01-01")
    later = "2100-01-01T00:00:00.000+00:00"
    assert G.model_record(db, m2["model_id"], at_utc=later)["state"] == "EXPIRED"
    assert db.model_approval(m2["model_id"], m2["sha256"], at_utc=later) is None
    assert db.model_approval(m2["model_id"], m2["sha256"]) is not None
    # revocation blocks immediately and is final
    rv = G.revoke_model(db, m1["model_id"], "owner", "live drift review")
    assert rv["state"] == "REVOKED" and db.model_approval(m1["model_id"], m1["sha256"]) is None
    for fn in (lambda: G.approve_model(db, m1["model_id"], "owner", ["r"]),
               lambda: G.revoke_model(db, m1["model_id"], "owner", "again"),
               lambda: G.revoke_model(db, m2["model_id"], "owner", "")):
        with pytest.raises(G.GovernanceError):
            fn()
    for sql in ("DELETE FROM model_revocations", "DELETE FROM model_approvals", "UPDATE model_approvals SET approved_by='x'"):
        with pytest.raises(sqlite3.DatabaseError):
            db.conn.execute(sql)
    kinds = [r["kind"] for r in db.q("SELECT kind FROM events WHERE kind LIKE 'MODEL_%' ORDER BY seq")]
    assert kinds.count("MODEL_APPROVED") == 2 and kinds.count("MODEL_REVOKED") == 1
    db.close()


# ----------------------------------------------------------------------------- 4. fixed model + passing LIVE gate
def test_fixed_approved_model_never_retrains_and_passes_the_live_gate(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("the fixed-model path must never fit, update or calibrate")

    monkeypatch.setattr(models_mod.DirectionModel, "fit", boom)
    monkeypatch.setattr(models_mod, "train_network", boom)
    monkeypatch.setattr(models_mod.PlattCalibrator, "fit", boom)
    w = _world(tmp_path)
    db, mid = w["db"], w["mid"]
    stored = Path(G.model_record(db, mid)["stored_path"])
    before = hashlib.sha256(stored.read_bytes()).hexdigest()
    out = _predict(w)
    assert out["status"] == C.LIVE and out["eligible"] and out["stored"], out
    assert abs(out["probability"] - 1 / (1 + np.exp(-0.9))) < 1e-12 and out["target"] == "2026-03-20"
    ev = out["evidence"]
    assert ev["calendar_id"] == w["cid"] and ev["universe_id"] == w["uid"] and ev["model_id"] == mid
    assert ev["approval_id"] == db.model_approval(mid, G.model_record(db, mid)["sha256"])["approval_id"]
    row = db.one("SELECT * FROM predictions WHERE mode='LIVE'")
    pl = json.loads(row["payload_json"])
    assert row["actionable"] == 1 and row["source_id"] == mid and pl["approval_id"] == ev["approval_id"]
    assert pl["live_decision"]["prediction_class"] == C.MODEL_ONLY and row["stop"] < 103.0 < row["target"]
    # deterministic, idempotent, artifact untouched, nothing trained
    again = _predict(w)
    assert again["probability"] == out["probability"] and not again["stored"]
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == before
    assert db.one("SELECT COUNT(*) n FROM models")["n"] == 0 and db.one("SELECT COUNT(*) n FROM training_history")["n"] == 0
    # an unapproved candidate runs but is only SHADOW
    cand = _register(db, _make_model(tmp_path / "c.npz", w=(0.7, 0.0)), tmp_path / "store", scope={"shared": True})
    sh = _predict(w, cand["model_id"])
    assert sh["status"] == C.SHADOW and C.MODEL_NOT_APPROVED in sh["reasons"] and not sh["eligible"]
    # revocation immediately blocks new LIVE output (operation refused, nothing stored)
    G.revoke_model(db, mid, "owner", "test revocation")
    rf = _predict(w)
    assert rf["status"] == "REFUSED" and "MODEL_REVOKED" in rf["reasons"] and not rf["stored"]
    assert db.one("SELECT COUNT(*) n FROM predictions WHERE mode='LIVE'")["n"] == 1
    db.close()


# ----------------------------------------------------------------------------- 5. strategy lifecycle
def test_strategy_lifecycle_transitions_evidence_and_origin_mapping(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "g.sqlite", tmp_path / "ev", run_id="t")
    for sid, origin in (("S1", "ML_DISCOVERED"), ("S2", "mystery")):
        db.conn.execute("INSERT INTO strategies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, 1, sid, origin, 1, 1, "1d", "{}", f"/x/{sid}.json", "a" * 64, None, None, utc_now(), "{}"))
    db.conn.commit()
    st = G.strategy_state(db, "S1", 1)
    assert st["stage"] == "DRAFT" and (st["origin"], st["origin_subtype"]) == ("ML_GENERATED", "ML_DISCOVERED")
    with pytest.raises(G.GovernanceError):
        G.advance_strategy(db, "S1", 1, "APPROVED", "owner", ["r"])  # cannot skip stages
    with pytest.raises(G.GovernanceError):
        G.advance_strategy(db, "S1", 1, "VALIDATED", "owner", [])  # evidence required
    for stage in ("VALIDATED", "BACKTESTED", "PAPER_TESTED"):
        G.advance_strategy(db, "S1", 1, stage, "owner", [f"reports/{stage.lower()}.json"])
    with pytest.raises(G.GovernanceError):
        G.advance_strategy(db, "S1", 1, "APPROVED", "owner", [])
    ap = G.advance_strategy(db, "S1", 1, "APPROVED", "owner", ["reports/approval-memo.md"])
    assert ap["stage"] == "APPROVED" and len(ap["history"]) == 4 and ap["seq"] is not None
    assert G.strategy_state(db, "S1", 2)["exists"] is False  # approval is per version
    with pytest.raises(G.GovernanceError):
        G.advance_strategy(db, "S1", 1, "RETIRED", "owner", [], None)  # retirement needs a reason
    assert G.advance_strategy(db, "S1", 1, "RETIRED", "owner", [], "superseded")["stage"] == "RETIRED"
    with pytest.raises(G.GovernanceError):
        G.advance_strategy(db, "S1", 1, "VALIDATED", "owner", ["r"])  # terminal
    with pytest.raises(G.GovernanceError):
        G.advance_strategy(db, "NOPE", 1, "VALIDATED", "owner", ["r"])
    for stage in ("VALIDATED", "BACKTESTED", "PAPER_TESTED"):
        G.advance_strategy(db, "S2", 1, stage, "owner", ["r"])
    with pytest.raises(G.GovernanceError):
        G.advance_strategy(db, "S2", 1, "APPROVED", "owner", ["r"])  # unclassified origin is never approved
    assert G.strategy_origin("REFERENCE") == ("USER_INPUT", "CATALOGUE")
    assert G.strategy_origin("CATALOGUE_DERIVED") == ("USER_INPUT", "DERIVED")
    assert G.strategy_origin("USER") == ("USER_INPUT", "MANUALLY_AUTHORED")
    assert G.strategy_origin("USER", has_parent=True) == ("USER_INPUT", "DERIVED")
    assert G.strategy_origin("import:mine.json") == ("USER_INPUT", "MANUALLY_AUTHORED")
    assert G.strategy_origin("AI_GENERATED") == ("ML_GENERATED", "AI_ASSISTED")
    assert G.strategy_origin("DISCOVERED") == ("ML_GENERATED", "ML_DISCOVERED")
    assert db.one("SELECT origin FROM strategies WHERE strategy_id='S1'")["origin"] == "ML_DISCOVERED"  # not rewritten
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("UPDATE strategy_lifecycle SET stage='APPROVED'")
    db.close()


# ----------------------------------------------------------------------------- 6. live-alert universe
def test_live_universe_registration_supplements_and_membership(tmp_path: Path) -> None:
    db = AuditDB(tmp_path / "g.sqlite", tmp_path / "ev", run_id="t")
    f = tmp_path / "ind_nifty200list.csv"
    f.write_text("Company Name,Industry,Symbol,Series,ISIN Code\nA Ltd,X,AAA,EQ,IN1\nB Ltd,Y,BBB,EQ,IN2\n",
                 encoding="utf-8")
    reg = G.register_universe(db, f, "nifty200", "2026-11-13", "owner")
    uid = reg["universe_id"]
    assert reg["symbols"] == ["AAA-EQ", "BBB-EQ"] and reg["status"] == "REGISTERED"
    assert reg["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest() and reg["symbol_count"] == 2
    assert G.universe_membership(db, "AAA")["reason"] == "UNIVERSE_NOT_ACTIVE"  # activation is explicit
    G.set_universe_active(db, uid, True, "owner")
    assert G.universe_membership(db, "AAA") == {"symbol": "AAA-EQ", "eligible": True, "universe_id": uid,
                                                "basis": "MEMBER", "reason": None}
    out = G.universe_membership(db, "CCC")
    assert not out["eligible"] and out["reason"] == "NOT_IN_LIVE_UNIVERSE"
    with pytest.raises(G.GovernanceError):
        G.supplement(db, "CCC", True, "owner", "")  # a reason is mandatory
    G.supplement(db, "CCC", True, "owner", "owner watch-list addition 2026-11-16")
    sup = G.universe_membership(db, "CCC")
    assert sup["eligible"] and sup["basis"].startswith("SUPPLEMENT:")
    assert G.active_supplements(db)["CCC-EQ"]["reason"] == "owner watch-list addition 2026-11-16"
    with pytest.raises(G.GovernanceError):
        G.supplement(db, "CCC", True, "owner", "again")
    G.supplement(db, "CCC", False, "owner", "removed")
    assert not G.universe_membership(db, "CCC")["eligible"]
    for text in ("AAA\nAAA\n", "Name,Code\nA,1\n", "bad symbol!\n"):
        (tmp_path / "u.txt").write_text(text, encoding="utf-8")
        with pytest.raises(G.GovernanceError):
            G.register_universe(db, tmp_path / "u.txt", "x", "2026-11-13", "owner")
    G.set_universe_active(db, uid, False, "owner")
    assert G.universe_membership(db, "AAA")["reason"] == "UNIVERSE_NOT_ACTIVE"
    with pytest.raises(sqlite3.DatabaseError):
        db.conn.execute("DELETE FROM universe_supplements")
    db.close()


# ----------------------------------------------------------------------------- 7. canonical LIVE gate (table-driven)
def test_canonical_live_gate_failure_table_status_mapping_and_db_guard(tmp_path: Path) -> None:
    fresh = C.evaluate_freshness("2026-03-19", "2026-03-19", calendar_confirmed=True)
    model = {"model_id": "fm-1", "artifact_sha256": "a" * 64, "approval_id": "approval:1", "hash_verified": True,
             "schema_compatible": True}
    base = dict(latest_eligible_bar=True, freshness=fresh, calendar_id="cal-nse-1", calendar_covers=True, model=model,
                universe={"eligible": True, "universe_id": "uni-nifty200-1", "basis": "MEMBER"},
                cost_policy_version="costs:x", neutral_band_version=C.NEUTRAL_BAND_VERSION)
    ok = C.live_decision(**base)
    assert ok.eligible and ok.status == C.LIVE and ok.reasons == [] and ok.prediction_class == C.MODEL_ONLY
    assert {ok.evidence[k] for k in ("calendar_id", "approval_id", "universe_id")} == {"cal-nse-1", "approval:1",
                                                                                       "uni-nifty200-1"}
    approved_strategy = {"strategy_id": "S", "version": "1", "stage": "APPROVED"}
    assert C.live_decision(**{**base, "strategy_driven": True, "strategy": approved_strategy}).status == C.LIVE
    rule = C.live_decision(**{**base, "model": None, "model_required": False, "strategy_driven": True,
                              "strategy": approved_strategy})
    assert rule.status == C.LIVE and rule.prediction_class == C.STRATEGY_TRIGGERED
    cases = [
        ("missing approval", {"model": {**model, "approval_id": None}}, C.SHADOW, C.MODEL_NOT_APPROVED),
        ("no model", {"model": None}, C.SHADOW, C.MODEL_NOT_APPROVED),
        ("model-only cannot drop its model", {"model": None, "model_required": False}, C.SHADOW, C.MODEL_NOT_APPROVED),
        ("stale data", {"freshness": C.evaluate_freshness("2026-03-18", "2026-03-19", calendar_confirmed=True)},
         C.UNAVAILABLE, C.STALE_DATA),
        ("stale beats approval", {"freshness": C.evaluate_freshness("2026-03-18", "2026-03-19", calendar_confirmed=True),
                                  "model": None}, C.UNAVAILABLE, C.STALE_DATA),
        ("quarantined", {"freshness": C.evaluate_freshness("2026-03-19", "2026-03-19", calendar_confirmed=True,
                                                           quarantined=True)}, C.UNAVAILABLE, C.QUARANTINED_INPUT),
        ("missing stop", {"opportunity_missing_fields": ["stop"]}, C.NO_TRADE, "OPPORTUNITY_INCOMPLETE:stop"),
        ("outside universe", {"universe": {"eligible": False, "universe_id": "uni", "basis": None}}, C.NO_TRADE,
         C.UNIVERSE_NOT_ELIGIBLE),
        ("no universe", {"universe": None}, C.NO_TRADE, C.UNIVERSE_NOT_ELIGIBLE),
        ("calendar coverage", {"calendar_covers": False}, C.UNAVAILABLE, C.CALENDAR_UNKNOWN),
        ("calendar not registered", {"calendar_id": None}, C.SHADOW, C.CALENDAR_NOT_REGISTERED),
        ("revoked", {"model": {**model, "revoked": True}}, C.SHADOW, C.MODEL_REVOKED),
        ("expired", {"model": {**model, "expired": True}}, C.SHADOW, C.MODEL_EXPIRED),
        ("altered", {"model": {**model, "hash_verified": False}}, C.SHADOW, C.MODEL_HASH_MISMATCH),
        ("incompatible", {"model": {**model, "schema_compatible": False}}, C.SHADOW, C.MODEL_SCHEMA_INCOMPATIBLE),
        ("adaptive", {"adaptive_model": True}, C.SHADOW, C.ADAPTIVE_MODEL),
        ("strategy unapproved", {"strategy_driven": True, "strategy": {**approved_strategy, "stage": "PAPER_TESTED"}},
         C.SHADOW, C.STRATEGY_NOT_APPROVED),
        ("strategy missing", {"strategy_driven": True, "strategy": None}, C.SHADOW, C.STRATEGY_NOT_APPROVED),
        ("strategy retired", {"strategy_driven": True, "strategy": {**approved_strategy, "stage": "RETIRED"}},
         C.SHADOW, C.STRATEGY_RETIRED),
        ("cost version", {"cost_policy_version": None}, C.NO_TRADE, "VERSION_UNKNOWN:cost_policy"),
        ("band version", {"neutral_band_version": None}, C.NO_TRADE, "VERSION_UNKNOWN:neutral_band"),
        ("action gates", {"gate_blockers": ["probability 0.50 < 0.56"]}, C.NO_TRADE, C.GATES_FAILED),
        ("not latest", {"latest_eligible_bar": False}, C.REPLAY, C.NOT_LATEST_BAR),
    ]
    for name, override, status, reason in cases:
        d = C.live_decision(**{**base, **override})
        assert not d.eligible and d.status == status and reason in d.reasons, (name, d.to_dict())
    # shared status vocabulary over legacy rows (read-only mapping)
    for mode, payload, expect in (("HISTORICAL_FROZEN", {}, C.RESEARCH), ("REPLAY", {}, C.REPLAY),
                                  ("LIVE", {}, "LEGACY_UNVERIFIED_LIVE"), ("SHADOW", {}, C.SHADOW),
                                  ("SHADOW", {"status": "NO_TRADE"}, C.NO_TRADE), ("LIVE", {"status": "LIVE"}, C.LIVE),
                                  ("LIVE", {"status": "WEIRD"}, "UNKNOWN_STATUS"), ("OTHER", {}, "UNKNOWN_STATUS")):
        assert C.shared_status(mode, payload) == expect, (mode, payload)
    assert set(C.MOVEMENTS) == {"UP", "DOWN", "NEUTRAL"} and not set(C.MOVEMENTS) & set(C.STATUSES)
    # DB guard: no LIVE row without the canonical eligible decision; actionable rows need a trade action + FRESH
    db = AuditDB(tmp_path / "g.sqlite", tmp_path / "ev", run_id="t")
    opp = {"entry": "next open", "invalidation": "below 98", "expected_return": 0.01, "model_version": "fm-1@a",
           "freshness_status": "FRESH"}
    row = {"natural_key": "k", "mode": "LIVE", "symbol": "AAA-EQ", "timeframe": "1d", "lane_key": "AAA-EQ|1d",
           "source_kind": "MODEL", "source_id": "fm-1", "source_version": "v", "horizon_bars": 1,
           "decision_label": "2026-03-19", "decision_ns": 1, "direction": "LONG", "prob_raw": 0.7, "actionable": 1,
           "entry_ref": 100.0, "stop": 97.0, "target": 105.0, "payload": {"opportunity": opp}}
    with pytest.raises(ValueError, match="live_decision"):
        db.add_prediction(row)
    with pytest.raises(ValueError, match="live_decision"):
        db.add_prediction({**row, "payload": {"opportunity": opp, "live_decision": {**ok.to_dict(), "eligible": False}}})
    with pytest.raises(ValueError, match="action"):
        db.add_prediction({**row, "mode": "SHADOW", "direction": "AVOID"})
    with pytest.raises(ValueError, match="freshness_status"):
        db.add_prediction({**row, "mode": "SHADOW", "payload": {"opportunity": {**opp, "freshness_status": "STALE_DATA"}}})
    assert db.add_prediction({**row, "payload": {"opportunity": opp, "live_decision": ok.to_dict()}})
    db.close()


# ----------------------------------------------------------------------------- 8. read-only verifier
def test_verifier_detects_unsafe_and_legacy_rows_without_modifying_them(tmp_path: Path) -> None:
    from stock_gplm.app.m59a_verify import M59B_KEYS, verify

    w = _world(tmp_path)
    db = w["db"]
    assert _predict(w)["status"] == C.LIVE
    good = db.one("SELECT * FROM predictions WHERE mode='LIVE'")
    gp = json.loads(good["payload_json"])
    dec = gp["live_decision"]
    m2 = _register(db, _make_model(tmp_path / "r.npz", w=(0.3, 0.1)), tmp_path / "store", scope={"shared": True})
    G.approve_model(db, m2["model_id"], "owner", ["r"])
    G.revoke_model(db, m2["model_id"], "owner", "bad")
    p2 = Path(m2["stored_path"])
    os.chmod(p2, stat.S_IWRITE | stat.S_IREAD)
    p2.write_bytes(p2.read_bytes() + b"x")
    base = {k: good[k] for k in good if k not in ("prediction_id", "natural_key", "payload_json", "created_utc", "run_id")}

    def clone(key: str, payload: dict, **over) -> None:
        _insert_raw(db, {**base, "natural_key": key, "payload": payload, **over})

    ev = dec["evidence"]
    clone("stale", {**gp, "freshness": {**gp["freshness"], "status": "STALE_DATA"}})
    clone("nodecision", {**gp, "live_decision": {**dec, "eligible": False, "status": "SHADOW"}})
    clone("nomodel", {**gp, "live_decision": {**dec, "evidence": {**ev, "model_id": "fm-unknown",
                                                                  "artifact_sha256": "f" * 64}}})
    clone("revoked", {**gp, "live_decision": {**dec, "evidence": {**ev, "model_id": m2["model_id"],
                                                                  "artifact_sha256": m2["sha256"]}}},
          created_utc="2999-01-01T00:00:00.000+00:00")
    clone("strategy", {**gp, "live_decision": {**dec, "prediction_class": C.STRATEGY_TRIGGERED}},
          source_kind="STRATEGY", source_id="S1", source_version="1")
    clone("universe", gp, symbol="ZZZ-EQ")
    clone("calendar", {**gp, "target_contract": {**gp["target_contract"], "decision_bar": "2026-05-04",
                                                 "derived_target": "2026-05-05"}},
          decision_label="2026-05-04", target_session="2026-05-05")
    clone("missing-fields", {**gp, "status": "REPLAY", "opportunity": {**gp["opportunity"], "invalidation": None}},
          mode="REPLAY")
    clone("legacy", {}, actionable=0)
    clone("unknown", {"status": "WEIRD"}, mode="SHADOW", actionable=0)
    db.close()
    path = tmp_path / "g.sqlite"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    out = verify(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before  # read-only: nothing repaired
    assert not out["ok"]
    assert {k: out["violations"][k] for k in M59B_KEYS} == {k: 1 for k in M59B_KEYS}, out
    assert all(out["violations"][k] == 0 for k in ("stale_or_ungated_live", "actionable_without_contract",
                                                   "target_contract_mismatch", "m58_bars_held_mismatch")), out
    info = out["info"]
    assert info["legacy_unverified_live"] == 1 and info["altered_model_artifacts"] == [m2["model_id"]]
    assert info["status_distribution"]["UNKNOWN_STATUS"] == 1 and info["governance_tables_present"]
    assert info["contract_version_distribution"][C.OUTPUT_CONTRACT] >= 9
    assert good["prediction_id"] not in sum(out["examples"].values(), [])  # the gate-passed row is clean
