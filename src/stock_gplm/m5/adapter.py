"""Historical NSE daily eligibility adapter over published Shoonya segments.

The adapter is the only component that releases historical bars to strategy
research. Publication integrity reuses :func:`stock_gplm.ingestion.daily.validate_daily`
(the M1/M2.4 ``.done``-marker, SHA-256, byte/record-count and ``ShoonyaDailyV1``
contract). Historical eligibility is a separate decision layered on top:
identity, locked-holdout isolation, calendar, point-in-time membership, aliases
and corporate actions each need explicit evidence. A valid candle file alone
never proves that an instrument belonged to the universe on a date.
"""
from __future__ import annotations

import fcntl
import re
from dataclasses import asdict
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

from stock_gplm.ingestion.daily import MAX_SIDECAR
from stock_gplm.m39b import (
    CorporateActionEvent,
    EventType,
    LicenceStatus,
    RawCandle,
    ValidationState,
    bonus_factor,
    detect_suspicious_discontinuities,
    split_factor,
)

from .evidence import (
    ReferenceEvidence,
    evidence_digest,
    evidence_issued,
    file_sha256,
    load_reference_evidence,
    read_json,
)
from .issuance import _issues
from .holdout_validation import validate_daily_for_development
from .models import (
    ADAPTER_VERSION,
    CanonicalDailyBar,
    DataEligibilityDecision,
    DataEligibilityRequest,
    DecisionStatus,
    EligibilityEvidence,
    EligibilityFailure,
    EligibilityMode,
    FailureClass,
    StaleAssessmentError,
    UniverseAssessment,
    UniverseRequest,
    VerifiedHistoricalDataset,
    bars_hash,
)

SUPPORTED_SERIES = frozenset({"EQ"})
LOCK_NAME = ".stock-gplm-m24.lock"
_SEGMENT = re.compile(r"^(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})(?P<suffix>\.json\.gz|\.json\.gz\.done|\.meta\.json)$")
# M2.1 catalog action_type -> M3.9-B event type. These aliases are the only normalization
# between the two contracts (documented in docs/m5-historical-data-adapter.md).
_SHARE_EVENTS = {"SPLIT": EventType.STOCK_SPLIT, "STOCK_SPLIT": EventType.STOCK_SPLIT,
                 "REVERSE_SPLIT": EventType.REVERSE_SPLIT, "CONSOLIDATION": EventType.REVERSE_SPLIT,
                 "BONUS": EventType.BONUS}
_PRICE_RETURN_EXCLUDED = frozenset({"DIVIDEND", "CASH_DIVIDEND"})
_DETAIL_KEYS = {"share": frozenset({"ratio", "record_date", "event_id"}),
                "dividend": frozenset({"amount_inr", "record_date", "event_id"})}
_RATIO = re.compile(r"^([1-9][0-9]{0,8}):([1-9][0-9]{0,8})$")
# Adjustment statuses that agree with a verified RAW_UNADJUSTED provider series.
_RAW_CONSISTENT = {"share": frozenset({"UNADJUSTED"}), "dividend": frozenset({"UNADJUSTED", "NOT_APPLICABLE"})}


def publication_lock_active(raw_root: Path) -> bool:
    """Same probe as M3 readiness: a held or symlinked M2.4 lock blocks reads."""
    lock = Path(raw_root) / LOCK_NAME
    if lock.is_symlink():
        return True
    if not lock.exists():
        return False
    try:
        with lock.open("r", encoding="utf-8") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def _segments(symbol_dir: Path) -> tuple[dict[str, dict[str, Path]], list[str]]:
    groups: dict[str, dict[str, Path]] = {}
    unexpected: list[str] = []
    for path in sorted(symbol_dir.iterdir()):
        match = _SEGMENT.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            unexpected.append(path.name)
            continue
        groups.setdefault(f"{match['start']}_{match['end']}", {})[match["suffix"]] = path
    return groups, unexpected


