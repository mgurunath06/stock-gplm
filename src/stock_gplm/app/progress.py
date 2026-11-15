"""Live terminal progress plus a ``progress.json`` snapshot that the dashboard polls.

Only values reported by the running job are shown; nothing is estimated except the remaining
time, which is computed from observed throughput (completed units / elapsed seconds) and labelled
as an estimate. On a TTY the block is redrawn in place with ANSI sequences (Windows 10+ consoles
support them; they are enabled explicitly on Windows); otherwise one line is printed at most every
``line_interval`` seconds.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from .db import utc_now


def _enable_vt() -> bool:
    if sys.platform != "win32":
        return True
    try:  # pragma: no cover - Windows only
        import ctypes

        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False
    return False


def fmt_secs(s: float | None) -> str:
    if s is None or s != s:
        return "-"
    s = int(s)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


class Progress:
    FIELDS = ("task", "stock", "timeframe", "session", "step", "steps", "model", "horizon", "epoch", "epochs",
              "train_loss", "train_acc", "oos_acc", "oos_n", "trades", "net_return", "device", "gpu_mem", "warnings",
              "errors", "note", "eligible_references", "evaluated_rules", "generated_candidates",
              "rejected_candidates", "completed_folds")

    def __init__(self, path: Path | None, stream: TextIO | None = None, quiet: bool = False, line_interval: float = 5.0):
        self.path = path
        self.stream = stream or sys.stderr
        self.quiet = quiet
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)()) and not os.environ.get("GPLM_NO_TTY")
        self.vt = self.tty and _enable_vt()
        self.state: dict[str, Any] = {"started_utc": utc_now(), "warnings": 0, "errors": 0}
        self.t0 = time.monotonic()
        self.units_done = 0
        self.units_total = 0
        self.last_line = 0.0
        self.last_write = 0.0
        self.drawn = 0

    def set_total(self, total: int) -> None:
        self.units_total = max(0, int(total))
        self.update()

    def advance(self, n: int = 1) -> None:
        self.units_done += n
        self.update()

    def warn(self, msg: str) -> None:
        self.state["warnings"] = self.state.get("warnings", 0) + 1
        self.state["last_warning"] = msg[:300]
        self.update(force=True)

    def error(self, msg: str) -> None:
        self.state["errors"] = self.state.get("errors", 0) + 1
        self.state["last_error"] = msg[:300]
        self.update(force=True)

    def snapshot(self) -> dict[str, Any]:
        el = time.monotonic() - self.t0
        rate = self.units_done / el if el > 0 and self.units_done else None
        eta = (self.units_total - self.units_done) / rate if rate and self.units_total else None
        return {**self.state, "elapsed_s": round(el, 1), "units_done": self.units_done, "units_total": self.units_total,
                "throughput_per_s": round(rate, 4) if rate else None, "eta_s_estimate": round(eta, 1) if eta else None,
                "updated_utc": utc_now(), "pid": os.getpid()}

    def update(self, force: bool = False, **fields: Any) -> None:
        self.state.update({k: v for k, v in fields.items()})
        now = time.monotonic()
        if self.path and (force or now - self.last_write > 0.5):
            self.last_write = now
            try:
                tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}")
                tmp.write_text(json.dumps(self.snapshot(), default=str), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass
        if self.quiet:
            return
        if self.vt:
            if force or now - self.last_line > 0.25:
                self.last_line = now
                self._draw()
        elif force or now - self.last_line > 5.0:
            self.last_line = now
            self.stream.write(self._line() + "\n")
            self.stream.flush()

    def _vals(self) -> dict[str, str]:
        s = self.snapshot()

        def f(v: Any, nd: int = 4) -> str:
            if v is None:
                return "-"
            return f"{v:.{nd}f}" if isinstance(v, float) else str(v)

        pct = f"{100 * s['units_done'] / s['units_total']:.1f}%" if s["units_total"] else "-"
        return {"task": f(s.get("task")), "stock": f(s.get("stock")), "tf": f(s.get("timeframe")),
                "session": f(s.get("session")), "steps": f"{f(s.get('step'))}/{f(s.get('steps'))}",
                "units": f"{s['units_done']}/{s['units_total']} ({pct})", "model": f(s.get("model")),
                "h": f(s.get("horizon")), "epoch": f"{f(s.get('epoch'))}/{f(s.get('epochs'))}",
                "loss": f(s.get("train_loss")), "tacc": f(s.get("train_acc"), 3), "oos": f(s.get("oos_acc"), 3),
                "oosn": f(s.get("oos_n")), "trades": f(s.get("trades")), "net": f(s.get("net_return"), 4),
                "device": f(s.get("device")), "gpu": f(s.get("gpu_mem")), "el": fmt_secs(s["elapsed_s"]),
                "eta": fmt_secs(s.get("eta_s_estimate")), "warn": f(s.get("warnings")), "err": f(s.get("errors")),
                "note": f(s.get("note"))[:90], "eligible": f(s.get("eligible_references")),
                "evaluated": f(s.get("evaluated_rules")), "generated": f(s.get("generated_candidates")),
                "rejected": f(s.get("rejected_candidates")), "folds": f(s.get("completed_folds"))}

    def _line(self) -> str:
        v = self._vals()
        return (f"[{v['el']}] {v['task']} {v['stock']} {v['tf']} session {v['session']} step {v['steps']} "
                f"units {v['units']} model {v['model']} h={v['h']} epoch {v['epoch']} loss {v['loss']} "
                f"train_acc {v['tacc']} oos_acc {v['oos']} (n={v['oosn']}) trades {v['trades']} net {v['net']} "
                f"refs {v['eligible']} eval {v['evaluated']} gen {v['generated']} reject {v['rejected']} "
                f"folds {v['folds']} device {v['device']} {v['gpu']} eta~{v['eta']} warn {v['warn']} err {v['err']}")

    def _draw(self) -> None:
        v = self._vals()
        lines = [
            f"GPLM {v['task']:<10} elapsed {v['el']:>8}   remaining (estimate from throughput) {v['eta']}",
            f"  stock {v['stock']:<14} timeframe {v['tf']:<4} session {v['session']:<12} step {v['steps']}",
            f"  progress {v['units']:<24} model {v['model']:<9} horizon {v['h']:<4} epoch {v['epoch']}",
            f"  train loss {v['loss']:<10} train acc {v['tacc']:<7} sequential OOS acc {v['oos']} (n={v['oosn']})",
            f"  refs {v['eligible']:<6} evaluated {v['evaluated']:<6} generated {v['generated']:<6} rejected {v['rejected']:<6} folds {v['folds']}",
            f"  trades evaluated {v['trades']:<8} mean net return {v['net']:<9} device {v['device']} {v['gpu']}",
            f"  warnings {v['warn']}  errors {v['err']}  {v['note']}",
        ]
        out = ""
        if self.drawn:
            out += f"\x1b[{self.drawn}F"
        out += "".join(f"\x1b[2K{line[:160]}\n" for line in lines)
        self.drawn = len(lines)
        self.stream.write(out)
        self.stream.flush()

    def close(self, final_note: str | None = None) -> None:
        if final_note:
            self.state["note"] = final_note
        self.update(force=True)
        if not self.quiet and not self.vt:
            self.stream.write(self._line() + "\n")
