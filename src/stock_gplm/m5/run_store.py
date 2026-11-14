"""The one verified loader for M5 run directories (M5.1).

Every consumer of a run reads it through :func:`load_run`. That covers comparison
and ranking, run listing and details, reports, Studio endpoints and exports.
Nothing else in M5 parses ``run.json``.

Verification levels:

``INTEGRITY_VERIFIED``
    The directory name is the run ID. The file inventory is exact and contains
    no symlinks. Every file matches the manifest hash. The manifest, ``run.json``
    and ``environment.json`` agree on the run ID. The run ID re-derives from the
    ``run.json`` binding fields. The classification agrees with the eligibility
    mode and issuance. The performance block recomputes exactly from the stored
    orders, trades, open positions and equity curve. ``report.md`` re-renders
    exactly from ``run.json``.
``REPLAY_VERIFIED``
    Additionally, re-running the run now, through the adapter on the current raw
    data and evidence and the registered strategy version, reproduces every
    analytical file byte for byte.
``LEGACY_UNVERIFIED``
    A pre-M5.1 (``...-run.v1``) run whose manifest hashes match. It lacks the
    binding needed for full verification. It can be listed and shown, labelled
    as legacy, but is never ranked.

Local attacker model. File hashes and internal recomputation catch accidental
corruption, partial edits and naive fabrication, such as a hand-written
``run.json`` claiming a 9,900% return. They do not authenticate. Someone who can
write the reports directory can also fabricate a self-consistent run, manifest
and equity curve together. Only replay closes that gap: the adapter re-derives
the inputs from raw data and evidence, and the result must match. Rankings
therefore require ``REPLAY_VERIFIED``. Someone who can also write the raw data,
evidence or code is outside what any local check can detect.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from stock_gplm.v1.metrics import comparable, performance_metrics
from stock_gplm.v1.portfolio import CostModel, SimulationResult

from .evidence import _reject_constant, _unique, read_json
from .models import EligibilityMode, UniverseRequest, canonical_hash
from .provenance import ENVIRONMENT_CONTRACT, LOCKFILES
from .runner import (
    ADAPTER_ISSUED,
    ANALYTICAL_FILES,
    DIAGNOSTIC_CLASSIFICATION,
    ENVIRONMENT_FILE,
    LEGACY_RUN_CONTRACT,
    MANIFEST_CONTRACT,
    PREVIOUS_MANIFEST_CONTRACT,
    PREVIOUS_RUN_CONTRACT,
    RUN_CONTRACT,
    STRICT_CLASSIFICATION,
    BacktestSettings,
    _encode,
    build_run,
    render_markdown,
)

INTEGRITY_VERIFIED = "INTEGRITY_VERIFIED"
REPLAY_VERIFIED = "REPLAY_VERIFIED"
LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"
MANIFEST_NAME = "run-manifest.json"
MAX_RUN_FILE_BYTES = 256 * 1024 * 1024
BINDING_KEYS = ("contract", "strategy_id", "strategy_version", "strategy_config_hash", "dataset_fingerprint",
                "requested_universe_sha256", "eligibility_issuance", "settings", "simulator_version", "code",
                "input_provenance_sha256")
PREVIOUS_BINDING_KEYS = BINDING_KEYS[:-1]
_RUN_ID = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = frozenset({"contract", "run_contract", "run_id", "file_sha256", "file_inventory", "created_at_utc",
                            "non_analytical_fields", "non_analytical_files"})


class RunNotFound(KeyError):
    """No run directory exists for the requested identifier (ordinary missing data)."""


class RunIntegrityError(ValueError):
    """A run directory exists but is incomplete, substituted, corrupted or inconsistent."""


@dataclass(frozen=True)
class VerifiedRun:
    run_id: str
    directory: Path
    run: dict[str, Any]
    manifest: dict[str, Any]
    markdown: str
    verification: str

    @property
    def rankable(self) -> bool:
        return (self.verification == REPLAY_VERIFIED and self.run.get("investable") is True
                and self.run.get("eligibility_mode") == EligibilityMode.STRICT_RESEARCH.value)

    def summary(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "verification": self.verification,
                "strategy_id": self.run.get("strategy_id"), "strategy_version": self.run.get("strategy_version"),
                "classification": self.run.get("classification")}


def _fail(directory: Path, message: str) -> RunIntegrityError:
    return RunIntegrityError(f"run {directory.name}: integrity failure: {message}")


def _normal(value: Any) -> Any:
    return json.loads(_encode(value))


def _read_files(directory: Path, expected: set[str], hashes: dict[str, Any]) -> dict[str, bytes]:
    entries = list(directory.iterdir())
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise _fail(directory, "run directories may contain only regular files")
    names = {entry.name for entry in entries}
    if names != expected | {MANIFEST_NAME}:
        raise _fail(directory, f"file inventory differs: missing {sorted(expected - names)}, "
                               f"unexpected {sorted(names - expected - {MANIFEST_NAME})}")
    if set(hashes) != expected:
        raise _fail(directory, "manifest does not hash exactly the required files")
    payloads: dict[str, bytes] = {}
    for name in sorted(expected):
        path = directory / name
        if path.stat().st_size > MAX_RUN_FILE_BYTES:
            raise _fail(directory, f"{name} exceeds {MAX_RUN_FILE_BYTES} bytes")
        payloads[name] = path.read_bytes()
        if hashlib.sha256(payloads[name]).hexdigest() != hashes[name]:
            raise _fail(directory, f"{name} does not match its manifest hash")
    return payloads


def _strict_json(directory: Path, name: str, raw: bytes) -> Any:
    try:
        return json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique)
    except ValueError as exc:
        raise _fail(directory, f"{name} is not strict JSON: {exc}") from exc


def load_run(directory: Path) -> VerifiedRun:
    """Load and verify one run directory; never returns unverified content as verified."""
    directory = Path(directory)
    if not directory.exists() and not directory.is_symlink():
        raise RunNotFound(f"unknown run {directory.name}")
    if directory.is_symlink() or not directory.is_dir():
        raise _fail(directory, "run path is not a regular directory")
    if not _RUN_ID.fullmatch(directory.name):
        raise _fail(directory, "directory name is not a run identifier")
    manifest_path = directory / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise _fail(directory, "run manifest is missing; the run is incomplete or substituted")
    try:
        manifest, _ = read_json(manifest_path)
    except ValueError as exc:
        raise _fail(directory, f"run manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("file_sha256"), dict):
        raise _fail(directory, "run manifest has an unsupported shape")
    if manifest.get("run_id") != directory.name:
        raise _fail(directory, "manifest run_id does not match the directory")

    if manifest.get("contract") == LEGACY_RUN_CONTRACT:
        payloads = _read_files(directory, set(ANALYTICAL_FILES), manifest["file_sha256"])
        run = _strict_json(directory, "run.json", payloads["run.json"])
        if not isinstance(run, dict) or run.get("run_id") != directory.name:
            raise _fail(directory, "run.json run_id does not match the directory")
        return VerifiedRun(directory.name, directory, run, manifest, payloads["report.md"].decode("utf-8"),
                           LEGACY_UNVERIFIED)

    current_contract = (manifest.get("contract"), manifest.get("run_contract")) == (
        MANIFEST_CONTRACT, RUN_CONTRACT)
    previous_contract = (manifest.get("contract"), manifest.get("run_contract")) == (
        PREVIOUS_MANIFEST_CONTRACT, PREVIOUS_RUN_CONTRACT)
    if set(manifest) != _MANIFEST_KEYS or not (current_contract or previous_contract):
        raise _fail(directory, "unsupported run manifest contract or fields")
    expected = set(ANALYTICAL_FILES) | {ENVIRONMENT_FILE}
    if manifest["file_inventory"] != sorted(expected) or manifest["non_analytical_files"] != [ENVIRONMENT_FILE]:
        raise _fail(directory, "manifest file inventory is not the required inventory")
    payloads = _read_files(directory, expected, manifest["file_sha256"])
    run = _strict_json(directory, "run.json", payloads["run.json"])
    expected_run_contract = RUN_CONTRACT if current_contract else PREVIOUS_RUN_CONTRACT
    if not isinstance(run, dict) or run.get("run_id") != directory.name or run.get("contract") != expected_run_contract:
        raise _fail(directory, "run.json identity or contract does not match the directory")
    binding_keys = BINDING_KEYS if current_contract else PREVIOUS_BINDING_KEYS
    if any(key not in run for key in binding_keys):
        raise _fail(directory, "run.json lacks identity binding fields")
    if canonical_hash({key: run[key] for key in binding_keys}) != directory.name:
        raise _fail(directory, "run_id does not re-derive from the run's identity binding")
    environment = _strict_json(directory, ENVIRONMENT_FILE, payloads[ENVIRONMENT_FILE])
    if (not isinstance(environment, dict) or environment.get("run_id") != directory.name
            or environment.get("part_of_run_identity") is not False):
        raise _fail(directory, "environment.json belongs to a different run")
    runtime = environment.get("environment")
    if current_contract and (not isinstance(runtime, dict) or runtime.get("contract") != ENVIRONMENT_CONTRACT
            or runtime.get("required_reproducibility_manifests") != list(LOCKFILES)
            or runtime.get("reproducibility_manifest_status") != "COMPLETE"
            or set(runtime.get("lockfile_sha256", {})) != set(LOCKFILES)):
        raise _fail(directory, "environment provenance lacks the complete required reproducibility manifests")

    provenance = run.get("input_provenance")
    if current_contract and (not isinstance(provenance, dict)
                             or canonical_hash(provenance) != run.get("input_provenance_sha256")):
        raise _fail(directory, "input provenance is missing or does not match its bound hash")
    if not current_contract:
        provenance = None
    expected_datasets = [{
        "symbol": item["symbol"], "decision_id": item["decision"]["decision_id"],
        "bars_sha256": item["decision"]["bars_sha256"],
        "segments": [{
            "file": segment["file"], "payload_sha256": segment.get("sha256"),
            "done_sha256": segment.get("done_sha256"), "metadata_sha256": segment.get("meta_sha256"),
            "records": segment.get("records"), "window": segment.get("window"),
        } for segment in item["decision"]["segments"]],
    } for item in sorted(run["datasets"], key=lambda item: item["symbol"])]
    expected_evidence = sorted({canonical_hash(item["sources"] or {}): {
        "evidence_sha256": item["evidence_sha256"], "source_hashes": item["sources"],
    } for item in run["evidence_versions"]}.values(), key=lambda item: str(item["evidence_sha256"]))
    expected_strategy = {"strategy_id": run["strategy_id"], "version": run["strategy_version"],
                         "config_sha256": run["strategy_config_hash"]}
    if current_contract and (provenance.get("contract") != "stock-gplm.m511-input-provenance.v1"
            or provenance.get("datasets") != expected_datasets or provenance.get("evidence") != expected_evidence
            or provenance.get("strategy") != expected_strategy
            or provenance.get("source_code_sha256") != run["code"]["source_sha256"]
            or provenance.get("model_artifacts") != {
                "applicable": False,
                "reason": "M5 evaluates declarative V1 strategies and loads no ML model artifact",
            }):
        raise _fail(directory, "input provenance contradicts the run's data, evidence, strategy or code identity")
    if current_contract and run.get("registered_cost_profile") is not None:
        expected_cost = {"authority": "REGISTERED_APPROVED", **run["registered_cost_profile"]}
    elif current_contract:
        expected_cost = {"authority": "DIAGNOSTIC_ASSUMPTION", "assumption_sha256": canonical_hash({
            "strategy_trading_costs": run["strategy_config_snapshot"]["trading_costs"],
            "settings": {"buy_statutory_bps": run["settings"]["buy_statutory_bps"],
                         "sell_statutory_bps": run["settings"]["sell_statutory_bps"],
                         "cost_basis": run["settings"]["cost_basis"]},
        })}
    if current_contract and provenance.get("cost_profile") != expected_cost:
        raise _fail(directory, "input provenance contradicts the registered or diagnostic cost identity")

    try:
        investable = (run["eligibility_mode"] == EligibilityMode.STRICT_RESEARCH.value
                      and run["eligibility_issuance"] == ADAPTER_ISSUED)
        if run["investable"] is not investable or run["ranking_eligible"] is not investable or run[
                "classification"] != (STRICT_CLASSIFICATION if investable else DIAGNOSTIC_CLASSIFICATION) or any(
                run[key] is not False for key in ("promotion_eligible", "paper_trading_eligible",
                                                  "ml_training_eligible")):
            raise _fail(directory, "classification contradicts the eligibility mode and issuance")
        if run["holdout_access"]["protected_rows_released_to_execution"] != 0 or run["holdout_access"][
                "protected_rows_scored_or_trained"] != 0:
            raise _fail(directory, "run records locked-holdout rows released or scored")
        tables = {name: _strict_json(directory, name, payloads[name])
                  for name in ("signals.json", "orders.json", "trades.json", "equity-curve.json")}
        cost = run["cost_model"]
        costs = CostModel(cost["commission_bps"], cost["slippage_bps"], cost["buy_statutory_bps"],
                          cost["sell_statutory_bps"])
        simulation = SimulationResult(run["simulator_version"], run["execution"]["policy"],
                                      run["execution"]["intrabar"], tuple(tables["signals.json"]),
                                      tuple(tables["orders.json"]), tuple(tables["trades.json"]),
                                      tuple(run["open_positions_at_end"]), tuple(tables["equity-curve.json"]),
                                      tuple(run["warnings"]))
        recomputed = performance_metrics(simulation, float(run["portfolio_settings"]["initial_capital"]), costs)
        curve = tables["equity-curve.json"]
        if _normal(recomputed) != _normal(run["performance"]):
            raise _fail(directory, "performance does not recompute from the stored orders, trades and equity curve")
        if [curve[0]["date"], curve[-1]["date"]] != run["evaluation_range"]["actual"] or len(curve) != run[
                "evaluation_range"]["sessions"]:
            raise _fail(directory, "equity curve does not match the recorded evaluation range")
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        if isinstance(exc, RunIntegrityError):
            raise
        raise _fail(directory, f"run content is inconsistent: {type(exc).__name__}: {exc}") from exc
    markdown = payloads["report.md"].decode("utf-8")
    if markdown != render_markdown(run):
        raise _fail(directory, "report.md does not re-render from run.json")
    return VerifiedRun(directory.name, directory, run, manifest, markdown, INTEGRITY_VERIFIED)


def scan_runs(root: Path) -> list[dict[str, Any]]:
    """List every entry under ``root`` with its verification status; failures are shown, not hidden."""
    rows: list[dict[str, Any]] = []
    root = Path(root)
    if not root.is_dir():
        return rows
    for path in sorted(root.iterdir()):
        if path.name.startswith("."):
            continue
        try:
            rows.append(load_run(path).summary())
        except RunIntegrityError as exc:
            rows.append({"run_id": path.name, "verification": "INTEGRITY_FAILED", "reason": str(exc)})
    return rows


def replay_run(verified: VerifiedRun, adapter: Any, registry: Any) -> VerifiedRun:
    """Re-run ``verified`` through the adapter and registry; every analytical byte must match."""
    if verified.verification != INTEGRITY_VERIFIED:
        raise _fail(verified.directory, f"{verified.verification} runs cannot be replay-verified")
    run = verified.run
    record = run["requested_universe"]
    canonical = record.get("universe")
    if not isinstance(canonical, dict):
        raise _fail(verified.directory, "run does not record a replayable requested universe")
    universe = UniverseRequest(tuple(canonical["requested_order"]), canonical["start"], canonical["end"],
                               canonical["mode"], canonical["diagnostic_subset"])
    try:
        strategy = registry.get(run["strategy_id"], int(run["strategy_version"]))
        if strategy.config_hash != run["strategy_config_hash"]:
            raise ValueError("registered strategy version no longer has the recorded config hash")
        assessment = adapter.assess_universe(universe)
        release = assessment if record["source"] == "UNIVERSE_ASSESSMENT" else list(assessment.released)
        replayed, files = build_run(strategy, release, BacktestSettings.from_dict(run["settings"]))
    except (KeyError, LookupError, ValueError) as exc:
        raise _fail(verified.directory, f"replay failed: {exc}") from exc
    if replayed["run_id"] != verified.run_id:
        raise _fail(verified.directory, "replay produced a different run identity (inputs or code changed)")
    for name in ANALYTICAL_FILES:
        value = files[name]
        payload = value.encode("utf-8") if isinstance(value, str) else _encode(value)
        if hashlib.sha256(payload).hexdigest() != verified.manifest["file_sha256"][name]:
            raise _fail(verified.directory, f"replay does not reproduce {name}")
    return replace(verified, verification=REPLAY_VERIFIED)


def make_replayer(adapter: Any, registry: Any) -> Callable[[VerifiedRun], VerifiedRun]:
    return lambda verified: replay_run(verified, adapter, registry)


def compare_runs(run_dirs: Sequence[Path], *,
                 replay: Callable[[VerifiedRun], VerifiedRun] | None = None) -> dict[str, Any]:
    """Side-by-side comparison of verified runs; a ranking needs comparable, replay-verified strict runs.

    Any integrity failure raises :class:`RunIntegrityError`: invalid runs are never
    shown next to valid research results.
    """
    verified = [load_run(Path(d)) for d in run_dirs]
    if len({v.run_id for v in verified}) != len(verified):
        raise ValueError("a run is listed more than once")
    if replay is not None:
        verified = [replay(v) if v.verification == INTEGRITY_VERIFIED else v for v in verified]
    runs = [v.run for v in verified]
    fair, reasons = comparable(runs)
    if any(v.verification == LEGACY_UNVERIFIED for v in verified):
        fair = False
        reasons.append("legacy runs without M5.1 verification evidence are never ranked")
    if len({run.get("terminal_valuation") for run in runs}) > 1:
        fair = False
        reasons.append("runs differ in terminal_valuation")
    if any(v.verification != REPLAY_VERIFIED for v in verified):
        fair = False
        reasons.append("ranking requires every run to be replay-verified against the adapter and registry")
    if any(not v.rankable for v in verified) and "only STRICT_RESEARCH runs may be ranked" not in reasons:
        if fair:
            reasons.append("only investable STRICT_RESEARCH runs may be ranked")
        fair = False
    rows = [{"run_id": r["run_id"], "verification": v.verification, "strategy_id": r["strategy_id"],
             "strategy_version": r["strategy_version"], "classification": r["classification"],
             "total_return": r["performance"]["metrics"]["total_return"],
             "total_return_marked_to_market": r["performance"]["metrics"].get("total_return_marked_to_market"),
             "max_drawdown": r["performance"]["metrics"]["max_drawdown"],
             "trades": r["performance"]["metrics"]["trades"],
             "benchmark_total_return": r["benchmark"].get("total_return")} for v, r in zip(verified, runs)]
    ranking = [row["run_id"] for row in sorted(rows, key=lambda row: -row["total_return"])] if fair else None
    return {"comparable": fair, "reasons": reasons, "rows": rows, "ranking_by_total_return": ranking,
            "terminal_valuation": runs[0].get("terminal_valuation") if runs else None}
