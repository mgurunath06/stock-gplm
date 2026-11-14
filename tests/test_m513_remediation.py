"""M5.1.3 adversarial tests: governed holdout authority, v4 run contract, trust-root ancestry.

Everything is synthetic. The administrative trust root is a SYNTHETIC stand-in installed
by ``tests/conftest.py`` (the test user plays the administrator). These tests prove code
behaviour only; they say nothing about deployment-level enforcement on a real host.
"""
from __future__ import annotations

import builtins
import gzip
import hashlib
import io
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from m5_fixtures import (
    FIXTURE_GOVERNED_HOLDOUT,
    FIXTURE_M32_RUN_ID,
    FIXTURE_SPLIT_PLAN_BYTES,
    HOLDOUT,
    build_world,
    install_trust_root,
    remove_trust_root,
    settings_dict,
    strategy_config,
    trust_policy_value,
    trust_root_value,
    write_m32_run,
    write_segment,
    write_symbol,
)
from test_m512_remediation import PROTECTED, SENTINEL, DecoderSpy

from stock_gplm.m5 import (
    BacktestSettings,
    DataEligibilityRequest,
    DecisionStatus,
    HistoricalDataAdapter,
    UniverseRequest,
    authority,
    compare_runs,
    run_historical_backtest,
)
from stock_gplm.m5 import cli as m5_cli
from stock_gplm.m5 import runner as m5_runner
from stock_gplm.m5.discovery import discover
from stock_gplm.m5.evidence import HoldoutBoundary, load_holdout_boundary, load_reference_evidence
from stock_gplm.m5.holdout_validation import validate_daily_for_development
from stock_gplm.m5.models import (
    ADAPTER_VERSION,
    CanonicalDailyBar,
    DataEligibilityDecision,
    VerifiedHistoricalDataset,
    bars_hash,
    canonical_hash,
)
from stock_gplm.m5.run_store import (
    PRIOR_CONTRACT_VERIFIED,
    REPLAY_VERIFIED,
    RunIntegrityError,
    load_run,
    replay_run,
)
from stock_gplm.m5.runner import _encode, render_markdown
from stock_gplm.v1.registry import StrategyRegistry

PAIR = ("ALPHA-EQ", "BETA-EQ")
DIAG = "DIAGNOSTIC_ONLY"
STRICT = "STRICT_RESEARCH"
EXTENDED = "2099-01-01"


def _extended_plan_bytes(cutoff: str = EXTENDED) -> bytes:
    return json.dumps({"schema_version": 1, "plans": [
        {"horizon": "6-12m", "locked_holdout": {"first_as_of": cutoff, "last_as_of": "2099-12-31"}}]}).encode()


def _sentinel_world(tmp_path: Path) -> tuple[Path, object, set[bytes]]:
    raw, evidence = build_world(tmp_path)
    protected: set[bytes] = set()
    for index, symbol in enumerate(PAIR):
        rows = write_symbol(tmp_path / "scratch", symbol, phase=index * 1.3)
        path = write_segment(raw, symbol, "2024-10-01", "2024-12-31",
                             [(*row[:5], SENTINEL) for row in rows if row[0] >= HOLDOUT])
        protected.add(path.read_bytes())
    return raw, evidence, protected


