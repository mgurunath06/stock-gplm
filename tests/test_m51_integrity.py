"""M5.1 adversarial regressions for the M5 integrity audit (findings A-H). Synthetic data only."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import pickle
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from m5_fixtures import (
    ROOT,
    build_world,
    event_record,
    price_basis_registration,
    settings_dict,
    strategy_config,
)
from m5_fixtures import waive_strict_authority  # noqa: F401 - fixture

# Synthetic fixtures: the strict authority gates are waived here and enforced in test_m511_authority.py.
pytestmark = pytest.mark.usefixtures("waive_strict_authority")

from stock_gplm.m5 import (
    BacktestSettings,
    DataEligibilityRequest,
    DecisionStatus,
    HistoricalDataAdapter,
    RunIntegrityError,
    RunNotFound,
    UniverseAssessment,
    UniverseRequest,
    compare_runs,
    load_run,
    run_historical_backtest,
)
from stock_gplm.m5 import cli as m5_cli
from stock_gplm.m5 import issuance, provenance
from stock_gplm.m5.evidence import ReferenceEvidence, evidence_issued, load_price_basis_registration
from stock_gplm.m5.models import (
    ADAPTER_VERSION,
    CanonicalDailyBar,
    DataEligibilityDecision,
    VerifiedHistoricalDataset,
    bars_hash,
    canonical_hash,
    universe_problems,
)
from stock_gplm.m5.run_store import (
    INTEGRITY_VERIFIED,
    LEGACY_UNVERIFIED,
    REPLAY_VERIFIED,
    make_replayer,
    scan_runs,
)
from stock_gplm.m5.studio_api import HistoricalResearchService
from stock_gplm.v1.metrics import TERMINAL_VALUATION_POLICY, equal_weight_buy_and_hold, liquidation_unit_value, \
    performance_metrics
from stock_gplm.v1.portfolio import CostModel, InstrumentSignals, PortfolioSettings, simulate_portfolio
from stock_gplm.v1.registry import StrategyRegistry
from stock_gplm.v1.schema import validate_strategy
from stock_gplm.v1.studio import LoopbackHTTPServer, StudioHandler, is_loopback_address, make_server, serve

STRICT, DIAG = "STRICT_RESEARCH", "DIAGNOSTIC_ONLY"
PAIR = ("ALPHA-EQ", "BETA-EQ")


def universe(symbols=PAIR, mode=STRICT, **kw) -> UniverseRequest:
    return UniverseRequest(tuple(symbols), "2024-01-01", "2024-09-30", mode, **kw)


def codes(decision) -> set[str]:
    return {f.code for f in decision.failures}


def settings(**kw) -> BacktestSettings:
    return BacktestSettings.from_dict(settings_dict(**kw))


def forged_dataset(mode: str = STRICT) -> VerifiedHistoricalDataset:
    """Internally consistent fabricated bars and decision: every hash matches, nothing was issued."""
    bars = tuple(CanonicalDailyBar(f"2024-02-{d:02d}", str(100 + d * 50), str(150 + d * 50), "99",
                                   str(140 + d * 50), 1000, "2024-02-01T00:00:00Z", "forged.json.gz")
                 for d in range(1, 28))
    request = DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", mode)
    status = DecisionStatus.ELIGIBLE if mode == STRICT else DecisionStatus.DIAGNOSTIC_ALLOWED
    decision = DataEligibilityDecision(ADAPTER_VERSION, request, status, (), (), (), (), "2024-10-01", len(bars),
                                       bars[0].date, bars[-1].date, bars_hash(bars),
                                       price_basis="RAW_UNADJUSTED_VERIFIED")
    return VerifiedHistoricalDataset(decision, bars, "RAW_UNADJUSTED_VERIFIED")


# ======================================================================= A: issuance and trust

def test_A_forged_eligible_decision_never_produces_a_strict_run(tmp_path: Path) -> None:
    dataset = forged_dataset()
    assert dataset.decision.status == DecisionStatus.ELIGIBLE  # the hash is self-consistent...
    assert not dataset.decision.adapter_issued and not dataset.decision.investable  # ...but proves nothing
    assert dataset.decision.to_dict()["investable"] is False
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    with pytest.raises(ValueError, match="issued by HistoricalDataAdapter"):
        run_historical_backtest(version, [dataset], settings(evaluation_start="2024-02-01",
                                                             evaluation_end="2024-02-27"), tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_A_copied_seal_does_not_transfer_to_other_content(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    genuine, _ = HistoricalDataAdapter(raw, evidence).assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01",
                                                                                   "2024-09-30", STRICT))
    assert genuine.investable
    forged = forged_dataset().decision
    object.__setattr__(forged, "issuance_seal", genuine.issuance_seal)
    assert not forged.adapter_issued and not forged.investable


def test_A_tampered_reused_and_mismatched_decisions_are_rejected(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path / "w1")
    adapter = HistoricalDataAdapter(raw, evidence)
    alpha = adapter.assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT))[1]
    beta = adapter.assess(DataEligibilityRequest("BETA-EQ", "2024-01-01", "2024-09-30", STRICT))[1]
    # Tampered after issuance: any field change breaks the content identifier.
    tampered = adapter.assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT))[0]
    object.__setattr__(tampered, "price_basis", "PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED")
    with pytest.raises(ValueError, match="does not match its content"):
        tampered.verify()
    # Reused with other bars: the decision binds its bars hash.
    with pytest.raises(ValueError, match="bars differ"):
        VerifiedHistoricalDataset(alpha.decision, beta.bars, alpha.basis)
    # Reused with another price basis.
    with pytest.raises(ValueError, match="price basis"):
        VerifiedHistoricalDataset(alpha.decision, alpha.bars, "PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED")
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    # Reused for a different period: evaluation outside the assessed range.
    with pytest.raises(ValueError, match="outside the assessed range"):
        run_historical_backtest(version, [alpha], settings(evaluation_end="2024-10-15"), tmp_path / "o1")
    # Mismatched evidence: genuine decisions issued over two different evidence sets.
    raw2, evidence2 = build_world(tmp_path / "w2", symbols=("BETA-EQ", "ALPHA-EQ"))  # other tokens and manifests
    assert evidence2.source_hashes != evidence.source_hashes
    beta2 = HistoricalDataAdapter(raw2, evidence2).assess(DataEligibilityRequest("BETA-EQ", "2024-01-01",
                                                                                 "2024-09-30", STRICT))[1]
    assert beta2.decision.investable
    with pytest.raises(ValueError, match="one evidence set"):
        run_historical_backtest(version, [alpha, beta2], settings(), tmp_path / "o2")


def test_A_decisions_from_another_process_are_never_issued(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    decision, _ = HistoricalDataAdapter(raw, evidence).assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01",
                                                                                    "2024-09-30", STRICT))
    assert decision.investable
    probe = "import pickle,sys; d=pickle.loads(sys.stdin.buffer.read()); print(d.adapter_issued, d.investable)"
    output = subprocess.run([sys.executable, "-c", probe], input=pickle.dumps(decision), capture_output=True,
                            check=True, env={"PYTHONPATH": os.pathsep.join(sys.path)}).stdout.decode().strip()
    assert output == "False False"


def test_A_modified_or_hand_built_evidence_cannot_grant_strict(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path, provider_basis=None)
    assert evidence_issued(evidence)
    request = DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT)
    assert "PROVIDER_PRICE_BASIS_UNVERIFIED" in codes(HistoricalDataAdapter(raw, evidence).assess(request)[0])
    # A caller claiming a raw basis, or hand-building evidence, is never strictly eligible.
    _, full = build_world(tmp_path / "full")
    for forged in (replace(evidence, provider_price_basis="RAW_UNADJUSTED"),
                   replace(evidence, provider_price_basis="RAW_UNADJUSTED",
                           price_basis_registration=full.price_basis_registration),
                   ReferenceEvidence(**{f: getattr(full, f) for f in full.__dataclass_fields__
                                        if f != "issuance_seal"})):
        assert not evidence_issued(forged)
        decision, _ = HistoricalDataAdapter(raw, forged).assess(request)
        assert decision.status != DecisionStatus.ELIGIBLE and "EVIDENCE_NOT_ISSUED" in codes(decision)
    # In-place mutation of loaded evidence voids the seal too.
    raw3, evidence3 = build_world(tmp_path / "mut")
    evidence3.master_tokens["ALPHA-EQ"] = "999999"
    assert "EVIDENCE_NOT_ISSUED" in codes(HistoricalDataAdapter(raw3, evidence3).assess(request)[0])


def test_A_caller_supplied_events_and_datasets_are_diagnostic_only(tmp_path: Path) -> None:
    from stock_gplm.m39b import CorporateActionEvent, SourceEvidence
    action = [{"action_type": "SPLIT", "effective_date": "2024-05-02", "adjustment_status": "UNADJUSTED",
               "details": {"ratio": "2:1"}}]
    raw, evidence = build_world(tmp_path, ca_actions=action)
    record = event_record("2024-05-02", "2024-04-15T00:00:00Z")
    event = CorporateActionEvent(**(record | {"source": SourceEvidence(**record["source"])}))
    adapter = HistoricalDataAdapter(raw, evidence)
    strict, _ = adapter.assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT), [event])
    assert strict.status == DecisionStatus.BLOCKED and "CORPORATE_ACTION_EVENTS_CALLER_SUPPLIED" in codes(strict)
    _, diagnostic = adapter.assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", DIAG), [event])
    assert diagnostic is not None and diagnostic.share_events
    # Caller-supplied research remains possible, explicitly non-investable.
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    run = run_historical_backtest(version, [forged_dataset(DIAG)], settings(
        evaluation_start="2024-02-01", evaluation_end="2024-02-27"), tmp_path / "out")
    assert run["eligibility_issuance"] == "CALLER_SUPPLIED_UNVERIFIED"
    assert run["classification"] == "DIAGNOSTIC_ONLY_NON_INVESTABLE" and not run["investable"]


def test_A_price_basis_registration_is_hash_bound_and_scoped(tmp_path: Path) -> None:
    value = price_basis_registration(("ALPHA-EQ",))
    path = tmp_path / "basis.json"
    path.write_text(json.dumps(value | {"basis": "MIXED"}))
    with pytest.raises(ValueError, match="identifier"):
        load_price_basis_registration(path)
    raw, evidence = build_world(tmp_path / "w")
    narrowed = tmp_path / "w" / "evidence" / "price-basis.json"
    narrowed.write_text(json.dumps(price_basis_registration(("BETA-EQ",))))
    from stock_gplm.m5.evidence import load_reference_evidence
    reloaded = load_reference_evidence(tmp_path / "w" / "evidence" / "evidence-paths.json")
    decision, _ = HistoricalDataAdapter(raw, reloaded).assess(DataEligibilityRequest("ALPHA-EQ", "2024-01-01",
                                                                                    "2024-09-30", STRICT))
    assert "PROVIDER_PRICE_BASIS_UNVERIFIED" in codes(decision)


def test_A_issuance_is_not_a_public_api() -> None:
    import stock_gplm.m5 as m5
    assert not any("seal" in name for name in m5.__all__)
    assert [name for name in vars(issuance) if not name.startswith("_") and callable(vars(issuance)[name])
            and vars(issuance)[name].__module__ == issuance.__name__] == ["verify_seal"]


def test_A_no_importable_signer_can_seal_a_forged_decision(tmp_path: Path) -> None:
    """M5.1.1: the M5.1 audit sealed a forged decision via ``from issuance import _seal``."""
    with pytest.raises(ImportError):
        from stock_gplm.m5.issuance import _seal  # noqa: F401
    for kind in sorted(issuance.SEAL_KINDS):
        with pytest.raises(RuntimeError, match="already claimed"):
            issuance._claim_signer(kind)
        with pytest.raises(RuntimeError, match="already claimed"):
            issuance._issues(kind)
    # No signer-like attribute anywhere in the package validates a forged decision.
    import importlib
    import pkgutil

    import stock_gplm.m5 as m5
    forged = forged_dataset().decision
    probed = 0
    for info in pkgutil.iter_modules(m5.__path__):
        module = importlib.import_module(f"stock_gplm.m5.{info.name}")
        for name, value in vars(module).items():
            if not callable(value) or isinstance(value, type) or not any(
                    word in name.lower() for word in ("seal", "sign", "issu")) or name == "verify_seal":
                continue
            probed += 1
            for args in ((forged.decision_id,), ("decision", forged.decision_id)):
                try:
                    candidate = value(*args)
                except Exception:  # noqa: BLE001 - probing signer-like callables
                    continue
                assert not issuance.verify_seal("decision", forged.decision_id, candidate), f"{info.name}.{name}"
    assert probed >= 2  # at least _claim_signer and _issues were exercised
    assert not forged.investable


def test_A_issuing_functions_reject_a_caller_supplied_signer(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    adapter = HistoricalDataAdapter(raw, evidence)
    request = DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT)
    with pytest.raises(TypeError, match="cannot be supplied"):
        adapter.assess(request, _sign=lambda digest: "x")
    from stock_gplm.m5.evidence import load_reference_evidence
    with pytest.raises(TypeError, match="cannot be supplied"):
        load_reference_evidence(tmp_path / "w" / "evidence" / "evidence-paths.json", _sign=lambda digest: "x")
    decision, _ = adapter.assess(request)
    assert decision.adapter_issued  # the legitimate path still issues


def test_A_hand_built_universe_assessment_is_never_issued(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    genuine = HistoricalDataAdapter(raw, evidence).assess_universe(universe())
    assert genuine.investable
    copy = UniverseAssessment(genuine.universe, genuine.decisions, genuine.status, genuine.released,
                              genuine.excluded)
    assert not copy.adapter_issued and not copy.investable
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    with pytest.raises(ValueError, match="not issued"):
        run_historical_backtest(version, copy, settings(), tmp_path / "out")


# ======================================================================= B: requested universe

def test_B_strict_universe_with_absent_member_fails_closed(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    assessment = HistoricalDataAdapter(raw, evidence).assess_universe(universe(("ALPHA-EQ", "ABSENT-EQ")))
    assert assessment.status == DecisionStatus.REJECTED and assessment.released == ()
    assert [e["symbol"] for e in assessment.excluded] == ["ABSENT-EQ"]
    assert "SYMBOL_NOT_FOUND" in assessment.excluded[0]["failures"]
    alpha = next(d for d in assessment.decisions if d.request.symbol == "ALPHA-EQ")
    assert alpha.status == DecisionStatus.ELIGIBLE  # per-symbol reasons are kept, but nothing runs
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    with pytest.raises(ValueError, match="no data released"):
        run_historical_backtest(version, assessment, settings(), tmp_path / "out")


@pytest.mark.parametrize("symbols, reason", [
    (["ALPHA-EQ", "ALPHA-EQ"], "DUPLICATE_OF_INDEX_0"),
    (["ALPHA-EQ", "alpha-eq"], "SYMBOL_NOT_CANONICAL_CASE"),
    (["ALPHA-EQ", " BETA-EQ"], "SYMBOL_HAS_SURROUNDING_WHITESPACE"),
    (["ALPHA-EQ", ""], "SYMBOL_INVALID_FORMAT"),
    (["ALPHA-EQ", 7], "SYMBOL_NOT_A_STRING"),
])
def test_B_duplicate_and_ambiguous_symbols_are_rejected_explicitly(symbols, reason) -> None:
    problems = universe_problems(symbols)
    assert [p["index"] for p in problems] == [1] and problems[0]["reason"] == reason
    with pytest.raises(ValueError, match="universe is invalid"):
        UniverseRequest(tuple(symbols), "2024-01-01", "2024-09-30", STRICT)


def test_B_diagnostic_subset_requires_explicit_opt_in(tmp_path: Path) -> None:
    raw, evidence = build_world(tmp_path)
    adapter = HistoricalDataAdapter(raw, evidence)
    implicit = adapter.assess_universe(universe(("ALPHA-EQ", "ABSENT-EQ"), DIAG))
    assert implicit.status == DecisionStatus.REJECTED and not implicit.released
    explicit = adapter.assess_universe(universe(("ALPHA-EQ", "ABSENT-EQ"), DIAG, diagnostic_subset=True))
    assert explicit.status == DecisionStatus.DIAGNOSTIC_ALLOWED
    assert [d.decision.request.symbol for d in explicit.released] == ["ALPHA-EQ"]
    with pytest.raises(ValueError, match="only allowed in DIAGNOSTIC_ONLY"):
        universe(("ALPHA-EQ",), STRICT, diagnostic_subset=True)
    version = StrategyRegistry(tmp_path / "r.sqlite3").save(strategy_config())
    run = run_historical_backtest(version, explicit, settings(), tmp_path / "out")
    assert not run["investable"] and run["requested_universe"]["excluded"][0]["symbol"] == "ABSENT-EQ"
    assert run["requested_universe"]["universe"]["symbols"] == ["ABSENT-EQ", "ALPHA-EQ"]


def _service(tmp_path: Path, **world) -> tuple[HistoricalResearchService, StrategyRegistry, object]:
    raw, evidence = build_world(tmp_path, **world)
    registry = StrategyRegistry(tmp_path / "studio.sqlite3")
    return HistoricalResearchService(raw, evidence, tmp_path / "reports", registry), registry, registry.save(
        strategy_config())


def _body(version, symbols, mode=STRICT, **extra) -> dict:
    return {"strategy_id": version.strategy_id, "version": version.version, "symbols": symbols,
            "start": "2024-01-01", "end": "2024-09-30", "mode": mode, "settings": settings_dict(), **extra}


def test_B_studio_strict_partial_universe_never_runs(tmp_path: Path) -> None:
    service, _, version = _service(tmp_path)
    status, body = service.backtest(_body(version, ["ALPHA-EQ", "ABSENT-EQ"]))
    assert status == 422 and "no member was run" in body["error"]
    assert {d["symbol"]: d["status"] for d in body["decisions"]} == {"ABSENT-EQ": "REJECTED",
                                                                      "ALPHA-EQ": "ELIGIBLE"}
    assert scan_runs(tmp_path / "reports") == []
    status, body = service.backtest(_body(version, ["ALPHA-EQ", "ALPHA-EQ"]))
    assert status == 400 and body["problems"][0]["reason"] == "DUPLICATE_OF_INDEX_0"
    status, body = service.backtest(_body(version, ["ALPHA-EQ", "ABSENT-EQ"], DIAG))
    assert status == 422
    status, body = service.backtest(_body(version, ["ALPHA-EQ", "ABSENT-EQ"], DIAG, diagnostic_subset=True))
    assert status == 201 and not body["investable"] and body["excluded"][0]["symbol"] == "ABSENT-EQ"
    status, body = service.backtest(_body(version, ["ALPHA-EQ", "BETA-EQ"]))
    assert status == 201 and body["investable"] and body["universe_status"] == "ELIGIBLE"


def test_B_cli_strict_partial_universe_never_runs(tmp_path: Path, capsys) -> None:
    raw, _ = build_world(tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps(settings_dict()))
    (tmp_path / "strategy.json").write_text(json.dumps(strategy_config()))
    base = ["--raw-root", str(raw), "backtest", "--evidence", str(tmp_path / "evidence" / "evidence-paths.json"),
            "--mode", STRICT, "--start", "2024-01-01", "--end", "2024-09-30", "--settings",
            str(tmp_path / "settings.json"), "--registry", str(tmp_path / "r.sqlite3"),
            "--strategy-file", str(tmp_path / "strategy.json"), "--output-root", str(tmp_path / "out")]
    assert m5_cli.main(base + ["--symbols", "ALPHA-EQ", "ABSENT-EQ"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "REJECTED"
    assert m5_cli.main(base + ["--symbols", "ALPHA-EQ", "ALPHA-EQ"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "INVALID_UNIVERSE"
    assert not (tmp_path / "out").exists()


# ======================================================================= C: corporate actions

SPLIT = {"action_type": "SPLIT", "effective_date": "2024-05-02", "adjustment_status": "UNADJUSTED",
         "details": {"ratio": "2:1"}}
EVENT = event_record("2024-05-02", "2024-04-15T00:00:00Z")


@pytest.mark.parametrize("actions, events, code, status", [
    ([SPLIT | {"adjustment_status": "UNKNOWN"}], [EVENT], "CORPORATE_ACTION_STATUS_UNRESOLVED", "BLOCKED"),
    ([SPLIT | {"adjustment_status": "POLICY_REQUIRED"}], [EVENT], "CORPORATE_ACTION_STATUS_UNRESOLVED", "BLOCKED"),
    ([SPLIT | {"adjustment_status": "ADJUSTED"}], [EVENT], "CORPORATE_ACTION_CONTRADICTION", "REJECTED"),
    ([SPLIT | {"details": {"ratio": "3:1"}}], [EVENT], "CORPORATE_ACTION_CONTRADICTION", "REJECTED"),
    ([SPLIT | {"details": {}}], [EVENT], "CORPORATE_ACTION_FIELDS_MISSING", "REJECTED"),
    ([SPLIT | {"details": {"ratio": "two:one"}}], [EVENT], "CORPORATE_ACTION_FIELDS_MISSING", "REJECTED"),
    ([SPLIT | {"details": {"ratio": "2:1", "factor": "0.5"}}], [EVENT], "CORPORATE_ACTION_DETAILS_UNSUPPORTED",
     "REJECTED"),
    ([SPLIT | {"details": {"ratio": "2:1", "record_date": "2024-05-03"}}], [EVENT | {"record_date": "2024-05-02"}],
     "CORPORATE_ACTION_CONTRADICTION", "REJECTED"),
    ([SPLIT | {"details": {"ratio": "2:1", "record_date": "2024-05-03"}}], [EVENT],
     "CORPORATE_ACTION_FIELDS_MISSING", "REJECTED"),
    ([SPLIT | {"details": {"ratio": "2:1", "event_id": "other"}}], [EVENT], "CORPORATE_ACTION_CONTRADICTION",
     "REJECTED"),
    ([SPLIT, SPLIT | {"action_type": "STOCK_SPLIT"}], [EVENT], "CORPORATE_ACTION_AMBIGUOUS", "REJECTED"),
    ([SPLIT], [EVENT, EVENT | {"event_id": "split-dup"}], "CORPORATE_ACTION_AMBIGUOUS", "REJECTED"),
    ([], [EVENT], "CORPORATE_ACTION_EVENT_UNMATCHED", "REJECTED"),
    ([SPLIT], [EVENT | {"ex_date": "2024-05-03"}], "CORPORATE_ACTION_EVENT_UNMATCHED", "REJECTED"),
    ([SPLIT | {"action_type": "BONUS", "details": {"ratio": "2:1"}}], [EVENT], "CORPORATE_ACTION_EVENT_UNRESOLVED",
     "REJECTED"),
])
def test_C_contradictory_or_unresolved_actions_never_become_eligible(tmp_path, actions, events, code,
                                                                    status) -> None:
    raw, evidence = build_world(tmp_path, ca_actions=actions, events=events)
    decision, dataset = HistoricalDataAdapter(raw, evidence).assess(
        DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT))
    assert code in codes(decision) and decision.status.value == status and not decision.investable
    assert decision.corporate_action_reconciliation  # outcomes are preserved, not just the verdict
    if status == "REJECTED":  # contradictions reject in every mode; diagnostics are not a bypass
        diagnostic, released = HistoricalDataAdapter(raw, evidence).assess(
            DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", DIAG))
        assert diagnostic.status == DecisionStatus.REJECTED and released is None


@pytest.mark.parametrize("action, event", [
    (SPLIT | {"details": {"ratio": "4:2"}}, EVENT),  # documented rule: ratios compare as reduced fractions
    (SPLIT | {"action_type": "CONSOLIDATION", "details": {"ratio": "1:10"}},
     event_record("2024-05-02", "2024-04-15T00:00:00Z", kind="REVERSE_SPLIT", new=1, base=10)),
    (SPLIT | {"details": {"ratio": "2:1", "record_date": "2024-05-03", "event_id": EVENT["event_id"]}},
     EVENT | {"record_date": "2024-05-03"}),
])
def test_C_documented_normalizations_reconcile(tmp_path: Path, action, event) -> None:
    raw, evidence = build_world(tmp_path, ca_actions=[action], events=[event])
    decision, dataset = HistoricalDataAdapter(raw, evidence).assess(
        DataEligibilityRequest("ALPHA-EQ", "2024-01-01", "2024-09-30", STRICT))
    assert decision.status == DecisionStatus.ELIGIBLE, codes(decision)
    assert decision.corporate_action_reconciliation[0]["outcome"] == "APPLIED"
    assert dataset.decision.share_events == dataset.share_events


# ======================================================================= D: verified run loader

def _strict_run(tmp_path: Path, world=None, out: str = "out", **config) -> tuple[dict, Path, tuple]:
    raw, evidence = world or build_world(tmp_path / "world")
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    validated = validate_strategy(strategy_config(**config))
    version = registry.find(validated.strategy_id, validated.config_hash) or registry.save(validated)
    assessment = HistoricalDataAdapter(raw, evidence).assess_universe(universe())
    run = run_historical_backtest(version, assessment, settings(), tmp_path / out)
    return run, tmp_path / out / run["run_id"], (raw, evidence, registry)


def _rehash(directory: Path) -> None:
    manifest = json.loads((directory / "run-manifest.json").read_text())
    manifest["file_sha256"] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                               for name in manifest["file_sha256"]}
    (directory / "run-manifest.json").write_text(json.dumps(manifest))


def _fabricate(source: Path, root: Path, total_return: float, *, manifest: bool) -> Path:
    run = json.loads((source / "run.json").read_text())
    run["performance"]["metrics"]["total_return"] = total_return
    target = root / ("f" * 64)
    shutil.copytree(source, target)
    (target / "run.json").write_text(json.dumps(run))
    if not manifest:
        (target / "run-manifest.json").unlink()
    else:
        _rehash(target)
    return target


def test_D_fabricated_9900_percent_run_is_never_ranked(tmp_path: Path) -> None:
    run, directory, (raw, evidence, registry) = _strict_run(tmp_path)
    other, other_dir, _ = _strict_run(tmp_path, world=(raw, evidence), risk={
        "stop_loss": {"type": "percent", "value": 3}, "take_profit": {"type": "percent", "value": 5}})
    replay = make_replayer(HistoricalDataAdapter(raw, evidence), registry)
    genuine = compare_runs([directory, other_dir], replay=replay)
    assert genuine["ranking_by_total_return"] is not None
    assert {r["verification"] for r in genuine["rows"]} == {REPLAY_VERIFIED}
    for manifest in (False, True):
        fake = _fabricate(directory, tmp_path / f"fake-{manifest}", 99.0, manifest=manifest)
        with pytest.raises(RunIntegrityError):
            compare_runs([directory, fake], replay=replay)
        with pytest.raises(RunIntegrityError):
            compare_runs([directory, fake])


def test_D_self_consistent_artifacts_from_other_data_fail_replay(tmp_path: Path) -> None:
    run, directory, (raw, evidence, registry) = _strict_run(tmp_path)
    assert load_run(directory).verification == INTEGRITY_VERIFIED
    other_raw, other_evidence = build_world(tmp_path / "other", symbols=PAIR)
    # Same symbols and evidence shape, different candles: every artifact is internally consistent,
    # but replaying through the adapter on this raw data cannot reproduce it.
    (other_raw / "ALPHA-EQ").rename(tmp_path / "moved")
    shutil.copytree(raw / "BETA-EQ", other_raw / "ALPHA-EQ")
    for meta in (other_raw / "ALPHA-EQ").glob("*.meta.json"):
        meta.write_text(meta.read_text().replace("BETA-EQ", "ALPHA-EQ"))
    replay = make_replayer(HistoricalDataAdapter(other_raw, other_evidence), registry)
    with pytest.raises(RunIntegrityError, match="replay"):
        replay(load_run(directory))


@pytest.mark.parametrize("attack", ["missing", "extra", "symlink", "substituted", "manifest_id", "report",
                                    "environment", "renamed", "performance_rehashed", "classification"])
def test_D_corrupted_or_inconsistent_artifacts_are_integrity_failures(tmp_path: Path, attack: str) -> None:
    run, directory, _ = _strict_run(tmp_path)
    second, second_dir, _ = _strict_run(tmp_path, world=build_world(tmp_path / "w2"), out="out2", position_sizing={
        "type": "fixed_fraction", "value": 0.2})
    if attack == "missing":
        (directory / "trades.json").unlink()
    elif attack == "extra":
        (directory / "notes.txt").write_text("x")
    elif attack == "symlink":
        (directory / "orders.json").unlink()
        (directory / "orders.json").symlink_to(second_dir / "orders.json")
    elif attack == "substituted":
        shutil.copy(second_dir / "run.json", directory / "run.json")
    elif attack == "manifest_id":
        manifest = json.loads((directory / "run-manifest.json").read_text())
        (directory / "run-manifest.json").write_text(json.dumps(manifest | {"run_id": "0" * 64}))
    elif attack == "report":
        (directory / "report.md").write_text("# Excellent results\n")
        _rehash(directory)
    elif attack == "environment":
        shutil.copy(second_dir / "environment.json", directory / "environment.json")
        _rehash(directory)
    elif attack == "renamed":
        directory = directory.rename(directory.parent / ("e" * 64))
    elif attack == "performance_rehashed":
        stored = json.loads((directory / "run.json").read_text())
        stored["performance"]["metrics"]["total_return"] = 99.0
        (directory / "run.json").write_text(json.dumps(stored))
        _rehash(directory)
    elif attack == "classification":
        stored = json.loads((directory / "run.json").read_text())
        stored["eligibility_issuance"] = "CALLER_SUPPLIED_UNVERIFIED"
        (directory / "run.json").write_text(json.dumps(stored))
        _rehash(directory)
    with pytest.raises(RunIntegrityError):
        load_run(directory)
    assert [r["verification"] for r in scan_runs(directory.parent)] == ["INTEGRITY_FAILED"]


def test_D_missing_run_is_not_an_integrity_failure_and_legacy_runs_are_never_ranked(tmp_path: Path) -> None:
    with pytest.raises(RunNotFound):
        load_run(tmp_path / ("a" * 64))
    run, directory, _ = _strict_run(tmp_path)
    legacy = tmp_path / "legacy" / ("b" * 64)
    legacy.mkdir(parents=True)
    hashes = {}
    for name in ("run.json", "signals.json", "orders.json", "trades.json", "equity-curve.json", "report.md"):
        payload = (directory / name).read_bytes()
        if name == "run.json":
            payload = json.dumps(json.loads(payload) | {"run_id": "b" * 64}).encode()
        (legacy / name).write_bytes(payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()
    (legacy / "run-manifest.json").write_text(json.dumps({
        "contract": "stock-gplm.m5-historical-backtest-run.v1", "run_id": "b" * 64, "file_sha256": hashes,
        "created_at_utc": "2026-11-14T00:00:00Z", "non_analytical_fields": ["created_at_utc"]}))
    assert load_run(legacy).verification == LEGACY_UNVERIFIED
    comparison = compare_runs([directory, legacy])
    assert comparison["ranking_by_total_return"] is None
    assert any("legacy" in reason for reason in comparison["reasons"])


def test_D_studio_endpoints_use_the_verified_loader(tmp_path: Path) -> None:
    service, registry, version = _service(tmp_path)
    status, body = service.backtest(_body(version, list(PAIR)))
    assert status == 201
    genuine = tmp_path / "reports" / body["run_id"]
    fake = _fabricate(genuine, tmp_path / "reports", 99.0, manifest=True)
    rows = {r["run_id"]: r["verification"] for r in service.runs()["runs"]}
    assert rows == {body["run_id"]: INTEGRITY_VERIFIED, fake.name: "INTEGRITY_FAILED"}
    with pytest.raises(RunIntegrityError):
        service.report(fake.name)
    with pytest.raises(RunIntegrityError):
        service.compare({"run_ids": [body["run_id"], fake.name]})
    assert service.report(body["run_id"])["verification"] == INTEGRITY_VERIFIED
    with pytest.raises(KeyError):
        service.report("c" * 64)


def test_D_cli_export_refuses_fabricated_runs(tmp_path: Path, capsys) -> None:
    run, directory, (raw, _, _) = _strict_run(tmp_path)
    fake = _fabricate(directory, tmp_path / "fakes", 99.0, manifest=True)
    assert m5_cli.main(["--raw-root", str(raw), "export", str(fake), "--output", str(tmp_path / "x.json")]) == 3
    assert not (tmp_path / "x.json").exists()
    assert m5_cli.main(["--raw-root", str(raw), "export", str(directory), "--output", str(tmp_path / "y.json")]) == 0
    assert json.loads((tmp_path / "y.json").read_text())["verification"] == INTEGRITY_VERIFIED
    capsys.readouterr()


# ======================================================================= E: provenance

def test_E_content_identity_covers_every_component_and_is_portable(tmp_path: Path, monkeypatch) -> None:
    identity = provenance.content_identity()
    assert set(identity["components"]) == {"m1_publication_validation", "m2_reference_importers",
                                           "m39b_corporate_actions", "v1_strategy_execution",
                                           "m5_adapter_runner_eligibility", "package_init"}
    assert identity == provenance.content_identity()
    encoded = json.dumps(identity)
    assert str(provenance.PACKAGE) not in encoded and str(Path.home()) not in encoded
    copy = tmp_path / "stock_gplm"
    shutil.copytree(provenance.PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(provenance, "PACKAGE", copy)
    relocated = provenance.content_identity()
    assert relocated == identity  # same bytes elsewhere on disk -> same identity
    engine = copy / "m39b" / "engine.py"
    engine.write_bytes(engine.read_bytes() + b"\n# changed\n")
    changed = provenance.content_identity()
    assert changed["components"]["m39b_corporate_actions"] != identity["components"]["m39b_corporate_actions"]
    assert changed["components"]["v1_strategy_execution"] == identity["components"]["v1_strategy_execution"]
    assert changed["source_sha256"] != identity["source_sha256"]


def test_E_run_identity_excludes_environment_but_records_it(tmp_path: Path) -> None:
    run, directory, _ = _strict_run(tmp_path)
    assert run["code"] == provenance.content_identity()
    assert "git_commit" not in json.dumps(run["code"])
    environment = json.loads((directory / "environment.json").read_text())
    assert environment["part_of_run_identity"] is False
    assert {"git_commit", "python_version", "lockfile_sha256", "loaded_third_party_versions"} <= set(
        environment["environment"])
    assert run["evidence_versions"][0]["sources"]["provider_price_basis"]
    assert run["requested_universe_sha256"] == universe().universe_sha256


def test_E_execution_path_imports_only_the_standard_library() -> None:
    probe = ("import sys, stock_gplm.m5, stock_gplm.m5.cli, stock_gplm.v1.studio;"
             "print(sorted({m.split('.')[0] for m in sys.modules} - set(sys.stdlib_module_names) - {'stock_gplm', '__main__'}))")
    output = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r});"
                             + probe], capture_output=True, text=True, check=True).stdout.strip()
    assert output == "[]"


# ======================================================================= F: Studio loopback

@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "192.0.2.10", "example.com", "127.0.0.1 ", None])
def test_F_programmatic_non_loopback_bind_is_refused(tmp_path: Path, host) -> None:
    with pytest.raises(ValueError, match="binds only"):
        make_server(tmp_path / "r.sqlite3", host, 0)
    with pytest.raises(ValueError, match="binds only"):
        serve(tmp_path / "r.sqlite3", host=host, port=0)
    assert not (tmp_path / "r.sqlite3").exists()  # refused before anything was opened


def test_F_loopback_bind_works(tmp_path: Path) -> None:
    for host in ("127.0.0.1", "localhost"):
        server = make_server(tmp_path / "r.sqlite3", host, 0)
        try:
            assert server.server_address[0] == "127.0.0.1" and isinstance(server, LoopbackHTTPServer)
        finally:
            server.server_close()


def test_F_peer_address_is_verified_not_headers(tmp_path: Path) -> None:
    server = LoopbackHTTPServer(("127.0.0.1", 0), StudioHandler)
    try:
        assert not server.verify_request(None, ("203.0.113.9", 4000))
        assert server.verify_request(None, ("127.0.0.1", 4000))
        port = server.server_address[1]

        class Headers(dict):
            def get(self, key, default=None):  # noqa: D401
                return super().get(key, default)

        def handler(peer, **headers):
            item = StudioHandler.__new__(StudioHandler)
            item.client_address, item.server, item.headers = peer, server, Headers(headers)
            return item

        spoofed = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
        assert not handler(("203.0.113.9", 4000), **spoofed)._trusted_origin()
        assert not handler(("10.0.0.5", 4000), Host=f"localhost:{port}")._trusted_origin()
        assert handler(("127.0.0.1", 4000), **spoofed)._trusted_origin()
        assert handler(("127.0.0.1", 4000), Host=f"127.0.0.1:{port}")._trusted_origin()
        assert not handler(("127.0.0.1", 4000), Host=f"127.0.0.1:{port}",
                           **{"Sec-Fetch-Site": "cross-site"})._trusted_origin()
        assert not handler(("127.0.0.1", 4000), Host=f"127.0.0.1:{port}", Origin="null")._trusted_origin()
        assert not handler(("127.0.0.1", 4000), Host=f"evil.test:{port}")._trusted_origin()
    finally:
        server.server_close()
    assert is_loopback_address("::ffff:127.0.0.1") and is_loopback_address("::1")
    assert not is_loopback_address("localhost") and not is_loopback_address("0.0.0.0")


def test_F_live_server_rejects_spoofed_cross_site_requests(tmp_path: Path) -> None:
    server = make_server(tmp_path / "r.sqlite3", "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        for headers in ({"Host": f"127.0.0.1:{port}", "Origin": "http://evil.test"},
                        {"Host": f"127.0.0.1:{port}", "Sec-Fetch-Site": "cross-site"},
                        {"Host": "evil.test"}):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/api/strategies", headers=headers)
            assert connection.getresponse().status == 403
            connection.close()
    finally:
        server.shutdown()
        server.server_close()


# ======================================================================= G: terminal valuation

def _hold_to_end(costs: CostModel):
    days = [f"2024-01-{d:02d}" for d in range(1, 7)]
    rows = tuple({"date": d, "open": 100.0, "high": 121.0, "low": 99.0, "close": 100.0 + 4 * i, "volume": 1.0}
                 for i, d in enumerate(days))
    instrument = InstrumentSignals("ALPHA-EQ", rows, (True,) + (False,) * 5, (False,) * 6)
    config = validate_strategy(strategy_config(position_sizing={"type": "fixed_fraction", "value": 1.0},
                                               risk={"stop_loss": {"type": "percent", "value": 50},
                                                     "take_profit": {"type": "percent", "value": 100}},
                                               exit={"condition": {"operator": "LT", "left": {"field": "close"},
                                                                   "right": {"constant": 1}},
                                                     "max_holding_sessions": 100})).config
    portfolio = PortfolioSettings(10_000.0, 1, 1.0, 1.0, days[0], days[-1])
    simulation = simulate_portfolio(config, [instrument], days, portfolio, costs)
    return simulation, performance_metrics(simulation, 10_000.0, costs), equal_weight_buy_and_hold(
        [instrument], days, days[0], days[-1], 10_000.0, costs)


def test_G_open_position_at_boundary_uses_one_terminal_convention() -> None:
    costs = CostModel(commission_bps=0, slippage_bps=0, buy_statutory_bps=0, sell_statutory_bps=50)
    simulation, performance, benchmark = _hold_to_end(costs)
    metrics = performance["metrics"]
    position = simulation.open_positions[0]
    assert position["quantity"] == 100.0 and position["mark_price"] == 120.0
    # Both the strategy and the benchmark hold 100 units bought at 100 and still open at the boundary.
    expected = (simulation.equity_curve[-1]["cash"] + 100 * liquidation_unit_value(120.0, costs)) / 10_000 - 1
    assert metrics["total_return"] == pytest.approx(expected) == pytest.approx(benchmark["total_return"])
    assert metrics["total_return_marked_to_market"] == pytest.approx(benchmark["total_return_marked_to_market"])
    assert metrics["terminal_exit_cost_estimate"] == pytest.approx(100 * 120 * 0.005)
    assert metrics["total_return"] < metrics["total_return_marked_to_market"]
    assert performance["conventions"]["terminal_valuation"] == benchmark["terminal_valuation"] == \
        TERMINAL_VALUATION_POLICY
    # Reconciliation: liquidation equity = initial + realized + unrealized - estimated exit costs.
    assert metrics["liquidation_equity_estimate"] - 10_000 == pytest.approx(
        metrics["net_pnl_realized"] + metrics["unrealized_pnl"] - metrics["terminal_exit_cost_estimate"])


def test_G_closed_trades_are_not_charged_twice(tmp_path: Path) -> None:
    run, _, _ = _strict_run(tmp_path, risk={"stop_loss": {"type": "percent", "value": 1},
                                            "take_profit": {"type": "percent", "value": 1}})
    metrics = run["performance"]["metrics"]
    expected = sum(p["quantity"] * p["mark_price"] for p in run["open_positions_at_end"]) - sum(
        p["quantity"] * liquidation_unit_value(p["mark_price"], CostModel(**{k: run["cost_model"][k] for k in (
            "commission_bps", "slippage_bps", "buy_statutory_bps", "sell_statutory_bps")}))
        for p in run["open_positions_at_end"])
    assert metrics["terminal_exit_cost_estimate"] == pytest.approx(expected)
    assert run["terminal_valuation"] == run["benchmark"]["terminal_valuation"] == TERMINAL_VALUATION_POLICY


def test_G_zero_costs_make_both_bases_equal() -> None:
    _, performance, benchmark = _hold_to_end(CostModel(0, 0, 0, 0))
    assert performance["metrics"]["total_return"] == performance["metrics"]["total_return_marked_to_market"]
    assert benchmark["total_return"] == benchmark["total_return_marked_to_market"]


# ======================================================================= H: holdout wording

def test_H_holdout_access_distinguishes_decoded_released_and_scored(tmp_path: Path) -> None:
    run, directory, _ = _strict_run(tmp_path)
    access = run["holdout_access"]
    assert access["cutoff"] == run["holdout_cutoff"] == "2024-10-01"
    assert access["protected_rows_decoded_during_source_validation"] > 0
    assert access["protected_rows_released_to_execution"] == 0 and access["protected_rows_scored_or_trained"] == 0
    assert "locked_holdout_accessed" not in run
    assert "decoded during source validation" in (directory / "report.md").read_text()
    assert canonical_hash(run["holdout_access"])  # finite, serializable evidence
