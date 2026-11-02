"""Strict, versioned adapters for manually downloaded NSE files.

Every adapter requires an exact header. The expected layouts are recorded from
NSE's published file formats but are UNCONFIRMED until a real file passes them;
a file that differs is refused with its observed header so a new versioned adapter
can be written. Nothing is coerced, filled or silently dropped: any row that cannot
be parsed is returned as a rejection, and a component with rejections is not used.

Corporate-action ratios are taken only from numbers stated in the NSE purpose text.
An action whose ratio is not stated, or whose price effect needs a policy (rights,
demergers, schemes, buybacks), is kept with an UNKNOWN or POLICY_REQUIRED status.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path

ADAPTER_VERSION = 1
CORPORATE_ACTIONS_HEADER = (
    "SYMBOL", "COMPANY NAME", "SERIES", "PURPOSE", "FACE VALUE", "EX-DATE", "RECORD DATE",
    "BOOK CLOSURE START DATE", "BOOK CLOSURE END DATE",
)
EQUITY_LIST_HEADER = (
    "SYMBOL", "NAME OF COMPANY", "SERIES", "DATE OF LISTING", "PAID UP VALUE", "MARKET LOT",
    "ISIN NUMBER", "FACE VALUE",
)
ETF_LIST_HEADER = ("Symbol", "Underlying", "SecurityName", "DateofListing", "MarketLot", "ISINNumber", "FaceValue")
SYMBOL_CHANGES_HEADER = ("SM_KEY_SYMBOL", "SM_NAME", "SM_APPLICABLE_FROM", "SM_NEW_SYMBOL")
TRANSCRIPTION_HEADER = ("date", "status", "label", "source_sha256")
LAYOUTS = {
    "nse-corporate-actions": CORPORATE_ACTIONS_HEADER,
    "nse-equity-list": EQUITY_LIST_HEADER,
    "nse-etf-list": ETF_LIST_HEADER,
    "nse-symbol-changes": SYMBOL_CHANGES_HEADER,
    "nse-calendar-transcription": TRANSCRIPTION_HEADER,
}
_ISIN = re.compile(r"^IN[A-Z0-9]{10}$")
_SYMBOL = re.compile(r"^[A-Z0-9&\-_.]{1,32}$")
_NUMBER = r"(\d+(?:\.\d+)?)"
_RUPEE = r"(?:rs|re|inr|₹)\.?\s*"


class LayoutError(ValueError):
    """The file is not the layout this adapter version accepts."""

    def __init__(self, component: str, observed: list[str]) -> None:
        self.observed = observed
        super().__init__(
            f"{component}: header {observed} does not match the V{ADAPTER_VERSION} layout "
            f"{list(LAYOUTS[component])}; supply the file so a versioned adapter can be added")


@dataclass
class Parsed:
    """Adapter output: accepted records plus every rejected row with its reason."""

    header: list[str]
    records: list[dict[str, object]] = field(default_factory=list)
    rejected: list[dict[str, object]] = field(default_factory=list)


def nse_date(value: str) -> date:
    """Parse NSE's DD-Mon-YYYY dates (e.g. 05-Jan-2024); anything else is refused."""
    try:
        return datetime.strptime(value, "%d-%b-%Y").date()
    except ValueError as exc:
        raise ValueError(f"date {value!r} is not DD-Mon-YYYY") from exc