class WideSpy(DecoderSpy):
    """DecoderSpy plus the alternative file-opening paths the M5.1.2 audit named."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, protected_bytes: set[bytes]) -> None:
        super().__init__(monkeypatch, protected_bytes)
        real_io_open, real_gzip_open = io.open, gzip.open
        real_read_bytes, real_read_text = Path.read_bytes, Path.read_text

        def protected_path(path: object) -> bool:
            name = os.fspath(path) if isinstance(path, (str, os.PathLike)) else ""
            return isinstance(name, str) and name.endswith(f"{PROTECTED}.json.gz")

        def spy_io_open(file, *args, **kwargs):
            if protected_path(file):
                self.violations.append(f"io.open:{file}")
            return real_io_open(file, *args, **kwargs)

        def spy_gzip_open(file, *args, **kwargs):
            if protected_path(file):
                self.violations.append(f"gzip.open:{file}")
            return real_gzip_open(file, *args, **kwargs)

        def spy_read_bytes(path):
            if protected_path(path):
                self.violations.append(f"Path.read_bytes:{path}")
            return real_read_bytes(path)

        def spy_read_text(path, *args, **kwargs):
            if protected_path(path):
                self.violations.append(f"Path.read_text:{path}")
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(io, "open", spy_io_open)
        monkeypatch.setattr(gzip, "open", spy_gzip_open)
        monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
        monkeypatch.setattr(Path, "read_text", spy_read_text)


def _point_evidence_at(config: Path, m32_dir_name: str) -> None:
    paths = json.loads(config.read_text())
    paths["m32_run"] = m32_dir_name
    config.write_text(json.dumps(paths))


# =============================================================== H: caller cannot set the cutoff

def test_H_caller_written_2099_split_plan_cannot_authorize_protected_reads(tmp_path: Path, monkeypatch) -> None:
    raw, evidence, protected = _sentinel_world(tmp_path / "world")
    config = Path(evidence.config_path)
    write_m32_run(config.parent / "caller-m32", _extended_plan_bytes(), FIXTURE_M32_RUN_ID)
    _point_evidence_at(config, "caller-m32")
    spy = WideSpy(monkeypatch, protected)
    with pytest.raises(ValueError, match="not the administratively governed split plan"):
        load_reference_evidence(config)
    assert spy.violations == []


def test_H_rewriting_the_governed_runs_plan_or_identity_is_rejected(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    config = Path(evidence.config_path)
    m32 = config.parent / "m32-run"
    write_m32_run(m32, _extended_plan_bytes(), FIXTURE_M32_RUN_ID)  # same run id, extended plan
    with pytest.raises(ValueError, match="SHA-256 differs"):
        load_reference_evidence(config)
    write_m32_run(m32, FIXTURE_SPLIT_PLAN_BYTES, "caller-renamed-run")  # governed bytes, other identity
    with pytest.raises(ValueError, match="not the administratively governed run"):
        load_reference_evidence(config)
    write_m32_run(m32, FIXTURE_SPLIT_PLAN_BYTES, FIXTURE_M32_RUN_ID)
    (m32 / "split-plan.json").write_bytes(_extended_plan_bytes())  # manifest no longer matches
    with pytest.raises(ValueError, match="does not match its run-manifest hash"):
        load_reference_evidence(config)


def test_H_trust_root_cutoff_must_equal_the_plans_cutoff(tmp_path: Path, monkeypatch) -> None:
    raw, evidence = build_world(tmp_path / "world")
    install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]),
                       governed_holdout=FIXTURE_GOVERNED_HOLDOUT | {"cutoff": EXTENDED})
    with pytest.raises(ValueError, match="differs from the governed cutoff"):
        load_reference_evidence(Path(evidence.config_path))


def test_H_hand_built_or_edited_boundaries_are_never_governed(tmp_path: Path, monkeypatch) -> None:
    raw, evidence, protected = _sentinel_world(tmp_path / "world")
    genuine = evidence.holdout
    assert genuine is not None and genuine.governed and genuine.cutoff == HOLDOUT
    forged = HoldoutBoundary(EXTENDED, "forged", "0" * 64, "1" * 64)
    extended = replace(genuine, cutoff=EXTENDED)  # keeps the genuine seal, changes the content
    assert not forged.governed and not extended.governed
    spy = WideSpy(monkeypatch, protected)
    for boundary in (forged, extended):
        with pytest.raises(ValueError, match="governed"):
            discover(raw, evidence.etf_heuristic, verify=True, holdout=boundary)
        tampered = replace(evidence, holdout=boundary)
        for mode in (DIAG, STRICT):
            decision, dataset = HistoricalDataAdapter(raw, tampered).assess(
                DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-12-31", mode))
            assert decision.status == DecisionStatus.REJECTED and dataset is None
            assert "HOLDOUT_BOUNDARY_UNVERIFIED" in {f.code for f in decision.failures}
    assert spy.violations == []


def test_H_without_any_governed_authority_nothing_opens_or_runs(tmp_path: Path, monkeypatch) -> None:
    raw, evidence, protected = _sentinel_world(tmp_path / "world")
    remove_trust_root(tmp_path / "no-admin", monkeypatch)
    spy = WideSpy(monkeypatch, protected)
    ungoverned = load_reference_evidence(Path(evidence.config_path))
    assert ungoverned.holdout is None and ungoverned.trust_policy is None
    for mode in (DIAG, STRICT):
        decision, dataset = HistoricalDataAdapter(raw, ungoverned).assess(
            DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", mode))
        assert decision.status == DecisionStatus.REJECTED and dataset is None and decision.segments == ()
        assert "HOLDOUT_BOUNDARY_UNVERIFIED" in {f.code for f in decision.failures}
    with pytest.raises(ValueError, match="no administratively governed holdout authority"):
        load_holdout_boundary(Path(evidence.config_path).parent / "m32-run", None)
    with pytest.raises(ValueError, match="no administratively governed holdout authority"):
        m5_cli.main(["--raw-root", str(raw), "discover", "--verify", "--m32-run",
                     str(Path(evidence.config_path).parent / "m32-run"), "--output", str(tmp_path / "d.json")])
    assert not (tmp_path / "d.json").exists()
    with pytest.raises(ValueError, match="no administratively governed holdout authority"):
        run_historical_backtest(StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config()),
                                [_caller_dataset()], BacktestSettings.from_dict(settings_dict(
                                    evaluation_start="2024-02-01", evaluation_end="2024-02-27")), tmp_path / "out")
    assert spy.violations == [] and not (tmp_path / "out").exists()


def test_H_cli_discover_uses_only_the_governed_boundary(tmp_path: Path, monkeypatch) -> None:
    raw, evidence, protected = _sentinel_world(tmp_path / "world")
    m32 = Path(evidence.config_path).parent / "m32-run"
    spy = WideSpy(monkeypatch, protected)
    assert m5_cli.main(["--raw-root", str(raw), "discover", "--verify", "--m32-run", str(m32),
                        "--output", str(tmp_path / "d.json")]) == 0
    report = json.loads((tmp_path / "d.json").read_text())
    assert report["holdout_cutoff"] == HOLDOUT and report["dispositions"]["NOT_OPENED_LOCKED_HOLDOUT"] == 2
    caller = tmp_path / "caller-m32"
    write_m32_run(caller, _extended_plan_bytes(), FIXTURE_M32_RUN_ID)
    with pytest.raises(ValueError, match="not the administratively governed split plan"):
        m5_cli.main(["--raw-root", str(raw), "discover", "--verify", "--m32-run", str(caller),
                     "--output", str(tmp_path / "e.json")])
    assert spy.violations == [] and not (tmp_path / "e.json").exists()


# =============================================================== H: exact cutoff day

@pytest.mark.parametrize("end, opened", [("2024-09-30", True), ("2024-10-01", False), ("2024-10-02", False)])
def test_H_exact_cutoff_day_boundary(tmp_path: Path, monkeypatch, end: str, opened: bool) -> None:
    raw, _ = build_world(tmp_path)
    rows = write_symbol(tmp_path / "scratch", "ALPHA-EQ")
    path = write_segment(raw, "ALPHA-EQ", "2024-07-01", end,
                         [(*r[:5], SENTINEL) if r[0] >= HOLDOUT else r for r in rows if "2024-07-01" <= r[0] <= end])
    spy = WideSpy(monkeypatch, {path.read_bytes()})
    report = validate_daily_for_development(path, HOLDOUT)
    if opened:
        assert report.disposition == "ACCEPT" and "gzip.GzipFile" in spy.events
    else:
        assert [i.code for i in report.publication_integrity.issues] == ["holdout_partition_required"]
        assert "gzip.GzipFile" not in spy.events and spy.violations == []


# =============================================================== H: build_run guard

def _caller_dataset(*, cutoff: str = HOLDOUT, last_day: str = "2024-02-27",
                    sources: dict | None = None) -> VerifiedHistoricalDataset:
    days = [f"2024-02-{d:02d}" for d in range(1, 28)]
    if last_day != days[-1]:
        days = [*days[:-1], last_day]
    bars = tuple(CanonicalDailyBar(day, str(100 + i), str(110 + i), "99", str(105 + i), 1000,
                                   "2024-02-01T00:00:00Z", "caller.json.gz") for i, day in enumerate(days))
    request = DataEligibilityRequest("ALPHA-EQ", "2024-01-01", max(last_day, "2024-09-30"), DIAG)
    decision = DataEligibilityDecision(ADAPTER_VERSION, request, DecisionStatus.DIAGNOSTIC_ALLOWED, (), (), (), (),
                                       cutoff, len(bars), bars[0].date, bars[-1].date, bars_hash(bars),
                                       evidence_sources=sources, price_basis="RAW_UNADJUSTED_VERIFIED")
    return VerifiedHistoricalDataset(decision, bars, "RAW_UNADJUSTED_VERIFIED")


def _diag_settings(**kw) -> BacktestSettings:
    return BacktestSettings.from_dict(settings_dict(**({"evaluation_start": "2024-02-01",
                                                        "evaluation_end": "2024-02-27"} | kw)))


def _tampered_after_construction() -> VerifiedHistoricalDataset:
    """Same-process tamper: a protected-day bar injected after the dataset passed its own checks."""
    dataset = _caller_dataset()
    bars = (*dataset.bars, replace(dataset.bars[-1], date="2024-10-01"))
    original = dataset.decision
    decision = DataEligibilityDecision(ADAPTER_VERSION, original.request, original.status, (), (), (), (),
                                       HOLDOUT, len(bars), bars[0].date, bars[-1].date, bars_hash(bars),
                                       price_basis="RAW_UNADJUSTED_VERIFIED")
    object.__setattr__(dataset, "bars", bars)  # bypasses the dataset's own construction-time check
    object.__setattr__(dataset, "decision", decision)
    return dataset


def test_H_dataset_construction_refuses_protected_bars() -> None:
    with pytest.raises(ValueError, match="locked-holdout observations"):
        _caller_dataset(last_day="2024-10-01")


@pytest.mark.parametrize("dataset, message", [
    (lambda: _caller_dataset(cutoff=EXTENDED), "the governed cutoff is 2024-10-01"),
    (_tampered_after_construction, "offered to execution; nothing was evaluated"),
    (lambda: _caller_dataset(sources={"m32_split_plan": "e" * 64, "administrative_trust_root": "f" * 64}),
     "different holdout authority"),
])
def test_H_build_run_guard_refuses_before_any_evaluation(tmp_path: Path, monkeypatch, dataset, message: str) -> None:
    if "offered" in message:
        # The dataset model's own check (models.VerifiedHistoricalDataset.verify) catches this tamper
        # first; disable it to prove the independent build_run guard still refuses on its own.
        with pytest.raises(ValueError, match="locked-holdout observations"):
            dataset().verify()
        monkeypatch.setattr(VerifiedHistoricalDataset, "verify", lambda self: None)
    evaluated: list[str] = []
    real = m5_runner.instrument_signals
    monkeypatch.setattr(m5_runner, "instrument_signals",
                        lambda validated, d: evaluated.append(d.decision.request.symbol) or real(validated, d))
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    with pytest.raises(ValueError, match=message):
        run_historical_backtest(version, [dataset()], _diag_settings(), tmp_path / "out")
    if "different holdout authority" not in message:
        assert evaluated == []  # refused before signals were evaluated
    assert not (tmp_path / "out").exists()


def test_H_caller_supplied_research_still_runs_against_the_governed_cutoff(tmp_path: Path) -> None:
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    run = run_historical_backtest(version, [_caller_dataset()], _diag_settings(), tmp_path / "out")
    assert run["eligibility_issuance"] == "CALLER_SUPPLIED_UNVERIFIED" and not run["investable"]
    assert run["holdout_access"]["holdout_authority"]["m32_split_plan_sha256"] == \
        FIXTURE_GOVERNED_HOLDOUT["split_plan_sha256"]
    assert load_run(tmp_path / "out" / run["run_id"]).run["contract"].endswith(".v4")


# =============================================================== Run contract v4

def _diag_run(tmp_path: Path, world=None):
    raw, evidence = world or build_world(tmp_path / "world")
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    adapter = HistoricalDataAdapter(raw, evidence)
    assessment = adapter.assess_universe(UniverseRequest(PAIR, "2024-01-01", "2024-09-30", DIAG))
    run = run_historical_backtest(registry.save(strategy_config()), assessment,
                                  BacktestSettings.from_dict(settings_dict()), tmp_path / "out")
    return run, tmp_path / "out" / run["run_id"], adapter, registry


def _rewrite(directory: Path, run: dict, *, manifest_contract: str | None = None, new_id: bool = False) -> Path:
    """Re-seal ``run`` (and its manifest) as an attacker or an older writer would."""
    files = {name: (directory / name).read_bytes() for name in
             ("signals.json", "orders.json", "trades.json", "equity-curve.json", "environment.json")}
    if new_id:
        binding = ("contract", "strategy_id", "strategy_version", "strategy_config_hash", "dataset_fingerprint",
                   "requested_universe_sha256", "eligibility_issuance", "settings", "simulator_version", "code",
                   "input_provenance_sha256")
        run["run_id"] = canonical_hash({key: run[key] for key in binding})
        environment = json.loads(files["environment.json"])
        environment["run_id"] = run["run_id"]
        files["environment.json"] = _encode(environment)
    target = directory.parent.parent / "rewritten" / run["run_id"]
    target.mkdir(parents=True, exist_ok=True)
    files["run.json"] = _encode(run)
    try:
        files["report.md"] = render_markdown(run).encode()
    except (KeyError, ValueError):  # no report wording for this shape; the loader must still refuse it
        files["report.md"] = b"unsupported contract\n"
    manifest = json.loads((directory / "run-manifest.json").read_text())
    for name, payload in files.items():
        (target / name).write_bytes(payload)
        manifest["file_sha256"][name] = hashlib.sha256(payload).hexdigest()
    manifest["run_id"] = run["run_id"]
    manifest["run_contract"] = run["contract"]
    if manifest_contract:
        manifest["contract"] = manifest_contract
    (target / "run-manifest.json").write_bytes(_encode(manifest))
    return target


def test_RUN_current_runs_are_v4_and_replay(tmp_path: Path) -> None:
    run, directory, adapter, registry = _diag_run(tmp_path)
    assert run["contract"] == "stock-gplm.m5-historical-backtest-run.v4"
    assert json.loads((directory / "run-manifest.json").read_text())["contract"] == "stock-gplm.m5-run-manifest.v4"
    assert replay_run(load_run(directory), adapter, registry).verification == REPLAY_VERIFIED


@pytest.mark.parametrize("field, value", [
    ("latest_released_bar", "2024-01-02"), ("latest_evaluated_session", "2024-01-02"),
    ("opened_segment_windows", [["2024-01-01", "2024-06-30"]]), ("protected_rows_released_to_execution", -1),
    ("holdout_authority", {"m32_split_plan_sha256": "e" * 64, "administrative_trust_root_sha256": "f" * 64}),
])
def test_RUN_v4_holdout_evidence_is_rederived_not_trusted(tmp_path: Path, field: str, value) -> None:
    run, directory, _, _ = _diag_run(tmp_path)
    forged = json.loads((directory / "run.json").read_text())
    forged["holdout_access"][field] = value
    with pytest.raises(RunIntegrityError, match="holdout"):
        load_run(_rewrite(directory, forged))


def test_RUN_v3_runs_stay_readable_but_are_never_replayed_or_compared(tmp_path: Path) -> None:
    run, directory, adapter, registry = _diag_run(tmp_path)
    previous = json.loads((directory / "run.json").read_text())
    previous["contract"] = "stock-gplm.m5-historical-backtest-run.v3"
    access = previous["holdout_access"]
    previous["holdout_access"] = {  # the exact M5.1.2 (v3) shape
        "policy": access["policy"], "cutoff": access["cutoff"],
        "partition_policy": access["partition_policy"], "opened_segment_windows": access["opened_segment_windows"],
        "protected_rows_released_to_execution": 0, "protected_rows_scored_or_trained": 0}
    target = _rewrite(directory, previous, manifest_contract="stock-gplm.m5-run-manifest.v3", new_id=True)
    verified = load_run(target)
    assert verified.verification == PRIOR_CONTRACT_VERIFIED and not verified.rankable
    with pytest.raises(RunIntegrityError, match="prior-contract runs"):
        replay_run(verified, adapter, registry)
    comparison = compare_runs([directory, target])
    assert comparison["comparable"] is False and comparison["ranking_by_total_return"] is None
    assert any("prior-contract" in reason for reason in comparison["reasons"])


def test_RUN_unknown_or_mismatched_contracts_are_integrity_failures(tmp_path: Path) -> None:
    run, directory, _, _ = _diag_run(tmp_path)
    future = json.loads((directory / "run.json").read_text())
    future["contract"] = "stock-gplm.m5-historical-backtest-run.v5"
    with pytest.raises(RunIntegrityError, match="unsupported run manifest contract"):
        load_run(_rewrite(directory, future, manifest_contract="stock-gplm.m5-run-manifest.v5", new_id=True))
    mixed = json.loads((directory / "run.json").read_text())
    mixed["contract"] = "stock-gplm.m5-historical-backtest-run.v3"
    with pytest.raises(RunIntegrityError, match="unsupported run manifest contract"):
        load_run(_rewrite(directory, mixed, new_id=True))  # v4 manifest naming a v3 run


def test_RUN_governed_holdout_rotation_invalidates_replay(tmp_path: Path, monkeypatch) -> None:
    run, directory, _, registry = _diag_run(tmp_path)
    world = tmp_path / "world"
    install_trust_root(tmp_path / "rotated-admin", monkeypatch, trust_policy_value([]), generation=2)
    rotated = HistoricalDataAdapter(world / "data" / "raw" / "shoonya" / "NSE",
                                    load_reference_evidence(world / "evidence" / "evidence-paths.json"))
    with pytest.raises(RunIntegrityError, match="replay"):
        replay_run(load_run(directory), rotated, registry)


# =============================================================== C: trust-root ancestry and holdout fields

def _nested_anchor(tmp_path: Path, monkeypatch) -> tuple[Path, list[Path]]:
    """Anchor at base/a/b/admin with the checked boundary at base: three governed ancestors."""
    base = tmp_path / "base"
    admin = base / "a" / "b" / "admin"
    install_trust_root(admin, monkeypatch, trust_policy_value([]))
    for directory in (base, base / "a", base / "a" / "b"):
        os.chmod(directory, 0o755)
    monkeypatch.setattr(authority, "_ANCHOR", authority.TrustAnchor(admin, os.geteuid(), base))
    assert authority.load_trust_policy() is not None  # the clean chain loads
    return admin, [admin, base / "a" / "b", base / "a", base]


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_C_every_ancestor_must_not_be_group_or_other_writable(tmp_path: Path, monkeypatch, level: int) -> None:
    _, chain = _nested_anchor(tmp_path, monkeypatch)
    for mode in (0o775, 0o757):
        os.chmod(chain[level], mode)
        with pytest.raises(ValueError, match="group/other writable"):
            authority.load_trust_policy()
    os.chmod(chain[level], 0o755)


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_C_every_ancestor_must_be_owned_by_the_administrative_account(tmp_path: Path, monkeypatch,
                                                                       level: int) -> None:
    _, chain = _nested_anchor(tmp_path, monkeypatch)
    target, real_lstat = chain[level], os.lstat

    def foreign_owner(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == target:
            values = list(result)
            values[stat.ST_UID] = os.geteuid() + 7
            return os.stat_result(values)
        return result

    monkeypatch.setattr(authority.os, "lstat", foreign_owner)
    with pytest.raises(ValueError, match="not owned by the administrative account"):
        authority.load_trust_policy()


@pytest.mark.parametrize("level", [1, 2])
def test_C_a_symlinked_ancestor_is_rejected(tmp_path: Path, monkeypatch, level: int) -> None:
    admin, chain = _nested_anchor(tmp_path, monkeypatch)
    real = chain[level]
    moved = real.with_name(real.name + "-real")
    real.rename(moved)
    real.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="not a real directory"):
        authority.load_trust_policy()


def test_C_real_filesystem_ancestors_up_to_root_are_enforced(tmp_path: Path, monkeypatch) -> None:
    """No stand-in: the real /, /tmp... chain above a user-owned anchor must fail closed."""
    admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
    monkeypatch.setattr(authority, "_ANCHOR", authority.TrustAnchor(admin, os.geteuid(), Path("/")))
    with pytest.raises(ValueError, match="administrative trust directory"):
        authority.load_trust_policy()


def test_C_anchor_directory_outside_its_boundary_is_rejected(tmp_path: Path, monkeypatch) -> None:
    admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
    monkeypatch.setattr(authority, "_ANCHOR", authority.TrustAnchor(admin, os.geteuid(), tmp_path / "elsewhere"))
    with pytest.raises(ValueError, match="inside its checked boundary"):
        authority.load_trust_policy()


def test_C_symlinked_trust_root_file_is_rejected(tmp_path: Path, monkeypatch) -> None:
    admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
    root = admin / authority.TRUST_ROOT_FILE
    moved = tmp_path / "root-elsewhere.json"
    root.rename(moved)
    root.symlink_to(moved)
    with pytest.raises(OSError):
        authority.load_trust_policy()


@pytest.mark.parametrize("governed, message", [
    ({"m32_run_id": "x", "split_plan_sha256": "a" * 64}, "exactly one M3.2 holdout"),
    (FIXTURE_GOVERNED_HOLDOUT | {"extra": 1}, "exactly one M3.2 holdout"),
    (FIXTURE_GOVERNED_HOLDOUT | {"split_plan_sha256": "A" * 64}, "lowercase SHA-256"),
    (FIXTURE_GOVERNED_HOLDOUT | {"cutoff": "2024-10-1"}, "canonical ISO-8601"),
    (FIXTURE_GOVERNED_HOLDOUT | {"cutoff": 20241001}, "canonical ISO-8601"),
    (FIXTURE_GOVERNED_HOLDOUT | {"m32_run_id": " "}, "m32_run_id"),
])
def test_C_trust_root_governed_holdout_is_validated(tmp_path: Path, monkeypatch, governed, message: str) -> None:
    install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]), governed_holdout=governed)
    with pytest.raises(ValueError, match=message):
        authority.load_trust_policy()


def test_C_editing_the_governed_holdout_without_re_deriving_root_id_is_rejected(tmp_path: Path,
                                                                                 monkeypatch) -> None:
    admin = install_trust_root(tmp_path / "admin", monkeypatch, trust_policy_value([]))
    path = admin / authority.TRUST_ROOT_FILE
    root = json.loads(path.read_text())
    root["governed_holdout"]["cutoff"] = EXTENDED
    path.write_text(json.dumps(root))
    with pytest.raises(ValueError, match="identifier does not match"):
        authority.load_trust_policy()


def test_C_trust_root_value_helper_matches_loader_contract() -> None:
    value = trust_root_value(b"{}")
    assert set(value) == authority._TRUST_ROOT_FIELDS
    assert value["governed_holdout"] == FIXTURE_GOVERNED_HOLDOUT


def test_RUN_v4_released_bar_at_cutoff_fails_rederivation_even_if_consistently_forged(tmp_path: Path) -> None:
    run, directory, _, _ = _diag_run(tmp_path)
    forged = json.loads((directory / "run.json").read_text())
    forged["datasets"][0]["decision"]["last_bar"] = HOLDOUT  # the decision and the record agree...
    forged["holdout_access"]["latest_released_bar"] = HOLDOUT  # ...but both reach the governed cutoff
    with pytest.raises(RunIntegrityError, match="released bar reaches the locked holdout"):
        load_run(_rewrite(directory, forged))