def _reconcile_details(kind: str, details: dict[str, str], event: CorporateActionEvent) -> list[tuple[str, str]]:
    """Field-by-field reconciliation of an M2.1 action's details with its M3.9-B event.

    Returns (failure code, message) pairs; empty means every applicable field agrees.
    Ratios compare as reduced fractions (``4:2`` equals ``2:1``); nothing else is
    coerced. SPLIT/REVERSE_SPLIT ratios are new:old and BONUS ratios are bonus:held,
    the same orientation as M3.9-B ``new_shares:base_shares``.
    """
    problems: list[tuple[str, str]] = []
    unsupported = sorted(set(details) - _DETAIL_KEYS[kind])
    if unsupported:
        problems.append(("CORPORATE_ACTION_DETAILS_UNSUPPORTED", f"unsupported details keys {unsupported}"))
    if kind == "share":
        ratio = details.get("ratio")
        match = _RATIO.fullmatch(ratio) if isinstance(ratio, str) else None
        if ratio is None:
            problems.append(("CORPORATE_ACTION_FIELDS_MISSING", "catalog details lack the required ratio"))
        elif match is None:
            problems.append(("CORPORATE_ACTION_FIELDS_MISSING", f"catalog ratio {ratio!r} is not A:B"))
        elif event.new_shares is None or event.base_shares is None:
            problems.append(("CORPORATE_ACTION_FIELDS_MISSING", f"event {event.event_id} lacks a share ratio"))
        elif Fraction(int(match[1]), int(match[2])) != Fraction(event.new_shares, event.base_shares):
            problems.append(("CORPORATE_ACTION_CONTRADICTION", f"catalog ratio {ratio} differs from event "
                             f"{event.event_id} ratio {event.new_shares}:{event.base_shares}"))
    if "amount_inr" in details:
        if event.cash_amount_inr is None:
            problems.append(("CORPORATE_ACTION_FIELDS_MISSING", f"event {event.event_id} lacks cash_amount_inr"))
        elif str(details["amount_inr"]) != str(event.cash_amount_inr):
            problems.append(("CORPORATE_ACTION_CONTRADICTION", f"dividend {details['amount_inr']} differs from "
                             f"event {event.event_id} amount {event.cash_amount_inr}"))
    if "record_date" in details:
        if event.record_date is None:
            problems.append(("CORPORATE_ACTION_FIELDS_MISSING", f"event {event.event_id} lacks record_date"))
        elif details["record_date"] != event.record_date:
            problems.append(("CORPORATE_ACTION_CONTRADICTION", f"record date {details['record_date']} differs "
                             f"from event {event.event_id} record date {event.record_date}"))
    if "event_id" in details and details["event_id"] != event.event_id:
        problems.append(("CORPORATE_ACTION_CONTRADICTION", f"catalog names event {details['event_id']}, "
                         f"matched {event.event_id}"))
    return problems


def _share_factor(event: CorporateActionEvent) -> Fraction:
    assert event.new_shares is not None and event.base_shares is not None
    if event.event_type == EventType.BONUS:
        return bonus_factor(bonus_shares=event.new_shares, held_shares=event.base_shares)
    return split_factor(new_shares=event.new_shares, old_shares=event.base_shares)


# M5.1.1 strict authority policy. Markers that identify synthetic, fixture or test evidence.
SYNTHETIC_MARKERS = ("synthetic", "fixture", "test", "example", "dummy", "fake", "mock", "sample")
_SOURCE_FIELDS = frozenset({"source", "source_reference", "reference", "source_identifier", "source_url"})


def asserted_sources(value: Any, _depth: int = 0) -> list[str]:
    """Every source/reference string asserted anywhere inside ``value`` (bounded walk)."""
    if _depth > 6:
        return []
    found: list[str] = []
    if isinstance(value, dict):
        items = list(value.items())
    elif hasattr(value, "__dataclass_fields__"):
        items = [(name, getattr(value, name, None)) for name in value.__dataclass_fields__]
    elif isinstance(value, (list, tuple)):
        items = [("", item) for item in value]
    else:
        return found
    for name, item in items:
        if name in _SOURCE_FIELDS and isinstance(item, str):
            found.append(item)
        elif not isinstance(item, (str, bytes, int, float, bool)) and item is not None:
            found.extend(asserted_sources(item, _depth + 1))
    return found


def strict_authority_failures(
    evidence: ReferenceEvidence | Any,
    request: DataEligibilityRequest | None = None,
) -> list[tuple[str, str]]:
    """Authority gaps that keep ``STRICT_RESEARCH`` BLOCKED (audit criteria 3-5).

    Each gap is an EVIDENCE failure, so DIAGNOSTIC_ONLY runs are unaffected.
    Strict status cannot be self-asserted by local files, a naming heuristic or
    caller bps. Positive authority requires a separately hash-pinned trust policy.
    """
    gaps: list[tuple[str, str]] = []
    synthetic = sorted({s for s in asserted_sources(evidence)
                        if any(marker in s.lower() for marker in SYNTHETIC_MARKERS)})
    if synthetic:
        gaps.append(("EVIDENCE_SYNTHETIC", f"evidence declares synthetic/test sources {synthetic[:5]}; "
                     "it supports diagnostics only"))
    registration = getattr(evidence, "authority_registration", None)
    approved = bool(getattr(evidence, "authority_approved", False))
    if not approved or (request is not None and registration is not None
                        and not registration.covers(request.start, request.end)):
        gaps.append(("EVIDENCE_AUTHORITY_UNAPPROVED", "loaded evidence is not covered by an approved registration "
                     "in a separately hash-pinned reviewer trust policy"))
    classification = getattr(evidence, "instrument_classification", None)
    classified = classification.classification_for(request.symbol, request.start, request.end) if (
        classification is not None and request is not None) else None
    if classification is None or not classification.authority_approved or classified is None:
        gaps.append(("INSTRUMENT_CLASSIFICATION_UNVERIFIED", "equity/ETF status is not covered by an approved, "
                     "effective-dated instrument-classification registration"))
    costs = getattr(evidence, "cost_schedule", None)
    if costs is None or not costs.authority_approved or request is None or not costs.covers(request.start, request.end):
        gaps.append(("COST_SCHEDULE_UNREGISTERED", "no approved, checksum-bound transaction-cost schedule covers "
                     "the request; caller bps and free-text cost bases are not authoritative"))
    return gaps


