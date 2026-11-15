"""Cross-platform process accounting and pools (Linux, macOS and native Windows).

``resource`` and the ``fork`` start method exist only on POSIX. On Windows the engine reports
CPU time from :func:`time.process_time`, leaves peak memory as ``None`` (unknown, not invented)
and evaluates serially instead of forking. Results never depend on which path is taken.
"""
from __future__ import annotations

import multiprocessing as mp
import time
from typing import Any

try:  # POSIX only
    import resource as _resource
except ImportError:  # pragma: no cover - exercised on Windows
    _resource = None  # type: ignore[assignment]

HAS_FORK = "fork" in mp.get_all_start_methods()


def usage(include_children: bool = False) -> dict[str, Any]:
    """Peak RSS in MB (``None`` where the platform does not report it) and CPU seconds."""
    if _resource is None:
        return {"max_rss_mb": None, "cpu_seconds": round(time.process_time(), 1)}
    me = _resource.getrusage(_resource.RUSAGE_SELF)
    cpu = me.ru_utime + me.ru_stime
    if include_children:
        ch = _resource.getrusage(_resource.RUSAGE_CHILDREN)
        cpu += ch.ru_utime + ch.ru_stime
    return {"max_rss_mb": round(me.ru_maxrss / 1024, 1), "cpu_seconds": round(cpu, 1)}


def fork_pool(workers: int) -> Any:
    """A fork-based pool when the platform has fork and more than one worker is asked for, else None."""
    if workers > 1 and HAS_FORK:
        return mp.get_context("fork").Pool(workers)
    return None


def limit_address_space(max_memory_gb: float | None) -> bool:
    """Apply a soft address-space cap where supported. Returns False when the platform cannot."""
    if not max_memory_gb or _resource is None:
        return False
    limit = int(max_memory_gb * (1 << 30))
    _, hard = _resource.getrlimit(_resource.RLIMIT_AS)
    if hard != _resource.RLIM_INFINITY:
        limit = min(limit, hard)
    _resource.setrlimit(_resource.RLIMIT_AS, (limit, hard))
    return True
