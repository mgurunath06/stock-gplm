"""Versioned evidence registration and explicit approval trust policy.

An evidence registration cannot approve itself.  Authority exists only when a
separately supplied trust-policy file has the exact out-of-band SHA-256 expected
by the caller and contains a matching approval by a trusted reviewer.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import canonical_hash

REGISTRATION_CONTRACT = "stock-gplm.m511-evidence-registration.v1"
TRUST_POLICY_CONTRACT = "stock-gplm.m511-approval-trust-policy.v1"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_REGISTRATION_FIELDS = frozenset({
    "contract", "schema_version", "registration_id", "evidence_type", "source_identity",
    "source_document_file", "source_document_sha256", "acquisition_method", "acquired_at_utc",
    "effective_start", "effective_end", "licensing_decision", "reviewer_identity",
    "approval_record", "approval_status", "evidence_schema", "evidence_version", "environment",
    "dependencies", "provenance_hash",
})
_APPROVAL_FIELDS = frozenset({
    "registration_id", "provenance_hash", "reviewer_identity", "approval_record", "status",
})


def _read_json(path: Path, maximum: int = 1024 * 1024) -> tuple[dict[str, Any], str]:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError(f"authority input must be a bounded regular file: {path}")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("authority input must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"registered source document must be a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class EvidenceRegistration:
    registration_id: str
    evidence_type: str
    source_identity: str
    source_document_sha256: str
    acquisition_method: str
    acquired_at_utc: str
    effective_start: str
    effective_end: str
    licensing_decision: str
    reviewer_identity: str
    approval_record: str
    approval_status: str
    evidence_schema: str
    evidence_version: int
    environment: str
    dependencies: dict[str, str]
    provenance_hash: str
    registration_sha256: str

    def covers(self, start: str, end: str) -> bool:
        return self.effective_start <= start and end <= self.effective_end


@dataclass(frozen=True)
class ApprovalTrustPolicy:
    policy_id: str
    policy_sha256: str
    trusted_reviewers: frozenset[str]
    approvals: dict[str, dict[str, str]]

    def approves(self, registration: EvidenceRegistration) -> bool:
        approval = self.approvals.get(registration.registration_id)
        return bool(
            registration.environment == "PRODUCTION"
            and registration.approval_status == "APPROVED"
            and registration.licensing_decision == "APPROVED_FOR_RESEARCH_USE"
            and registration.reviewer_identity in self.trusted_reviewers
            and approval == {
                "registration_id": registration.registration_id,
                "provenance_hash": registration.provenance_hash,
                "reviewer_identity": registration.reviewer_identity,
                "approval_record": registration.approval_record,
                "status": "APPROVED",
            }
        )


def load_evidence_registration(path: Path) -> EvidenceRegistration:
    value, digest = _read_json(path)
    if set(value) != _REGISTRATION_FIELDS or value["contract"] != REGISTRATION_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported evidence-registration contract or fields")
    for key in ("source_document_sha256", "provenance_hash"):
        if not isinstance(value[key], str) or _HEX.fullmatch(value[key]) is None:
            raise ValueError(f"registration {key} must be lowercase SHA-256")
    document_name = value["source_document_file"]
    if (not isinstance(document_name, str) or not document_name or Path(document_name).is_absolute()
            or len(Path(document_name).parts) != 1):
        raise ValueError("registration source_document_file must be one local filename")
    document_path = Path(path).resolve().parent / document_name
    if _file_sha256(document_path) != value["source_document_sha256"]:
        raise ValueError("registered source document bytes do not match source_document_sha256")
    if value["environment"] not in {"PRODUCTION", "SYNTHETIC"}:
        raise ValueError("registration environment must be PRODUCTION or SYNTHETIC")
    if value["approval_status"] not in {"PENDING", "APPROVED", "REJECTED"}:
        raise ValueError("registration approval_status is unsupported")
    if value["acquisition_method"] not in {"MANUAL_BROWSER_DOWNLOAD", "MANUAL_REVIEW"}:
        raise ValueError("registration acquisition_method is unsupported")
    for key in ("evidence_type", "source_identity", "reviewer_identity", "approval_record", "evidence_schema"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"registration {key} must be a non-empty string")
    if isinstance(value["evidence_version"], bool) or not isinstance(value["evidence_version"], int) \
            or value["evidence_version"] < 1:
        raise ValueError("registration evidence_version must be a positive integer")
    try:
        acquired = datetime.fromisoformat(value["acquired_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("registration acquired_at_utc must be an ISO-8601 timestamp") from exc
    if acquired.tzinfo is None:
        raise ValueError("registration acquired_at_utc must include a timezone")
    if not isinstance(value["dependencies"], dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _HEX.fullmatch(v) for k, v in value["dependencies"].items()
    ):
        raise ValueError("registration dependencies must map identities to SHA-256")
    start = date.fromisoformat(value["effective_start"]).isoformat()
    end = date.fromisoformat(value["effective_end"]).isoformat()
    if start > end:
        raise ValueError("registration effective range is reversed")
    document = Path(path).resolve().parent / value["source_document_file"]
    if _file_sha256(document) != value["source_document_sha256"]:
        raise ValueError("registered source document does not match its SHA-256")
    provenance_binding = {key: value[key] for key in sorted(_REGISTRATION_FIELDS - {
        "registration_id", "provenance_hash", "approval_status", "reviewer_identity", "approval_record",
    })}
    if value["provenance_hash"] != canonical_hash(provenance_binding):
        raise ValueError("registration provenance_hash does not match its source metadata and dependencies")
    registration_binding = {key: value[key] for key in sorted(_REGISTRATION_FIELDS - {"registration_id"})}
    if value["registration_id"] != canonical_hash(registration_binding):
        raise ValueError("registration_id does not match the registration content")
    return EvidenceRegistration(
        value["registration_id"], value["evidence_type"], value["source_identity"],
        value["source_document_sha256"], value["acquisition_method"], value["acquired_at_utc"], start, end,
        value["licensing_decision"], value["reviewer_identity"], value["approval_record"],
        value["approval_status"], value["evidence_schema"], value["evidence_version"], value["environment"],
        dict(sorted(value["dependencies"].items())), value["provenance_hash"], digest,
    )


def load_trust_policy(path: Path, expected_sha256: str) -> ApprovalTrustPolicy:
    """Load a policy only when its digest was supplied out of band by the caller."""
    if not isinstance(expected_sha256, str) or _HEX.fullmatch(expected_sha256) is None:
        raise ValueError("an out-of-band trust-policy SHA-256 is required")
    value, digest = _read_json(path)
    if digest != expected_sha256:
        raise ValueError("trust-policy file does not match the out-of-band SHA-256")
    if set(value) != {"contract", "schema_version", "policy_id", "trusted_reviewers", "approvals"}:
        raise ValueError("trust policy has missing or unsupported fields")
    if value["contract"] != TRUST_POLICY_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported trust-policy contract")
    reviewers = value["trusted_reviewers"]
    if not isinstance(reviewers, list) or not reviewers or len(reviewers) != len(set(reviewers)) or not all(
        isinstance(item, str) and item for item in reviewers
    ):
        raise ValueError("trust policy requires unique reviewer identities")
    approvals: dict[str, dict[str, str]] = {}
    if not isinstance(value["approvals"], list):
        raise ValueError("trust policy approvals must be a list")
    for approval in value["approvals"]:
        if not isinstance(approval, dict) or set(approval) != _APPROVAL_FIELDS or approval["status"] != "APPROVED":
            raise ValueError("trust policy contains an invalid approval")
        if approval["reviewer_identity"] not in reviewers:
            raise ValueError("approval reviewer is not trusted by this policy")
        if not all(isinstance(approval[key], str) and approval[key] for key in _APPROVAL_FIELDS):
            raise ValueError("trust policy approval fields must be non-empty strings")
        if _HEX.fullmatch(approval["registration_id"]) is None or _HEX.fullmatch(approval["provenance_hash"]) is None:
            raise ValueError("trust policy approval identities must be SHA-256")
        if approval["registration_id"] in approvals:
            raise ValueError("trust policy contains duplicate registration approvals")
        approvals[approval["registration_id"]] = approval
    binding = {key: value[key] for key in ("contract", "schema_version", "trusted_reviewers", "approvals")}
    if value["policy_id"] != canonical_hash(binding):
        raise ValueError("trust-policy identifier does not match its content")
    return ApprovalTrustPolicy(value["policy_id"], digest, frozenset(reviewers), approvals)
