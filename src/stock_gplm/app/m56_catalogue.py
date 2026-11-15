"""Extensible, versioned M5.6 reference catalogue (M5.6.1).

Three source formats are accepted:

``markdown-v1``
    The owner's original ``N. **Name:** explanation `rule` trailing`` lines inside
    ``### Category C: Title (50 Strategies)`` sections. The original 300 entries keep byte-identical
    definitions and digests.
``markdown-v2``
    An ``N. **Name:** explanation`` header line, followed by indented ``- **Candidate logic:**``
    and ``- **Data dependencies:**`` bullets. This format is used by the 301+ addendum.
``json-v2``
    The structured contract ``stock-gplm.reference-catalogue.v2``. It is used for supplements and
    for editable user copies (``kind: "user"``, ``USR-`` IDs). An optional ``definition`` holds a
    reviewed ``strategy.v3`` document. It is the only way, besides the reviewed translations in
    :mod:`stock_gplm.app.m56`, that an entry becomes executable.

Policy: the base layout (default 6 x 50) must be complete, and extension categories use global
numbers above the base total. The total must reach ``min_strategies``, which defaults to 300. That
number is a floor, not a ceiling. Every line that is not imported is reported with its line
number and kind. Nothing is dropped silently. Text is never evaluated as code, and descriptive entries are
never turned into executable logic.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .db import sha

TIMEFRAMES = ("1m", "5m", "15m", "30m", "60m", "1d", "1w")
INTRADAY = ("1m", "5m", "15m", "30m", "60m")
JSON_CONTRACT = "stock-gplm.reference-catalogue.v2"
ERROR_KINDS = ("duplicate", "rejected", "unparseable", "unsupported_field", "missing")
INFO_KINDS = ("ignored_outside_category", "ignored_prose")

_CATEGORY = re.compile(r"^### Category (\d+): (.+?)(?: \((\d+) Strategies\))?\s*$")
_ITEM_V1 = re.compile(r"^(\d+)\. \*\*(.+?):\*\*\s*(.*?)\s*`([^`]+)`(?:\s*(.*))?$")
_ITEM_V2 = re.compile(r"^(\d+)\. \*\*(.+?):\*\*\s*(.*)$")
_NUMBERED = re.compile(r"^\d+\.")
_BULLET = re.compile(r"^\s+[-*] \*\*(.+?):\*\*\s*(.*)$")
_KNOWN_FIELDS = {"candidate logic": "rule_text", "data dependencies": "data_dependencies",
                 "timeframes": "timeframes", "direction": "direction"}
# v2 fields are left out of the digest while they hold their defaults, so v1 digests stay stable.
_V2_DEFAULTS: dict[str, Any] = {"data_dependencies": "", "source_format": "markdown-v1", "definition": None,
                                "extra_fields": {}, "origin": "REFERENCE", "derived_from": None}


@dataclass(frozen=True)
class Reference:
    reference_id: str
    ordinal: int
    category_number: int
    category: str
    name: str
    explanation: str
    rule_text: str
    trailing_text: str
    direction: str
    supported_timeframes: tuple[str, ...]
    required_inputs: tuple[str, ...]
    implementation_status: str
    implementation_reason: str
    parameter_ranges: Mapping[str, Any]
    version: int = 1
    data_dependencies: str = ""
    source_format: str = "markdown-v1"
    definition: Mapping[str, Any] | None = None
    extra_fields: Mapping[str, str] = field(default_factory=dict)
    origin: str = "REFERENCE"
    derived_from: Mapping[str, Any] | None = None

    @property
    def digest(self) -> str:
        payload = asdict(self)
        for key, default in _V2_DEFAULTS.items():
            if payload[key] == default:
                payload.pop(key)
        return sha(payload)


@dataclass(frozen=True)
class CataloguePolicy:
    """Catalogue shape rules. ``min_strategies`` is a floor, never a ceiling."""

    min_strategies: int = 300
    max_strategies: int | None = None
    base_categories: int = 6
    base_items_per_category: int = 50
    on_issue: str = "reject"  # "reject" fails the run; "quarantine" proceeds with imported entries only

    @classmethod
    def from_cfg(cls, cfg: Mapping[str, Any] | None) -> "CataloguePolicy":
        c = (cfg or {}).get("catalogue") or {}
        on_issue = str(c.get("on_issue", "reject"))
        if on_issue not in ("reject", "quarantine"):
            raise ValueError(f"m56.catalogue.on_issue must be reject or quarantine, not {on_issue!r}")
        mx = c.get("max_strategies")
        return cls(int(c.get("min_strategies", 300)), None if mx in (None, "") else int(mx),
                   int(c.get("base_categories", 6)), int(c.get("base_items_per_category", 50)), on_issue)

    @property
    def base_total(self) -> int:
        return self.base_categories * self.base_items_per_category

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CatalogueParse:
    references: list[Reference]
    issues: list[dict[str, Any]]
    policy: CataloguePolicy
    sources: list[dict[str, Any]]

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [i for i in self.issues if i["kind"] in ERROR_KINDS]

    def report(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for issue in self.issues:
            by_kind[issue["kind"]] = by_kind.get(issue["kind"], 0) + 1
        cats: dict[str, int] = {}
        fmts: dict[str, int] = {}
        for r in self.references:
            key = f"{r.category_number}: {r.category}"
            cats[key] = cats.get(key, 0) + 1
            fmts[r.source_format] = fmts.get(r.source_format, 0) + 1
        return {"policy": self.policy.to_dict(), "sources": self.sources, "imported": len(self.references),
                "base_imported": sum(1 for r in self.references if r.category_number <= self.policy.base_categories
                                     and r.origin == "REFERENCE"),
                "extension_imported": sum(1 for r in self.references if r.category_number > self.policy.base_categories
                                          and r.origin == "REFERENCE"),
                "user_imported": sum(1 for r in self.references if r.origin == "USER"),
                "by_category": dict(sorted(cats.items(), key=lambda kv: int(kv[0].split(":")[0]))),
                "by_format": fmts,
                "duplicate": by_kind.get("duplicate", 0), "rejected": by_kind.get("rejected", 0),
                "missing": by_kind.get("missing", 0), "unparseable": by_kind.get("unparseable", 0),
                "unsupported_field": by_kind.get("unsupported_field", 0),
                "ignored_outside_category": by_kind.get("ignored_outside_category", 0),
                "ignored_prose": by_kind.get("ignored_prose", 0),
                "error_count": len(self.errors), "status": "CLEAN" if not self.errors else "ISSUES",
                "issues": self.issues}


class CatalogueError(ValueError):
    def __init__(self, message: str, parsed: CatalogueParse):
        super().__init__(message)
        self.parsed = parsed


# --------------------------------------------------------------------------- requirements / scope


def requirements(text: str) -> tuple[str, ...]:
    """Keyword inputs of the original v1 rules. Unchanged so that v1 digests stay stable."""
    s = text.lower()
    req = {"OHLCV"}
    checks = {
        "DELIVERY": ("delivery",),
        "DERIVATIVES_OI": ("open interest", " oi", "oi_", "futures"),
        "OPTIONS_CHAIN": ("pcr", "max pain", "iv ", "implied volatility"),
        "INDEX_BENCHMARK": ("nifty", "banknifty", "sector", "relative strength", "benchmark"),
        "CORPORATE_EVENT": ("earnings", "quarterly result", "corporate action", "dividend"),
        "INSTITUTIONAL_FLOW": ("fii", "dii", "institutional cash"),
        "FREE_FLOAT": ("free-float", "free float"),
        "MARKET_CALENDAR": ("expiry", "month-end", "day-of-week", "seasonal", "holiday"),
        "ORDER_BOOK_OR_DEALS": ("block deal", "bulk deal", "order book"),
    }
    for key, words in checks.items():
        if any(w in s for w in words):
            req.add(key)
    if re.search(r"\boptions?\b", s):
        req.add("OPTIONS_CHAIN")
    return tuple(sorted(req))


def timeframes_v1(text: str) -> tuple[str, ...]:
    s = text.lower()
    if any(x in s for x in ("9:15", "9:30", "15:30", "intraday", "opening range", "first-hour", "last-hour")):
        return INTRADAY
    if any(x in s for x in ("52-week", "252", "day", "session")):
        return ("1d",)
    if any(x in s for x in ("quarter", "month", "weekly", "week")):
        return ("1d", "1w")
    return TIMEFRAMES


# Ordered: the first matching phrase classifies a dependency token. Unmatched tokens become
# UNREGISTERED:<token>, so an unknown input blocks evaluation instead of being treated as OHLCV.
_DEPENDENCY_MAP: tuple[tuple[str, str | None], ...] = (
    ("pre-open", "PRE_OPEN_AUCTION"),
    ("expiry calendar", "DERIVATIVES_EXPIRY_CALENDAR"),
    ("holiday", "MARKET_CALENDAR"), ("trading calendar", "MARKET_CALENDAR"),
    ("session calendar", "MARKET_CALENDAR"),
    ("industry membership", "POINT_IN_TIME_MEMBERSHIP"), ("index membership", "POINT_IN_TIME_MEMBERSHIP"),
    ("constituent", "POINT_IN_TIME_MEMBERSHIP"), ("point-in-time", "POINT_IN_TIME_MEMBERSHIP"),
    ("pair universe", "PAIR_UNIVERSE"),
    ("sector-index", "INDEX_BENCHMARK"), ("index prices", "INDEX_BENCHMARK"), ("nifty", "INDEX_BENCHMARK"),
    ("traded value", "TRADED_VALUE"),
    ("fitted hmm", "MODEL_ARTIFACT"),
    ("risk constraints", "RISK_POLICY"),
    ("transaction costs", "COST_MODEL"),
    ("feature matrix", "RESEARCH_METHOD_INPUTS"), ("walk-forward folds", "RESEARCH_METHOD_INPUTS"),
    ("ast evaluator", "RESEARCH_METHOD_INPUTS"), ("labelled horizons", "RESEARCH_METHOD_INPUTS"),
    ("computed features", "RESEARCH_METHOD_INPUTS"), ("training folds", "RESEARCH_METHOD_INPUTS"),
    ("feature vectors", "RESEARCH_METHOD_INPUTS"), ("forward labels", "RESEARCH_METHOD_INPUTS"),
    ("primary signals", "RESEARCH_METHOD_INPUTS"), ("train-only outcomes", "RESEARCH_METHOD_INPUTS"),
    ("validated strategy outputs", "RESEARCH_METHOD_INPUTS"), ("residual/spread series", "RESEARCH_METHOD_INPUTS"),
    ("ohlcv", "OHLCV"), ("daily close", "OHLCV"), ("daily stock", "OHLCV"), ("minute", "OHLCV"),
    ("contiguous sessions", None), ("historical time slots", None), ("optionally minute", None),
)
# COST_MODEL is satisfied by the configured cost section. All other non-OHLCV inputs need a registered adapter.
CONFIG_SATISFIED = {"COST_MODEL"}


def dependency_inputs(deps: str) -> tuple[str, ...]:
    out: set[str] = set()
    for token in re.split(r"[;+]|\band\b|,", deps.lower()):
        t = token.strip(" .")
        if not t:
            continue
        for phrase, key in _DEPENDENCY_MAP:
            if phrase in t:
                if key:
                    out.add(key)
                break
        else:
            out.add("UNREGISTERED:" + re.sub(r"[^a-z0-9]+", "_", t).strip("_"))
    return tuple(sorted(out))


def timeframes_from_dependencies(deps: str) -> tuple[str, ...]:
    s = deps.lower()
    minute, daily = "minute" in s, "daily" in s
    if minute and daily:
        return TIMEFRAMES
    if minute:
        return INTRADAY
    if daily:
        return ("1d", "1w")
    return TIMEFRAMES


def _direction(text: str) -> str:
    return "short" if any(x in text.lower() for x in (" short", "bearish", "death cross")) else "long"


def _strip_code(text: str) -> str:
    t = text.strip()
    return t[1:-1] if len(t) >= 2 and t[0] == "`" and t[-1] == "`" and t.count("`") == 2 else t


# --------------------------------------------------------------------------- markdown


class _Builder:
    def __init__(self, policy: CataloguePolicy, source: str):
        self.policy = policy
        self.source = source
        self.refs: list[Reference] = []
        self.issues: list[dict[str, Any]] = []
        self.seen_ids: set[str] = set()
        self.seen_numbers: dict[int, str] = {}
        self.category_counts: dict[int, int] = {}
        self.declared: dict[int, tuple[int, int]] = {}  # category -> (declared count, line)

    def issue(self, kind: str, line: int | None, text: str, reason: str) -> None:
        self.issues.append({"kind": kind, "source": self.source, "line": line, "text": text[:200], "reason": reason})

    def accept(self, ref: Reference, line: int | None, text: str) -> bool:
        if ref.reference_id in self.seen_ids:
            self.issue("duplicate", line, text, f"duplicate reference id {ref.reference_id}; first occurrence kept")
            return False
        self.seen_ids.add(ref.reference_id)
        self.refs.append(ref)
        self.category_counts[ref.category_number] = self.category_counts.get(ref.category_number, 0) + 1
        return True

    def number_ok(self, category_no: int, number: int, line: int, text: str) -> tuple[str, int] | None:
        p = self.policy
        if category_no <= p.base_categories:
            if not 1 <= number <= p.base_items_per_category:
                self.issue("rejected", line, text, f"category {category_no} has item {number}, expected "
                                                   f"1..{p.base_items_per_category}")
                return None
            return f"REF-C{category_no}-{number:03d}", (category_no - 1) * p.base_items_per_category + number
        if number <= p.base_total:
            self.issue("rejected", line, text, f"extension category {category_no} must use global numbers above "
                                               f"{p.base_total}; got {number}")
            return None
        if number in self.seen_numbers:
            self.issue("duplicate", line, text, f"global number {number} already used by {self.seen_numbers[number]}")
            return None
        rid = f"REF-C{category_no}-{number:03d}"
        self.seen_numbers[number] = rid
        return rid, number


def _v2_reference(rid: str, ordinal: int, category_no: int, category: str, name: str, explanation: str,
                  fields: Mapping[str, str], extras: Mapping[str, str], fmt: str,
                  definition: Mapping[str, Any] | None = None, origin: str = "REFERENCE",
                  derived_from: Mapping[str, Any] | None = None, version: int = 1,
                  explicit_tfs: Sequence[str] | None = None, explicit_direction: str | None = None) -> Reference:
    rule = _strip_code(fields.get("rule_text", ""))
    deps = fields.get("data_dependencies", "").strip()
    combined = " ".join((name, explanation, rule))
    dep_inputs = dependency_inputs(deps) if deps else ()
    req = tuple(sorted((set(requirements(combined)) | set(dep_inputs)) - CONFIG_SATISFIED))
    tf_text = fields.get("timeframes")
    if explicit_tfs:
        tfs = tuple(t for t in TIMEFRAMES if t in set(explicit_tfs))
    elif tf_text:
        wanted = {t.strip() for t in tf_text.replace("`", "").split(",")}
        tfs = tuple(t for t in TIMEFRAMES if t in wanted)
    elif deps:
        tfs = timeframes_from_dependencies(deps)
    else:
        tfs = timeframes_v1(combined)
    direction = explicit_direction or fields.get("direction", "").strip().lower() or _direction(combined)
    if definition is not None:
        status, reason = "STRUCTURED_DEFINITION", "reviewed strategy.v3 definition supplied with the entry"
    elif "RESEARCH_METHOD_INPUTS" in req:
        status, reason = "METHOD_SPECIFICATION", ("describes a discovery or modelling method, not an executable "
                                                  "rule; no logic is invented for it")
    elif not rule:
        status, reason = "DESCRIPTION_ONLY", "no candidate logic supplied; descriptive text is never coerced"
    else:
        status, reason = "PENDING_COMPILATION", "safe declarative translation not yet selected"
    return Reference(rid, ordinal, category_no, category, name, explanation, rule, "", direction, tfs, req,
                     status, reason, {}, version, deps, fmt, definition, dict(extras), origin, derived_from)


def _parse_markdown(path: Path, policy: CataloguePolicy, b: _Builder) -> None:
    text = path.read_text(encoding="utf-8-sig")
    category_no, category, within = 0, "", False
    pending: dict[str, Any] | None = None  # current v2 item that is still collecting bullets

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        p = pending
        pending = None
        ref = _v2_reference(p["rid"], p["ordinal"], p["category_no"], p["category"], p["name"], p["explanation"],
                            p["fields"], p["extras"], "markdown-v2")
        if b.accept(ref, p["line"], p["text"]):
            for key, (value, line) in p["extras_lines"].items():
                b.issue("unsupported_field", line, f"{key}: {value}",
                        f"unknown field {key!r} on {ref.reference_id}; kept in extra_fields, not interpreted")

    for lineno, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        cm = _CATEGORY.match(s)
        if cm:
            flush()
            category_no, category, within = int(cm.group(1)), cm.group(2), True
            if category_no in b.declared or category_no in b.category_counts:
                b.issue("duplicate", lineno, s, f"category {category_no} header repeated")
            if cm.group(3):
                b.declared[category_no] = (int(cm.group(3)), lineno)
            continue
        if s.startswith("### Category"):
            flush()
            within = False
            b.issue("unparseable", lineno, s, "category header does not match '### Category N: Title [(K Strategies)]'")
            continue
        if s.startswith("#"):
            flush()
            within = False
            continue
        if not within:
            if _NUMBERED.match(s):
                b.issue("ignored_outside_category", lineno, s, "numbered line outside a category section "
                                                               "(taxonomy or prose); not a strategy")
            continue
        bm = _BULLET.match(raw)
        if bm and pending is not None:
            key = bm.group(1).strip().lower()
            if key in _KNOWN_FIELDS:
                pending["fields"][_KNOWN_FIELDS[key]] = bm.group(2).strip()
            else:
                pending["extras"][bm.group(1).strip()] = bm.group(2).strip()
                pending["extras_lines"][bm.group(1).strip()] = (bm.group(2).strip(), lineno)
            continue
        if bm:
            b.issue("unparseable", lineno, s, "field bullet without a preceding strategy header")
            continue
        if not s or s == "---":
            continue
        m1 = _ITEM_V1.match(s)
        if m1:
            flush()
            number, name, explanation, rule, trailing = int(m1.group(1)), *m1.groups()[1:]
            ident = b.number_ok(category_no, number, lineno, s)
            if ident is None:
                continue
            combined = " ".join((name, explanation, rule, trailing or ""))
            b.accept(Reference(ident[0], ident[1], category_no, category, name, explanation, rule, trailing or "",
                               _direction(combined), timeframes_v1(combined), requirements(combined),
                               "PENDING_COMPILATION", "safe declarative translation not yet selected", {}, 1),
                     lineno, s)
            continue
        m2 = _ITEM_V2.match(s)
        if m2:
            flush()
            number = int(m2.group(1))
            ident = b.number_ok(category_no, number, lineno, s)
            if ident is None:
                continue
            pending = {"rid": ident[0], "ordinal": ident[1], "category_no": category_no, "category": category,
                       "name": m2.group(2), "explanation": m2.group(3).strip(), "fields": {}, "extras": {},
                       "extras_lines": {}, "line": lineno, "text": s}
            continue
        if _NUMBERED.match(s):
            flush()
            b.issue("unparseable", lineno, s, "numbered line inside a category matches neither the v1 nor the v2 "
                                              "item grammar")
            continue
        b.issue("ignored_prose", lineno, s, "non-item prose inside a category section")
    flush()


# --------------------------------------------------------------------------- structured json

_JSON_REQUIRED = ("reference_id", "category_number", "category", "name")
_JSON_KNOWN = set(_JSON_REQUIRED) | {"number", "explanation", "rule_text", "data_dependencies", "version",
                                     "direction", "timeframes", "definition", "derived_from", "notes"}


def _parse_json(path: Path, policy: CataloguePolicy, b: _Builder) -> None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        b.issue("unparseable", None, path.name, f"invalid JSON: {exc}")
        return
    if not isinstance(doc, dict) or doc.get("contract") != JSON_CONTRACT or not isinstance(doc.get("strategies"), list):
        b.issue("unparseable", None, path.name, f"structured catalogue must have contract {JSON_CONTRACT} "
                                                "and a strategies list")
        return
    kind = doc.get("kind", "reference")
    if kind not in ("reference", "user"):
        b.issue("unparseable", None, path.name, f"unknown catalogue kind {kind!r}")
        return
    from .library import validate

    for idx, entry in enumerate(doc["strategies"]):
        where = f"strategies[{idx}]"
        if not isinstance(entry, dict) or any(k not in entry for k in _JSON_REQUIRED):
            b.issue("unparseable", None, where, f"entry must be an object with {', '.join(_JSON_REQUIRED)}")
            continue
        rid, cat_no = str(entry["reference_id"]), entry["category_number"]
        if not isinstance(cat_no, int):
            b.issue("unparseable", None, where, "category_number must be an integer")
            continue
        if kind == "user":
            if not rid.startswith("USR-"):
                b.issue("rejected", None, where, f"user copies need a USR- id; {rid} would shadow an immutable reference")
                continue
            ordinal = 0
        else:
            number = entry.get("number")
            if not isinstance(number, int):
                b.issue("unparseable", None, where, "reference entries need an integer number")
                continue
            ident = b.number_ok(cat_no, number, 0, where)
            if ident is None:
                continue
            if ident[0] != rid:
                b.issue("rejected", None, where, f"reference_id {rid} does not match its number ({ident[0]})")
                continue
            ordinal = ident[1]
        extras = {k: json.dumps(v, sort_keys=True) for k, v in entry.items() if k not in _JSON_KNOWN}
        definition = entry.get("definition")
        if definition is not None:
            try:
                validate({**definition, "timeframe": definition.get("timeframe", "1d")})
            except Exception as exc:  # validation errors are reported, never repaired
                b.issue("rejected", None, where, f"{rid}: definition fails strategy.v3 validation: {exc}")
                continue
        fields = {"rule_text": str(entry.get("rule_text", "")), "data_dependencies": str(entry.get("data_dependencies", ""))}
        tfs = entry.get("timeframes")
        bad_tf = sorted(set(tfs or []) - set(TIMEFRAMES))
        if bad_tf:
            b.issue("rejected", None, where, f"{rid}: unsupported timeframes {bad_tf}")
            continue
        ref = _v2_reference(rid, ordinal, cat_no, str(entry["category"]), str(entry["name"]),
                            str(entry.get("explanation", "")), fields, extras, "json-v2", definition,
                            "USER" if kind == "user" else "REFERENCE", entry.get("derived_from"),
                            int(entry.get("version", 1)), tfs, entry.get("direction"))
        if b.accept(ref, None, where):
            for key in extras:
                b.issue("unsupported_field", None, where, f"unknown field {key!r} on {rid}; kept, not interpreted")


# --------------------------------------------------------------------------- entry points


def parse_catalogue(path: Path, policy: CataloguePolicy | None = None,
                    supplements: Sequence[Path] = ()) -> CatalogueParse:
    """Parse the main catalogue plus supplements and report every issue. Never raises on content."""
    policy = policy or CataloguePolicy()
    all_refs: list[Reference] = []
    issues: list[dict[str, Any]] = []
    sources = []
    shared: _Builder | None = None
    for src in (Path(path), *map(Path, supplements)):
        b = _Builder(policy, src.name)
        if shared is not None:  # IDs and global numbers are unique across the whole bundle
            b.seen_ids, b.seen_numbers, b.category_counts = shared.seen_ids, shared.seen_numbers, shared.category_counts
        if src.suffix.lower() == ".json":
            _parse_json(src, policy, b)
        else:
            _parse_markdown(src, policy, b)
        sources.append({"path": str(src.resolve()), "sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
                        "format": "json" if src.suffix.lower() == ".json" else "markdown",
                        "imported": len(b.refs)})
        for cat, (declared, line) in b.declared.items():
            got = sum(1 for r in b.refs if r.category_number == cat)
            if got != declared:
                b.issue("rejected", line, f"Category {cat}", f"header declares {declared} strategies; {got} imported")
        all_refs.extend(b.refs)
        issues.extend(b.issues)
        shared = b
    base_expected = {f"REF-C{c}-{i:03d}" for c in range(1, policy.base_categories + 1)
                     for i in range(1, policy.base_items_per_category + 1)}
    have = {r.reference_id for r in all_refs}
    missing = sorted(base_expected - have)
    if missing:
        issues.append({"kind": "missing", "source": Path(path).name, "line": None, "text": ", ".join(missing[:20]),
                       "reason": f"reference catalogue must contain {policy.base_total} base strategies "
                                 f"({policy.base_categories}x{policy.base_items_per_category}); missing {len(missing)}"})
    if len(all_refs) < policy.min_strategies:
        issues.append({"kind": "missing", "source": Path(path).name, "line": None, "text": str(len(all_refs)),
                       "reason": f"catalogue holds {len(all_refs)} strategies; the floor is {policy.min_strategies}"})
    if policy.max_strategies is not None and len(all_refs) > policy.max_strategies:
        issues.append({"kind": "rejected", "source": Path(path).name, "line": None, "text": str(len(all_refs)),
                       "reason": f"catalogue holds {len(all_refs)} strategies; configured maximum is "
                                 f"{policy.max_strategies}"})
    all_refs.sort(key=lambda r: (r.origin != "REFERENCE", r.ordinal, r.reference_id))
    return CatalogueParse(all_refs, issues, policy, sources)


def load_catalogue(path: Path, policy: CataloguePolicy | None = None,
                   supplements: Sequence[Path] = ()) -> CatalogueParse:
    """Parse and enforce the policy. ``reject`` raises :class:`CatalogueError` holding the full report."""
    parsed = parse_catalogue(path, policy, supplements)
    if parsed.errors and parsed.policy.on_issue == "reject":
        first = next((e for e in parsed.errors if e["kind"] == "missing"), parsed.errors[0])
        raise CatalogueError(f"{first['reason']} ({len(parsed.errors)} catalogue issue(s); "
                             f"see 'gplm m56-catalogue' for the full report)", parsed)
    return parsed


def export_user_copy(refs: Sequence[Reference], output: Path, source_sha256: str) -> dict[str, Any]:
    """Write an editable structured copy. Originals stay immutable, and an existing copy is never overwritten."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite an existing user copy: {output}")
    rows = []
    for r in refs:
        if r.origin != "REFERENCE":
            continue
        rows.append({"reference_id": "USR-" + r.reference_id, "category_number": r.category_number,
                     "category": r.category, "name": r.name, "explanation": r.explanation,
                     "rule_text": r.rule_text if not r.trailing_text else f"{r.rule_text} {r.trailing_text}".strip(),
                     "data_dependencies": r.data_dependencies, "version": 1, "direction": r.direction,
                     "timeframes": list(r.supported_timeframes),
                     "derived_from": {"reference_id": r.reference_id, "sha256": r.digest,
                                      "catalogue_sha256": source_sha256}})
    doc = {"contract": JSON_CONTRACT, "kind": "user", "catalogue_version": 1,
           "note": "Editable user copy. IDs carry USR-; originals are immutable. Add a reviewed strategy.v3 "
                   "'definition' to make an entry executable. Prose is never executed.",
           "strategies": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(output), "strategies": len(rows)}


__all__ = ["CatalogueError", "CatalogueParse", "CataloguePolicy", "JSON_CONTRACT", "Reference", "TIMEFRAMES",
           "dependency_inputs", "export_user_copy", "load_catalogue", "parse_catalogue", "requirements",
           "timeframes_v1"]
