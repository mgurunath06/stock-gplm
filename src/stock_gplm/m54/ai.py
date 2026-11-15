"""Optional AI hypothesis proposals. Advisory only; the local engine decides.

The prompt carries the *feature catalog* (names, families, kinds) and the
hypothesis grammar. It never carries prices, volumes, events or results unless the
user separately approves an upload. A reply is parsed into a
``stock-gplm.m54-hypotheses.v1`` file; unknown features and malformed conditions are
dropped and listed. Proposals then run through ``gplm discover --hypotheses`` and
count as tests like any other candidate.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .hypotheses import OPERATORS, Condition, Hypothesis


def build_prompt(catalog: Sequence[Mapping[str, Any]], symbol: str, max_hypotheses: int) -> str:
    lines = [f"Propose up to {max_hypotheses} long-side daily hypotheses for the NSE equity {symbol}.",
             "Use only these causal features (computed at the session close):"]
    lines += [f"- {c['key']} [{c['family']}, {c['kind']}]" for c in catalog]
    lines += ["Grammar: each hypothesis is an AND of 1-3 conditions {feature, op, threshold}.",
              f"op is one of {list(OPERATORS)}; 'is' (binary features) takes threshold null.",
              'Reply with JSON only: {"hypotheses": [{"rationale": "...", "conditions": [...]}]}.',
              "Do not claim results; the local engine will test every proposal."]
    return "\n".join(lines)


def parse_reply(text: str, known_features: set[str], source: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    rejected: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    if not match:
        return {"contract": "stock-gplm.m54-hypotheses.v1", "source": source, "hypotheses": [],
                "rejected": [{"reason": "no JSON object in reply"}], "authority": "ADVISORY_ONLY"}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"contract": "stock-gplm.m54-hypotheses.v1", "source": source, "hypotheses": [],
                "rejected": [{"reason": f"invalid JSON: {exc.msg}"}], "authority": "ADVISORY_ONLY"}
    for item in payload.get("hypotheses", [])[:50]:
        try:
            conds = [Condition(str(c["feature"]), str(c["op"]),
                               None if c.get("threshold") is None else float(c["threshold"])) for c in item["conditions"]]
            if not 1 <= len(conds) <= 3:
                raise ValueError("1-3 conditions required")
            unknown = [c.feature for c in conds if c.feature not in known_features]
            if unknown:
                raise ValueError(f"unknown features {unknown}")
            h = Hypothesis(tuple(conds), f"file:{source}")
            accepted.append({**{k: v for k, v in h.to_dict().items() if k in {"conditions"}},
                             "rationale": str(item.get("rationale", ""))[:500]})
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append({"item": str(item)[:300], "reason": str(exc)})
    return {"contract": "stock-gplm.m54-hypotheses.v1", "source": source, "hypotheses": accepted,
            "rejected": rejected, "authority": "ADVISORY_ONLY"}
