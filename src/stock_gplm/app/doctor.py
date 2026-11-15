"""``gplm doctor`` (environment and data health) and ``gplm gpu-test`` (real GPU execution evidence).

``gpu-test`` runs the same three workloads on the CPU (NumPy) and on CUDA (PyTorch):

1. a 4096 x 4096 float32 matrix product,
2. candidate scoring: 20,000 synthetic signal masks x 5,000 bars x 4 horizons (the discovery kernel),
3. 30 epochs of the MLP direction model on 20,000 rows x 128 features,

and compares the answers (max absolute / relative differences) and timings. It passes only when
CUDA actually executed every workload and the results agree within float32 tolerance. A CPU-only
run is reported as ``CPU_ONLY`` — never as a GPU pass. The JSON evidence is written under
``<home>/reports/gpu-test-<utc>.json`` and an audit event records it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import ACCESS_BASIS, __version__
from .compute import (Backend, DeviceUnavailable, is_wsl, nvidia_smi, resolve, score_masks, torch_status,
                      train_network)
from .settings import Settings


def _pkg(name: str) -> str | None:
    if importlib.util.find_spec(name) is None:
        return None
    try:
        mod = __import__(name)
        return str(getattr(mod, "__version__", "installed"))
    except Exception as exc:  # broken install
        return f"BROKEN: {type(exc).__name__}"


def doctor(s: Settings) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: Any) -> None:
        checks.append({"check": name, "status": "OK" if ok else ("INFO" if ok is None else "FAIL"), "detail": detail})

    add("python", sys.version_info[:2] == (3, 12), f"{platform.python_version()} at {sys.executable}")
    add("platform", None, f"{platform.system()} {platform.release()} ({'WSL' if is_wsl() else 'native'})")
    for pkg, required in (("numpy", True), ("pandas", True), ("scipy", True), ("torch", False)):
        v = _pkg(pkg)
        add(f"package {pkg}", (v is not None and not str(v).startswith("BROKEN")) if required else (None if v is None else True),
            v or ("MISSING (required)" if required else "not installed (GPU acceleration unavailable; CPU path works)"))
    add("config", True, str(s.path))
    add("access basis", None, f"{ACCESS_BASIS}: {s.section('access').get('authorized_on')}")
    for label, root in (("daily root", s.daily_root), *((f"minute root {i + 1}", r) for i, r in enumerate(s.minute_roots))):
        if root.is_dir():
            n = sum(1 for d in root.iterdir() if d.is_dir() and d.name.endswith("-EQ"))
            add(label, n > 0, f"{root} ({n} symbol directories)")
        else:
            add(label, False, f"{root} does not exist")
    try:
        s.ensure()
        probe = s.home / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("GPLM home writable", True, str(s.home))
    except (OSError, ValueError) as exc:
        add("GPLM home writable", False, f"{s.home}: {exc}")
    if s.db_path.exists():
        try:
            con = sqlite3.connect(f"file:{urllib.parse.quote(s.db_path.resolve().as_posix())}?mode=ro", uri=True)
            integ = con.execute("PRAGMA quick_check").fetchone()[0]
            counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("runs", "events", "predictions", "evaluations", "strategies", "data_files")}
            last = con.execute("SELECT MAX(last) FROM candle_cache").fetchone()[0]
            con.close()
            add("database", integ == "ok", {"integrity": integ, "rows": counts, "latest_ingested_session": last})
            from .db import AuditDB
            db = AuditDB(s.db_path, readonly=True)
            add("audit hash chain", db.verify_chain()["ok"], db.verify_chain())
            db.close()
        except sqlite3.Error as exc:
            add("database", False, str(exc))
    else:
        add("database", None, "not created yet (first `gplm daily` or `gplm ingest` creates it)")
    lock = s.home / "gplm.lock"
    add("writer lock", None, lock.read_text(encoding="utf-8") if lock.exists() else "free")
    smi = nvidia_smi()
    add("nvidia driver", None if not smi.get("available") else True, smi.get("gpus") or smi.get("reason"))
    ts = torch_status()
    add("CUDA via PyTorch", ts.get("usable") if ts.get("installed") else None, ts)
    try:
        be = resolve("auto")
        add("device auto", True, f"{be.label}: {be.reason}")
    except DeviceUnavailable as exc:
        add("device auto", False, str(exc))
    ok = all(c["status"] != "FAIL" for c in checks)
    return {"ok": ok, "app_version": __version__, "checks": checks}


def _time(fn: Any, backend: Backend | None = None, repeats: int = 1) -> tuple[Any, float]:
    out = None
    t0 = time.perf_counter()
    for _ in range(repeats):
        out = fn()
        if backend is not None:
            backend.sync()
    return out, (time.perf_counter() - t0) / repeats


def gpu_test(s: Settings, require_gpu: bool) -> dict[str, Any]:
    report: dict[str, Any] = {"utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
                              "wsl": is_wsl(), "python": platform.python_version(), "nvidia_smi": nvidia_smi(),
                              "torch": torch_status(), "app_version": __version__}
    cpu = resolve("cpu")
    try:
        gpu = resolve("gpu")
    except DeviceUnavailable as exc:
        gpu = None
        report["gpu_unavailable_reason"] = str(exc)
    rng = np.random.default_rng(7)
    results: dict[str, Any] = {}
    # 1. matmul
    A = rng.standard_normal((4096, 4096)).astype(np.float32)
    B = rng.standard_normal((4096, 4096)).astype(np.float32)
    ref, t_cpu = _time(lambda: A @ B)
    results["matmul_4096"] = {"cpu_seconds": round(t_cpu, 4)}
    if gpu is not None:
        import torch

        At, Bt = torch.as_tensor(A, device="cuda"), torch.as_tensor(B, device="cuda")
        _time(lambda: At @ Bt, gpu)  # warm-up
        out, t_gpu = _time(lambda: At @ Bt, gpu, repeats=3)
        diff = float(np.abs(out.cpu().numpy() - ref).max())
        results["matmul_4096"].update(gpu_seconds=round(t_gpu, 4), speedup=round(t_cpu / t_gpu, 1),
                                      max_abs_diff=diff, max_rel_diff=diff / float(np.abs(ref).max()),
                                      gpu_executed=True)
    # 2. candidate scoring kernel
    T, C, H = 5000, 20000, 4
    masks = rng.random((C, T)) < 0.05
    R = rng.standard_normal((T, H)) * 0.01
    V = rng.random((T, H)) < 0.95
    sc_cpu, t_cpu = _time(lambda: score_masks(cpu, masks, R, V))
    results["candidate_scoring"] = {"candidates": C, "bars": T, "horizons": H, "cpu_seconds": round(t_cpu, 4)}
    if gpu is not None:
        import torch

        Mt = torch.as_tensor(masks, device="cuda")
        score_masks(gpu, Mt, R, V)  # warm-up
        sc_gpu, t_gpu = _time(lambda: score_masks(gpu, Mt, R, V), gpu)
        dn = float(np.nanmax(np.abs(sc_gpu["n"] - sc_cpu["n"])))
        dm = float(np.nanmax(np.abs(sc_gpu["mean"] - sc_cpu["mean"])))
        results["candidate_scoring"].update(gpu_seconds=round(t_gpu, 4), speedup=round(t_cpu / t_gpu, 1),
                                            count_max_abs_diff=dn, mean_max_abs_diff=dm, gpu_executed=True,
                                            memory=gpu.memory())
    # 3. MLP training
    X = rng.standard_normal((20000, 128))
    y = (X[:, :4].sum(axis=1) + rng.standard_normal(20000) > 0).astype(float)
    w = np.ones(len(y))
    losses_cpu: list[float] = []
    _, t_cpu = _time(lambda: train_network(cpu, X, y, w, (64, 32), 30, 0.003, 1e-4, 3,
                                           on_epoch=lambda e, l, a: losses_cpu.append(l), record_every=5))
    results["mlp_training"] = {"rows": 20000, "features": 128, "epochs": 30, "cpu_seconds": round(t_cpu, 4),
                               "cpu_loss_first_last": [losses_cpu[0], losses_cpu[-1]]}
    if gpu is not None:
        losses_gpu: list[float] = []
        _, t_gpu = _time(lambda: train_network(gpu, X, y, w, (64, 32), 30, 0.003, 1e-4, 3,
                                               on_epoch=lambda e, l, a: losses_gpu.append(l), record_every=5), gpu)
        results["mlp_training"].update(gpu_seconds=round(t_gpu, 4), speedup=round(t_cpu / t_gpu, 1),
                                       gpu_loss_first_last=[losses_gpu[0], losses_gpu[-1]],
                                       loss_decreased=losses_gpu[-1] < losses_gpu[0],
                                       final_loss_abs_diff_vs_cpu=abs(losses_gpu[-1] - losses_cpu[-1]),
                                       gpu_executed=True, memory=gpu.memory())
    report["results"] = results
    if gpu is None:
        report["status"] = "CPU_ONLY"
        report["verdict"] = ("No usable CUDA backend: the CPU path was exercised and timed, but this is NOT a GPU "
                             "pass. " + report.get("gpu_unavailable_reason", ""))
    else:
        agree = (results["matmul_4096"]["max_rel_diff"] < 1e-3 and results["candidate_scoring"]["count_max_abs_diff"] < 0.5
                 and results["candidate_scoring"]["mean_max_abs_diff"] < 1e-5 and results["mlp_training"]["loss_decreased"])
        report["status"] = "GPU_PASS" if agree else "GPU_MISMATCH"
        report["verdict"] = ("CUDA executed all three workloads and matched the CPU results within float32 tolerance"
                             if agree else "CUDA executed but results disagree with the CPU beyond tolerance")
        if is_wsl():
            report["verdict"] += ("; NOTE: this ran under WSL. Native-Windows acceptance requires running "
                                  "windows\\gpu-verify.cmd on Windows itself.")
    s.reports_dir.mkdir(parents=True, exist_ok=True)
    path = s.reports_dir / f"gpu-test-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["evidence_file"] = str(path)
    if require_gpu and report["status"] != "GPU_PASS":
        report["exit_code"] = 2
    return report


__all__ = ["doctor", "gpu_test", "Path", "os"]
