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

from stock_gplm.ingestion.daily import MAX_SIDECAR, validate_daily
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

from .evidence import ReferenceEvidence, read_json
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
    VerifiedHistoricalDataset,
    bars_hash,
)

SUPPORTED_SERIES = frozenset({"EQ"})
LOCK_NAME = ".stock-gplm-m24.lock"
_SEGMENT = re.compile(r"^(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})(?P<suffix>\.json\.gz|\.json\.gz\.done|\.meta\.json)$")
_SHARE_EVENTS = {"SPLIT": EventType.STOCK_SPLIT, "STOCK_SPLIT": EventType.STOCK_SPLIT,
                 "REVERSE_SPLIT": EventType.REVERSE_SPLIT, "CONSOLIDATION": EventType.REVERSE_SPLIT,
                 "BONUS": EventType.BONUS}
_PRICE_RETURN_EXCLUDED = frozenset({"DIVIDEND", "CASH_DIVIDEND"})


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


def _share_factor(event: CorporateActionEvent) -> Fraction:
    assert event.new_shares is not None and event.base_shares is not None
    if event.event_type == EventType.BONUS:
        return bonus_factor(bonus_shares=event.new_shares, held_shares=event.base_shares)
    return split_factor(new_shares=event.new_shares, old_shares=event.base_shares)