def _rows(raw: bytes, component: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{component}: file is not UTF-8") from exc
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError(f"{component}: file is empty") from exc
    # Surrounding whitespace in header cells is normalised and the raw header kept in the report;
    # names, order and count must still match exactly.
    if tuple(cell.strip() for cell in header) != LAYOUTS[component]:
        raise LayoutError(component, header)
    rows = [(number, row) for number, row in enumerate(reader, start=2) if any(cell.strip() for cell in row)]
    return header, rows


def _fields(row: list[str], width: int) -> list[str]:
    if len(row) != width:
        raise ValueError(f"expected {width} fields, found {len(row)}")
    return [cell.strip() for cell in row]


def _symbol(value: str) -> str:
    if not _SYMBOL.fullmatch(value):
        raise ValueError(f"invalid NSE symbol {value!r}")
    return value


def _optional_date(value: str) -> str | None:
    return None if value in {"", "-"} else nse_date(value).isoformat()


# --------------------------------------------------------------------------- corporate actions

@dataclass(frozen=True)
class ParsedAction:
    action_type: str
    adjustment_status: str
    details: dict[str, str]


def _ratio_text(value: Fraction) -> str:
    """Exact ratio text such as 1/2 or 10; Fraction keeps it free of float rounding."""
    return str(value)


_POLICY_TERMS = (
    ("DEMERGER", r"de-?merger|spin[- ]?off"),
    ("SCHEME_OF_ARRANGEMENT", r"scheme of arrangement|amalgamation|merger"),
    ("CAPITAL_REDUCTION", r"capital reduction|reduction of (?:share )?capital|return of capital"),
    ("BUYBACK", r"buy[- ]?back"),
)


def parse_purpose(text: str) -> list[ParsedAction]:
    """Classify an NSE purpose string into explicit actions; never invent a ratio."""
    lowered = " ".join(text.lower().split())
    actions: list[ParsedAction] = []
    for new, held in re.findall(r"\bbonus\b[^0-9:]*?(\d+)\s*:\s*(\d+)", lowered):
        new_n, held_n = int(new), int(held)
        if new_n == 0 or held_n == 0:
            actions.append(ParsedAction("BONUS", "UNKNOWN", {"ratio": "UNPARSED", "purpose": text}))
            continue
        factor = Fraction(held_n, new_n + held_n)
        actions.append(ParsedAction("BONUS", "UNADJUSTED", {
            "bonus_new_shares": str(new_n), "bonus_per_held_shares": str(held_n),
            "price_adjustment_factor": _ratio_text(factor), "ratio_source": "NSE_PURPOSE_TEXT",
        }))
    if "bonus" in lowered and not any(a.action_type == "BONUS" for a in actions):
        actions.append(ParsedAction("BONUS", "UNKNOWN", {"ratio": "UNPARSED", "purpose": text}))
    for kind, keyword in (("SPLIT", r"split|sub-?division"), ("CONSOLIDATION", r"consolidat")):
        if not re.search(keyword, lowered):
            continue
        match = re.search(
            rf"(?:{keyword}).*?from\s*{_RUPEE}{_NUMBER}.*?to\s*{_RUPEE}{_NUMBER}", lowered)
        old_fv, new_fv = (Fraction(match.group(1)), Fraction(match.group(2))) if match else (None, None)
        if not old_fv or not new_fv or old_fv == new_fv:
            actions.append(ParsedAction(kind, "UNKNOWN", {"ratio": "UNPARSED", "purpose": text}))
            continue
        actions.append(ParsedAction(kind, "UNADJUSTED", {
            "face_value_from": str(match.group(1)), "face_value_to": str(match.group(2)),
            "price_adjustment_factor": _ratio_text(new_fv / old_fv), "ratio_source": "NSE_PURPOSE_TEXT",
        }))
    if re.search(r"\brights?\b", lowered):
        match = re.search(rf"\brights?\b[^0-9:]*?(\d+)\s*:\s*(\d+)(?:.*?premium\s*(?:of\s*)?{_RUPEE}{_NUMBER})?",
                          lowered)
        details = {"purpose": text, "reason": "factor needs the cum-rights price and an approved policy"}
        if match:
            details |= {"rights_new_shares": match.group(1), "rights_per_held_shares": match.group(2)}
            if match.group(3):
                details["premium_inr"] = match.group(3)
        actions.append(ParsedAction("RIGHTS", "POLICY_REQUIRED", details))
    for kind, pattern in _POLICY_TERMS:
        if re.search(pattern, lowered):
            actions.append(ParsedAction(kind, "POLICY_REQUIRED", {"purpose": text}))
            break
    if "dividend" in lowered:
        amounts = re.findall(rf"{_RUPEE}{_NUMBER}", lowered)
        details = {"purpose": text, "price_treatment": "PRICE_RETURN_LABELS_EXCLUDE_DISTRIBUTIONS"}
        if amounts:
            details["amounts_inr"] = ";".join(amounts)
        actions.append(ParsedAction("DIVIDEND", "NOT_APPLICABLE", details))
    if re.search(r"general meeting|\bagm\b|\begm\b", lowered):
        actions.append(ParsedAction("MEETING", "NOT_APPLICABLE", {"purpose": text}))
    if not actions:
        actions.append(ParsedAction("UNRECOGNIZED", "UNKNOWN", {"purpose": text}))
    return actions


def parse_corporate_actions(raw: bytes) -> Parsed:
    component = "nse-corporate-actions"
    header, rows = _rows(raw, component)
    parsed = Parsed(header)
    seen: set[tuple[str, str, str, str]] = set()
    for number, row in rows:
        try:
            symbol, _name, series, purpose, _fv, ex_date, record_date, *_ = _fields(row, len(CORPORATE_ACTIONS_HEADER))
            symbol = _symbol(symbol)
            if not purpose:
                raise ValueError("empty PURPOSE")
            if ex_date in {"", "-"}:
                raise ValueError("missing EX-DATE")
            effective = nse_date(ex_date).isoformat()
            key = (symbol, series, effective, purpose)
            if key in seen:
                raise ValueError("duplicate row")
            seen.add(key)
            for action in parse_purpose(purpose):
                parsed.records.append({
                    "symbol": symbol, "series": series, "ex_date": effective,
                    "record_date": _optional_date(record_date), "purpose": purpose,
                    "action_type": action.action_type, "adjustment_status": action.adjustment_status,
                    "details": action.details,
                })
        except ValueError as exc:
            parsed.rejected.append({"line": number, "reason": str(exc), "row": row})
    return parsed


# --------------------------------------------------------------------------- listings and classes

def _isin(value: str) -> str:
    if not _ISIN.fullmatch(value):
        raise ValueError(f"invalid ISIN {value!r}")
    return value


def parse_equity_list(raw: bytes) -> Parsed:
    header, rows = _rows(raw, "nse-equity-list")
    parsed = Parsed(header)
    seen: set[tuple[str, str]] = set()
    for number, row in rows:
        try:
            symbol, name, series, listed, _paid, _lot, isin, _fv = _fields(row, len(EQUITY_LIST_HEADER))
            key = (_symbol(symbol), series)
            if not series or key in seen:
                raise ValueError("missing or duplicate SYMBOL/SERIES")
            seen.add(key)
            parsed.records.append({"symbol": symbol, "series": series, "name": name,
                                   "listing_date": nse_date(listed).isoformat(), "isin": _isin(isin)})
        except ValueError as exc:
            parsed.rejected.append({"line": number, "reason": str(exc), "row": row})
    return parsed


def parse_etf_list(raw: bytes) -> Parsed:
    header, rows = _rows(raw, "nse-etf-list")
    parsed = Parsed(header)
    seen: set[str] = set()
    for number, row in rows:
        try:
            symbol, underlying, name, listed, _lot, isin, _fv = _fields(row, len(ETF_LIST_HEADER))
            if _symbol(symbol) in seen:
                raise ValueError("duplicate Symbol")
            seen.add(symbol)
            parsed.records.append({"symbol": symbol, "underlying": underlying, "name": name,
                                   "listing_date": nse_date(listed).isoformat(), "isin": _isin(isin)})
        except ValueError as exc:
            parsed.rejected.append({"line": number, "reason": str(exc), "row": row})
    return parsed


def parse_symbol_changes(raw: bytes) -> Parsed:
    header, rows = _rows(raw, "nse-symbol-changes")
    parsed = Parsed(header)
    seen: set[tuple[str, str]] = set()
    for number, row in rows:
        try:
            old, name, applicable, new = _fields(row, len(SYMBOL_CHANGES_HEADER))
            old, new = _symbol(old), _symbol(new)
            if old == new:
                raise ValueError("old and new symbol are identical")
            effective = nse_date(applicable).isoformat()
            if (old, effective) in seen:
                raise ValueError("duplicate change of the same symbol on the same date")
            seen.add((old, effective))
            parsed.records.append({"old_symbol": old, "new_symbol": new, "name": name, "effective_date": effective})
        except ValueError as exc:
            parsed.rejected.append({"line": number, "reason": str(exc), "row": row})
    return parsed


# --------------------------------------------------------------------------- calendar transcription

TRANSCRIBED_STATUSES = frozenset({"MARKET_CLOSED", "SPECIAL_SESSION"})


def parse_calendar_transcription(raw: bytes, derived_from: list[str]) -> Parsed:
    """Dates a person transcribed from registered NSE circulars, each citing its source."""
    header, rows = _rows(raw, "nse-calendar-transcription")
    parsed = Parsed(header)
    seen: set[str] = set()
    for number, row in rows:
        try:
            day, status, label, source = _fields(row, len(TRANSCRIPTION_HEADER))
            day = date.fromisoformat(day).isoformat()
            if status not in TRANSCRIBED_STATUSES:
                raise ValueError(f"status must be one of {sorted(TRANSCRIBED_STATUSES)}")
            if not label:
                raise ValueError("label is required")
            if source not in derived_from:
                raise ValueError("source_sha256 is not listed in derived_from")
            if day in seen:
                raise ValueError("duplicate date")
            seen.add(day)
            parsed.records.append({"date": day, "status": status, "label": label, "source_sha256": source})
        except ValueError as exc:
            parsed.rejected.append({"line": number, "reason": str(exc), "row": row})
    return parsed


def parse(component: str, path: Path, derived_from: list[str] | None = None) -> Parsed:
    raw = Path(path).read_bytes()
    if component == "nse-corporate-actions":
        return parse_corporate_actions(raw)
    if component == "nse-equity-list":
        return parse_equity_list(raw)
    if component == "nse-etf-list":
        return parse_etf_list(raw)
    if component == "nse-symbol-changes":
        return parse_symbol_changes(raw)
    if component == "nse-calendar-transcription":
        return parse_calendar_transcription(raw, derived_from or [])
    raise ValueError(f"no V{ADAPTER_VERSION} adapter exists for {component}")
