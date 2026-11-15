"""Compute backends: NumPy (CPU, always available) and PyTorch (CUDA GPU when installed and usable).

Real GPU work, not detection only. The GPU backend runs:

* **candidate scoring**: boolean signal masks (candidates x bars) multiplied against forward-return
  matrices (bars x horizons) to get counts, sums, sums of squares and wins for every candidate and
  horizon in one batched matmul per chunk;
* **mask generation**: threshold comparisons of the feature matrix and AND-combinations of pairs;
* **model training**: full-batch logistic regression and a two-layer MLP with Adam.

Chunks are sized from free VRAM (``torch.cuda.mem_get_info``) with a safety fraction, so a 12 GB
card processes large candidate pools in batches. The NumPy path implements the same algorithms;
tests check that both give matching answers. ``--device gpu`` fails loudly when CUDA is not usable;
``--device auto`` uses the GPU when usable and otherwise says exactly why it chose the CPU.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np

DEVICE_CHOICES = ("auto", "cpu", "gpu")


class DeviceUnavailable(RuntimeError):
    """``--device gpu`` was requested but no usable CUDA backend exists."""


def is_wsl() -> bool:
    return "microsoft" in platform.release().lower() or "WSL_DISTRO_NAME" in os.environ


def nvidia_smi() -> dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"available": False, "reason": "nvidia-smi not found on PATH"}
    try:
        out = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": f"nvidia-smi failed: {type(exc).__name__}"}
    if out.returncode != 0 or not out.stdout.strip():
        return {"available": False, "reason": (out.stderr or out.stdout).strip()[:300]}
    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            gpus.append({"name": parts[0], "driver": parts[1], "memory_total_mb": _num(parts[2]),
                         "memory_used_mb": _num(parts[3]), "utilization_pct": _num(parts[4])})
    return {"available": bool(gpus), "gpus": gpus}


def _num(x: str) -> float | None:
    try:
        return float(x)
    except ValueError:
        return None


def torch_status() -> dict[str, Any]:
    """Import torch (if installed) and describe CUDA usability, including Blackwell (sm_120) support."""
    if importlib.util.find_spec("torch") is None:
        return {"installed": False, "usable": False,
                "reason": "PyTorch is not installed in this environment (see windows/install.ps1 -Gpu)"}
    try:
        import torch
    except Exception as exc:  # broken install
        return {"installed": True, "usable": False, "reason": f"import torch failed: {type(exc).__name__}: {exc}"}
    st: dict[str, Any] = {"installed": True, "version": torch.__version__, "cuda_build": torch.version.cuda}
    if not torch.cuda.is_available():
        st.update(usable=False, reason="torch.cuda.is_available() is False (CPU-only wheel, driver, or WSL CUDA "
                                         "passthrough missing)")
        return st
    try:
        idx = torch.cuda.current_device()
        cap = torch.cuda.get_device_capability(idx)
        arch = f"sm_{cap[0]}{cap[1]}"
        archs = list(torch.cuda.get_arch_list())
        free, total = torch.cuda.mem_get_info(idx)
        st.update(device_name=torch.cuda.get_device_name(idx), capability=arch, arch_list=archs,
                  vram_total_mb=round(total / 2**20), vram_free_mb=round(free / 2**20))
        if archs and arch not in archs and not any(a.startswith("compute_") for a in archs):
            st.update(usable=False, reason=f"this PyTorch build has kernels for {archs} but the GPU is {arch}; "
                                           "install a CUDA 12.8+ build (for RTX 50-series)")
            return st
        x = torch.ones(8, device="cuda")
        if float((x * 2).sum().item()) != 16.0:
            raise RuntimeError("CUDA arithmetic self-check failed")
        st.update(usable=True)
    except Exception as exc:
        st.update(usable=False, reason=f"CUDA initialisation failed: {type(exc).__name__}: {exc}")
    return st


# ============================================================================ backends
@dataclass
class Backend:
    name: str  # "cpu" or "gpu"
    label: str
    reason: str
    torch_device: Any = None
    stats: dict[str, float] = field(default_factory=lambda: {"gpu_seconds": 0.0, "cpu_seconds": 0.0, "gpu_calls": 0})

    @property
    def is_gpu(self) -> bool:
        return self.name == "gpu"

    def memory(self) -> dict[str, Any]:
        if not self.is_gpu:
            return {}
        import torch

        free, total = torch.cuda.mem_get_info()
        return {"vram_used_mb": round((total - free) / 2**20), "vram_total_mb": round(total / 2**20),
                "torch_allocated_mb": round(torch.cuda.memory_allocated() / 2**20),
                "torch_peak_mb": round(torch.cuda.max_memory_allocated() / 2**20)}

    def chunk_rows(self, bytes_per_row: int, cap: int = 1 << 20, fraction: float = 0.35) -> int:
        """How many rows fit in a fraction of free VRAM (GPU) or a fixed 512 MB budget (CPU)."""
        if self.is_gpu:
            import torch

            free, _ = torch.cuda.mem_get_info()
            budget = free * fraction
        else:
            budget = 512 * 2**20
        return int(max(256, min(cap, budget // max(1, bytes_per_row))))

    def sync(self) -> None:
        if self.is_gpu:
            import torch

            torch.cuda.synchronize()


def resolve(requested: str = "auto") -> Backend:
    if requested not in DEVICE_CHOICES:
        raise ValueError(f"--device must be one of {DEVICE_CHOICES}")
    if requested == "cpu":
        return Backend("cpu", "cpu (NumPy)", "requested --device cpu")
    st = torch_status()
    if st.get("usable"):
        import torch

        return Backend("gpu", f"gpu ({st.get('device_name')}, torch {st.get('version')}, CUDA {st.get('cuda_build')})",
                       f"{requested} -> gpu", torch.device("cuda"))
    smi = nvidia_smi()
    why = st.get("reason", "unknown")
    if smi.get("available"):
        why += f"; GPU visible to the driver: {smi['gpus'][0]['name']}"
    if is_wsl():
        why += "; running under WSL (native Windows GPU verification is separate: windows/gpu-verify.cmd)"
    if requested == "gpu":
        raise DeviceUnavailable(f"--device gpu requested but no usable CUDA backend: {why}")
    return Backend("cpu", "cpu (NumPy)", f"auto -> cpu ({why})")


# ============================================================================ candidate scoring
def forward_matrix(open_: np.ndarray, close: np.ndarray, horizons: Sequence[int], session_id: np.ndarray | None,
                   intraday: bool, cost: float) -> tuple[np.ndarray, np.ndarray]:
    """Net forward return of entering at open t+1 and exiting at close t+h, per bar and horizon.

    Returns (returns [T,H] with 0 where invalid, valid [T,H] bool). Intraday labels never cross a
    session. ``cost`` is the round-trip fraction subtracted from every return.
    """
    n = len(close)
    hs = list(horizons)
    r = np.zeros((n, len(hs)))
    v = np.zeros((n, len(hs)), dtype=bool)
    for j, h in enumerate(hs):
        idx = np.arange(n - h)
        ok = idx + h < n
        idx = idx[ok]
        if idx.size == 0:
            continue
        val = close[idx + h] / open_[idx + 1] - 1 - cost
        good = np.isfinite(val)
        if intraday and session_id is not None:
            good &= session_id[idx + h] == session_id[idx]
        r[idx[good], j] = val[good]
        v[idx[good], j] = True
    return r, v


def score_masks(backend: Backend, masks: np.ndarray | Any, returns: np.ndarray, valid: np.ndarray,
                rows: slice | None = None) -> dict[str, np.ndarray]:
    """Per candidate and horizon: n, mean, std, win rate over bars where the mask fires and the label is valid.

    ``masks`` is (C, T) bool (NumPy or a torch tensor already on the device). The four reductions are
    computed as matrix products ``M @ [valid, r, r^2, win]`` in VRAM-sized chunks.
    """
    t0 = time.perf_counter()
    rows = rows or slice(0, returns.shape[0])
    R = np.where(valid, returns, 0.0)[rows]
    V = valid[rows].astype(np.float64)
    W = ((returns > 0) & valid)[rows].astype(np.float64)
    H = R.shape[1]
    stack = np.concatenate([V, R, R * R, W], axis=1)  # T x 4H
    if backend.is_gpu:
        import torch

        dev = backend.torch_device
        S = torch.as_tensor(stack, device=dev, dtype=torch.float32)
        C = masks.shape[0]
        out = np.zeros((C, 4 * H))
        T = S.shape[0]
        chunk = backend.chunk_rows(T * 4 * 2)
        for a in range(0, C, chunk):
            m = masks[a:a + chunk]
            m = m if isinstance(m, torch.Tensor) else torch.as_tensor(np.asarray(m), device=dev)
            m = m[:, rows].to(torch.float32)
            out[a:a + chunk] = (m @ S).double().cpu().numpy()
        backend.sync()
        backend.stats["gpu_seconds"] += time.perf_counter() - t0
        backend.stats["gpu_calls"] += 1
    else:
        M = np.asarray(masks)[:, rows]
        out = np.zeros((M.shape[0], 4 * H))
        chunk = backend.chunk_rows(M.shape[1] * 8)
        for a in range(0, M.shape[0], chunk):
            out[a:a + chunk] = M[a:a + chunk].astype(np.float64) @ stack
        backend.stats["cpu_seconds"] += time.perf_counter() - t0
    n, s, ss, w = out[:, :H], out[:, H:2 * H], out[:, 2 * H:3 * H], out[:, 3 * H:]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n > 0, s / np.maximum(n, 1), np.nan)
        var = np.where(n > 1, (ss - n * mean ** 2) / np.maximum(n - 1, 1), np.nan)
        std = np.sqrt(np.maximum(var, 0))
        win = np.where(n > 0, w / np.maximum(n, 1), np.nan)
    return {"n": n, "mean": mean, "std": std, "win": win}


def threshold_masks(backend: Backend, X: np.ndarray, feat_idx: np.ndarray, thresholds: np.ndarray,
                    ops_gt: np.ndarray) -> Any:
    """Single-condition masks (C, T): X[:, f] > thr (or < thr). NaN features never fire."""
    if backend.is_gpu:
        import torch

        dev = backend.torch_device
        Xt = torch.as_tensor(X.T[feat_idx], device=dev, dtype=torch.float32)  # C x T
        thr = torch.as_tensor(thresholds, device=dev, dtype=torch.float32)[:, None]
        gt = torch.as_tensor(ops_gt, device=dev)[:, None]
        finite = torch.isfinite(Xt)
        return torch.where(gt, Xt > thr, Xt < thr) & finite
    Xt = X.T[feat_idx]
    with np.errstate(invalid="ignore"):
        m = np.where(ops_gt[:, None], Xt > thresholds[:, None], Xt < thresholds[:, None])
    return m & np.isfinite(Xt)


def pair_masks(backend: Backend, singles: Any, pairs: np.ndarray) -> Any:
    if backend.is_gpu:
        import torch

        p = torch.as_tensor(pairs, device=backend.torch_device, dtype=torch.long)
        return singles[p[:, 0]] & singles[p[:, 1]]
    return singles[pairs[:, 0]] & singles[pairs[:, 1]]


def to_numpy(x: Any) -> np.ndarray:
    if hasattr(x, "detach"):
        return x.detach().cpu().numpy()
    return np.asarray(x)


# ============================================================================ models (logit / MLP, Adam)
def _init_params(sizes: Sequence[int], seed: int) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    params = []
    for a, b in zip(sizes[:-1], sizes[1:]):
        params.append(rng.normal(0, np.sqrt(2.0 / a), size=(a, b)))
        params.append(np.zeros(b))
    return params


def train_network(backend: Backend, X: np.ndarray, y: np.ndarray, w: np.ndarray, hidden: Sequence[int],
                  epochs: int, lr: float, l2: float, seed: int, init: list[np.ndarray] | None = None,
                  on_epoch: Callable[[int, float, float], None] | None = None, record_every: int = 5
                  ) -> list[np.ndarray]:
    """Full-batch weighted binary cross-entropy with L2 and Adam. ``hidden=()`` is logistic regression.

    Returns the parameter list (NumPy). ``on_epoch(epoch, loss, train_acc)`` is called every
    ``record_every`` epochs and on the last epoch with real measured values.
    """
    sizes = [X.shape[1], *hidden, 1]
    params = [p.copy() for p in init] if init is not None else _init_params(sizes, seed)
    wn = w / max(w.sum(), 1e-12)
    if backend.is_gpu:
        import torch

        t0 = time.perf_counter()
        dev = backend.torch_device
        P = [torch.tensor(p, device=dev, dtype=torch.float32, requires_grad=True) for p in params]
        Xt = torch.as_tensor(X, device=dev, dtype=torch.float32)
        yt = torch.as_tensor(y, device=dev, dtype=torch.float32)
        wt = torch.as_tensor(wn, device=dev, dtype=torch.float32)
        opt = torch.optim.Adam(P, lr=lr)
        for ep in range(1, epochs + 1):
            opt.zero_grad()
            h = Xt
            for i in range(0, len(P) - 2, 2):
                h = torch.relu(h @ P[i] + P[i + 1])
            z = (h @ P[-2] + P[-1]).squeeze(1)
            bce = torch.nn.functional.binary_cross_entropy_with_logits(z, yt, weight=wt, reduction="sum")
            reg = sum((P[i] ** 2).sum() for i in range(0, len(P), 2))
            loss = bce + 0.5 * l2 * reg  # bce is a weighted mean (weights sum to 1), so l2 is per-sample scale
            loss.backward()
            opt.step()
            if on_epoch and (ep % record_every == 0 or ep == epochs):
                with torch.no_grad():
                    acc = float((wt * (((z > 0).float()) == yt).float()).sum().item())
                on_epoch(ep, float(bce.item()), acc)
        out = [p.detach().cpu().numpy().astype(np.float64) for p in P]
        backend.sync()
        backend.stats["gpu_seconds"] += time.perf_counter() - t0
        backend.stats["gpu_calls"] += 1
        return out
    t0 = time.perf_counter()
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    b1, b2, eps = 0.9, 0.999, 1e-8
    for ep in range(1, epochs + 1):
        acts = [X]
        h = X
        for i in range(0, len(params) - 2, 2):
            h = np.maximum(h @ params[i] + params[i + 1], 0)
            acts.append(h)
        z = (h @ params[-2] + params[-1])[:, 0]
        p = 1 / (1 + np.exp(-np.clip(z, -40, 40)))
        bce = float(-(wn * (y * np.log(np.clip(p, 1e-12, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-12, 1)))).sum())
        g = (wn * (p - y))[:, None]  # dLoss/dz
        grads: list[np.ndarray] = [np.zeros_like(q) for q in params]
        for layer in range(len(params) // 2 - 1, -1, -1):
            a_in = acts[layer]
            grads[2 * layer] = a_in.T @ g + l2 * params[2 * layer]
            grads[2 * layer + 1] = g.sum(axis=0)
            if layer > 0:
                g = (g @ params[2 * layer].T) * (acts[layer] > 0)
        for i, gr in enumerate(grads):
            m[i] = b1 * m[i] + (1 - b1) * gr
            v[i] = b2 * v[i] + (1 - b2) * gr * gr
            mh = m[i] / (1 - b1 ** ep)
            vh = v[i] / (1 - b2 ** ep)
            params[i] = params[i] - lr * mh / (np.sqrt(vh) + eps)
        if on_epoch and (ep % record_every == 0 or ep == epochs):
            acc = float((wn * ((z > 0).astype(float) == y)).sum())
            on_epoch(ep, bce, acc)
    backend.stats["cpu_seconds"] += time.perf_counter() - t0
    return params


def predict_network(params: Sequence[np.ndarray], X: np.ndarray) -> np.ndarray:
    h = X
    for i in range(0, len(params) - 2, 2):
        h = np.maximum(h @ params[i] + params[i + 1], 0)
    z = (h @ params[-2] + params[-1])[:, 0]
    return 1 / (1 + np.exp(-np.clip(z, -40, 40)))
