"""M5.1.3 partition-migration plan contract: specification and validator only (never executes).

Real Shoonya daily segments are calendar-year files. A segment whose declared window
straddles the governed locked-holdout cutoff is rejected by M5 without being opened, so
it must be republished as separate development and protected segments. This module
defines the plan a publisher must submit for that republication and validates it
*without reading any data*. It performs no file, network or decoding operation.

Rules a valid ``stock-gplm.m513-partition-migration-plan.v1`` must satisfy:

* **Governed cutoff provenance.** ``governed_holdout`` equals the administrative trust
  root's governed holdout exactly (run id, split-plan SHA-256, cutoff).
* **Immutable source lineage.** Every source segment names its symbol, file, declared
  window and the SHA-256 of payload, ``.done`` and ``.meta.json``. The source root's
  ``inventory_sha256`` is the canonical hash of the sorted source list.
* **Deterministic partitioning.** ``targets`` must equal :func:`plan_partitions` of the
  sources and governed cutoff: windows ending before the cutoff are development byte
  copies; windows starting on or after it are protected byte copies; straddling windows
  (including one ending exactly on the cutoff day) split into ``[start, cutoff-1]``
  development and ``[cutoff, end]`` protected. Every target carries the source lineage.
* **Atomic versioned publication.** The target root is a new version directory, neither
  equal to, inside, nor containing the source root. Publication stages the whole
  version and makes it visible with one atomic rename; nothing in the source root is
  written.
* **Validation and rollback.** The required post-publication validation steps are
  listed; rollback keeps the untouched source root and repoints readers to it.
* **Protected-data decoding authorization.** ``RECOLLECT_CUTOFF_ALIGNED_WINDOWS`` decodes
  nothing. ``PUBLISHER_SPLIT`` decodes protected rows once, inside the ingestion
  boundary, and is valid only with an explicit, hash-bound authorization record.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

from .authority import GovernedHoldout
from .models import canonical_hash

MIGRATION_PLAN_CONTRACT = "stock-gplm.m513-partition-migration-plan.v1"
PUBLICATION_METHOD = "STAGED_VERSION_ATOMIC_RENAME"
ROLLBACK_METHOD = "KEEP_SOURCE_ROOT_AND_REPOINT_READERS"
REQUIRED_VALIDATION = (
    "VERIFY_SOURCE_HASHES_UNCHANGED_BEFORE_AND_AFTER",
    "VERIFY_TARGET_SIDECAR_WINDOWS_EQUAL_FILENAMES",
    "VERIFY_DEVELOPMENT_TARGETS_END_BEFORE_CUTOFF",
    "VERIFY_DEVELOPMENT_ROWS_WITHIN_DECLARED_WINDOWS_WITH_M5_VALIDATOR",
    "VERIFY_PROTECTED_TARGETS_NOT_OPENED_BY_M5",
    "RECORD_TARGET_PAYLOAD_DONE_META_SHA256",
)
DECODING_METHODS = ("RECOLLECT_CUTOFF_ALIGNED_WINDOWS", "PUBLISHER_SPLIT")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_FIELDS = frozenset({"symbol", "file", "window", "payload_sha256", "done_sha256", "meta_sha256"})
_PLAN_FIELDS = frozenset({"contract", "plan_id", "governed_holdout", "source_root", "target_root", "sources",
                          "targets", "publication", "validation", "rollback", "protected_decoding"})


def _source_identity(source: dict[str, Any]) -> dict[str, Any]:
    return {key: source[key] for key in sorted(_SOURCE_FIELDS)}


def plan_partitions(sources: list[dict[str, Any]], cutoff: str) -> list[dict[str, Any]]:
    """Deterministic target segments for ``sources`` at ``cutoff`` (input order is irrelevant)."""
    boundary = date.fromisoformat(cutoff)
    last_development = (boundary - timedelta(days=1)).isoformat()
    targets: list[dict[str, Any]] = []
    for source in sorted(sources, key=lambda item: (item["symbol"], item["window"][0], item["window"][1])):
        start, end = source["window"]
        lineage = [_source_identity(source)]
        if end < cutoff:
            targets.append({"symbol": source["symbol"], "window": [start, end], "side": "DEVELOPMENT",
                            "derivation": "BYTE_COPY", "lineage": lineage})
        elif start >= cutoff:
            targets.append({"symbol": source["symbol"], "window": [start, end], "side": "PROTECTED",
                            "derivation": "BYTE_COPY", "lineage": lineage})
        else:
            targets.append({"symbol": source["symbol"], "window": [start, last_development], "side": "DEVELOPMENT",
                            "derivation": "SPLIT_AT_CUTOFF", "lineage": lineage})
            targets.append({"symbol": source["symbol"], "window": [cutoff, end], "side": "PROTECTED",
                            "derivation": "SPLIT_AT_CUTOFF", "lineage": lineage})
    return targets


def inventory_sha256(sources: list[dict[str, Any]]) -> str:
    return canonical_hash(sorted((_source_identity(s) for s in sources),
                                 key=lambda item: (item["symbol"], item["window"][0], item["window"][1])))


def plan_identity(plan: dict[str, Any]) -> str:
    return canonical_hash({key: plan[key] for key in sorted(_PLAN_FIELDS - {"plan_id"})})


def _path_parts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ValueError("root paths must be absolute strings")
    parts = tuple(part for part in value.split("/") if part)
    if any(part in {".", ".."} for part in parts):
        raise ValueError("root paths must be normalized")
    return parts


def validate_migration_plan(plan: dict[str, Any], governed: GovernedHoldout) -> None:
    """Raise :class:`ValueError` listing every problem; return ``None`` only for a valid plan."""
    problems: list[str] = []
    if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS:
        raise ValueError(f"migration plan must have exactly the fields {sorted(_PLAN_FIELDS)}")
    if plan["contract"] != MIGRATION_PLAN_CONTRACT:
        problems.append("unsupported migration-plan contract")
    if plan["governed_holdout"] != asdict(governed):
        problems.append("governed_holdout differs from the administrative trust root's governed holdout")
    sources = plan["sources"]
    valid_sources = isinstance(sources, list) and bool(sources)
    if not valid_sources:
        problems.append("sources must be a non-empty list")
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
                problems.append(f"source {index} must have exactly {sorted(_SOURCE_FIELDS)}")
                valid_sources = False
                continue
            if not all(isinstance(source[k], str) and _HEX.fullmatch(source[k])
                       for k in ("payload_sha256", "done_sha256", "meta_sha256")):
                problems.append(f"source {index} hashes must be lowercase SHA-256")
            window = source["window"]
            try:
                start, end = (date.fromisoformat(window[0]), date.fromisoformat(window[1]))
                if len(window) != 2 or start > end or source["file"] != f"{window[0]}_{window[1]}.json.gz":
                    raise ValueError
            except (TypeError, ValueError, IndexError):
                problems.append(f"source {index} window must be an ordered ISO pair matching its file name")
                valid_sources = False
        identities = [(s["symbol"], s["file"]) for s in sources if isinstance(s, dict) and set(s) == _SOURCE_FIELDS]
        if len(identities) != len(set(identities)):
            problems.append("sources must not repeat a symbol/file")
    try:
        source_parts, target_parts = _path_parts(plan["source_root"].get("path")), _path_parts(
            plan["target_root"].get("path"))
        shorter = min(len(source_parts), len(target_parts))
        if source_parts[:shorter] == target_parts[:shorter]:
            problems.append("target root must be a new version outside the source root (not equal, inside or over it)")
    except (AttributeError, ValueError) as exc:
        problems.append(f"invalid root path: {exc}")
    if valid_sources:
        if not isinstance(plan["source_root"], dict) or plan["source_root"].get(
                "inventory_sha256") != inventory_sha256(sources):
            problems.append("source_root.inventory_sha256 does not bind the listed sources")
        if plan["targets"] != plan_partitions(sources, governed.cutoff):
            problems.append("targets are not the deterministic partition of the sources at the governed cutoff")
    if plan["publication"] != {"method": PUBLICATION_METHOD, "writes_source_root": False}:
        problems.append(f"publication must be {PUBLICATION_METHOD} with writes_source_root false")
    if plan["validation"] != list(REQUIRED_VALIDATION):
        problems.append("validation must list exactly the required post-publication steps, in order")
    if plan["rollback"] != {"method": ROLLBACK_METHOD, "source_root_retained": True}:
        problems.append(f"rollback must be {ROLLBACK_METHOD} with the source root retained")
    decoding = plan["protected_decoding"]
    method = decoding.get("method") if isinstance(decoding, dict) else None
    if method not in DECODING_METHODS:
        problems.append(f"protected_decoding.method must be one of {list(DECODING_METHODS)}")
    elif method == "RECOLLECT_CUTOFF_ALIGNED_WINDOWS":
        if decoding != {"method": method, "decodes_protected_rows": False}:
            problems.append("re-collection must declare decodes_protected_rows false and nothing else")
    else:
        authorization = decoding.get("authorization")
        if set(decoding) != {"method", "decodes_protected_rows", "authorization"} or decoding[
                "decodes_protected_rows"] is not True or not isinstance(authorization, dict) or set(authorization) != {
                "authorized_by", "authorization_record_sha256", "authorized_at_utc", "scope"} or not (
                isinstance(authorization["authorized_by"], str) and authorization["authorized_by"].strip()) or not (
                isinstance(authorization["authorization_record_sha256"], str)
                and _HEX.fullmatch(authorization["authorization_record_sha256"])) or authorization[
                "scope"] != "SPLIT_ONLY_NO_RETENTION_OR_ANALYSIS_OF_DECODED_PROTECTED_ROWS":
            problems.append("PUBLISHER_SPLIT decodes protected rows and requires an explicit hash-bound "
                            "authorization (authorized_by, authorization_record_sha256, authorized_at_utc, scope)")
    if plan["plan_id"] != plan_identity(plan):
        problems.append("plan_id does not match the plan content")
    if problems:
        raise ValueError("invalid partition-migration plan: " + "; ".join(problems))
