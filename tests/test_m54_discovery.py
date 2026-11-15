"""Synthetic tests for M5.4 discovery, bulk execution, providers and secure env loading.

Every price series here is synthetic. Nothing is market evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from m5_fixtures import write_segment, write_symbol
from stock_gplm.llm import envfile
from stock_gplm.llm.client import LLMClient
from stock_gplm.llm.registry import (LiveCallRefused, RunBudget, authorize_live, check, load_registry, mock_health,
                                     to_llm_config)
from stock_gplm.m54 import ai, device
from stock_gplm.m54.bulk import run_batch
from stock_gplm.m54.cli import main as cli_main
from stock_gplm.m54.engine import Run, Settings, normalize_symbol
from stock_gplm.m54.evaluate import Market, bh_adjust, eligible_starts, evaluate, independent
from stock_gplm.m54.hypotheses import Condition, Hypothesis, condition_mask
from stock_gplm.m54.indicators import REGISTRY, build_features
from stock_gplm.mvp import data as mvp_data
from stock_gplm.v1.portfolio import CostModel

ROOT = Path(__file__).parents[1]
CUTOFF = "2024-10-01"


# ---------------------------------------------------------------- synthetic world

def _m32(directory: Path) -> tuple[Path, str]:
    directory.mkdir(parents=True)
    plan = json.dumps({"schema_version": 1, "plans": [
        {"horizon": "h5", "locked_holdout": {"first_as_of": CUTOFF, "last_as_of": "2024-12-31"}}]}).encode()
    (directory / "split-plan.json").write_bytes(plan)
    digest = hashlib.sha256(plan).hexdigest()
    (directory / "run-manifest.json").write_text(json.dumps({
        "contract": "stock-gplm.m32-research-run.v1", "run_id": "synthetic-run", "outputs": {"split-plan.json": digest}}))
    return directory, digest


SMALL_SPACE = {"contract": "stock-gplm.m54-search-space.v1", "levels": {"RSI.value": [30, 50, 70]},
               "indicators": {"EMA": {"params": {"period": [5, 10, 20]}}, "RSI": {"params": {"period": [7, 14]}},
                              "ROC": {"params": {"period": [1, 3, 5]}}, "RVOL": {"params": {"period": [10], "spike": [1.5]}},
                              "BREAKOUT": {"params": {"period": [10, 20]}}, "ATR": {"params": {"period": [7, 14]}}}}
BUDGETS = {"contract": "stock-gplm.m54-budgets.v1", "budgets": {"quick": {
    "max_single_candidates": 60, "horizons": [3, 5], "max_period": 30, "refine_top_k": 10, "interaction_pool": 6,
    "max_interactions": 10, "max_finalists": 6, "bootstrap_iterations": 50, "random_entry_draws": 50,
    "walk_forward_folds": 2, "checkpoint_every": 20}}}


def world(tmp: Path, symbols: tuple[str, ...] = ("ALPHA-EQ", "BETA-EQ"), *, screen_p: float = 0.9) -> dict[str, Path]:
    raw = tmp / "data" / "raw" / "shoonya" / "NSE"
    for i, s in enumerate(symbols):
        write_symbol(raw, s, phase=0.7 * i)
    m32, digest = _m32(tmp / "m32")
    cfg_dir = tmp / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "search-space.json").write_text(json.dumps(SMALL_SPACE))
    (cfg_dir / "budgets.json").write_text(json.dumps(BUDGETS))
    base = json.loads((ROOT / "config" / "m54" / "discovery-default.json").read_text())
    base["data"] = {"raw_root": str(raw), "m32_run": str(m32), "m32_run_id": "synthetic-run", "split_plan_sha256": digest}
    base["segments"] = {"discovery": ["2024-01-01", "2024-05-31"], "validation": ["2024-06-01", "2024-07-31"],
                        "test": ["2024-08-01", "2024-09-30"]}
    base["criteria"].update({"screen_max_p": screen_p, "min_events_discovery": 3, "min_events_validation": 1,
                             "min_events_test": 1, "min_neighbour_positive_fraction": 0.0})
    base["regimes"] = {"trend_sma": 20}
    (cfg_dir / "discovery.json").write_text(json.dumps(base))
    return {"raw": raw, "config": cfg_dir / "discovery.json", "out": tmp / "out", "m32": m32}


def _run(w: dict[str, Path], symbol: str = "ALPHA", **kw) -> Run:
    return Run(Settings.load(w["config"], "quick"), symbol, w["out"], log=lambda _m: None, **kw)


# ---------------------------------------------------------------- indicators

def _ohlcv(n: int = 400, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    opn = close * (1 + rng.normal(0, 0.004, n))
    return pd.DataFrame({"date": [f"d{i:04d}" for i in range(n)], "open": opn,
                         "high": np.maximum(opn, close) * 1.01, "low": np.minimum(opn, close) * 0.99,
                         "close": close, "volume": rng.integers(1000, 5000, n).astype(float)})


def _full_space() -> dict:
    space = json.loads((ROOT / "config" / "m54" / "search-space.json").read_text())
    for spec in space["indicators"].values():  # one smallest value per parameter keeps the test fast
        spec["params"] = {k: [v[0]] for k, v in spec["params"].items()}
    return space


def test_every_registered_indicator_is_in_the_search_space_and_causal() -> None:
    space = _full_space()
    assert set(space["indicators"]) == set(REGISTRY)
    df = _ohlcv()
    full, defs, _ = build_features(df, space)
    cut = 300
    truncated, _, _ = build_features(df.iloc[:cut].reset_index(drop=True), space)
    for key, values in truncated.items():
        a, b = full[key][:cut], values
        both = np.isfinite(a) & np.isfinite(b)
        assert np.array_equal(np.isfinite(a), np.isfinite(b)), key
        assert np.allclose(a[both], b[both], rtol=1e-9, atol=1e-12), f"{key} uses future bars"
    assert {d.family for d in defs.values()} >= {"moving_average", "momentum", "volume", "trend", "volatility",
                                                  "price_action", "statistical"}


def test_rsi_and_ema_distance_match_hand_values() -> None:
    df = pd.DataFrame({"open": [1.0] * 6, "high": [2.0] * 6, "low": [0.5] * 6, "close": [10, 11, 10.5, 11.5, 11, 12.0],
                       "volume": [1.0] * 6})
    rsi = REGISTRY["RSI"].fn(df, period=3)["value"].to_numpy()
    gain, loss = 2 / 3, 0.5 / 3
    assert np.isnan(rsi[:3]).all() and rsi[3] == pytest.approx(100 - 100 / (1 + gain / loss))
    ema = REGISTRY["EMA"].fn(df, period=3)["distance"].to_numpy()
    assert np.isnan(ema[:2]).all() and np.isfinite(ema[2])


def test_conditions_cross_and_nan_never_fire() -> None:
    x = np.array([np.nan, -1.0, 1.0, 2.0, -0.5, np.nan, 3.0])
    f = {"x": x}
    assert condition_mask(Condition("x", "cross_above", 0.0), f).tolist() == [False, False, True, False, False, False, False]
    assert condition_mask(Condition("x", "cross_below", 0.0), f).tolist() == [False, False, False, False, True, False, False]
    assert not condition_mask(Condition("x", "gt", -10.0), f)[[0, 5]].any()
    with pytest.raises(ValueError):
        Condition("x", "is", 1.0)
    h1 = Hypothesis((Condition("a", "gt", 1.0), Condition("b", "lt", 2.0)))
    h2 = Hypothesis((Condition("b", "lt", 2.0), Condition("a", "gt", 1.0)))
    assert h1.hypothesis_id == h2.hypothesis_id


# ---------------------------------------------------------------- evaluation timing and statistics

def _market(n: int = 40, blocked: set[int] = frozenset()) -> Market:
    opn = np.arange(100.0, 100.0 + n)
    return Market(tuple(f"d{i:02d}" for i in range(n)), opn, opn + 1, opn - 1, opn + 0.5, np.ones(n),
                  np.full(n, 0.01), np.array([i in blocked for i in range(n)]))


def test_entry_next_open_exit_open_after_horizon_and_costs() -> None:
    m = _market()
    mask = np.zeros(40, bool)
    mask[5] = True
    costs = CostModel(10, 0, 0, 0)
    r = evaluate(m, mask, (0, 40), 3, costs, target_atr=1.0, bootstrap=0, random_draws=0, seed=0, keep_events=True)
    e = r["events"][0]
    assert (e["signal_date"], e["entry_date"], e["exit_date"]) == ("d05", "d06", "d09")
    assert e["net_return"] == pytest.approx(m.open[9] * 0.999 / (m.open[6] * 1.001) - 1)
    assert e["gross_return"] == pytest.approx(m.open[9] / m.open[6] - 1)


def test_independent_events_do_not_overlap_and_stay_inside_segment() -> None:
    assert independent(np.arange(0, 10), 3) == [0, 4, 8]
    m = _market()
    starts = eligible_starts(m, (10, 20), 3)
    assert starts.min() == 10 and starts.max() == 20 - 1 - 4  # exit open index t+4 <= 19
    blocked = eligible_starts(_market(blocked={15}), (10, 20), 3)
    assert all(not (t + 1 <= 15 <= t + 4) for t in blocked)


def test_bh_adjust_matches_reference() -> None:
    q = bh_adjust([0.01, 0.04, 0.03, None], 4)
    assert q[0] == pytest.approx(0.04) and q[1] == pytest.approx(0.0533333, rel=1e-4) and q[3] is None


# ---------------------------------------------------------------- end-to-end engine

def test_run_completes_never_opens_protected_segment_and_reproduces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(tmp_path)
    opened: list[str] = []
    real = mvp_data.validate_daily
    monkeypatch.setattr(mvp_data, "validate_daily", lambda p: (opened.append(Path(p).name), real(p))[1])
    raw_before = {p: p.read_bytes() for p in w["raw"].rglob("*") if p.is_file()}
    result = _run(w).execute()
    assert result["state"] == "COMPLETED"
    assert opened and all(not n.startswith("2024-10-01") for n in opened)
    d = Path(result["dir"])
    for name in ("report.md", "findings.json", "results.csv", "events.csv", "run-manifest.json"):
        assert (d / name).is_file()
    findings = json.loads((d / "findings.json").read_text())
    assert findings["governance"]["m32_locked_holdout_opened"] is False
    assert findings["total_discovery_tests"] > 0 and findings["classification"] == "DIAGNOSTIC_ONLY_NON_INVESTABLE"
    assert {p: p.read_bytes() for p in w["raw"].rglob("*") if p.is_file()} == raw_before
    # Reuse, then byte-identical outputs from a fresh output root.
    assert _run(w).execute()["reused"] is True
    w2 = dict(w, out=tmp_path / "out2")
    other = Path(_run(w2).execute()["dir"])
    for name in ("report.md", "findings.json", "results.csv", "events.csv"):
        assert (other / name).read_bytes() == (d / name).read_bytes()


def test_tampered_completed_output_is_detected(tmp_path: Path) -> None:
    w = world(tmp_path)
    d = Path(_run(w).execute()["dir"])
    (d / "results.csv").write_text("edited\n")
    with pytest.raises(ValueError, match="modified"):
        _run(w).execute()


def test_stop_and_resume_reuses_completed_stages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(tmp_path)
    run = _run(w)
    original = Run._stage_refine

    def stop_during_refine(self, screen):
        (self.dir / "STOP").write_text("x")
        return original(self, screen)

    monkeypatch.setattr(Run, "_stage_refine", stop_during_refine)
    assert run.execute()["state"] == "STOPPED"
    screen = run.dir / "stages" / "screen.json"
    stamp = screen.stat().st_mtime_ns
    assert json.loads((run.dir / "status.json").read_text())["state"] == "STOPPED"
    monkeypatch.setattr(Run, "_stage_refine", original)
    (run.dir / "STOP").unlink()
    assert _run(w).execute()["state"] == "COMPLETED"
    assert screen.stat().st_mtime_ns == stamp


def test_corrupt_checkpoint_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(tmp_path)
    run = _run(w)
    monkeypatch.setattr(Run, "_stage_refine", lambda self, s: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        run.execute()
    path = run.dir / "stages" / "screen.json"
    raw = json.loads(path.read_text())
    raw["payload"]["tests"] += 1
    path.write_text(json.dumps(raw))
    monkeypatch.undo()
    with pytest.raises(ValueError, match="integrity"):
        _run(w).execute()


def test_test_segment_reaching_holdout_is_refused(tmp_path: Path) -> None:
    w = world(tmp_path)
    cfg = json.loads(w["config"].read_text())
    cfg["segments"]["test"] = ["2024-08-01", "2024-10-15"]
    w["config"].write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="locked holdout"):
        _run(w).execute()


def test_hypothesis_file_is_tested_and_counted(tmp_path: Path) -> None:
    w = world(tmp_path)
    hyp = tmp_path / "h.json"
    hyp.write_text(json.dumps({"contract": "stock-gplm.m54-hypotheses.v1", "source": "unit", "hypotheses": [
        {"conditions": [{"feature": "RSI(period=14).value", "op": "cross_above", "threshold": 50}]},
        {"conditions": [{"feature": "NOT_A_FEATURE", "op": "gt", "threshold": 1}]}]}))
    d = Path(_run(w, hypotheses_file=hyp).execute()["dir"])
    screen = json.loads((d / "stages" / "screen.json").read_text())["payload"]
    assert screen["file_hypotheses"] == 1 and len(screen["file_hypotheses_unavailable_features"]) == 1
    assert any(r["hypothesis"]["origin"] == "file:unit" for r in screen["rows"])


def test_bulk_isolates_quarantined_symbol_and_resumes(tmp_path: Path) -> None:
    w = world(tmp_path, ("ALPHA-EQ", "BETA-EQ"))
    write_segment(w["raw"], "GAMMA-EQ", "2024-01-01", "2024-06-30", [("2024-01-02", "1", "0.5", "2", "1", 10)])
    out = run_batch(w["config"], "quick", ["alpha", "BETA", "GAMMA", "MISSINGX"], w["out"], workers=2,
                    max_memory_gb=None, log=lambda _m: None)
    manifest = json.loads((Path(out["dir"]) / "batch-manifest.json").read_text())
    states = {k: v["state"] for k, v in manifest["results"].items()}
    assert states["ALPHA-EQ"] == states["BETA-EQ"] == "COMPLETED"
    assert states["GAMMA-EQ"] == "QUARANTINED" and states["MISSINGX-EQ"] == "QUARANTINED"
    again = run_batch(w["config"], "quick", ["ALPHA", "BETA", "GAMMA", "MISSINGX"], w["out"], workers=1,
                      max_memory_gb=None, log=lambda _m: None)
    assert again["batch_id"] == out["batch_id"]
    assert all(v.get("reused") for k, v in json.loads((Path(out["dir"]) / "batch-manifest.json").read_text())["results"].items()
               if v["state"] == "COMPLETED")


def test_symbol_normalization_rejects_paths() -> None:
    assert normalize_symbol("reliance") == "RELIANCE-EQ"
    for bad in ("../x", "a/b", "", "-EQ"):
        with pytest.raises(ValueError):
            normalize_symbol(bad)


def test_cli_dry_run_writes_nothing_and_cuda_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = world(tmp_path)
    assert cli_main(["discover", "--stock", "ALPHA", "--budget", "quick", "--dry-run", "--device", "cpu",
                     "--config", str(w["config"]), "--output-root", str(w["out"])]) == 0
    assert "screen_tests" in capsys.readouterr().out
    assert not w["out"].exists()
    if not device.GPU_IMPLEMENTED_WORKLOADS:
        with pytest.raises(RuntimeError, match="cuda refused"):
            device.resolve("cuda")
        assert device.resolve("auto")[0] == "cpu"


def test_export_creates_editable_copy_and_keeps_original(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = world(tmp_path, screen_p=1.0)
    res = _run(w).execute()
    d = Path(res["dir"])
    discovered = sorted((d / "discovered").glob("*.json"))
    if not discovered:
        pytest.skip("no validation passer in this synthetic world")
    hid, hz = discovered[0].stem.rsplit("-h", 1)
    dest = tmp_path / "user"
    args = ["export", "--run-id", res["run_id"][:12], "--hypothesis", hid, "--horizon", hz, "--name", "mine",
            "--dest", str(dest), "--output-root", str(w["out"])]
    assert cli_main(args) == 0
    copy = json.loads((dest / "mine.json").read_text())
    assert copy["status"] == "USER_EDITABLE_COPY" and copy["activation"] == "RESEARCH_ONLY_NOT_FOR_TRADING"
    with pytest.raises(SystemExit):
        cli_main(args)


# ---------------------------------------------------------------- env file and providers

def test_envfile_requires_0600_and_loads_only_gplm_names(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("GPLM_X_API_KEY='secret-value'\nOTHER=1\nexport GPLM_Y_API_KEY=abc # note\n# GPLM_Z=1\n")
    os.chmod(p, 0o644)
    with pytest.raises(envfile.InsecureEnvFile):
        envfile.load(p, {})
    os.chmod(p, 0o600)
    env: dict[str, str] = {"GPLM_Y_API_KEY": "preset"}
    info = envfile.load(p, env)
    assert env == {"GPLM_X_API_KEY": "secret-value", "GPLM_Y_API_KEY": "preset"}
    assert info["loaded"] == ["GPLM_X_API_KEY"] and "secret-value" not in json.dumps(info)
    link = tmp_path / "link.env"
    link.symlink_to(p)
    with pytest.raises(envfile.InsecureEnvFile):
        envfile.check_permissions(link)


def test_envfile_found_from_linked_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".env").write_text("GPLM_A_API_KEY=x\n")
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    assert envfile.locate(wt / "sub", {}) == main / ".env"
    assert envfile.locate(wt, {"GPLM_ENV_FILE": str(tmp_path / "x.env")}) == main / ".env"  # explicit path absent


def _registry_file(tmp: Path, **changes) -> Path:
    raw = json.loads((ROOT / "config" / "research" / "providers.json").read_text())
    for name, patch in changes.items():
        raw["providers"][name].update(patch)
    p = tmp / "providers.json"
    p.write_text(json.dumps(raw))
    return p


def test_provider_registry_defaults_are_safe_and_check_hides_values(tmp_path: Path) -> None:
    reg = load_registry(ROOT / "config" / "research" / "providers.json")
    assert reg.offline and not any(p.enabled or p.live_calls_authorized for p in reg.providers.values())
    rows = check(reg, {"GPLM_MWAPI_API_KEY": "super-secret"})
    assert "super-secret" not in json.dumps(rows)
    assert next(r for r in rows if r["provider"] == "mwapi")["key_configured"] is True
    assert not any(r["live_possible"] for r in rows)
    with pytest.raises(LiveCallRefused, match="offline"):
        authorize_live(reg, "mwapi", {"GPLM_MWAPI_API_KEY": "k"}, True)


def test_provider_config_rejects_embedded_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="credential"):
        load_registry(_registry_file(tmp_path, mwapi={"api_key": "x"}))
    with pytest.raises(ValueError, match="credential"):
        load_registry(_registry_file(tmp_path, mwapi={"model": "sk-abcdefghijklmnop"}))


def test_live_authorization_requires_every_gate(tmp_path: Path) -> None:
    raw = json.loads(_registry_file(tmp_path, openrouter={"enabled": True, "live_calls_authorized": True}).read_text())
    raw["offline"] = False
    p = tmp_path / "live.json"
    p.write_text(json.dumps(raw))
    reg = load_registry(p)
    with pytest.raises(LiveCallRefused, match="allow-live"):
        authorize_live(reg, "openrouter", {"GPLM_OPENROUTER_API_KEY": "k"}, False)
    with pytest.raises(LiveCallRefused, match="not configured"):
        authorize_live(reg, "openrouter", {}, True)
    prov = authorize_live(reg, "openrouter", {"GPLM_OPENROUTER_API_KEY": "k"}, True)
    budget = RunBudget(prov)
    for _ in range(prov.max_requests_per_run):
        budget.before(100)
        budget.after(100, 0.0)
    with pytest.raises(LiveCallRefused, match="max_requests_per_run"):
        budget.before(100)


def test_mock_health_uses_no_network_and_shapes_anthropic_requests(tmp_path: Path) -> None:
    reg = load_registry(ROOT / "config" / "research" / "providers.json")
    rows = {r["provider"]: r for r in mock_health(reg, tmp_path)}
    assert rows["mwapi"]["status"] == "MOCK_OK" and rows["mwapi"]["url"] == "https://api.mwapi.dev/v1/messages"
    assert rows["mwapi"]["auth_headers"] == ["x-api-key"]
    assert rows["openrouter"]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert rows["gemini"]["status"] == "SKIPPED_FUTURE_ADAPTER"
    audit = (tmp_path / "mock-health-audit.jsonl").read_text()
    assert "MOCK-KEY-NOT-REAL" not in audit


def test_client_refuses_market_rows_to_remote_anthropic_provider(tmp_path: Path) -> None:
    reg = load_registry(_registry_file(tmp_path, mwapi={"enabled": True, "model": "m"}))
    cfg = to_llm_config(reg, ["mwapi"])
    cfg.audit_log = tmp_path / "a.jsonl"
    client = LLMClient(cfg, transport=lambda *a: pytest.fail("network used"), environ={"GPLM_MWAPI_API_KEY": "k"})
    rows = "\n".join(f"2024-01-{i:02d},100,101,99,100,1000" for i in range(1, 20))
    with pytest.raises(Exception, match="market-data"):
        client.complete(provider="mwapi", model="m", purpose="t", prompt=rows)


def test_ai_reply_parser_keeps_only_known_features() -> None:
    reply = 'Sure: {"hypotheses": [{"rationale": "r", "conditions": [{"feature": "A", "op": "gt", "threshold": 1}]},' \
            '{"conditions": [{"feature": "B", "op": "gt", "threshold": 1}]}, {"conditions": []}]}'
    doc = ai.parse_reply(reply, {"A"}, "mock")
    assert len(doc["hypotheses"]) == 1 and len(doc["rejected"]) == 2 and doc["authority"] == "ADVISORY_ONLY"
    assert "AND" not in ai.build_prompt([{"key": "A", "family": "f", "kind": "signed"}], "X", 3).split("Grammar")[0]


def test_ml_dataset_is_purged_and_refuses_holdout(tmp_path: Path) -> None:
    from stock_gplm.m54.ml import build_dataset, feature_coverage
    w = world(tmp_path)
    run = _run(w)
    run._load(create=False)
    run._prepare()
    data = build_dataset(run, 5, ["RSI(period=14).value", "ROC(period=1).value"])
    for seg, mat in data.items():
        a, b = run.segs[seg]
        assert mat.X.shape == (len(mat.dates), 2) and len(mat.y_net_return) == len(mat.dates)
        last_signal = run.dates.index(mat.dates[-1])
        assert last_signal + 1 + 5 <= b - 1  # label exits inside its own segment
    assert set(feature_coverage(data)) == {"discovery", "validation", "test"}
    with pytest.raises(ValueError, match="holdout"):
        build_dataset(run, 5, segments=("holdout",))
