"""M7.2 — optional owner risk inputs and simple limit checks (PRD §7).

The owner may supply a local JSON file (``risk.file`` in the config, or ``gplm ... --risk FILE``)::

    {"as_of": "2026-11-19", "capital_inr": 1000000,
     "holdings": [{"symbol": "RELIANCE-EQ", "qty": 10, "avg_price": 2900, "sector": "Energy"}],
     "sectors": {"RELIANCE-EQ": "Energy"},
     "limits": {"max_position_pct": 10, "max_sector_pct": 30, "max_new_alerts": 5, "max_open_positions": 10}}

Missing inputs are **reported as missing** (``RISK_INPUT_MISSING:<field>``) and never treated as zero
exposure; a check that cannot run says so. No broker is contacted; holdings are what the owner typed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

RISK_CONTRACT = "stock-gplm.risk-inputs.m72.v1"


class RiskError(ValueError):
    pass


def load(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        raise RiskError(f"risk file not found: {p}")
    d = json.loads(p.read_text(encoding="utf-8-sig"))
    for h in d.get("holdings", []):
        if not h.get("symbol") or float(h.get("qty", 0)) < 0:
            raise RiskError(f"bad holding row {h}")
    return d


def holding(risk: Mapping[str, Any] | None, symbol: str) -> dict[str, Any] | None:
    if not risk:
        return None
    for h in risk.get("holdings", []):
        if str(h["symbol"]).upper() == symbol.upper():
            return dict(h)
    return None


def _sector(risk: Mapping[str, Any], symbol: str) -> str | None:
    h = holding(risk, symbol)
    return (h or {}).get("sector") or (risk.get("sectors") or {}).get(symbol)


def check(risk: Mapping[str, Any] | None, opp: Mapping[str, Any], accepted: Sequence[Mapping[str, Any]] = ()
          ) -> dict[str, Any]:
    """Blockers and notes for one BUY opportunity, given opportunities already accepted this run."""
    if opp.get("action") != "BUY":
        return {"blockers": [], "notes": [], "missing": []}
    if not risk:
        return {"blockers": [], "notes": ["no risk inputs supplied: exposure, capital and limits were not checked"],
                "missing": ["RISK_INPUT_MISSING:file"]}
    lim = risk.get("limits") or {}
    cap = risk.get("capital_inr")
    blockers, notes, missing = [], [], []
    sym = opp["symbol"]
    px = float(opp.get("trigger") or 0)
    hold = holding(risk, sym)
    if cap is None:
        missing.append("RISK_INPUT_MISSING:capital_inr")
    else:
        cap = float(cap)
        pos_val = float(hold["qty"]) * float(hold.get("avg_price") or px) if hold else 0.0
        if "max_position_pct" in lim and cap > 0 and pos_val / cap * 100 >= float(lim["max_position_pct"]):
            blockers.append(f"RISK_POSITION_LIMIT:{sym} already {pos_val / cap * 100:.1f}% of capital")
        sec = _sector(risk, sym)
        if "max_sector_pct" in lim:
            if sec is None:
                missing.append(f"RISK_INPUT_MISSING:sector({sym})")
            elif cap > 0:
                sec_val = sum(float(h["qty"]) * float(h.get("avg_price") or 0) for h in risk.get("holdings", [])
                              if (h.get("sector") or (risk.get("sectors") or {}).get(h["symbol"])) == sec)
                if sec_val / cap * 100 >= float(lim["max_sector_pct"]):
                    blockers.append(f"RISK_SECTOR_LIMIT:{sec} at {sec_val / cap * 100:.1f}% of capital")
    if "max_new_alerts" in lim and len(accepted) >= int(lim["max_new_alerts"]):
        blockers.append(f"RISK_MAX_NEW_ALERTS:{lim['max_new_alerts']}")
    if "max_open_positions" in lim:
        n_open = sum(1 for h in risk.get("holdings", []) if float(h.get("qty", 0)) > 0)
        if n_open + len(accepted) >= int(lim["max_open_positions"]):
            blockers.append(f"RISK_MAX_OPEN_POSITIONS:{lim['max_open_positions']}")
    if any(a["symbol"] == sym for a in accepted):
        blockers.append("RISK_CONFLICT:another opportunity for this symbol was already accepted this run")
    if hold:
        notes.append(f"you hold {hold['qty']} @ {hold.get('avg_price')}")
    return {"blockers": blockers, "notes": notes, "missing": missing}


__all__ = ["RISK_CONTRACT", "RiskError", "check", "holding", "load"]
