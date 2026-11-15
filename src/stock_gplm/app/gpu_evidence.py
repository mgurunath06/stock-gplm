"""Standalone, isolated local-candle discovery and rolling training evidence.

No network, checkpoint reuse, or writes to the application's historical database.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .candidates import Rule, search
from .compute import forward_matrix, nvidia_smi, resolve
from .context import App
from .ingest import Ingestor, SymbolData, normalize_symbol
from .lanes import build_lane
from .models import DirectionModel, PlattCalibrator
from .settings import load
from ..m54.backtest import ExitPolicy, simulate


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, default=str, allow_nan=False), encoding="utf-8")


def _summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    r = np.array([t["net_return"] for t in trades], dtype=float)
    eq = np.r_[1., np.cumprod(1 + r)]
    return {"trades": len(r), "net_return": float(eq[-1] - 1),
            "win_rate": float((r > 0).mean()) if len(r) else None,
            "max_drawdown_at_closed_trades": float((eq / np.maximum.accumulate(eq) - 1).min())}


def run(args: argparse.Namespace) -> int:
    """Create a new research namespace and fit only on labels preceding each OOS block."""
    if args.folds is not None and args.folds < 2:
        raise ValueError("--folds must be >= 2")
    if args.epochs is not None and args.epochs < 1:
        raise ValueError("--epochs must be positive")
    if args.horizon < 1:
        raise ValueError("--horizon must be positive")
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    if start > end:
        raise ValueError("--start must precede --end")
    tfs = args.timeframes.split(",")
    if any(tf not in {"1m", "5m", "15m", "30m", "60m", "1d", "1w"} for tf in tfs):
        raise ValueError("unsupported timeframe")
    syms = list(dict.fromkeys(normalize_symbol(s) for s in args.stocks.split(",") if s.strip()))
    if not syms:
        raise ValueError("at least one stock is required")
    settings = load(args.config, args.home)
    # Refuse output roots inside raw trees before creating anything.
    root = Path(args.output).resolve() if args.output else settings.home / "research-runs"
    for raw in (settings.real_raw_tree, settings.daily_root, *settings.minute_roots):
        if root == raw.resolve() or root.is_relative_to(raw.resolve()):
            raise ValueError("research output must be outside raw data")
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    folder = root / run_id
    folder.mkdir(parents=True, exist_ok=False)
    app = App(args.config, folder, quiet=True)
    clock0, cpu0 = time.perf_counter(), time.process_time()
    stop = threading.Event()
    telemetry: dict[str, Any] = {"gpu": "pending", "cpu_process_pct_one_core": None}
    log_lock = threading.Lock()
    log = (folder / "execution.jsonl").open("x", encoding="utf-8", buffering=1)

    def emit(event: str, **detail: Any) -> None:
        row = {"elapsed_seconds": round(time.perf_counter() - clock0, 3), "event": event, **detail}
        with log_lock:
            log.write(json.dumps(row, default=str) + "\n")
            print(json.dumps(row, default=str), flush=True)

    def monitor() -> None:
        prev_wall, prev_cpu = clock0, cpu0
        while not stop.is_set():
            now, cpu = time.perf_counter(), time.process_time()
            telemetry.update(cpu_process_pct_one_core=round(100 * (cpu - prev_cpu) / max(now - prev_wall, .001), 1),
                             gpu=nvidia_smi())
            prev_wall, prev_cpu = now, cpu
            emit("utilization", **telemetry)
            stop.wait(2)

    thread: threading.Thread | None = None
    try:
        backend = resolve(args.device)
        app.start("gpu-evidence-research", " ".join(sys.argv), vars(args), device=backend.label)
        emit("run_started", research_id=run_id, audit_run_id=app.run_id, output=str(folder),
             device=backend.label, reason=backend.reason, fresh=True,
             cpu_stages="ingestion, features, inference, trade simulation; CUDA discovery/training when selected")
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        folds = args.folds or {"quick": 2, "standard": 5, "deep": 10}[args.depth]
        epochs = args.epochs or {"quick": 20, "standard": 80, "deep": 200}[args.depth]
        cfg = app.s.section("models")
        cfg.update(_epochs_full=epochs, _epochs_update=epochs)
        cfg["logit"]["epochs"] = epochs
        candidates = app.s.section("candidates")
        candidates["audit_candidates"] = True
        if args.depth == "quick":
            candidates.update(max_pairs=30, top_singles_for_pairs=10, max_selected=3)
        elif args.depth == "deep":
            candidates.update(max_pairs=2000, top_singles_for_pairs=80, max_selected=10)
        results: list[dict[str, Any]] = []
        for symbol in syms:
            emit("ingesting", stock=symbol)
            data = Ingestor(app.s, app.db, lambda msg: emit("ingestion", message=msg)).ingest(symbol)
            data = SymbolData(symbol, data.daily[(data.daily.date >= start.date().isoformat()) &
                              (data.daily.date <= end.date().isoformat())].reset_index(drop=True),
                              data.minute[(data.minute.session >= start.date().isoformat()) &
                              (data.minute.session <= end.date().isoformat())].reset_index(drop=True)
                              if len(data.minute) else data.minute, data.fingerprint, data.summary)
            emit("candles_processed", stock=symbol, daily=len(data.daily), minute=len(data.minute),
                 fingerprint=data.fingerprint, summary=data.summary)
            for tf in tfs:
                lane = build_lane(data, tf, app.s.section("costs"), cache_dir=folder / "features")
                if lane is None:
                    raise ValueError(f"no real candles for {symbol}/{tf}")
                m, h = lane.market, args.horizon
                n = len(m)
                # Expanding chronological windows. Leave room for indicator warmup, training and purge.
                first = max(250, n // 2)
                if n - first < folds * (h + 10):
                    raise ValueError(f"insufficient {symbol}/{tf} bars ({n}) for {folds} folds/horizon {h}")
                edges = np.linspace(first, n, folds + 1, dtype=int)
                R, V = forward_matrix(m.open, m.close, [h], m.session_id, m.intraday,
                                      lane.costs.buy + lane.costs.sell)
                valid = V[:, 0] & ~m.no_signal
                y = (R[:, 0] > 0).astype(int)
                history: dict[str, list[tuple[int, float, int]]] = {k: [] for k in ("logit", "mlp")}
                for fold, (a, b) in enumerate(zip(edges[:-1], edges[1:]), 1):
                    # t+h < a: every training outcome is available before OOS starts.
                    train = np.flatnonzero(valid & (np.arange(n) + h < a))
                    test = np.flatnonzero(valid & (np.arange(n) >= a) & (np.arange(n) + h < b))
                    if len(train) < 150 or not len(test) or len(np.unique(y[train])) < 2:
                        raise ValueError(f"insufficient valid training/evaluation labels for {symbol}/{tf}/fold{fold}")
                    emit("discovery_started", stock=symbol, timeframe=tf, fold=fold, candles=n,
                         training_rows=len(train), evaluation_rows=len(test))
                    found = search(backend, lane, int(a - 1), [h], candidates)
                    _write(folder / f"{symbol}-{tf}-fold{fold}-discovery.json", found)
                    emit("discovery_completed", stock=symbol, timeframe=tf, fold=fold,
                         strategies_tested=found["tests"], retained=len(found["rules"]),
                         rejection_counts=found.get("rejection_counts"))
                    for candidate in found["rules"]:
                        emit("candidate_strategy", **candidate)
                    for rejected in found.get("candidate_decisions", [])[:10]:
                        if rejected["reason"] != "retained":
                            emit("rejected_strategy", **rejected)
                    record: dict[str, Any] = {"symbol": symbol, "timeframe": tf, "fold": fold,
                        "train_rows": len(train), "test_rows": len(test), "oos_start": lane.label(int(a)),
                        "oos_end": lane.label(int(b - 1)), "strategies": [], "models": {}}
                    exit_policy = ExitPolicy(h)
                    masks: dict[str, np.ndarray] = {"always_long": np.ones(n, dtype=bool)}
                    majority = float(y[train].mean())
                    record["majority_accuracy"] = float((y[test] == (majority >= .5)).mean())
                    record["cash_return"] = 0.
                    for kind in ("logit", "mlp"):
                        model = DirectionModel(kind, h)
                        def on_epoch(ep: int, loss: float, acc: float) -> None:
                            emit("learning", stock=symbol, timeframe=tf, fold=fold, model=kind,
                                 epoch=ep, epochs=epochs, loss=loss, training_accuracy=acc,
                                 prior_oos_accuracy=(float(np.mean([(p >= .5) == yy for _, p, yy in history[kind]]))
                                                     if history[kind] else None), **telemetry)
                        gpu_calls_before = backend.stats.get("gpu_calls", 0)
                        fit = model.fit(backend, lane.X[train], y[train], np.ones(len(train)), cfg,
                                        int(cfg.get("seed", 55)) + fold, full=True, on_epoch=on_epoch)
                        fit["cuda_calls"] = backend.stats.get("gpu_calls", 0) - gpu_calls_before
                        fit["training_backend"] = backend.label
                        model_path = folder / "models" / f"{symbol}-{tf}-fold{fold}-{kind}.npz"
                        model_sha = model.save(model_path)
                        past = [row for row in history[kind] if row[0] + h < a]
                        cal = PlattCalibrator(30).fit([p for _, p, _ in past], [yy for _, _, yy in past])
                        raw = model.predict(lane.X[test])
                        prob = cal.apply(raw)
                        accuracy = float(((prob >= .5) == y[test]).mean())
                        mask = np.zeros(n, dtype=bool)
                        mask[test] = prob >= .56
                        masks[kind] = mask
                        predictions = [{"bar": int(i), "label": lane.label(int(i)), "p_raw": float(p),
                                        "p_calibrated": float(q), "up": int(y[i]), "net_forward_return": float(R[i, 0])}
                                       for i, p, q in zip(test, raw, prob)]
                        history[kind].extend((int(i), float(p), int(y[i])) for i, p in zip(test, raw))
                        record["models"][kind] = {"fit": fit, "accuracy": accuracy,
                            "brier": float(np.mean((prob - y[test]) ** 2)), "calibration": cal.status,
                            "model_sha256": model_sha, "predictions": predictions,
                            "majority_brier": float(np.mean((majority - y[test]) ** 2))}
                        emit("out_of_sample", stock=symbol, timeframe=tf, fold=fold, model=kind,
                             accuracy=accuracy, majority_accuracy=record["majority_accuracy"], calibration=cal.status)
                    for name, mask in masks.items():
                        trades = simulate(m, mask, (int(a), int(b)), exit_policy, lane.costs)
                        record["strategies"].append({"name": name, "entry": "next bar open after closed-bar signal; long only",
                            "condition": "every eligible bar" if name == "always_long" else "P(net forward return > 0) >= 0.56",
                            "exit": exit_policy.to_dict(), "metrics": _summary(trades), "trades": trades})
                    for item in found["rules"]:
                        rule = Rule.from_dict(item)
                        trades = simulate(m, rule.mask(lane), (int(a), int(b)), ExitPolicy.from_dict(rule.exit), lane.costs)
                        record["strategies"].append({**item, "name": rule.rule_id,
                            "entry": "next bar open after conditions true; long only", "metrics": _summary(trades), "trades": trades})
                    results.append(record)
                    _write(folder / f"{symbol}-{tf}-fold{fold}-evaluation.json", record)
                    emit("fold_completed", stock=symbol, timeframe=tf, fold=fold,
                         strategies=[{"name": s["name"], **s["metrics"]} for s in record["strategies"]])
        backend.sync()
        _write(folder / "source-files.json", app.db.q("SELECT * FROM data_files"))
        _write(folder / "source-issues.json", app.db.q("SELECT * FROM data_issues"))
        report = {"contract": "gplm.local-training-evidence.v2", "research_id": run_id,
            "audit_run_id": app.run_id, "classification": "EXPLORATORY_RESEARCH_ONLY",
            "arguments": vars(args), "python": sys.executable, "platform": platform.platform(),
            "code_sha256": app.code_sha, "config": app.s.raw, "config_sha256": app.s.sha256,
            "device": backend.label, "device_reason": backend.reason, "compute_stats": backend.stats,
            "memory": backend.memory(), "elapsed_seconds": time.perf_counter() - clock0,
            "costs": app.s.section("costs"), "folds": results}
        _write(folder / "report.json", report)
        lines = [f"# GPLM research {run_id}", "", f"Device: {backend.label}. {backend.reason}",
            "", "Expanding walk-forward; horizon-purged training; fresh model fits each fold. No locked holdout or live forecast.",
            "Training loss/accuracy is in-sample. OOS outcomes are research evidence, not future guarantees.",
            "Costs and slippage are configured assumptions. Prices are unadjusted; corporate-action/membership evidence may be unknown.",
            "Drawdown is measured at closed trades, not intratrade equity. Fold boundaries force trade closure.",
            "Each strategy has its own fully reinvested equity; returns must not be added across strategies or stocks.",
            "", "Full conditions, exit policies, trades, predictions and source decisions: report.json, discovery files and gplm.sqlite."]
        for rec in results:
            lines += ["", f"## {rec['symbol']} {rec['timeframe']} fold {rec['fold']}",
                      f"OOS {rec['oos_start']} through {rec['oos_end']}; majority accuracy {rec['majority_accuracy']:.2%}; cash return 0%."]
            for k, model in rec["models"].items():
                lines.append(f"{k}: OOS accuracy {model['accuracy']:.2%}; Brier {model['brier']:.4f}; {model['calibration']}.")
            for strategy in rec["strategies"]:
                lines += [f"- {strategy['name']}: {strategy.get('text', strategy.get('condition'))}; entry: {strategy['entry']}; exit: {strategy['exit']}; results: {strategy['metrics']}"]
        (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
        app.finish("COMPLETED", {"report": str(folder / "report.json")})
        emit("completed", report=str(folder / "report.md"), compute_stats=backend.stats)
        return 0
    except BaseException as exc:
        app.fail(exc)
        emit("failed", error=f"{type(exc).__name__}: {exc}", output=str(folder))
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        stop.set()
        if thread:
            thread.join(timeout=16)
        app.close()
        log.close()


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stocks", "--stock", default="RELIANCE")
    p.add_argument("--start", required=True, help="inclusive local-candle start YYYY-MM-DD")
    p.add_argument("--end", required=True, help="inclusive local-candle end YYYY-MM-DD")
    p.add_argument("--timeframes", "--timeframe", default="15m")
    p.add_argument("--depth", choices=["quick", "standard", "deep"], default="quick")
    p.add_argument("--device", choices=["auto", "cpu", "gpu"], default="auto")
    p.add_argument("--fresh", action="store_true", help="explicit fresh computation (every execution is isolated and fresh)")
    p.add_argument("--folds", type=int)
    p.add_argument("--epochs", type=int)
    p.add_argument("--horizon", type=int, default=4, help="forecast/holding horizon in selected timeframe bars")
    p.add_argument("--output", help="parent folder for unique research run folders")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config")
    p.add_argument("--home")
    add_arguments(p)
    return run(p.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
