"""M5.1.2 adversarial tests for findings H (holdout isolation) and C (approval authority).

Everything here is synthetic. The trust-root tests exercise the loader's logic with
the test user standing in for the administrative account; real uid-0 ownership is
exercised only negatively (a user-owned anchor is rejected when root ownership is
required), because a test cannot create root-owned files.
"""
from __future__ import annotations

import builtins
import gzip
import hashlib
import inspect
import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from m5_fixtures import (
    HOLDOUT,
    approval_for,
    build_world,
    install_trust_root,
    remove_trust_root,
    settings_dict,
    strategy_config,
    trust_policy_value,
    trust_root_value,
    write_segment,
    write_symbol,
)

from stock_gplm.ingestion import daily
from stock_gplm.ingestion.shoonya_daily_v1 import ShoonyaDailyV1
from stock_gplm.m5 import (
    BacktestSettings,
    DataEligibilityRequest,
    DecisionStatus,
    HistoricalDataAdapter,
    UniverseRequest,
    authority,
    run_historical_backtest,
)
from stock_gplm.m5 import cli as m5_cli
from stock_gplm.m5.adapter import StaleAssessmentError, strict_authority_failures
from stock_gplm.m5.authority import (
    ApprovalTrustPolicy,
    GovernedHoldout,
    load_evidence_registration,
    load_trust_policy,
)
from stock_gplm.m5.discovery import discover
from stock_gplm.m5.evidence import (
    evidence_digest,
    file_sha256,
    load_holdout_boundary,
    load_reference_evidence,
)
from stock_gplm.m5.holdout_validation import validate_daily_for_development
from stock_gplm.m5.models import canonical_hash
from stock_gplm.m5.run_store import REPLAY_VERIFIED, RunIntegrityError, load_run, replay_run
from stock_gplm.v1.registry import StrategyRegistry

PAIR = ("ALPHA-EQ", "BETA-EQ")
DIAG = "DIAGNOSTIC_ONLY"
STRICT = "STRICT_RESEARCH"
SENTINEL = 987_654_321  # volume written only into protected rows
PROTECTED = "2024-10-01_2024-12-31"


# =============================================================== H: instrumented decoders