class HistoricalDataAdapter:
    """Assess one symbol and date range; release bars only for usable decisions."""

    def __init__(self, raw_root: Path, evidence: ReferenceEvidence):
        self.raw_root = Path(raw_root)
        self.evidence = evidence

    @_issues("decision")
    def assess(
        self,
        request: DataEligibilityRequest,
        adjustment_events: Sequence[CorporateActionEvent] = (),
        *,
        _sign: Any = None,
    ) -> tuple[DataEligibilityDecision, VerifiedHistoricalDataset | None]:
        failures: list[EligibilityFailure] = []
        evidence: list[EligibilityEvidence] = []
        warnings: list[str] = []
        segments: list[dict[str, Any]] = []
        bars: list[CanonicalDailyBar] = []
        share_events: list[dict[str, Any]] = []
        used_events: list[CorporateActionEvent] = []
        reconciliation: list[dict[str, Any]] = []
        holdout = self.evidence.holdout
        issued = evidence_issued(self.evidence)
        registration = self.evidence.price_basis_registration
        cost_schedule = self.evidence.cost_schedule
        raw_basis = (self.evidence.provider_price_basis == "RAW_UNADJUSTED" and registration is not None
                     and registration.basis == "RAW_UNADJUSTED"
                     and registration.covers(request.symbol, request.start, request.end))

        def fail(code: str, message: str) -> None:
            failures.append(EligibilityFailure(code, message))

        def decide() -> tuple[DataEligibilityDecision, VerifiedHistoricalDataset | None]:
            if any(f.failure_class == FailureClass.INTEGRITY for f in failures):
                status = DecisionStatus.REJECTED
            elif request.mode == EligibilityMode.DIAGNOSTIC_ONLY:
                status = DecisionStatus.DIAGNOSTIC_ALLOWED
            else:
                status = DecisionStatus.BLOCKED if failures else DecisionStatus.ELIGIBLE
            if raw_basis:
                basis = "RAW_UNADJUSTED_VERIFIED" + ("_WITH_VALIDATED_SHARE_EVENTS" if share_events else "")
            else:
                basis = "PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED"
            decision = DataEligibilityDecision(
                ADAPTER_VERSION, request, status, tuple(failures), tuple(evidence), tuple(warnings),
                tuple(segments), holdout.cutoff if holdout else None, len(bars),
                bars[0].date if bars else None, bars[-1].date if bars else None,
                bars_hash(bars) if bars else None, evidence_sha256=evidence_digest(self.evidence),
                evidence_sources=dict(sorted(self.evidence.source_hashes.items())), price_basis=basis,
                cost_profile=({"profile_id": cost_schedule.profile_id, **cost_schedule.components()}
                              if cost_schedule and cost_schedule.authority_approved
                              and cost_schedule.covers(request.start, request.end) else None),
                share_events=tuple(share_events), corporate_action_reconciliation=tuple(reconciliation),
            )
            object.__setattr__(decision, "issuance_seal", _sign(decision.decision_id))
            object.__setattr__(decision, "revalidator", self.revalidate)
            if not decision.usable:
                return decision, None
            return decision, VerifiedHistoricalDataset(decision, tuple(bars), basis, tuple(share_events),
                                                      tuple(used_events))

        # 0. Trust gates (M5.1): only file-loaded, unmodified evidence and its own events grant STRICT status.
        if not issued:
            fail("EVIDENCE_NOT_ISSUED", "reference evidence was not loaded by load_reference_evidence in this "
                 "process, or was modified afterwards; it supports diagnostics only")
        if adjustment_events and request.mode == EligibilityMode.STRICT_RESEARCH:
            fail("CORPORATE_ACTION_EVENTS_CALLER_SUPPLIED", "STRICT_RESEARCH uses only adjustment events from "
                 "the evidence-paths file; caller-supplied events are diagnostic only")

        if request.mode == EligibilityMode.STRICT_RESEARCH:
            for code, message in strict_authority_failures(self.evidence, request):
                fail(code, message)

        # 1. Request-level integrity gates: series, holdout, publication lock, symbol directory.
        if request.series not in SUPPORTED_SERIES:
            fail("UNSUPPORTED_SERIES", f"series {request.series} is unsupported; only EQ is accepted")
        if holdout is None:
            fail("HOLDOUT_BOUNDARY_UNVERIFIED", "a hash-verified M3.2 split plan is required to prove holdout isolation")
        elif request.end >= holdout.cutoff:
            fail("HOLDOUT_OVERLAP", f"request end {request.end} reaches the locked holdout starting {holdout.cutoff}")
        classification = self.evidence.instrument_classification
        authoritative_class = classification.classification_for(request.symbol, request.start, request.end) if (
            classification is not None and classification.authority_approved) else None
        if authoritative_class == "ETF" or (authoritative_class is None and self.evidence.etf_heuristic.is_etf(request.symbol)):
            fail("ETF_EXCLUDED", "ETFs stay out of the equity universe (M3.7 naming heuristic)")
        if publication_lock_active(self.raw_root):
            fail("PUBLICATION_LOCK_ACTIVE", "an M2.4 publication is in progress")
        symbol_dir = self.raw_root / request.symbol
        if symbol_dir.is_symlink() or not symbol_dir.is_dir():
            fail("SYMBOL_NOT_FOUND", f"no published directory for {request.symbol}")
        if any(f.failure_class == FailureClass.INTEGRITY for f in failures):
            return decide()
        assert holdout is not None
        evidence.append(EligibilityEvidence("holdout_isolation", "VERIFIED",
                                            f"bars dated on or after {holdout.cutoff} are withheld",
                                            holdout.split_plan_sha256))
        if authoritative_class == "EQUITY":
            assert classification is not None
            evidence.append(EligibilityEvidence("instrument_classification", "VERIFIED",
                                                f"approved classification {classification.classification_id}",
                                                classification.source_sha256))
        else:
            evidence.append(EligibilityEvidence("instrument_classification", "HEURISTIC",
                                                "EQ series and not ETF by the unverified M3.7 naming heuristic",
                                                self.evidence.etf_heuristic.source_sha256))

        # 2. Identity: current master proves current identifiers only.
        tokens = self.evidence.master_tokens
        if tokens is None or request.symbol not in tokens:
            fail("IDENTITY_EVIDENCE_MISSING", "symbol is absent from a supplied M2.2-accepted instrument master")
            evidence.append(EligibilityEvidence("identity", "MISSING", "no current-master identity"))
        elif request.expected_token is not None and tokens[request.symbol] != request.expected_token:
            fail("IDENTITY_MISMATCH", f"master token {tokens[request.symbol]} differs from expected {request.expected_token}")
        else:
            evidence.append(EligibilityEvidence("identity", "VERIFIED", f"current master token {tokens[request.symbol]}",
                                                self.evidence.source_hashes.get("instrument_master")))

        # 3. Publication integrity for every segment overlapping the request.
        groups, unexpected = _segments(symbol_dir)
        for name in unexpected:
            fail("UNEXPECTED_FILE", f"unexpected entry {name} in the symbol directory")
        start, end = request.start, request.end
        covered: list[tuple[str, str]] = []
        observed: dict[str, CanonicalDailyBar] = {}
        for stem, files in sorted(groups.items()):
            seg_start, seg_end = stem.split("_")
            if seg_end < start or seg_start > end:
                continue
            if ".json.gz" not in files:
                fail("ORPHAN_MARKER", f"{stem} has sidecars without a payload")
                continue
            if ".json.gz.done" not in files or ".meta.json" not in files:
                fail("UNPUBLISHED_SEGMENT", f"{stem} lacks its completion marker or metadata")
                continue
            report = validate_daily_for_development(files[".json.gz"], holdout.cutoff)
            record = {"file": files[".json.gz"].name, "sha256": report.publication_integrity.sha256,
                      "records": report.publication_integrity.records, "disposition": report.disposition,
                      "window": [seg_start, seg_end],
                      "done_sha256": file_sha256(files[".json.gz.done"]),
                      "meta_sha256": file_sha256(files[".meta.json"])}
            segments.append(record)
            if report.disposition == "REJECT_PUBLICATION":
                fail("SEGMENT_REJECTED", f"{stem}: {[i.message for i in report.publication_integrity.issues]}")
                continue
            if report.disposition != "ACCEPT":
                fail("SEGMENT_QUARANTINED", f"{stem}: {report.disposition}")
                continue
            metadata, _ = read_json(files[".meta.json"], MAX_SIDECAR)
            expected_meta = {"source": "shoonya", "symbol": request.symbol, "from": seg_start, "to": seg_end}
            mismatched = sorted(k for k, v in expected_meta.items() if metadata.get(k) != v)
            if mismatched:
                fail("METADATA_MISMATCH", f"{stem}: metadata fields {mismatched} disagree with the publication path")
                continue
            if metadata.get("quality_status") not in {None, "UNVALIDATED_RAW"}:
                fail("QUARANTINED_SOURCE", f"{stem}: metadata quality_status {metadata.get('quality_status')!r}")
                continue
            covered.append((seg_start, seg_end))
            for candle in report.content_quality.normalized_candles:
                # The M5-specific validator never returns protected candles.
                assert candle.trading_date < holdout.cutoff
                bar = CanonicalDailyBar(candle.trading_date, str(candle.open), str(candle.high), str(candle.low),
                                        str(candle.close), candle.volume, candle.source_timestamp_utc,
                                        files[".json.gz"].name)
                prior = observed.get(bar.date)
                if prior is None:
                    observed[bar.date] = bar
                elif asdict(prior) | {"segment_file": ""} != asdict(bar) | {"segment_file": ""}:
                    fail("CONTRADICTORY_OBSERVATION", f"{bar.date} differs between {prior.segment_file} and {bar.segment_file}")
                else:
                    warnings.append(f"{bar.date}: identical observation in overlapping segments")
        bars.extend(observed[day] for day in sorted(observed) if start <= day <= end)
        if not bars and not any(f.failure_class == FailureClass.INTEGRITY for f in failures):
            fail("NO_OBSERVATIONS", "no accepted observations inside the requested range")
        cursor, last = date.fromisoformat(start), date.fromisoformat(end)
        for seg_start, seg_end in sorted(covered):
            if date.fromisoformat(seg_start) > cursor:
                break
            cursor = max(cursor, date.fromisoformat(seg_end) + timedelta(days=1))
        if cursor <= last:
            fail("PUBLICATION_COVERAGE_GAP", f"no accepted publication window covers {cursor.isoformat()}")
        if any(f.failure_class == FailureClass.INTEGRITY for f in failures):
            bars.clear()
            return decide()
        evidence.append(EligibilityEvidence("publication_integrity", "VERIFIED",
                                            f"{len(segments)} accepted ShoonyaDailyV1 segment(s)"))

        # 4. Point-in-time listing membership (never inferred from first candles).
        listing = self.evidence.listing
        listed = next((r for r in listing.records if r.canonical_symbol == request.symbol), None) if listing else None
        membership_start = start
        if listed is None:
            fail("PIT_MEMBERSHIP_MISSING", "no manifest-verified listing history for this symbol")
            evidence.append(EligibilityEvidence("pit_membership", "MISSING", "listing dates are UNKNOWN"))
        else:
            if any(bar.date < listed.listing_date for bar in bars) or (
                    listed.delisting_date and any(bar.date >= listed.delisting_date for bar in bars)):
                fail("LISTING_CONTRADICTION", "observations fall outside the evidenced listing interval")
            membership_start = max(start, listed.listing_date)
            verified = listed.membership_verified_through
            if verified is None or verified < end:
                fail("PIT_MEMBERSHIP_MISSING", f"membership verified only through {verified}")
            else:
                evidence.append(EligibilityEvidence("pit_membership", "VERIFIED",
                                                    f"listed {listed.listing_date}, verified through {verified}",
                                                    listing.manifest.sha256 if listing else None))

        # 5. Trading calendar: every observed bar must be a session; every session needs a bar.
        calendar = self.evidence.calendar
        if calendar is None or not calendar.covers(start, end):
            fail("CALENDAR_EVIDENCE_MISSING", "no registered NSE calendar covers the request; gaps stay UNKNOWN")
            evidence.append(EligibilityEvidence("calendar", "MISSING", "weekday gaps are not interpreted as holidays"))
        else:
            closed = [bar.date for bar in bars if calendar.statuses[bar.date] == "MARKET_CLOSED"]
            if closed:
                fail("CALENDAR_CONTRADICTION", f"observations on closed dates: {closed[:5]}")
            observed_dates = {bar.date for bar in bars}
            missing = [d for d in calendar.trading_sessions(membership_start, end) if d not in observed_dates]
            if missing:
                fail("MISSING_SESSIONS", f"{len(missing)} calendar session(s) without a bar, first {missing[:5]}")
            evidence.append(EligibilityEvidence("calendar", "VERIFIED", f"registration {calendar.registration_id}",
                                                calendar.source_sha256))

        # 6. Alias history: stitching renamed series is unsupported, so overlap blocks.
        aliases = self.evidence.aliases
        if aliases is None or aliases.manifest.coverage_status != "COMPLETE" or not (
                aliases.manifest.effective_start <= start and end <= aliases.manifest.effective_end):
            fail("ALIAS_EVIDENCE_MISSING", "no complete, manifest-verified symbol-change coverage for the range")
            evidence.append(EligibilityEvidence("alias_history", "MISSING", "aliases stay UNKNOWN"))
        else:
            overlapping = [r for r in aliases.records if request.symbol in {r.canonical_symbol, r.alias_symbol}
                           and (r.valid_from or "0000") <= end and (r.valid_to or "9999") >= start]
            if overlapping:
                fail("ALIAS_STITCHING_UNSUPPORTED", f"{len(overlapping)} alias interval(s) overlap the range")
            else:
                evidence.append(EligibilityEvidence("alias_history", "VERIFIED", "no alias interval overlaps the range",
                                                    aliases.manifest.sha256))

        # 7. Provider price basis: M3.9-B adjusts only RAW_UNADJUSTED series. Applying it to a
        # provider series that is already (partly) back-adjusted would double-adjust. The basis
        # must come from a reviewed registration covering this symbol and range.
        if raw_basis:
            evidence.append(EligibilityEvidence("provider_price_basis", "VERIFIED",
                                                f"RAW_UNADJUSTED registration {registration.registration_id}",
                                                registration.source_sha256))
        else:
            fail("PROVIDER_PRICE_BASIS_UNVERIFIED", "no reviewed RAW_UNADJUSTED price-basis registration covers "
                 "this symbol and range; Shoonya history is observed to mix adjusted and unadjusted actions")
            evidence.append(EligibilityEvidence("provider_price_basis", "MISSING",
                                                "levels may be provider back-adjusted as of fetch time"))

        # 8. Corporate actions: complete coverage, then field-by-field reconciliation of every in-range
        # M2.1 catalog action with M3.9-B events. Only reconciled split/bonus events on a verified raw
        # basis are applied; contradictions, ambiguity and unmatched events are rejected.
        catalogs = self.evidence.corporate_actions
        catalog = next((c for c in catalogs.catalogs if c.symbol == request.symbol), None) if catalogs else None
        first_bar = bars[0].date
        if catalog is None or not catalog.covers(date.fromisoformat(start), date.fromisoformat(end)):
            fail("CORPORATE_ACTION_COVERAGE_MISSING", "no complete corporate-action catalog covers the range")
            evidence.append(EligibilityEvidence("corporate_actions", "MISSING",
                                                "no adjustment is applied; corporate actions are UNKNOWN"))
        else:
            supplied = [e for e in (*self.evidence.adjustment_events, *adjustment_events)
                        if e.symbol == request.symbol and e.validation_state == ValidationState.VALIDATED
                        and e.source.licence_status == LicenceStatus.APPROVED_FOR_DERIVATION
                        and e.ex_date is not None and first_bar < e.ex_date <= end]
            in_range = [a for a in catalog.actions if first_bar < a.effective_date <= end]
            keys: dict[tuple[str, str], int] = {}
            for action in in_range:
                kind = action.action_type.upper()
                normalized = (EventType.CASH_DIVIDEND.value if kind in _PRICE_RETURN_EXCLUDED else
                              _SHARE_EVENTS[kind].value if kind in _SHARE_EVENTS else kind)
                keys[(normalized, action.effective_date)] = keys.get((normalized, action.effective_date), 0) + 1
            matched_ids: set[str] = set()
            unresolved = 0
            for action in in_range:
                kind = action.action_type.upper()
                outcome: dict[str, Any] = {"action_type": action.action_type, "effective_date": action.effective_date,
                                           "adjustment_status": action.adjustment_status,
                                           "details": dict(sorted(action.details.items()))}
                reconciliation.append(outcome)
                if kind not in _SHARE_EVENTS and kind not in _PRICE_RETURN_EXCLUDED:
                    fail("CORPORATE_ACTION_UNSUPPORTED", f"{action.effective_date}: {kind} has no supported treatment")
                    outcome["outcome"] = "UNSUPPORTED"
                    continue
                family = "share" if kind in _SHARE_EVENTS else "dividend"
                target = _SHARE_EVENTS[kind] if family == "share" else EventType.CASH_DIVIDEND
                outcome["normalized_type"] = target.value
                if keys[(target.value, action.effective_date)] > 1:
                    fail("CORPORATE_ACTION_AMBIGUOUS", f"{action.effective_date}: catalog lists {target.value} "
                         "more than once")
                    outcome["outcome"] = "AMBIGUOUS_CATALOG"
                    continue
                status = action.adjustment_status
                if status in {"UNKNOWN", "POLICY_REQUIRED"}:
                    fail("CORPORATE_ACTION_STATUS_UNRESOLVED", f"{action.effective_date}: {kind} adjustment status "
                         f"is {status}")
                    outcome["outcome"] = "STATUS_UNRESOLVED"
                elif raw_basis and status not in _RAW_CONSISTENT[family]:
                    fail("CORPORATE_ACTION_CONTRADICTION", f"{action.effective_date}: {kind} is {status} but the "
                         "registered provider basis is RAW_UNADJUSTED")
                    outcome["outcome"] = "STATUS_CONTRADICTS_BASIS"
                match = [e for e in supplied if e.event_type == target and e.ex_date == action.effective_date]
                matched_ids.update(e.event_id for e in match)
                if len(match) > 1:
                    fail("CORPORATE_ACTION_AMBIGUOUS", f"{action.effective_date}: {len(match)} validated "
                         f"{target.value} events match one catalog action")
                    outcome.setdefault("outcome", "AMBIGUOUS_EVENTS")
                    continue
                if not match:
                    if family == "share":
                        unresolved += 1
                        outcome.setdefault("outcome", "EVENT_UNRESOLVED")
                    else:
                        outcome.setdefault("outcome", "EXCLUDED_PRICE_RETURN_NO_EVENT")
                        warnings.append(f"{action.effective_date}: cash dividend excluded (price-return convention)")
                    continue
                event = match[0]
                outcome["matched_event_id"] = event.event_id
                problems = _reconcile_details(family, action.details, event)
                for code, message in problems:
                    fail(code, f"{action.effective_date}: {message}")
                if problems:
                    outcome.setdefault("outcome", "DETAILS_CONTRADICTION")
                    continue
                if "outcome" in outcome:
                    continue
                if family == "dividend":
                    outcome["outcome"] = "EXCLUDED_PRICE_RETURN"
                    warnings.append(f"{action.effective_date}: cash dividend excluded (price-return convention)")
                    continue
                if not raw_basis:
                    outcome["outcome"] = "RECONCILED_NOT_APPLIED_UNVERIFIED_BASIS"
                    continue
                factor = _share_factor(event)
                used_events.append(event)
                share_events.append({"ex_date": action.effective_date, "event_id": event.event_id,
                                     "event_type": event.event_type.value,
                                     "price_factor": f"{factor.numerator}/{factor.denominator}",
                                     "knowledge_available_at": event.source.available_at})
                outcome["outcome"] = "APPLIED"
            for event in supplied:
                if event.event_id not in matched_ids:
                    fail("CORPORATE_ACTION_EVENT_UNMATCHED", f"{event.ex_date}: validated {event.event_type.value} "
                         f"event {event.event_id} has no matching catalog action")
                    reconciliation.append({"event_id": event.event_id, "event_type": event.event_type.value,
                                           "ex_date": event.ex_date, "outcome": "EVENT_UNMATCHED"})
            if unresolved:
                fail("CORPORATE_ACTION_EVENT_UNRESOLVED",
                     f"{unresolved} split/bonus action(s) lack exactly one validated, licensed M3.9-B event")
            ca_failures = [f for f in failures if f.code.startswith("CORPORATE_ACTION")]
            if ca_failures:
                evidence.append(EligibilityEvidence("corporate_actions", "MISSING",
                                                    f"{len(ca_failures)} unreconciled corporate-action finding(s)",
                                                    catalogs.manifest.sha256 if catalogs else None))
            elif not raw_basis:
                evidence.append(EligibilityEvidence("corporate_actions", "MISSING",
                                                    "catalog reconciled but not applied to an unverified price basis",
                                                    catalogs.manifest.sha256 if catalogs else None))
            else:
                evidence.append(EligibilityEvidence("corporate_actions", "VERIFIED",
                                                    f"{len(share_events)} reconciled share event(s) in range",
                                                    catalogs.manifest.sha256 if catalogs else None))

        # Discontinuities are flagged, never attributed or repaired.
        findings = detect_suspicious_discontinuities(
            RawCandle(b.date, b.open, b.high, b.low, b.close, b.volume) for b in bars)
        explained = {e["ex_date"] for e in share_events}
        unexplained = [f for f in findings if f["current_date"] not in explained]
        if unexplained:
            warnings.append(f"{len(unexplained)} unexplained open gap(s) of 20% or more, first "
                            f"{[f['current_date'] for f in unexplained[:5]]}; cause UNKNOWN")
        if any(f.failure_class == FailureClass.INTEGRITY for f in failures):
            bars.clear()
            share_events.clear()
            used_events.clear()
        return decide()

    def revalidate(self, decision: DataEligibilityDecision) -> None:
        """Re-hash every source ``decision`` relied on; raise :class:`StaleAssessmentError` on any change.

        Only bytes are hashed (payload, ``.done``, ``.meta.json``); nothing is decoded,
        so protected holdout rows are not parsed again. The overlapping-segment set
        must be unchanged, and the evidence-paths file is reloaded and must yield the
        same evidence digest. A check-then-use window remains: the run uses the
        in-memory bars verified here, so a later file change cannot alter this run,
        but it is not detected until the next revalidation or replay.
        """
        symbol = decision.request.symbol
        if decision.evidence_sha256 != evidence_digest(self.evidence):
            raise StaleAssessmentError(f"{symbol}: decision was issued over different evidence")
        if not self.evidence.config_path:
            raise StaleAssessmentError(f"{symbol}: evidence was not loaded from a registered evidence-paths file")
        try:
            reloaded = load_reference_evidence(
                Path(self.evidence.config_path),
                trust_policy_path=(Path(self.evidence.trust_policy_path)
                                   if self.evidence.trust_policy_path else None),
                trust_policy_sha256=(self.evidence.trust_policy.policy_sha256
                                     if self.evidence.trust_policy else None),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StaleAssessmentError(f"{symbol}: registered evidence can no longer be loaded: {exc}") from exc
        if evidence_digest(reloaded) != decision.evidence_sha256:
            raise StaleAssessmentError(f"{symbol}: registered evidence changed after assessment")
        symbol_dir = self.raw_root / symbol
        if symbol_dir.is_symlink() or not symbol_dir.is_dir():
            raise StaleAssessmentError(f"{symbol}: published directory disappeared after assessment")
        groups, unexpected = _segments(symbol_dir)
        if unexpected:
            raise StaleAssessmentError(f"{symbol}: unexpected entries appeared: {unexpected}")
        start, end = decision.request.start, decision.request.end
        current = sorted(stem for stem in groups if not (stem.split("_")[1] < start or stem.split("_")[0] > end))
        recorded = sorted(record["file"].removesuffix(".json.gz") for record in decision.segments)
        if current != recorded:
            raise StaleAssessmentError(f"{symbol}: overlapping segments changed from {recorded} to {current}")
        for record in decision.segments:
            files = groups[record["file"].removesuffix(".json.gz")]
            for suffix, key in ((".json.gz", "sha256"), (".json.gz.done", "done_sha256"),
                                (".meta.json", "meta_sha256")):
                path = files.get(suffix)
                if path is None or path.is_symlink() or not path.is_file():
                    raise StaleAssessmentError(f"{symbol}: {record['file']} lost its {suffix} file")
                if file_sha256(path) != record.get(key):
                    raise StaleAssessmentError(f"{symbol}: {path.name} changed after assessment")

    @_issues("universe")
    def assess_universe(
        self,
        universe: UniverseRequest,
        adjustment_events: Sequence[CorporateActionEvent] = (),
        *,
        _sign: Any = None,
    ) -> UniverseAssessment:
        """Assess every requested member; never substitute a surviving subset.

        ``STRICT_RESEARCH`` releases data only if every member is ``ELIGIBLE``. A
        ``DIAGNOSTIC_ONLY`` universe releases a usable subset only when the request
        set ``diagnostic_subset``; excluded members are recorded with reasons.
        """
        if not isinstance(universe, UniverseRequest):
            raise TypeError("assess_universe requires a UniverseRequest")
        pairs = [self.assess(request, adjustment_events) for request in universe.member_requests()]
        decisions = tuple(decision for decision, _ in pairs)
        usable = tuple(dataset for _, dataset in pairs if dataset is not None)
        strict = universe.mode == EligibilityMode.STRICT_RESEARCH
        excluded = tuple({"symbol": d.request.symbol, "status": d.status.value, "failures": [f.code for f in d.failures]}
                         for d in decisions if (d.status != DecisionStatus.ELIGIBLE if strict else not d.usable))
        if strict:
            if not excluded:
                status, released = DecisionStatus.ELIGIBLE, usable
            else:
                rejected = any(d.status == DecisionStatus.REJECTED for d in decisions)
                status, released = (DecisionStatus.REJECTED if rejected else DecisionStatus.BLOCKED), ()
        elif not excluded or (universe.diagnostic_subset and usable):
            status, released = DecisionStatus.DIAGNOSTIC_ALLOWED, usable
        else:
            status, released = DecisionStatus.REJECTED, ()
        assessment = UniverseAssessment(universe, decisions, status, released, excluded)
        object.__setattr__(assessment, "issuance_seal", _sign(assessment.assessment_id))
        return assessment
