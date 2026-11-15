"""Check results, evidence collection and verdict aggregation for the verification suite."""
from __future__ import annotations

import math
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}


def worst(*verdicts: str) -> str:
    return max(verdicts, key=lambda v: _RANK[v]) if verdicts else PASS


@dataclass
class Check:
    """One verified claim with the evidence that supports or refutes it."""

    section: str
    name: str
    claim: str
    verdict: str = PASS
    details: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0

    def fail(self, msg: str) -> None:
        self.verdict = FAIL
        self.details.append("FAIL: " + msg)

    def warn(self, msg: str) -> None:
        self.verdict = worst(self.verdict, WARN)
        self.details.append("WARN: " + msg)

    def note(self, msg: str) -> None:
        self.details.append(msg)

    def expect(self, cond: bool, msg: str) -> bool:
        if not cond:
            self.fail(msg)
        return bool(cond)

    def to_dict(self) -> dict[str, Any]:
        return {"section": self.section, "name": self.name, "claim": self.claim, "verdict": self.verdict,
                "details": self.details, "evidence": clean(self.evidence), "seconds": round(self.seconds, 3)}


class Section:
    """Collects checks for one report section. ``run`` turns an unexpected exception into a FAIL."""

    def __init__(self, name: str):
        self.name = name
        self.checks: list[Check] = []

    def run(self, name: str, claim: str, fn: Callable[[Check], None]) -> Check:
        c = Check(self.name, name, claim)
        t0 = time.perf_counter()
        try:
            fn(c)
        except Exception as exc:  # a crashing check is a failed check, with its traceback as evidence
            c.fail(f"check raised {type(exc).__name__}: {exc}")
            c.evidence["traceback"] = traceback.format_exc(limit=8)
        c.seconds = time.perf_counter() - t0
        self.checks.append(c)
        return c

    @property
    def verdict(self) -> str:
        return worst(*(c.verdict for c in self.checks))


def clean(x: Any) -> Any:
    """JSON-safe copy: NaN/inf become strings, tuples become lists, numpy scalars become Python."""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        np = None  # type: ignore[assignment]
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [clean(v) for v in x]
    if np is not None:
        if isinstance(x, np.ndarray):
            return [clean(v) for v in x.tolist()]
        if isinstance(x, np.generic):
            x = x.item()
    if isinstance(x, float):
        if math.isnan(x):
            return "NaN"
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        return x
    if isinstance(x, (str, int, bool)) or x is None:
        return x
    return str(x)


__all__ = ["Check", "FAIL", "PASS", "Section", "WARN", "clean", "worst"]