class DecoderSpy:
    """Instruments every layer a payload passes through on its way to a candle.

    File opens (``open``/``Path.open``/``os.open``), byte reads (``daily._read``),
    decompression (``gzip``), JSON parsing (``json.loads`` and
    ``daily._strict_json_loads``) and schema decoding (``ShoonyaDailyV1``) all
    record what they saw. A protected payload is detected by path and by a
    sentinel value that only protected rows contain.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, protected_bytes: set[bytes]) -> None:
        self.protected_bytes = protected_bytes
        self.events: list[str] = []
        self.violations: list[str] = []
        sentinel = str(SENTINEL)

        def check_path(where: str, path: object) -> None:
            name = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
            if isinstance(name, bytes):
                name = name.decode(errors="replace")
            if f"{PROTECTED}.json.gz" in name and not name.endswith((".done", ".meta.json")):
                self.violations.append(f"{where}:{name}")

        def check_data(where: str, data: object) -> None:
            self.events.append(where)
            text = data if isinstance(data, (bytes, bytearray)) else str(data).encode()
            if bytes(text) in self.protected_bytes or sentinel.encode() in bytes(text):
                self.violations.append(where)

        real_open, real_path_open, real_os_open = builtins.open, Path.open, os.open

        def spy_open(file, *args, **kwargs):
            check_path("open", file)
            return real_open(file, *args, **kwargs)

        def spy_path_open(self_path, *args, **kwargs):
            check_path("Path.open", self_path)
            return real_path_open(self_path, *args, **kwargs)

        def spy_os_open(path, *args, **kwargs):
            check_path("os.open", path)
            return real_os_open(path, *args, **kwargs)

        real_read, real_strict, real_loads = daily._read, daily._strict_json_loads, json.loads
        real_gzip_file, real_decompress = gzip.GzipFile, gzip.decompress
        real_structure, real_parse = ShoonyaDailyV1.decode_structure, ShoonyaDailyV1.parse_decoded

        def spy_read(path, limit):
            check_path("daily._read", path)
            data = real_read(path, limit)
            check_data("daily._read", data)
            return data

        def spy_strict(data):
            check_data("daily._strict_json_loads", data)
            return real_strict(data)

        def spy_loads(data, *args, **kwargs):
            check_data("json.loads", data)
            return real_loads(data, *args, **kwargs)

        def spy_gzip_file(*args, **kwargs):
            source = kwargs.get("fileobj")
            if isinstance(source, io.BytesIO):
                check_data("gzip.GzipFile", source.getvalue())
            return real_gzip_file(*args, **kwargs)

        def spy_decompress(data, *args, **kwargs):
            check_data("gzip.decompress", data)
            return real_decompress(data, *args, **kwargs)

        def spy_structure(row):
            check_data("ShoonyaDailyV1.decode_structure", row)
            return real_structure(row)

        def spy_parse(row):
            check_data("ShoonyaDailyV1.parse_decoded", row)
            return real_parse(row)

        monkeypatch.setattr(builtins, "open", spy_open)
        monkeypatch.setattr(Path, "open", spy_path_open)
        monkeypatch.setattr(os, "open", spy_os_open)
        monkeypatch.setattr(daily, "_read", spy_read)
        monkeypatch.setattr(daily, "_strict_json_loads", spy_strict)
        monkeypatch.setattr(json, "loads", spy_loads)
        monkeypatch.setattr(gzip, "GzipFile", spy_gzip_file)
        monkeypatch.setattr(gzip, "decompress", spy_decompress)
        monkeypatch.setattr(ShoonyaDailyV1, "decode_structure", staticmethod(spy_structure))
        monkeypatch.setattr(ShoonyaDailyV1, "parse_decoded", staticmethod(spy_parse))


def _sentinel_world(tmp_path: Path) -> tuple[Path, object, set[bytes]]:
    """A world whose protected segments carry a sentinel volume in every row."""
    raw, evidence = build_world(tmp_path)
    protected: set[bytes] = set()
    for index, symbol in enumerate(PAIR):
        rows = write_symbol(tmp_path / "scratch", symbol, phase=index * 1.3)
        poisoned = [(*row[:5], SENTINEL) for row in rows if row[0] >= HOLDOUT]
        path = write_segment(raw, symbol, "2024-10-01", "2024-12-31", poisoned)
        protected.add(path.read_bytes())
    return raw, evidence, protected


def test_H_protected_content_reaches_no_decoder_across_every_m5_path(tmp_path: Path, monkeypatch) -> None:
    raw, evidence, protected = _sentinel_world(tmp_path / "world")
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    version = registry.save(strategy_config())
    holdout = load_holdout_boundary(Path(evidence.config_path).parent / "m32-run", evidence.trust_policy)
    spy = DecoderSpy(monkeypatch, protected)

    adapter = HistoricalDataAdapter(raw, evidence)
    strict = adapter.assess_universe(UniverseRequest(PAIR, "2024-01-01", "2024-09-30", STRICT))
    diagnostic = adapter.assess_universe(UniverseRequest(PAIR, "2024-01-01", "2024-09-30", DIAG))
    for decision in diagnostic.decisions:
        adapter.revalidate(decision)
    run = run_historical_backtest(version, diagnostic, BacktestSettings.from_dict(settings_dict()), tmp_path / "out")
    replay_run(load_run(tmp_path / "out" / run["run_id"]), adapter, registry)
    inventory = discover(raw, evidence.etf_heuristic, verify=True, holdout=holdout)

    assert spy.events, "the instrumentation must observe development decoding"
    assert spy.violations == []
    assert strict.status in {DecisionStatus.BLOCKED, DecisionStatus.ELIGIBLE}
    assert diagnostic.released and all(bar.date < HOLDOUT for d in diagnostic.released for bar in d.bars)
    assert inventory["dispositions"]["NOT_OPENED_LOCKED_HOLDOUT"] == len(PAIR)
    assert inventory["holdout_cutoff"] == HOLDOUT


def test_H_spy_detects_a_protected_decode_so_the_proof_is_not_vacuous(tmp_path: Path, monkeypatch) -> None:
    raw, _, protected = _sentinel_world(tmp_path)
    spy = DecoderSpy(monkeypatch, protected)
    daily.validate_daily(raw / "ALPHA-EQ" / f"{PROTECTED}.json.gz")  # the M1/M2 validator decodes everything
    assert spy.violations


def test_H_straddling_segment_fails_closed_without_being_opened(tmp_path: Path, monkeypatch) -> None:
    raw, evidence = build_world(tmp_path)
    rows = write_symbol(tmp_path / "scratch", "ALPHA-EQ")
    for path in (raw / "ALPHA-EQ").glob("2024-07-01_*"):
        path.unlink()
    for path in (raw / "ALPHA-EQ").glob(f"{PROTECTED}*"):
        path.unlink()
    straddle = write_segment(raw, "ALPHA-EQ", "2024-07-01", "2024-12-31",
                             [(*r[:5], SENTINEL) if r[0] >= HOLDOUT else r for r in rows if r[0] >= "2024-07-01"])
    spy = DecoderSpy(monkeypatch, {straddle.read_bytes()})
    opened: list[str] = []
    real_sha = file_sha256
    monkeypatch.setattr("stock_gplm.m5.adapter.file_sha256", lambda p: opened.append(Path(p).name) or real_sha(p))
    decision, dataset = HistoricalDataAdapter(raw, evidence).assess(
        DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", DIAG))
    assert decision.status == DecisionStatus.REJECTED and dataset is None
    message = next(f.message for f in decision.failures if f.code == "SEGMENT_REJECTED")
    assert "payload was not opened" in message and "republish" in message
    assert not [v for v in spy.violations if "2024-07-01_2024-12-31.json.gz" in v or "_read" in v]
    assert "2024-07-01_2024-12-31.json.gz" not in opened  # not even byte-hashed through the adapter


@pytest.mark.parametrize("override, expected", [
    ({"from": None}, "lacks 'from'"),
    ({"to": None}, "lacks 'to'"),
    ({"to": "2024-12-31"}, "disagrees with the publication filename"),
    ({"from": "2024-06-01"}, "disagrees with the publication filename"),
    ({"to": 20240930}, "must be an ISO date string"),
    ({"symbol": "OTHER-EQ"}, "symbol disagrees"),
])
def test_H_missing_or_inconsistent_partition_metadata_is_rejected_before_open(
        tmp_path: Path, monkeypatch, override: dict, expected: str) -> None:
    raw, evidence = build_world(tmp_path)
    rows = write_symbol(tmp_path / "scratch", "ALPHA-EQ")
    path = write_segment(raw, "ALPHA-EQ", "2024-07-01", "2024-09-30",
                         [r for r in rows if "2024-07-01" <= r[0] <= "2024-09-30"])
    meta_path = path.with_name("2024-07-01_2024-09-30.meta.json")
    meta = json.loads(meta_path.read_text())
    for key, value in override.items():
        if value is None:
            meta.pop(key)
        else:
            meta[key] = value
    meta_path.write_text(json.dumps(meta))
    spy = DecoderSpy(monkeypatch, {path.read_bytes()})
    report = validate_daily_for_development(path, HOLDOUT)
    assert report.disposition == "REJECT_PUBLICATION"
    assert [i.code for i in report.publication_integrity.issues] == ["partition_metadata"]
    assert expected in report.publication_integrity.issues[0].message
    # Only the small sidecar was parsed; the payload bytes were never read or decompressed.
    assert spy.violations == []
    assert "gzip.GzipFile" not in spy.events and "gzip.decompress" not in spy.events
    decision, _ = HistoricalDataAdapter(raw, evidence).assess(
        DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", DIAG))
    assert decision.status == DecisionStatus.REJECTED and "METADATA_MISMATCH" in {f.code for f in decision.failures}


def test_H_missing_partition_sidecar_and_bad_filename_fail_closed(tmp_path: Path) -> None:
    raw, _ = build_world(tmp_path)
    path = raw / "ALPHA-EQ" / "2024-07-01_2024-09-30.json.gz"
    path.with_name("2024-07-01_2024-09-30.meta.json").unlink()
    report = validate_daily_for_development(path, HOLDOUT)
    assert report.disposition == "REJECT_PUBLICATION" and "sidecar is missing" in report.publication_integrity.issues[0].message
    bogus = path.with_name("2024-09-30_2024-07-01.json.gz")
    bogus.write_bytes(b"x")
    assert validate_daily_for_development(bogus, HOLDOUT).publication_integrity.issues[0].code == "partition_metadata"


@pytest.mark.parametrize("mutation, issue", [
    (lambda rows: [*rows[:-1], json.dumps({"time": "02-SEP-2024", "unexpected": "shape"})], "unsupported_schema"),
    (lambda rows: [*rows[:-1], json.dumps(json.loads(rows[-1]) | {"intc": "not-a-price"})], "invalid_record"),
    (lambda rows: [json.dumps(json.loads(rows[0]) | {"time": "15-OCT-2024"}), *rows[1:]], "invalid_record"),
])
def test_H_malformed_or_unassignable_development_rows_raise_explicit_integrity_failures(
        tmp_path: Path, mutation, issue: str) -> None:
    raw, evidence = build_world(tmp_path)
    path = raw / "ALPHA-EQ" / "2024-07-01_2024-09-30.json.gz"
    rows = json.loads(gzip.decompress(path.read_bytes()))
    rewritten = gzip.compress(json.dumps(mutation(rows)).encode(), mtime=0)
    path.write_bytes(rewritten)
    digest = hashlib.sha256(rewritten).hexdigest()
    meta_path = path.with_name("2024-07-01_2024-09-30.meta.json")
    meta_path.write_text(json.dumps(json.loads(meta_path.read_text()) | {"sha256_compressed": digest}))
    done_path = Path(f"{path}.done")
    done_path.write_text(json.dumps(json.loads(done_path.read_text()) | {"sha256": digest, "bytes": len(rewritten)}))

    report = validate_daily_for_development(path, HOLDOUT)
    assert report.disposition.startswith("QUARANTINE") and issue in {i.code for i in report.content_quality.issues}
    assert report.content_quality.normalized_candles == []  # nothing silently kept or dropped
    for mode in (DIAG, STRICT):
        decision, dataset = HistoricalDataAdapter(raw, evidence).assess(
            DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", mode))
        failure = next(f for f in decision.failures if f.code == "SEGMENT_QUARANTINED")
        assert decision.status == DecisionStatus.REJECTED and dataset is None
        assert issue in failure.message and "@" in failure.message  # row index is reported


def test_H_discovery_verification_requires_the_hash_verified_holdout(tmp_path: Path, monkeypatch) -> None:
    raw, evidence = build_world(tmp_path)
    with pytest.raises(ValueError, match="holdout boundary"):
        discover(raw, evidence.etf_heuristic, verify=True)
    with pytest.raises(SystemExit):
        m5_cli.main(["--raw-root", str(raw), "discover", "--verify", "--output", str(tmp_path / "d.json")])
    assert not (tmp_path / "d.json").exists()


def test_H_run_store_rederives_partition_evidence(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path / "world")
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    assessment = HistoricalDataAdapter(raw, evidence).assess_universe(
        UniverseRequest(PAIR, "2024-01-01", "2024-09-30", DIAG))
    run = run_historical_backtest(registry.save(strategy_config()), assessment,
                                  BacktestSettings.from_dict(settings_dict()), tmp_path / "out")
    directory = tmp_path / "out" / run["run_id"]
    verified = load_run(directory)
    assert verified.run["holdout_access"]["opened_segment_windows"] == [
        ["2024-01-01", "2024-06-30"], ["2024-07-01", "2024-09-30"]]
    # Rewriting the claim (and re-sealing the manifest) is caught by re-derivation from decisions.
    run_path = directory / "run.json"
    forged = json.loads(run_path.read_text())
    forged["holdout_access"]["opened_segment_windows"] = [["2024-01-01", "2024-06-30"]]
    _rewrite_run(directory, forged)
    with pytest.raises(RunIntegrityError, match="holdout access evidence differs"):
        load_run(directory)


def _rewrite_run(directory: Path, run: dict) -> None:
    from stock_gplm.m5.runner import _encode

    payload = _encode(run)
    (directory / "run.json").write_bytes(payload)
    manifest_path = directory / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["file_sha256"]["run.json"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_bytes(_encode(manifest))


# =============================================================== C: independent approval authority

def _registration(directory: Path, *, environment: str = "PRODUCTION", reviewer: str = "test-reviewer",
                  name: str = "cost-registration", payload_sha256: str = "a" * 64,
                  evidence_type: str = "COST_SCHEDULE") -> tuple[Path, dict]:
    document = directory / f"{name}-source.bin"
    document.write_bytes(b"synthetic reviewer-downloaded document; not market evidence")
    value = {
        "contract": "stock-gplm.m511-evidence-registration.v1", "schema_version": 1,
        "evidence_type": evidence_type, "source_identity": "synthetic-m512-test",
        "source_document_file": document.name, "source_document_sha256": file_sha256(document),
        "acquisition_method": "MANUAL_REVIEW", "acquired_at_utc": "2024-10-02T00:00:00Z",
        "effective_start": "2024-01-01", "effective_end": "2024-12-31",
        "licensing_decision": "APPROVED_FOR_RESEARCH_USE", "reviewer_identity": reviewer,
        "approval_record": "synthetic-approval-record", "approval_status": "APPROVED",
        "evidence_schema": "synthetic.v1", "evidence_version": 1, "environment": environment,
        "dependencies": {"payload": payload_sha256},
    }
    value["provenance_hash"] = canonical_hash({k: value[k] for k in sorted(value) if k not in {
        "approval_record", "approval_status", "reviewer_identity"}})
    value["registration_id"] = canonical_hash({k: value[k] for k in sorted(value)})
    path = directory / f"{name}.json"
    path.write_text(json.dumps(value))
    return path, value


def _cost_world(tmp_path: Path, **registration_options) -> tuple[Path, object, dict]:
    """Synthetic world whose evidence-paths file references a PRODUCTION cost registration."""
    raw, evidence = build_world(tmp_path / "world")
    evidence_dir = Path(evidence.config_path).parent
    costs = {"contract": "stock-gplm.m511-cost-schedule.v1", "schema_version": 1,
             "effective_start": "2024-01-01", "effective_end": "2024-12-31",
             "components": {"commission_bps": 5, "slippage_bps": 10,
                            "buy_statutory_bps": 12.41, "sell_statutory_bps": 10.91}}
    costs["profile_id"] = canonical_hash(costs)
    costs_path = evidence_dir / "cost-schedule.json"
    costs_path.write_text(json.dumps(costs))
    registration_path, registration = _registration(evidence_dir, payload_sha256=file_sha256(costs_path),
                                                    **registration_options)
    paths = json.loads(Path(evidence.config_path).read_text())
    paths["cost_schedule"] = {"payload": costs_path.name, "registration": registration_path.name}
    Path(evidence.config_path).write_text(json.dumps(paths))
    return raw, Path(evidence.config_path), registration


def test_C_research_interfaces_accept_no_policy_or_checksum() -> None:
    from stock_gplm.v1 import studio

    with pytest.raises(TypeError):
        load_trust_policy(Path("caller-policy.json"))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        load_trust_policy(expected_sha256="0" * 64)  # type: ignore[call-arg]
    assert "trust_policy_path" not in inspect.signature(load_reference_evidence).parameters
    assert "trust_policy_sha256" not in inspect.signature(load_reference_evidence).parameters
    assert not {"trust_policy", "trust_policy_sha256"} & set(inspect.signature(studio.make_server).parameters)
    with pytest.raises(SystemExit):
        m5_cli.main(["compare", "x", "--trust-policy", "p.json", "--trust-policy-sha256", "0" * 64])
    with pytest.raises(TypeError, match="signer"):
        load_trust_policy(_sign=lambda digest: "forged")


def test_C_caller_generated_production_registration_and_matching_policy_are_not_authority(
        tmp_path: Path, monkeypatch) -> None:
    raw, config, registration = _cost_world(tmp_path)
    remove_trust_root(tmp_path / "no-admin", monkeypatch)
    evidence_dir = config.parent
    # The attacker writes a PRODUCTION registration, a matching policy and its checksum
    # next to ordinary research inputs. None of them is reachable as authority.
    policy = trust_policy_value([approval_for(registration)])
    (evidence_dir / "approval-trust-policy.json").write_text(json.dumps(policy))
    (evidence_dir / "m5-approval-trust-root.json").write_text(json.dumps(
        trust_root_value(json.dumps(policy).encode())))
    loaded = load_reference_evidence(config)
    assert loaded.trust_policy is None and loaded.cost_schedule is not None
    assert loaded.holdout is None  # M5.1.3: no governed authority means no holdout boundary at all
    assert not loaded.cost_schedule.authority_approved
    assert "administrative_trust_root" not in loaded.source_hashes
    request = DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT)
    assert "COST_SCHEDULE_UNREGISTERED" in {code for code, _ in strict_authority_failures(loaded, request)}


def test_C_caller_constructed_or_edited_policy_objects_never_approve(tmp_path: Path, monkeypatch) -> None:
    registration_path, raw = _registration(tmp_path)
    registration = load_evidence_registration(registration_path)
    approval = approval_for(raw)
    forged = ApprovalTrustPolicy("p", "0" * 64, frozenset({"test-reviewer"}),
                                 {registration.registration_id: approval}, "r", "1" * 64, 1,
                                 GovernedHoldout("synthetic-m32", "2" * 64, "2099-01-01"))
    assert not forged.governed and not forged.approves(registration)
    install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
    genuine = load_trust_policy()
    assert genuine is not None and genuine.governed and not genuine.approves(registration)
    edited = replace(genuine, approvals={registration.registration_id: approval})
    assert not edited.governed and not edited.approves(registration)
    copied_seal = replace(forged, issuance_seal=genuine.issuance_seal)
    assert not copied_seal.approves(registration)


def test_C_governed_trust_root_is_the_only_path_to_approval(tmp_path: Path, monkeypatch) -> None:
    raw, config, registration = _cost_world(tmp_path)
    install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([approval_for(registration)]))
    loaded = load_reference_evidence(config)
    assert loaded.trust_policy is not None and loaded.trust_policy.governed
    assert loaded.trust_policy.trust_root_generation == 1
    assert loaded.cost_schedule is not None and loaded.cost_schedule.authority_approved
    assert loaded.source_hashes["administrative_trust_root"] == file_sha256(
        tmp_path / "admin" / authority.TRUST_ROOT_FILE)
    assert loaded.source_hashes["approval_trust_policy"] == loaded.trust_policy.policy_sha256
    # Deterministic provenance: an identical reload yields the same evidence identity.
    assert evidence_digest(load_reference_evidence(config)) == evidence_digest(loaded)


def test_C_synthetic_or_untrusted_reviewer_registrations_stay_unapproved(tmp_path: Path, monkeypatch) -> None:
    _, synthetic = _registration(tmp_path, environment="SYNTHETIC", name="synthetic")
    _, rogue = _registration(tmp_path, reviewer="rogue-reviewer", name="rogue")
    _, genuine = _registration(tmp_path, name="genuine")
    forged_approval = approval_for(genuine) | {"provenance_hash": "f" * 64}
    install_trust_root(tmp_path / "admin", monkeypatch,
                       trust_policy_value([approval_for(synthetic), forged_approval]))
    policy = load_trust_policy()
    assert policy is not None
    assert not policy.approves(load_evidence_registration(tmp_path / "synthetic.json"))
    assert not policy.approves(load_evidence_registration(tmp_path / "rogue.json"))
    assert not policy.approves(load_evidence_registration(tmp_path / "genuine.json"))  # provenance mismatch


def test_C_forged_registration_content_is_rejected(tmp_path: Path) -> None:
    path, value = _registration(tmp_path)
    path.write_text(json.dumps(value | {"effective_end": "2025-12-31"}))
    with pytest.raises(ValueError, match="provenance_hash|registration_id"):
        load_evidence_registration(path)


def test_C_modified_or_replaced_policy_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _, registration = _registration(tmp_path)
    admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([approval_for(registration)]))
    policy_path = admin / "approval-trust-policy.json"
    policy_path.write_text(json.dumps(trust_policy_value([approval_for(registration)], ["test-reviewer", "x"])))
    with pytest.raises(ValueError, match="does not match the administrative trust-root SHA-256"):
        load_trust_policy()


@pytest.mark.parametrize("change, message", [
    (lambda root: root | {"generation": 2}, "identifier does not match"),
    (lambda root: root | {"policy_sha256": "0" * 64}, "identifier does not match"),
    (lambda root: root | {"extra": True}, "contract or fields"),
])
def test_C_trust_root_mismatch_is_rejected(tmp_path: Path, monkeypatch, change, message: str) -> None:
    admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
    root_path = admin / authority.TRUST_ROOT_FILE
    root_path.write_text(json.dumps(change(json.loads(root_path.read_text()))))
    with pytest.raises(ValueError, match=message):
        load_trust_policy()


@pytest.mark.parametrize("options, message", [
    ({"status": "REVOKED"}, "not ACTIVE"),
    ({"reviewers": ["someone-else"]}, "reviewer not authorized"),
    ({"policy_file": "../escape.json"}, "one policy file"),
])
def test_C_revoked_root_unauthorized_reviewer_and_escaping_policy_are_rejected(
        tmp_path: Path, monkeypatch, options: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        if options.get("policy_file") == "../escape.json":
            admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
            policy_bytes = (admin / "approval-trust-policy.json").read_bytes()
            (admin / authority.TRUST_ROOT_FILE).write_text(json.dumps(
                trust_root_value(policy_bytes, policy_file="../escape.json")))
        else:
            install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]), **options)
        load_trust_policy()


def test_C_revoked_policy_hash_cannot_be_reinstated(tmp_path: Path, monkeypatch) -> None:
    policy = trust_policy_value([])
    revoked = hashlib.sha256(json.dumps(policy).encode()).hexdigest()
    install_trust_root(tmp_path / "admin", monkeypatch, policy, revoked=[revoked], generation=3)
    with pytest.raises(ValueError, match="revoked policy"):
        load_trust_policy()


def test_C_research_running_as_the_administrative_account_is_refused(tmp_path: Path, monkeypatch) -> None:
    install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]), research_uid=os.geteuid())
    with pytest.raises(ValueError, match="must not run as the administrative"):
        load_trust_policy()


def test_C_writable_or_symlinked_governed_files_are_rejected(tmp_path: Path, monkeypatch) -> None:
    admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
    os.chmod(admin / "approval-trust-policy.json", 0o666)
    with pytest.raises(ValueError, match="group/other writable"):
        load_trust_policy()
    os.chmod(admin / "approval-trust-policy.json", 0o644)
    os.chmod(admin, 0o777)
    with pytest.raises(ValueError, match="group/other writable"):
        load_trust_policy()
    os.chmod(admin, 0o755)
    real = admin / "approval-trust-policy.json"
    moved = tmp_path / "elsewhere.json"
    real.rename(moved)
    real.symlink_to(moved)
    with pytest.raises(OSError):
        load_trust_policy()


@pytest.mark.skipif((not hasattr(os, "geteuid") or os.geteuid() == 0), reason="needs a non-root test user to prove ownership enforcement")
def test_C_user_owned_trust_root_is_rejected_when_root_ownership_is_required(tmp_path: Path, monkeypatch) -> None:
    admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
    monkeypatch.setattr(authority, "_ANCHOR", authority.TrustAnchor(admin, 0, admin))
    monkeypatch.setattr(authority, "_effective_uid", os.geteuid)  # real identity, real ownership check
    with pytest.raises(ValueError, match="not owned by the administrative account"):
        load_trust_policy()


def test_C_production_anchor_is_fixed_outside_research_inputs() -> None:
    source = inspect.getsource(authority)
    assert source.count("_ANCHOR = ") == 1
    assert '_ANCHOR = TrustAnchor(Path("/etc/stock-gplm"), 0, Path("/"))' in source
    for forbidden in ("PRIVATE KEY", "BEGIN OPENSSH", "secret_key", "os.environ", "getenv"):
        assert forbidden not in source


def test_C_replay_and_provenance_follow_the_governed_authority(tmp_path: Path, monkeypatch) -> None:
    raw, config, registration = _cost_world(tmp_path)
    install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([approval_for(registration)]))
    evidence = load_reference_evidence(config)
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    version = registry.save(strategy_config())
    adapter = HistoricalDataAdapter(raw, evidence)
    assessment = adapter.assess_universe(UniverseRequest(PAIR, "2024-01-01", "2024-09-30", DIAG))
    run = run_historical_backtest(version, assessment, BacktestSettings.from_dict(settings_dict()), tmp_path / "out")
    directory = tmp_path / "out" / run["run_id"]
    sources = run["evidence_versions"][0]["sources"]
    assert sources["administrative_trust_root"] == evidence.source_hashes["administrative_trust_root"]

    # Same governed authority: replay reproduces the run exactly.
    same = HistoricalDataAdapter(raw, load_reference_evidence(config))
    assert replay_run(load_run(directory), same, registry).verification == REPLAY_VERIFIED

    # Administrative rotation (new generation, new policy): earlier decisions go stale and
    # replay under the new authority no longer reproduces the recorded identity.
    install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]), generation=2)
    with pytest.raises(StaleAssessmentError, match="registered evidence changed"):
        adapter.revalidate(assessment.decisions[0])
    rotated = HistoricalDataAdapter(raw, load_reference_evidence(config))
    with pytest.raises(RunIntegrityError, match="replay"):
        replay_run(load_run(directory), rotated, registry)
