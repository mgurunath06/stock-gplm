"""Compute-device detection and resource limits.

The discovery engine is vectorized NumPy/pandas on the CPU. A GPU is used only when
a CUDA-capable framework is importable *and* a workload has a benchmarked GPU
implementation. No such implementation exists yet, so ``auto`` resolves to the CPU
and ``cuda`` is refused with the exact reason. Results never depend on the device.
"""
from __future__ import annotations

import importlib.util
import os
import resource
import shutil
import subprocess
from typing import Any

GPU_IMPLEMENTED_WORKLOADS: tuple[str, ...] = ()


def detect() -> dict[str, Any]:
    info: dict[str, Any] = {"cpu_count": os.cpu_count(), "nvidia_smi": None, "frameworks": {}}
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=10, check=False)
            info["nvidia_smi"] = out.stdout.strip() or out.stderr.strip() or None
        except (OSError, subprocess.TimeoutExpired) as exc:
            info["nvidia_smi"] = f"error: {type(exc).__name__}"
    for name in ("torch", "cupy", "cudf", "xgboost"):
        info["frameworks"][name] = importlib.util.find_spec(name) is not None
    info["cuda_usable"] = bool(info["nvidia_smi"]) and any(info["frameworks"].values())
    info["gpu_workloads_implemented"] = list(GPU_IMPLEMENTED_WORKLOADS)
    return info


def resolve(requested: str) -> tuple[str, str]:
    """Return (device, reason). Never silently changes semantics."""
    if requested not in {"cpu", "cuda", "auto"}:
        raise ValueError("device must be cpu, cuda or auto")
    if requested == "cpu":
        return "cpu", "requested"
    info = detect()
    if not GPU_IMPLEMENTED_WORKLOADS:
        reason = ("no discovery workload has a benchmarked GPU implementation; "
                  f"GPU visible: {info['nvidia_smi'] or 'none'}; CUDA frameworks importable: "
                  f"{[k for k, v in info['frameworks'].items() if v] or 'none'}")
        if requested == "cuda":
            raise RuntimeError(f"--device cuda refused: {reason}")
        return "cpu", f"auto -> cpu ({reason})"
    return ("cuda", "auto -> cuda") if info["cuda_usable"] else ("cpu", "auto -> cpu (CUDA not usable)")


def limit_process(max_memory_gb: float | None, threads: int = 1) -> None:
    """Bound address space and BLAS threads for the current (worker) process."""
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(var, str(threads))
    if max_memory_gb:
        limit = int(max_memory_gb * (1 << 30))
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))  # soft limit only; hard limit unchanged