class HistoricalDataAdapter:
    """Assess one symbol and date range; release bars only for usable decisions."""

    def __init__(self, raw_root: Path, evidence: ReferenceEvidence):
        self.raw_root = Path(raw_root)
        self.evidence = evidence

    def assess(
        self,
        request: DataEligibilityRequest,
        adjustment_events: Sequence[CorporateActionEvent] = (),
    ) -> tuple[DataEligibilityDecision, VerifiedHistoricalDataset | None]:
        failures: list[EligibilityFailure] = []
        evidence: list[EligibilityEvidence] = []
        warnings: list[str] = []
        segments: list[dict[str, Any]] = []
        bars: list[CanonicalDailyBar] = []
        share_events: list[dict[str, Any]] = []
        used_events: list[CorporateActionEvent] = []
        holdout = self.evidence.holdout

        def fail(code: str, message: str) -> None:
            failures.append(EligibilityFailure(code, message))

        def decide() -> tuple[DataEligibilityDecision, VerifiedHistoricalDataset | None]:
            if any(f.failure_class == FailureClass.INTEGRITY for f in failures):
                status = DecisionStatus.REJECTED
            elif request.mode == EligibilityMode.DIAGNOSTIC_ONLY:
                status = DecisionStatus.DIAGNOSTIC_ALLOWED
            else:
                status = DecisionStatus.BLOCKED if failures else DecisionStatus.ELIGIBLE
            decision = DataEligibilityDecision(
                ADAPTER_VERSION, request, status, tuple(failures), tuple(evidence), tuple(warnings),
                tuple(segments), holdout.cutoff if holdout else None, len(bars),
                bars[0].date if bars else None, bars[-1].date if bars else None,
                bars_hash(bars) if bars else None,
            )
            if not decision.usable:
                return decision, None
            if self.evidence.provider_price_basis == "RAW_UNADJUSTED":
                basis = "RAW_UNADJUSTED_VERIFIED" + ("_WITH_VALIDATED_SHARE_EVENTS" if share_events else "")
            else:
                basis = "PROVIDER_SERIES_ADJUSTMENT_BASIS_UNVERIFIED"
            return decision, VerifiedHistoricalDataset(decision, tuple(bars), basis, tuple(share_events),
                                                      tuple(used_events))

        # 1. Request-level integrity gates: series, holdout, publication lock, symbol directory.
        if request.series not in SUPPORTED_SERIES:
            fail("UNSUPPORTED_SERIES", f"series {request.series} is unsupported; only EQ is accepted")
        if holdout is None:
            fail("HOLDOUT_BOUNDARY_UNVERIFIED", "a hash-verified M3.2 split plan is required to prove holdout isolation")
        elif request.end >= holdout.cutoff:
            fail("HOLDOUT_OVERLAP", f"request end {request.end} reaches the locked holdout starting {holdout.cutoff}")
        if self.evidence.etf_heuristic.is_etf(request.symbol):
            fail("ETF_EXCLUDED", "ETFs stay out of the equity universe (M3.7 naming heuristic)")
        if publication_lock_active(self.raw_root):
            fail("PUBLICATION_LOCK_ACTIVE", "an M2.4 publication is in progress")
        symbol_dir = self.raw_root / request.symbol
        if symbol_dir.is_symlink() or not symbol_dir.is_dir():
            fail("SYMBOL_NOT_FOUND", f"no published directory for {request.symbol}")
        if failures:
            return decide()
        assert holdout is not None
        evidence.append(EligibilityEvidence("holdout_isolation", "VERIFIED",
                                            f"bars dated on or after {holdout.cutoff} are withheld",
                                            holdout.split_plan_sha256))
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
            report = validate_daily(files[".json.gz"])
            record = {"file": files[".json.gz"].name, "sha256": report.publication_integrity.sha256,
                      "records": report.publication_integrity.records, "disposition": report.disposition,
                      "window": [seg_start, seg_end], "holdout_rows_withheld": 0}
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
                if candle.trading_date >= holdout.cutoff:
                    record["holdout_rows_withheld"] += 1
                    continue
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
        if not bars and not failures:
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
        # provider series that is already (partly) back-adjusted would double-adjust.
        raw_basis = self.evidence.provider_price_basis == "RAW_UNADJUSTED"
        if raw_basis:
            evidence.append(EligibilityEvidence("provider_price_basis", "VERIFIED", "RAW_UNADJUSTED"))
        else:
            fail("PROVIDER_PRICE_BASIS_UNVERIFIED", "the provider series' corporate-action adjustment basis is "
                 "not evidenced; Shoonya history is observed to mix adjusted and unadjusted actions")
            evidence.append(EligibilityEvidence("provider_price_basis", "MISSING",
                                                "levels may be provider back-adjusted as of fetch time"))

        # 8. Corporate actions: complete coverage and validated share events for every split/bonus.
        catalogs = self.evidence.corporate_actions
        catalog = next((c for c in catalogs.catalogs if c.symbol == request.symbol), None) if catalogs else None
        first_bar = bars[0].date
        if catalog is None or not catalog.covers(date.fromisoformat(start), date.fromisoformat(end)):
            fail("CORPORATE_ACTION_COVERAGE_MISSING", "no complete corporate-action catalog covers the range")
            evidence.append(EligibilityEvidence("corporate_actions", "MISSING",
                                                "no adjustment is applied; corporate actions are UNKNOWN"))
        elif not raw_basis:
            evidence.append(EligibilityEvidence("corporate_actions", "MISSING",
                                                "catalog present but not applied to an unverified price basis",
                                                catalogs.manifest.sha256 if catalogs else None))
        else:
            unresolved = 0
            supplied = [e for e in (*self.evidence.adjustment_events, *adjustment_events)
                        if e.symbol == request.symbol and e.validation_state == ValidationState.VALIDATED
                        and e.source.licence_status == LicenceStatus.APPROVED_FOR_DERIVATION]
            for action in catalog.actions:
                if not first_bar < action.effective_date <= end:
                    continue
                kind = action.action_type.upper()
                if kind in _PRICE_RETURN_EXCLUDED:
                    warnings.append(f"{action.effective_date}: cash dividend excluded (price-return convention)")
                    continue
                if kind not in _SHARE_EVENTS:
                    fail("CORPORATE_ACTION_UNSUPPORTED", f"{action.effective_date}: {kind} has no supported treatment")
                    continue
                match = [e for e in supplied if e.event_type == _SHARE_EVENTS[kind] and e.ex_date == action.effective_date]
                if len(match) != 1:
                    unresolved += 1
                    continue
                factor = _share_factor(match[0])
                used_events.append(match[0])
                share_events.append({"ex_date": action.effective_date, "event_id": match[0].event_id,
                                     "event_type": match[0].event_type.value,
                                     "price_factor": f"{factor.numerator}/{factor.denominator}",
                                     "knowledge_available_at": match[0].source.available_at})
            if unresolved:
                fail("CORPORATE_ACTION_EVENT_UNRESOLVED",
                     f"{unresolved} split/bonus action(s) lack exactly one validated, licensed M3.9-B event")
            else:
                evidence.append(EligibilityEvidence("corporate_actions", "VERIFIED",
                                                    f"{len(share_events)} validated share event(s) in range",
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
        return decide()
